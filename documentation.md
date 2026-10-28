# Harness documentation

This document describes how the harness around the two agents works and what was changed
from the baseline:

- stop/resume for clarification replies and insurer decisions;
- deterministic write guards;
- the connection of workflow, classification and human events to the supplied logger;
- one domain skill per agent;
- tests, verification runs, and how to operate and extend the harness.

Rules referenced below come from `docs/challenge.html`, `docs/policy.md`,
`docs/minimum-information.md`, `docs/tools.md` and `docs/contracts.md`.

## Contents

1. [Overview](#1-overview)
2. [Changed files](#2-changed-files)
3. [Stop and resume for human replies](#3-stop-and-resume-for-human-replies)
4. [Agent states](#4-agent-states)
5. [Logger integration](#5-logger-integration)
6. [Write guards](#6-write-guards)
7. [Skills](#7-skills)
8. [Running the common test](#8-running-the-common-test)
9. [Configuration and troubleshooting](#9-configuration-and-troubleshooting)
10. [Design decisions](#10-design-decisions)
11. [Challenge requirements coverage](#11-challenge-requirements-coverage)
12. [Code reference](#12-code-reference)
13. [Tests](#13-tests)
14. [Extending the harness](#14-extending-the-harness)
15. [Verification with the real model](#15-verification-with-the-real-model)
16. [Known limitations and next steps](#16-known-limitations-and-next-steps)
17. [Notes for the team](#17-notes-for-the-team)

## 1. Overview

Every message is one request with one operation. The router chooses an agent, and the
agent works with its domain tools in a model loop. The harness around that loop does six
things:

- it **blocks writes** the model may not do on its own;
- it **stops** when a person must answer;
- it **saves the workflow** so the request can continue later;
- it **admits or refuses** the reply;
- it **applies insurer decisions** itself;
- it **records everything** in the supplied evaluation log.

```text
  request.json --> python -m app run --> run_request()
                                           |  route()                      app/router.py
                                           v
                                         advance()
                                           |  agent_turns(): model <-> tools
                                           |    |  blocked_write()         guards, before any write
                                           |    |  proposal_problem()      before waiting for the insurer
                                           |    v
                                           |  DomainClient --MCP--> mcp_server --> DomainStore
                                           v
                                         finish() --> EvaluationLog   logs/RUN_ID.json
                                                  --> workflow        .runtime/workflows/RUN_ID.json

  reply.json ----> python -m app resume --> admit() --> resume_request()
                                              |           |-- clarification_reply --> advance()
                                              |           '-- insurer_decision ----> decide() --> finish()
                                              '-- refused --> refuse(): ReplyRejected in the run's log
```

### Where state lives

All three stores use the same run ID. A rerun of a ticket is a new run with a new ID and
three new files. A resume uses the same files.

| Store | Path | Written by | Contains |
|---|---|---|---|
| Domain data | `.runtime/domain/RUN_ID.json` | The MCP server, through the nine tools | Policies and claims of this run |
| Workflow | `.runtime/workflows/RUN_ID.json` | `app/runtime.py` | Conversation, state, saved proposal, human rounds, processed event IDs |
| Evaluation log | `logs/RUN_ID.json` | `EvaluationLog` (`app/evaluation.py`) | The evidence: events, classification, state, outcome, snapshot |

### Who decides what

| Decision | Made by |
|---|---|
| Which agent handles the request | Router (`app/router.py`) |
| Category, procedure, questions, proposals, final answer | Model, guided by prompt and skills |
| Whether a write may run (thresholds, decreases, duplicates, amounts) | Harness (`blocked_write()`) |
| Whether a proposal may go to the insurer | Harness (`proposal_problem()`) |
| Whether a reply is admitted | Harness (`admit()`) |
| Executing an approved proposal and reporting its result | Harness (`decide()`), without the model |
| The one-clarification rule and the stable category | Harness (`finish()`) |

## 2. Changed files

| File | Change |
|---|---|
| `app/runtime.py` | Persisted workflow state, reply correlation/admission, resume on clarification replies and insurer decisions, proposal check, one-clarification rule, write guards, reimbursement formula, logger calls |
| `app/__main__.py` | New `resume` command (clarification replies and insurer decisions); shared `run_logged()` failure/snapshot handling |
| `skills/administration/coverage_change.md` | New Administration skill |
| `skills/claims/reimbursement_request.md` | New Claims skill |
| `tests/test_runtime.py` | New offline tests (scripted model, real MCP domain) |
| `README.md` | Usage of `resume`, description of resume and the guards |
| `documentation.md` | This document |

Not part of these changes: the router rework in `app/router.py` and the skill loading in
the second `system_prompt()` in `app/runtime.py` were done by the team.

## 3. Stop and resume for human replies

The baseline handled only the first message. A request that asked the policyholder a
question or the insurer for authorization could stop, but the answer could not be
delivered. Now a clarification reply or an insurer decision continues the **same run, the
same workflow and the same evaluation log**. A reply is never restarted as a new request.

### Workflow state

Every run keeps its state in `.runtime/workflows/RUN_ID.json` (directory overridable
with `EURAPCO_WORKFLOW_DIR`). It is saved:

- when the run starts;
- at every waiting or final state;
- when a reply is admitted;
- when a run fails.

| Field | Content |
|---|---|
| `run_id`, `ticket_id`, `agent`, `log_path` | Identity of the run, the handling agent and its evaluation log |
| `request` | The original request, unchanged: its facts are fixed |
| `messages` | The full model conversation, including tool calls and results |
| `agent_state`, `request_category` | Current state and the category set before waiting |
| `question`, `proposal` | What the run is waiting on |
| `human_rounds` | Clarifications and authorizations requested so far |
| `event_ids` | Every event already processed, to refuse duplicates |
| `updated_at` | Last save; the latest waiting run of a ticket wins correlation |

A real workflow after an approved T06, with the conversation shortened:

```json
{
  "run_id": "RUN-0b03ae871c324ab3a66cfb3ef38bed59",
  "ticket_id": "T06",
  "agent": "administration",
  "log_path": "...\\logs\\RUN-0b03ae871c324ab3a66cfb3ef38bed59.json",
  "request": {"event_id": "EV-T06-01", "ticket_id": "T06", "policy_id": "POL-001",
              "text": "Please increase my annual reimbursement limit to EUR 10000 and my per-claim deductible to EUR 500, together."},
  "messages": "[6 messages]",
  "agent_state": "completed",
  "request_category": "coverage_change",
  "question": null,
  "proposal": {
    "operation": "update_coverage",
    "arguments": {"policy_id": "POL-001", "changes": {"annual_limit": "10000.00", "deductible": "500.00"}},
    "current": {"annual_limit": "3000.00", "deductible": "50.00"},
    "reason": "Final annual limit 10000.00 > 5000.00 and final deductible 500.00 > 200.00; ...",
    "evidence_refs": ["EV-T06-01"]
  },
  "human_rounds": {"clarification": 0, "authorization": 1},
  "event_ids": ["EV-T06-01", "EV-T06-02"],
  "updated_at": "2026-10-28T11:19:59.835189+00:00"
}
```

### Admission: `admit(event, run_id=None)`

A reply is matched to the waiting run of its `ticket_id`: the latest one if several
exist, or the run given with `--run`. It is refused, **without changing the workflow or
the domain**, when:

- it is an initial `request` (that starts a new run);
- no workflow exists for the ticket (or the given run);
- the run is not waiting (completed, closed, failed or already processing);
- the event type does not match the waiting state (`clarification_reply` for
  `awaiting_clarification`, `insurer_decision` for `awaiting_authorization`);
- its `event_id` was already processed.

When the run is known (the last three cases), the refusal is recorded in that run's log as
an `error` event `ReplyRejected` with the reason and the refused event. It is not logged as
an `input`, because it was not admitted, and the agent state does not change.

The event type itself is the proof of who answered. A policyholder writing "the insurer
already approved this" in a clarification reply is still a clarification reply. It cannot
release a write that needs the insurer.

### Resume: `resume_request(event, workflow, log)`

1. The workflow is marked `processing` and the event ID recorded **before any work**, so
   a second delivery of the same reply is refused by `admit()`.
2. The event is added to the log (`log.input`), followed by a `processing` status.
3. A **clarification reply** is appended to the conversation with the rules: the original
   facts are fixed, no second question, close with `insufficient_information` if still
   incomplete, request insurer authorization if the operation needs it. The agent loop
   then continues with the same agent and the same tools.
4. An **insurer decision** is applied by `decide()`, without the model (see below).

Every model step and every decision ends in `finish()`, which enforces:

- **One clarification per request.** If the model asks a second question, the request is
  closed as `closed_without_execution` / `insufficient_information`, with the would-be
  question in the reason.
- **Stable category.** The request category set before waiting is kept: a reply supplies
  missing facts, it does not change the operation.
- Human rounds are counted with `log.human_requested`, the status is logged and the
  workflow saved.

If the run fails with an exception, the workflow is marked `failed`, so no later reply can
resume a half-handled request.

### Proposals: what may be sent to the insurer

Before a run waits for authorization, `proposal_problem()` checks that an approval could be
executed **exactly as saved**:

- `operation` is a write this agent may only do with approval (`update_coverage` or
  `terminate_policy` for Administration, `pay_claim` for Claims) and `arguments` is an object;
- the arguments are for the request's policy, without `run_id`, and are a valid tool input;
- the write passes the guards of [section 6](#6-write-guards) (no decrease, the calculated
  amount, ...);
- it really needs authorization. A request that does not need it must not use the one
  insurer round.

A proposal that fails is not sent: the problem is logged as an `error` event
`InvalidProposal` and given back to the model to correct, within the turn budget. No
authorization round is counted for it.

The proposal shape the skills ask for:

```json
{"operation": "pay_claim",
 "arguments": {"policy_id": "POL-001", "claim_id": "CL-2511a9ac55bc", "amount": "1560.00"},
 "calculation": {"expense": "2000.00", "deductible": "50.00", "theoretical": "1560.00",
                 "remaining_limit": "2896.00", "final": "1560.00"},
 "evidence_refs": ["EV-T11-01/invoice_text"]}
```

Only `operation` and `arguments` are executed. The other fields explain the proposal to the
insurer and are kept in the log.

### Insurer decisions: `decide()`

The insurer decision is applied by the harness alone. No model is called and no API key is
needed.

- **Reject:** nothing is executed. For a reimbursement, the pending claim is recorded as
  `rejected` with the insurer's reason. The request closes as `closed_without_execution` /
  `insurer_rejected`, with the insurer's reason in the reason and the message.
- **Approve:** the proposal is checked again (an approval cannot make an unsupported write
  allowed), then its `operation` is run once with exactly its `arguments`. For a
  reimbursement, the claim is then recorded as `paid`. The outcome is taken from the actual
  tool result only:

| Approved operation | Tool result | Agent state / request outcome |
|---|---|---|
| `update_coverage` | changed | `completed` / `updated`, with before and after values |
| `update_coverage`, `terminate_policy` | not changed | `completed` / `no_change` |
| `terminate_policy` | changed | `completed` / `terminated` |
| `pay_claim` | ok | `completed` / `paid`, with the payment ID |
| any | error | `failed` / `technical_failure`, with the tool error |
| any | the re-check finds a problem | `failed` / `technical_failure`, nothing executed |

The messages to the policyholder are built from the tool result, for example: "The insurer
approved your coverage change: annual limit from EUR 3000.00 to EUR 10000.00 and deductible
from EUR 50.00 to EUR 500.00."

There is no revision, second round or other amount: after the decision the run is closed,
and any further event for it is refused.

### CLI

```sh
python -m app run examples/training/T04.json
python -m app resume examples/events/clarification_reply.json          # latest waiting run of T04
python -m app resume examples/events/clarification_reply.json --run RUN-...
python -m app run examples/training/T06.json
python -m app resume examples/events/insurer_approve.json              # or insurer_reject.json
python -m app validate-log logs/RUN-....json
```

`run` still refuses reply events and points to `resume`. Both commands share
`run_logged()`, which records failures (`failed` / `technical_failure`) and always saves the
final domain snapshot in the log. `resume` prints the run it continues
(`Resuming run RUN-... of ticket T06.`), then the final response and the log path.

## 4. Agent states

The harness uses exactly the six common agent states; there are no richer internal states.

```text
run (initial request)    ------------------------------------> processing
processing               --(model asks, once)----------------> awaiting_clarification
awaiting_clarification   --(clarification_reply admitted)----> processing
processing               --(model proposes, check passes)----> awaiting_authorization
awaiting_authorization   --(insurer_decision admitted)-------> processing
processing               --(final answer, decision, error)---> completed | closed_without_execution | failed

completed, closed_without_execution, failed: terminal, every further event is refused
```

| From | Trigger | To | Typical request outcome | Decided by |
|---|---|---|---|---|
| `processing` | Model's final answer | `completed` | `answered`, `updated`, `paid`, `no_change` | Model, with writes checked by the guards |
| `processing` | Model's final answer | `closed_without_execution` | `not_covered`, `duplicate`, `invalid_input`, `inactive_policy`, `out_of_scope`, `unclear` | Model |
| `processing` | Model asks; no clarification used yet | `awaiting_clarification` | `information_requested` | Model; counted by the harness |
| `processing` | Model asks again after the reply | `closed_without_execution` | `insufficient_information` | Harness |
| `processing` | Model proposes; proposal check passes | `awaiting_authorization` | `authorization_requested` | Model; checked by the harness |
| `processing` | Exception, invalid model output, turn budget exhausted | `failed` | `technical_failure` | Harness / CLI |
| `awaiting_clarification` | `clarification_reply` admitted | `processing` | `in_progress` | Harness |
| `awaiting_authorization` | `insurer_decision` admitted | `processing` | `in_progress` | Harness |
| `processing` (decision) | Approval executed, tool ok | `completed` | `updated`, `terminated`, `paid`, `no_change` | Harness, from the tool result |
| `processing` (decision) | Rejection | `closed_without_execution` | `insurer_rejected` | Harness |
| `processing` (decision) | Approved write fails or no longer passes the check | `failed` | `technical_failure` | Harness |

A request can pass through both waiting states, clarification first and authorization
second, but each at most once. The category is set from the first waiting, completed or
closed status on.

## 5. Logger integration

All evidence goes through the supplied `EvaluationLog` (`app/evaluation.py`). Nothing writes
the log JSON directly, and the log reports observed results, not what the model says.

| What happens | Logger call | Where |
|---|---|---|
| Initial request received | `EvaluationLog(request, ...)` (logs it as `input`) | CLI `run` |
| Request routed to an agent | `log.route(agent)`: sets `handling_agent` | `run_request()` |
| Every tool call and its actual result, from the model or the harness | automatic in `DomainClient` (`tool_call`, `tool_result`) | MCP client |
| Final or waiting model response | `log.event("model_response", response=...)` | `agent_turns()` |
| Write blocked by a guard | `log.event("error", error_type=...)`: `UnsupportedDecrease`, `DuplicateInvoice`, `ZeroReimbursement`, `AmountMismatch`, `AuthorizationRequired` | `agent_turns()` |
| Proposal that cannot be sent to the insurer | `log.event("error", error_type="InvalidProposal")` | `agent_turns()` |
| Clarification question or authorization proposal | `log.human_requested("clarification" / "authorization", ...)`: counts `human_rounds` | `finish()` |
| Classification and every state change: waiting, resumed, completed, closed | `log.status(...)` with `request_category`, `agent_state`, `request_outcome`, reason and message | `finish()`, `resume_request()` |
| Clarification reply or insurer decision admitted | `log.input(event)` on the loaded log of the same run | `resume_request()` |
| Reply refused for a known run | `log.event("error", error_type="ReplyRejected", ...)` | `refuse()` |
| Technical failure | `log.event("error", ...)` and `failed` / `technical_failure` status | CLI `run_logged()` |
| Data after every command | `log.snapshot(store.snapshot(run_id))` | CLI `run_logged()` |

The category is set with the first status that waits, completes or closes. On a resume it
stays the one set before waiting, so every later status repeats the same classification.

### Walkthrough: clarification (T04, real run)

| # | Event | Content |
|---|---|---|
| 1 | `input` | The request: change the address, no postal code |
| 2 | `routing` | `administration` |
| 3–4 | `tool_call` / `tool_result` | `get_policy`: active, current address |
| 5 | `model_response` | The final response, waiting for clarification |
| 6 | `model_response` (`interaction: clarification`) | The question for the postal code; `human_rounds.clarification` becomes 1 |
| 7 | `status` | `awaiting_clarification` / `information_requested`, category `contact_update` |
| 8 | `input` | The clarification reply "The postal code is 10100." |
| 9 | `status` | `processing` / `in_progress` |
| 10–11 | `tool_call` / `tool_result` | `update_contact_details` with the full address, `changed: true` |
| 12 | `model_response` | The final response |
| 13 | `status` | `completed` / `updated` |

The snapshot shows the new address. Sending the same reply again is refused, and the
refusal is logged as a `ReplyRejected` error without changing the result, as in the T06 run
in [section 15](#15-verification-with-the-real-model). (This T04 run predates the logging of
refusals, so its duplicate reply was refused without a log entry.)

### Walkthrough: reimbursement with insurer approval (T11, real run)

| Step | Events |
|---|---|
| Request and routing | `input` request, `routing` `claims` |
| Model reads | `get_policy`, `list_claims` |
| Duplicate guard before `create_claim` | `list_claims` (harness read), then `create_claim` |
| Waiting for the insurer is recorded on the claim | `record_claim_outcome` `pending_authorization` |
| Model proposes `pay_claim` 1560.00 | `model_response` |
| Proposal check | `get_claim`, `get_policy` (harness reads): the amount equals the calculation |
| Waiting | `model_response` (`interaction: authorization`, the proposal), `status` `awaiting_authorization` |
| A clarification reply arrives instead | `error` `ReplyRejected`; still waiting |
| Insurer approves | `input` insurer decision, `status` `processing` |
| Re-check, then exactly the proposal | `get_claim`, `get_policy`, `pay_claim` 1560.00, `record_claim_outcome` `paid` |
| Result | `status` `completed` / `paid` |

The snapshot shows the claim as `paid` with 1560.00 and the policy's paid consumption at
1664.00.

### Walkthrough: termination rejected (T07, real run)

| Step | Events |
|---|---|
| Request and routing | `input`, `routing` `administration`, `get_policy` |
| The model tries to terminate directly | `error` `AuthorizationRequired` for `terminate_policy`; no `tool_call` for it |
| The model proposes instead | `model_response`, `model_response` (`interaction: authorization`), `status` `awaiting_authorization` |
| Insurer rejects | `input` insurer decision, `status` `processing`, `status` `closed_without_execution` / `insurer_rejected` |

`terminate_policy` never appears as a tool call in this log, and the snapshot shows the
policy still active.

### Event reference

Error events written by the harness:

| `error_type` | Written by | Data | Meaning |
|---|---|---|---|
| `UnsupportedDecrease`, `DuplicateInvoice`, `ZeroReimbursement`, `AmountMismatch`, `AuthorizationRequired` | `agent_turns()` | `tool`, `arguments`, `reason` | A write requested by the model was blocked before the domain |
| `InvalidProposal` | `agent_turns()` | `reason` | A proposal was not sent to the insurer and went back to the model |
| `ReplyRejected` | `refuse()` | `reason`, `event` | A reply was refused; no state change |
| Exception class names, e.g. `RuntimeError`, `JSONDecodeError`, `ValidationError`, `NotFoundError` | CLI `run_logged()` | `error_type` | A technical failure; a `failed` / `technical_failure` status follows |
| `IncompleteEvaluationLog`, `InvalidEvaluationLog` | The logger itself | `fields` or `field` | A rejected log update (see `docs/contracts.md`) |

Examples from the T07 and T11 runs (the second with the current wording of the reason):

```json
{"kind": "error", "data": {"error_type": "AuthorizationRequired", "tool": "terminate_policy",
  "arguments": {"policy_id": "POL-001"}, "reason": "Policy termination always needs insurer authorization."}}

{"kind": "error", "data": {"error_type": "ReplyRejected",
  "reason": "Run RUN-772a... is awaiting_authorization and accepts only insurer_decision events, not clarification_reply.",
  "event": {"event_type": "clarification_reply", "event_id": "EV-T11-03", "ticket_id": "T11", "...": "..."}}}
```

## 6. Write guards

The supplied tools only check formats and catalogue values: `docs/tools.md` says they do
not enforce increase-only, authorization or duplicate rules. A real run showed why this
matters: on T06 the model called `update_coverage` without insurer approval in 1 of 3 runs,
even with the skill in its prompt.

Every write the model requests now passes `blocked_write()` in `agent_turns()` before it
reaches the domain. Checks run in this order; the first that applies blocks the call:

| Tool | Blocked when | Harness reads | Error type | The model is told to |
|---|---|---|---|---|
| `update_coverage` | A requested value is below the current one | `get_policy` | `UnsupportedDecrease` | Close with `invalid_input` |
| `create_claim` | Same provider ID + invoice number + invoice-date year as a claim in `list_claims` | `list_claims` | `DuplicateInvoice` | Close with `duplicate`, reporting the existing claim ID |
| `pay_claim` | The calculated reimbursement is 0.00 | `get_claim`, `get_policy` | `ZeroReimbursement` | Close with `zero_reimbursement` |
| `pay_claim` | The amount differs from the calculated reimbursement | `get_claim`, `get_policy` | `AmountMismatch` | Use the calculated amount (given in the error) |
| `terminate_policy` | Always | – | `AuthorizationRequired` | Return a proposal |
| `update_coverage` | Final annual limit > 5000.00 or final deductible > 200.00 | – | `AuthorizationRequired` | Return a proposal |
| `pay_claim` | Amount > 1500.00 | – | `AuthorizationRequired` | Return a proposal |

The order matters: an unsupported decrease, a duplicate or a wrong amount must be closed or
corrected, not sent to the insurer as a proposal. Equal values (5000.00, 200.00, 1500.00)
are autonomous, as in `docs/policy.md`.

A blocked call:

- never reaches the domain, so nothing changes;
- is logged as an `error` event with `error_type`, `tool`, `arguments` and `reason`;
- returns an `INVALID_STATE` error to the model, deliberately not `INVALID_ARGUMENT`, so it
  is not retried as a format error under the three-attempt rule.

The model receives `Blocked by the harness: <reason> Do not call <tool>. <what to do instead>`.
Example reasons:

| Error type | Reason as the model sees it |
|---|---|
| `UnsupportedDecrease` | Coverage can only be increased: annual_limit 5000.00 is below the current 10000.00. |
| `DuplicateInvoice` | This invoice already belongs to claim CL-H001 (status paid). |
| `ZeroReimbursement` | The calculated reimbursement is 0.00 (expense 40.00 - deductible 50.00 = 0.00; 80% = 0.00; remaining limit 2896.00; final 0.00). |
| `AmountMismatch` | Amount 144.00 differs from the calculated 104.00 (expense 180.00 - deductible 50.00 = 130.00; 80% = 104.00; remaining limit 2896.00; final 104.00). |
| `AuthorizationRequired` | Coverage change needs insurer authorization: final annual_limit 10000.00 is above 5000.00 and final deductible 500.00 is above 200.00. |

Cases the guards pass on to the domain unchanged: values that are not amounts (the tool's
own format check rejects them) and `pay_claim` on a claim that is already paid (the domain
returns the existing payment without paying again).

The harness reads (`get_policy`, `list_claims`, `get_claim`) go through the normal tool
client, so they appear in the log as tool calls next to the model's own calls. In a T08
log, for example: `get_policy`, `list_claims` (model), `list_claims` (duplicate guard),
`create_claim`, `get_claim`, `get_policy` (amount guard), `pay_claim`, `record_claim_outcome`.

### Reimbursement formula

`reimbursement(expense, deductible, remaining_limit)` implements `docs/policy.md` with
`Decimal`:

```text
eligible    = max(0, expense - deductible)
theoretical = 80% of eligible, rounded half-up to cents
final       = min(theoretical, remaining_limit)
```

It returns the final amount and the working shown to the model.

| Expense | Deductible | Remaining limit | Final | Authorization |
|---|---|---|---|---|
| 180.00 | 50.00 | 2896.00 | 104.00 | No |
| 1925.00 | 50.00 | 2896.00 | 1500.00 | No (equal to the threshold) |
| 2000.00 | 50.00 | 2896.00 | 1560.00 | Yes |
| 2000.00 | 50.00 | 900.00 | 900.00 | No (capped) |
| 40.00 | 50.00 | 2896.00 | 0.00 | Payment blocked: zero reimbursement |

The deductible and remaining limit are read at the time of the check, so a coverage change
earlier in the same run is taken into account.

## 7. Skills

The team's loader (`system_prompt()` in `app/runtime.py`) appends every Markdown file of
the agent's folder under a `## Skill: <file name>` heading, after the agent prompt and
before the response-schema instruction. Each agent sees only its own skills.

### Administration: `skills/administration/coverage_change.md`

Procedure for annual limit and deductible changes:

1. Read the policy; close as `inactive_policy` when not active, without asking.
2. Work out the requested final values (relative requests: current + amount); ask once if
   no target value is given.
3. Validate against the catalogues (limit 3000/5000/10000, deductible 50/100/200/500):
   outside the catalogue or a decrease → `invalid_input`, writing nothing; all equal → `no_change`.
4. Authorization from the final values only; exactly 5000/200 needs none.
5. Otherwise one `update_coverage` with only the changing parts, reporting actual before/after.

Defines the proposal shape `{"operation": "update_coverage", "arguments": {...}, "current":
{...}, "reason": ..., "evidence_refs": [...]}`, where `arguments` is the exact tool input to
run on approval. A combined change is one proposal.

### Claims: `skills/claims/reimbursement_request.md`

Procedure for new reimbursements, stopping at the first check that decides:

1. Read the policy and the claim history.
2. Service: map the description (cardiology consultation → specialist; MRI, blood test →
   diagnostic); dental, cosmetic, medicines → `not_covered` without asking for documents.
3. Inactive policy → `inactive_policy`; patient not insured → `not_insured`; service date
   outside the period (both days included) → `outside_coverage_period`.
4. Duplicate key match → `duplicate`, reporting the existing claim.
5. Evidence completeness from `docs/minimum-information.md`; one question for everything missing.

Then the calculation and the claim lifecycle chosen for this harness:

| Situation | Claim record | Request outcome |
|---|---|---|
| A check in steps 2–4 fails, or still incomplete after the clarification | No claim is created | The refusal's outcome |
| Final 0.00 | `create_claim`, then `record_claim_outcome` "rejected" | `zero_reimbursement` |
| Final ≤ 1500.00 | `create_claim`, `pay_claim` (exactly final), `record_claim_outcome` "paid" | `paid` |
| Final > 1500.00 | `create_claim`, `record_claim_outcome` "pending_authorization", proposal `pay_claim` | `authorization_requested` |
| … then insurer approves | The harness pays, then `record_claim_outcome` "paid" | `paid` |
| … then insurer rejects | The harness runs `record_claim_outcome` "rejected" | `insurer_rejected` |

The skills describe the procedure; the guards in section 6 enforce the parts that must not
depend on the model.

## 8. Running the common test

Each message of the common test must produce an evaluation log. A message is one initial
request; its replies belong to the same run and the same log.

**Before the test**

1. `python -m app check`: the MCP server starts and the nine tools answer.
2. In `.env`: `TEAM_ID` is the assigned team, and `ANTHROPIC_MODEL` is the model you tested with.
   An `ANTHROPIC_MODEL` set in the shell overrides `.env` (see [section 9](#9-configuration-and-troubleshooting)).
3. Optional: `python -m pytest -q tests/test_runtime.py` (no API key needed).

**For each message**

1. `python -m app run MESSAGE.json`. Note the `Domain run: RUN-...` line and the
   `agent_state` in the printed response.
2. `completed` or `closed_without_execution`: done.
3. `awaiting_clarification`: write the reply the instructions give, as a
   `clarification_reply` with a new `event_id` and the same `ticket_id`. Add only the missing
   facts; never change a fact that was already supplied. Then
   `python -m app resume REPLY.json --run RUN-...`.
4. `awaiting_authorization`: write the insurer decision the instructions give, as an
   `insurer_decision` (`approve`, or `reject` with a `reason`). Then
   `python -m app resume DECISION.json --run RUN-...`.
5. A request can need both: after the clarification it may wait for the insurer. Resume a
   second time, with the decision.
6. `python -m app validate-log logs/RUN-....json`.

Always pass `--run` in the test: if a ticket was run more than once, it makes sure the reply
reaches the intended run.

**Event templates**

```json
{"event_type": "clarification_reply", "event_id": "EV-T04-02", "ticket_id": "T04",
 "timestamp": "2026-10-28T16:15:00+01:00", "text": "The postal code is 10100.",
 "invoice_text": null, "prescription_text": null}

{"event_type": "insurer_decision", "event_id": "EV-T06-02", "ticket_id": "T06",
 "timestamp": "2026-10-28T16:15:00+01:00", "decision": "approve"}

{"event_type": "insurer_decision", "event_id": "EV-T07-02", "ticket_id": "T07",
 "timestamp": "2026-10-28T16:15:00+01:00", "decision": "reject",
 "reason": "Termination is declined."}
```

`invoice_text` and `prescription_text` in a clarification reply are optional; use them when
the missing information is a document text, for example the prescription in T09.

**If something goes wrong**

- A failed run is recorded in its log as `failed` / `technical_failure`, with the domain
  snapshot. Do not resend the same reply: the run is closed and the reply would be refused.
- `run` again starts a fresh execution with a new run ID and its own log.
- `resume` errors explain why a reply was refused (see the table in section 9).

## 9. Configuration and troubleshooting

### Environment

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | `run`, `resume` with a clarification reply | – | Not needed for `resume` with an insurer decision, `check` or the tests |
| `ANTHROPIC_MODEL` | Agent loop | `claude-haiku-5-5` | A value set in the shell overrides `.env` |
| `TEAM_ID` | Evaluation log | `TEAM-1` | Written into every log |
| `MCP_URL` | MCP client | empty: stdio server started automatically | For the optional HTTP server |
| `EURAPCO_DATA_DIR` | Domain store | `.runtime/domain` | Must be the same for `run` and `resume` |
| `EURAPCO_WORKFLOW_DIR` | Workflow store | `.runtime/workflows` | Must be the same for `run` and `resume` |

Logs are written to `logs/`. `.runtime/` and `logs/` are ignored by git.

### Messages and what they mean

| Message | Cause | What to do |
|---|---|---|
| `Reply events are not admitted by run, which starts a new execution: ...` | A reply was given to `run` | Use `resume` |
| `No workflow found for ticket T..` | The initial request was not run here, or different data/workflow directories are used | `run` the request first; check the environment variables |
| `Run RUN-... of ticket T.. is completed, not waiting; a reply cannot reopen or restart it.` | The request already finished (or `failed` / `processing`) | Nothing to resume; a new attempt is a new `run` |
| `Run RUN-... is awaiting_authorization and accepts only insurer_decision events, not clarification_reply.` | Wrong event type for the waiting state | Send the other event type |
| `Event EV-... was already processed in run RUN-....` | The same event was delivered twice | Nothing to do: it was already applied |
| `An initial request starts a new run; it cannot resume one.` | A request was given to `resume` | Use `run` |
| `ANTHROPIC_API_KEY is not set.` | `.env` missing or without key | Add the key to `.env` |
| `ERROR (NotFoundError): ...` | The model ID is not available | Check `ANTHROPIC_MODEL` in the shell and in `.env` |
| `ERROR (RuntimeError): ...` with reason `RuntimeError` in the log | The 12-turn budget of one model step was used up | Inspect the log; rerun with `run` |
| `ERROR (JSONDecodeError)` or `(ValidationError)` | The model's final answer was not valid JSON or did not match the response schema | Inspect the log; rerun with `run` |

A workflow stays in `processing` only if the process was interrupted mid-step (for example
Ctrl+C). Such a run cannot be resumed; start a new one.

## 10. Design decisions

- **The harness, not the model, executes approved proposals.** "Approved: execute exactly
  the saved proposal, nothing else." Running `proposal["arguments"]` directly leaves no room
  for a changed amount or an extra write, and needs no model call.
- **Guards in code instead of only in prompts.** The tools accept any catalogue value, and a
  real T06 run showed the model skipping the authorization rule. A guard makes the rule hold
  on every run.
- **Guards read current data through the tool client.** They see the values at the moment of
  the write, not what the model read earlier, they work with both MCP transports, and their
  reads are visible in the log.
- **A blocked call returns `INVALID_STATE`.** It is a business refusal, not a format error, so
  it does not count towards the three format-correction attempts and is not retried.
- **Guard order: refusals before authorization.** A decrease, duplicate or wrong amount must
  never reach the insurer as a proposal.
- **Proposals are checked before waiting.** The insurer never receives a proposal the harness
  could not execute, or one that needs no authorization; the one insurer round is not wasted.
- **Claim the workflow before working.** Marking the run `processing` before handling a reply
  means a second delivery is refused even if the first one is still running or crashed.
- **Refused replies are `error` events, not `input`.** `input` means "admitted and processed".
  A refused reply is still visible in the log, but cannot be mistaken for a processed one.
- **The category is fixed before waiting.** A reply adds facts; it cannot change what was asked.
- **The one-clarification rule is enforced in code.** If the model asks again, the request
  closes with `insufficient_information` instead of waiting a second time.
- **Claims are registered only once eligible.** Refusals leave no orphan records; a claim
  waiting for the insurer is marked `pending_authorization`, and the decision updates it.
- **Decision messages come from tool results.** The policyholder is told what actually
  happened, with the values and IDs the tools returned.
- **Workflow and log are separate stores.** `docs/contracts.md` asks for the workflow and
  proposal to be stored separately from the log; the log stays pure evidence.

## 11. Challenge requirements coverage

Status of the "What your team builds" items from `docs/challenge.html`:

| Requirement | Status | Where |
|---|---|---|
| Improve the router and classification | Router reworked by the team; the category stays stable across resume; no re-routing between agents yet | `app/router.py`, `finish()` |
| Collect genuinely missing information once | Skills ask one question for everything missing; a second question is closed by code | Skills, `finish()` |
| Workflow state, correlation and stop/resume for human replies | Done for clarification replies and insurer decisions, same run and log | Section 3 |
| Admissibility, active-policy and date checks, authority and proposal integrity | Authority, proposal integrity, decreases, duplicates and amounts in code; active policy, insured patient and dates in the skills only | Sections 3, 6, 7 |
| Deterministic Claims calculation and duplicate prevention | Done in code | Section 6 |
| At least one meaningful skill per agent | Done | Section 7 |
| Reimbursement-record lifecycle | Defined in the Claims skill; the insurer decision updates the record | Sections 3, 7 |
| Connect workflow, classification and human events to the logger | Done | Section 5 |
| Communicate actual results and keep the enquiries working | Decision messages from tool results; T01 and T02 verified after the changes | Sections 3, 15 |

## 12. Code reference

### `app/runtime.py`

| Function | Role |
|---|---|
| `run_request(request, run_id, log, provider=None)` | Starts a run: validates the request, saves the workflow, routes, logs the routing, calls `advance()` |
| `admit(event, run_id=None)` | Correlates a reply with its waiting run and checks it may resume it; returns the workflow |
| `refuse(event, workflow, reason)` | Logs `ReplyRejected` in the run's log and raises `ResumeError` |
| `resume_request(event, workflow, log, provider=None)` | Claims the workflow, logs the input and `processing`, then continues with the model or `decide()` |
| `advance(workflow, log, provider)` | Opens the model client and the MCP connection, runs `agent_turns()`, ends in `finish()` |
| `agent_turns(...)` | The model/tool loop (12 turns per step): guards every tool call, validates the final answer, checks proposals |
| `decide(event, workflow, log)` | Applies an insurer decision without the model |
| `decided(...)`, `executed(...)` | Build the harness's final response; `executed()` maps a tool result to the outcome |
| `finish(workflow, log, final)` | Enforces the round rules and the stable category, logs human rounds and status, saves the workflow |
| `proposal_problem(domain, agent, policy_id, proposal)` | Why a proposal could not be executed exactly as approved, or `None` |
| `blocked_write(domain, name, arguments)` | The write guards; returns `(error_type, reason)` or `None` |
| `coverage_decreases(...)`, `duplicate_claim(...)`, `duplicate_key(...)`, `calculated_reimbursement(...)`, `authorization_required(...)` | The individual guard checks |
| `reimbursement(expense, deductible, remaining_limit)` | The policy formula, returns `(final, working)` |
| `system_prompt(agent)` | Agent prompt, skills and response schema (the second definition is the one used) |
| `workflow_dir()`, `save_workflow()`, `marks_failure()`, `model_provider()` | Workflow storage, failure marking and model client handling |

Constants: `RESUMED_BY` (which event resumes which state), `COVERAGE_THRESHOLDS`,
`REIMBURSEMENT_THRESHOLD`, `AUTHORIZABLE` (writes an approval can release, per agent),
`DESCRIBED` (how the policyholder reads them), `INSTEAD` (what the model is told after a block).

### `app/__main__.py`

| Function | Role |
|---|---|
| `run(path)` | `python -m app run`: new run, new log, `run_request()` |
| `resume(path, run_id=None)` | `python -m app resume`: `admit()`, loads the run's log, `resume_request()` |
| `run_logged(work, log, store, run_id)` | Records failures and always saves the domain snapshot |

## 13. Tests

`tests/test_runtime.py` runs without an API key: a scripted model replaces the Anthropic
client, and the real MCP server and domain are used. 35 tests:

| Area | Tests |
|---|---|
| Resume | T04 reply resumes the same run, conversation and log; a second question closes as `insufficient_information`; refused replies (wrong type, duplicate event) are logged as `ReplyRejected` without a state change, and unknown tickets or runs are refused |
| Insurer decisions | T06 approval runs exactly the saved proposal once and a second decision is refused; T06 rejection executes nothing; T11 approval pays 1560.00 and records the claim as paid, rejection records it as rejected |
| Proposal check | A proposal that needs no authorization goes back to the model; no authorization round is counted |
| Authorization guard | Threshold cases (equality allowed, above blocked, termination always); a T06 write never reaches the domain |
| Decrease guard | A decrease is blocked before authorization; an increase with an unchanged part still runs |
| Calculation | The `docs/policy.md` examples, zero, cent rounding |
| Payment guard | Correct amounts pass; forgotten deductible, missed cap and zero are blocked; above 1500.00 needs authorization |
| Duplicate guard | T13's invoice (same as CL-H001) is not registered again |
| Payment flow | T08: a wrong payment (144.00) is blocked, then 104.00 is paid once |

Run with `python -m pytest -q` (all tests) or `python -m pytest -q tests/test_runtime.py`.

### Writing a test

| Helper | Use |
|---|---|
| `ScriptedModel(*turns)` | Replaces the model. A turn is a list of content blocks (e.g. `ToolUseBlock`), a dict (returned as the final JSON answer), or a callable that builds the turn from the messages so far. `seen` keeps the messages of every call |
| `answer(agent_state, request_outcome, **fields)` | A final answer in the response schema |
| `start(ticket, model, prepare=None)` | Runs a training ticket in a fresh run; `prepare(store, run_id)` can change the data first |
| `waiting`, `awaiting_insurer` | T04 waiting for its clarification; T06 waiting for the insurer |
| `reply(**changes)`, `decision(ticket, verdict, **changes)` | Reply events |
| `claim_call(invoice_text)`, `created_claim_id(messages)` | A `create_claim` call from a training invoice; the claim ID it returned |

```python
def test_policy_enquiry_is_answered(start):
    read = ToolUseBlock(type="tool_use", id="toolu_1", name="get_policy", input={"policy_id": "POL-001"})
    model = ScriptedModel([read], answer("completed", "answered", request_category="policy_enquiry"))
    store, run_id, log, final = start("T01", model)
    assert log.record["agent_state"] == "completed"
```

## 14. Extending the harness

- **A new skill:** add a Markdown file to `skills/administration/` or `skills/claims/`. No
  code change; it is loaded on the next model turn.
- **A new guard:** add a branch to `blocked_write()` that returns `(ErrorType, reason)`, add
  `INSTEAD["ErrorType"]` with what the model must do instead, and add a test. Keep refusals
  before the authorization check.
- **A new write that needs approval:** add it to `AUTHORIZABLE` and `DESCRIBED`, add its
  threshold rule to `authorization_required()`, map its tool result in `executed()`, and
  describe the proposal shape in the agent's skill.
- **Changed thresholds:** `COVERAGE_THRESHOLDS` and `REIMBURSEMENT_THRESHOLD`. The skills
  state the same numbers and must be kept in sync.

## 15. Verification with the real model

Single runs with `claude-haiku-5-5`; every evaluation log passed `python -m app validate-log`.

| Ticket | Result |
|---|---|
| T01 | `completed` / `answered` from `get_policy`, after all changes |
| T02 | `completed` / `answered` from `get_claim`, after all changes |
| T04 + clarification reply | Waited for the postal code, resumed in the same run, `completed` / `updated`; the same reply sent again was refused |
| T05 | `completed` / `updated` to 5000/200, no authorization |
| T06 | Before the guard: 1 of 3 runs wrote 10000/500 without approval. After: `awaiting_authorization`, no write |
| T07 | The model tried `terminate_policy`; the guard blocked it; `awaiting_authorization`, policy still active |
| T08 | `paid` 104.00 |
| T09 | `awaiting_clarification`, asking only for the prescription |
| T10 | `paid` 1500.00 without authorization (last day of coverage, exactly at the threshold) |
| T11 | Claim `pending_authorization`; proposal `pay_claim` 1560.00 |
| T12 | `paid` 900.00, capped by the remaining limit |
| T13 | `duplicate` of CL-H001, no new claim |
| T14 | `not_covered`, no documents requested |
| T15 | `inactive_policy` |
| T06 + approval | `completed` / `updated` to 10000.00 / 500.00 through the CLI; the same approval sent again was refused and logged as `ReplyRejected` |
| T07 + rejection | `closed_without_execution` / `insurer_rejected`, nothing executed, policy still active |
| T11 + approval | `completed` / `paid` 1560.00, claim recorded as paid, consumption 1664.00; a clarification reply sent while it waited for the insurer was refused and logged |

These are single runs: model behaviour can vary between runs, which is why the rules that
must always hold are enforced in code.

## 16. Known limitations and next steps

- **No re-routing between agents.** If the router picks the wrong agent, that agent closes
  the request as out of scope (`out_of_scope_claims` / `out_of_scope_administration`);
  it is not handed to the other agent.
- **The three-attempt rule for tool format errors is not counted by the harness**, and the
  logger's `handover` event is not used yet. The model is told the rule in its prompt.
- **Not checked by code:** writes on an inactive policy; claim eligibility (excluded service,
  patient, dates) before `pay_claim`; whether the expense on the claim matches the invoice text.
  These still depend on the skills and prompts.
- **Correlation:** without `--run`, a reply goes to the most recently updated waiting run of
  its ticket.
- **Interrupted processes:** a run interrupted mid-step (Ctrl+C) stays `processing` in its
  workflow and cannot be resumed.

## 17. Notes for the team

- Two supplied tests fail in this environment, before and after these changes:
  `test_cli_names_the_actual_problem` loads the real key from `.env` and calls the API;
  a logging test expects `/` in a path and gets `\` on Windows.
- `examples/route2_demo.py` imports `route2`, which the router change removed.
- `app/runtime.py` contains two `system_prompt()` definitions; the second (with skill loading)
  is the one used. The `prompt` parameter of `agent_turns()` is no longer used.
- `load_dotenv` does not override existing environment variables: an `ANTHROPIC_MODEL` set in
  the shell wins over `.env`.
