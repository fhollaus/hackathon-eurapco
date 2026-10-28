# Events and evaluation records

Machine-readable contracts live in contracts/. All money is a non-negative EUR
string with exactly two decimals, dates YYYY-MM-DD, timestamps ISO8601 with timezone.
The three event types have separate schemas within events.schema.json. Unknown
fields are rejected; examples are in examples/events/ and examples/training/.

## Initial request and replies

request requires event_id, ticket_id, policy_id, author_name, timestamp, text.
claim_id is an optional explicit record reference. invoice_text/prescription_text
are optional/null according to operation; required evidence content is a business
check, not a universal schema requirement.

clarification_reply requires event_id, ticket_id, timestamp, text and can supply
invoice_text/prescription_text. It correlates with the existing request/run and
preserves fixed facts. insurer_decision requires event_id, ticket_id, timestamp,
decision approve/reject, with a non-empty reason for reject. It has no alternative
amount or revise option. Bind the event to the sole proposal saved at suspension.
Typed events distinguish the insurer from holder text claiming an approval.

Event ID identifies one event. Ticket ID identifies the request/workflow. Run ID
identifies an isolated execution of that ticket. Fresh reruns use a NEW run ID;
resume uses the SAME run ID. The domain setup API refuses an accidental overwrite.

## Supplied cumulative logger

EvaluationLog is in app/evaluation.py. It is implemented, with baseline and MCP
client wiring. One logs/RUN_ID.json is updated across the request's events.
It records team/run/ticket/policy, actual handling agent, request category, agent state,
request outcome/reason, human round counts, ordered events and domain snapshot.
Snapshots make actual effects inspectable alongside the model's claimed outcome.
Do not generate the log as a model-authored narrative or erase prior events.

For your continuation implementation:

```python
from app.evaluation import EvaluationLog

log = EvaluationLog.load(existing_log_path)
log.input(reply_event)  # validates format and matching ticket; does not resume workflow
# Your harness processes the reply and makes the actual transition.
log.status(agent_state="completed", request_outcome="updated",
           reason="Contact update succeeded", request_category="contact_update")
log.snapshot(domain_store.snapshot(existing_run_id))
```

This documents logger integration, not a supplied continuation solution.
Use log.human_requested("clarification", {"question": question}) or
log.human_requested("authorization", proposal) when issuing those interactions.
It measures counts, not enforces the business-round budget. Every DomainClient
call automatically records arguments and actual results; pass the same log on
resumption. Store your workflow/proposal separately using your chosen design.

Agent states (the same six for both agents):

| Agent state | Meaning |
|---|---|
| processing | Currently handling the request |
| awaiting_clarification | Suspended waiting for the one policyholder reply |
| awaiting_authorization | Suspended waiting for the insurer decision |
| completed | Finished with an answer, successful effect or coherent team-defined completion |
| closed_without_execution | Closed without the requested effect, with an explanatory request outcome |
| failed | Technical failure prevented normal completion |

The full request outcome and request category vocabularies are in vocabulary.json
(`agent_states`, `request_outcomes`, `request_categories`). Richer internal
states are allowed if mapped to these common values; explain subtype detail in
reason. No hidden model reasoning, API keys or irrelevant environment secrets.

A record saying paid is not proof of payment: compare actual tool results and
snapshot. A successful write followed by explanation failure must not be repeated.
Logs cannot by themselves certify code structure or skill use.

## Required classification at waiting and completion

While agent_state is processing, handling_agent and request_category may still be null. They
must both be assigned values from the common vocabulary before a record is
completed, closed_without_execution, awaiting_clarification or awaiting_authorization.
The schema also rejects externally supplied final/waiting records missing them.

If an integration omits them or supplies an unknown value, the logger preserves
all events, saves a failed/technical_failure record with an explicit logging error,
and raises IncompleteEvaluationLog. It does not invent an agent/request category or report
successful completion. The attempted transition is retained in the error event.
A genuine technical failure before classification can still be saved with null
metadata. The baseline preserves the error and its final domain snapshot, including
any effects that already happened. This is logging validation, not a supplied
workflow engine or business-permission gate.

## Safe serialization and rejected updates

The logger converts nested finite `Decimal` values to exact strings (for example,
`Decimal("104.00")` becomes `"104.00"`), timezone-aware `datetime` values to ISO8601
strings with their offset, and `Path` values to strings. It does not convert money
through a float or round arbitrary decimals. Lists and tuples become JSON arrays;
JSON object keys must be strings. Tool money fields still require the two-decimal
representation defined by their schemas.

Every update is copied, normalized, schema-validated and checked for JSON encoding
before it replaces the saved record. Unsupported objects, non-finite numbers,
datetimes without a timezone, circular references and invalid schema values are
rejected. The last valid events and snapshot are preserved, an explicit error event
is saved with agent state `failed` and request outcome `technical_failure`, and
`InvalidEvaluationLog` is raised. The rejected payload is not retained as an event;
the diagnostic identifies its field. Subsequent error events, status updates and
snapshots remain writable, including after `EvaluationLog.load()`.

The baseline preserves this logging diagnostic and still saves the final domain
snapshot. Neither a logging failure nor retrying logging undoes an already applied
domain effect. If storage itself fails, the write raises the storage error and the
in-memory record stays at its last successful save; persistence requires working
storage. `EvaluationLogError` is the common base for `IncompleteEvaluationLog` and
`InvalidEvaluationLog`.

## Baseline boundaries

app/runtime.py handles initial messages, tool calls and one structured final/waiting
response. It does not accept/replay human responses or persist a complete workflow.
Implement correlation, admission, proposal binding and round enforcement yourself.
Neither the log nor its input() method is a workflow engine. Skills loader reads
team-written Markdown; the starting runtime does not ship complete domain skills.

## Troubleshooting

- check fails: use the supplied environment and run from repository root. For HTTP,
  start server in another terminal and match URL/port/data directory.
- Missing key: edit .env; never paste it into a ticket or commit it.
- Provider error: check model access/key and network; the SDK has automatic retries
  disabled. CLI logs exception type without copying provider secrets.
- Reply rejected by baseline: expected initial limitation; implement continuation.
- Tool returns INVALID_ARGUMENT: read field/allowed_values, correct representation
  without altering the requested facts/action; the three-attempt rule is in tools.md.
- Validate JSON with python -m app validate-log PATH. Schema success proves shape,
  not business correctness; inspect events/effects too.
