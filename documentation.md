# Harness changes

This document describes the changes made to the baseline harness: stop/resume for
clarification replies, deterministic write guards, one domain skill per agent, tests
and the verification runs. Rules referenced below come from `docs/challenge.html`,
`docs/policy.md`, `docs/minimum-information.md`, `docs/tools.md` and `docs/contracts.md`.

## Changed files

| File | Change |
|---|---|
| `app/runtime.py` | Persisted workflow state, reply correlation/admission, resume, one-clarification rule, write guards, reimbursement formula |
| `app/__main__.py` | New `resume` command; shared `run_logged()` failure/snapshot handling |
| `skills/administration/coverage_change.md` | New Administration skill |
| `skills/claims/reimbursement_request.md` | New Claims skill |
| `tests/test_runtime.py` | New offline tests (scripted model, real MCP domain) |
| `README.md` | Usage of `resume`, description of the guards |

Not part of these changes: the router rework in `app/router.py` and the skill loading in
the second `system_prompt()` in `app/runtime.py` were done by the team.

## 1. Stop and resume for clarification replies

The baseline handled only the first message. A request that asked the policyholder a
question could stop, but the reply could not be delivered. Now a reply continues the
**same run, the same model conversation and the same evaluation log**.

### Workflow state

Every run keeps its state in `.runtime/workflows/RUN_ID.json` (directory overridable
with `EURAPCO_WORKFLOW_DIR`). It is saved when the run starts, at every waiting or final
state, and when a reply is admitted.

| Field | Content |
|---|---|
| `run_id`, `ticket_id`, `agent`, `log_path` | Identity of the run, the handling agent and its evaluation log |
| `request` | The original request, unchanged: its facts are fixed |
| `messages` | The full model conversation, including tool calls and results |
| `agent_state`, `request_category` | Current state and the category set before waiting |
| `question`, `proposal` | What the run is waiting on |
| `human_rounds` | Clarifications and authorizations requested so far |
| `event_ids` | Every event already processed, to reject duplicates |
| `updated_at` | Last save |

### Admission: `admit(event, run_id=None)`

A reply is matched to the waiting run of its `ticket_id` (the latest one if several
exist, or the run given with `--run`). It is rejected, **without changing or logging
anything**, when:

- it is an initial `request` (that starts a new run);
- no workflow exists for the ticket (or the given run);
- the run is not waiting (completed, closed, failed or already processing);
- the event type does not match the waiting state (`clarification_reply` for
  `awaiting_clarification`, `insurer_decision` for `awaiting_authorization`);
- its `event_id` was already processed;
- it is an `insurer_decision`: resuming on insurer decisions is not implemented yet.

### Resume: `resume_request(event, workflow, log)`

1. The workflow is marked `processing` and the event ID recorded **before any work**, so
   a second delivery of the same reply is rejected by `admit()`.
2. The reply is added to the log (`log.input`), followed by a `processing` status.
3. The reply is appended to the conversation with the rules: the original facts are
   fixed, no second question, close with `insufficient_information` if still incomplete,
   request insurer authorization if the operation needs it.
4. The agent loop continues with the same agent and the same tools.

After every run of the loop, `advance()` enforces:

- **One clarification per request.** If the model asks a second question, the request is
  closed as `closed_without_execution` / `insufficient_information`, with the would-be
  question in the reason.
- **Stable category.** The request category set before waiting is kept: a reply supplies
  missing facts, it does not change the operation.
- Human rounds are counted with `log.human_requested`, the status is logged and the
  workflow saved.

If the run fails with an exception, the workflow is marked `failed`, so no later reply can
resume a half-handled request.

### CLI

```sh
python -m app run examples/training/T04.json
python -m app resume examples/events/clarification_reply.json          # latest waiting run of T04
python -m app resume examples/events/clarification_reply.json --run RUN-...
```

`run` still refuses reply events and now points to `resume`. Both commands share
`run_logged()`, which records failures (`failed` / `technical_failure`) and always saves the
final domain snapshot in the log.

## 2. Write guards

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

Cases the guards pass on to the domain unchanged: values that are not amounts (the tool's
own format check rejects them) and `pay_claim` on a claim that is already paid (the domain
returns the existing payment without paying again).

The harness reads (`get_policy`, `list_claims`, `get_claim`) go through the normal tool
client, so they appear in the log as tool calls next to the model's own calls.

### Reimbursement formula

`reimbursement(expense, deductible, remaining_limit)` implements `docs/policy.md` with
`Decimal`: eligible = max(0, expense − deductible); theoretical = 80% of eligible, rounded
half-up to cents; final = min(theoretical, remaining limit). It returns the final amount and
the working shown to the model. With deductible 50.00: 180.00 → 104.00, 1925.00 → 1500.00,
2000.00 → 1560.00, and 2000.00 with 900.00 remaining → 900.00.

## 3. Skills

The team's loader (`system_prompt()` in `app/runtime.py`) appends every Markdown file of
the agent's folder under a `## Skill: <file name>` heading. Each agent sees only its own skills.

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

- A claim is registered only after every check passed; refusals create no claim.
- Final ≤ 1500.00: `create_claim`, `pay_claim` with exactly the final amount,
  `record_claim_outcome` "paid".
- Final > 1500.00: `create_claim`, `record_claim_outcome` "pending_authorization", and an
  `awaiting_authorization` proposal `{"operation": "pay_claim", "arguments": {policy_id,
  claim_id, amount}, "calculation": {...}, "evidence_refs": [...]}`.
- Final 0.00: `create_claim`, `record_claim_outcome` "rejected", `zero_reimbursement`.

## 4. Tests

`tests/test_runtime.py` runs without an API key: a scripted model replaces the Anthropic
client, and the real MCP server and domain are used. 30 tests:

| Area | Tests |
|---|---|
| Resume | T04 reply resumes the same run, conversation and log; a second question closes as `insufficient_information`; unadmissible replies (wrong type, duplicate event, unknown ticket, wrong run) are rejected without touching the log |
| Authorization guard | Threshold cases (equality allowed, above blocked, termination always); a T06 write never reaches the domain |
| Decrease guard | A decrease is blocked before authorization; an increase with an unchanged part still runs |
| Calculation | The `docs/policy.md` examples, zero, cent rounding |
| Payment guard | Correct amounts pass; forgotten deductible, missed cap and zero are blocked; above 1500.00 needs authorization |
| Duplicate guard | T13's invoice (same as CL-H001) is not registered again |
| Payment flow | T08: a wrong payment (144.00) is blocked, then 104.00 is paid once |

Run with `python -m pytest -q`.

## 5. Verification with the real model

Single runs with `claude-haiku-5-5`; every evaluation log passed `python -m app validate-log`.

| Ticket | Result |
|---|---|
| T04 + clarification reply | Waited for the postal code, resumed in the same run, `completed` / `updated`; the same reply sent again was rejected |
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

## 6. Known limitations and next steps

- **Insurer decisions cannot be resumed yet.** `resume` rejects them, so an approved proposal
  (T06, T07, T11) cannot be executed. The intended design: on approval the runtime runs the
  saved `proposal["arguments"]` itself, not through the model (which is why the guards have
  no exception for approved writes); on rejection it closes with `insurer_rejected`.
- **Not checked by code:** writes on an inactive policy; claim eligibility (excluded service,
  patient, dates) before `pay_claim`; whether the expense on the claim matches the invoice text.
  These still depend on the skills and prompts.
- **Correlation:** without `--run`, a reply goes to the most recently updated waiting run of
  its ticket.

## 7. Notes for the team

- Two supplied tests fail in this environment, before and after these changes:
  `test_cli_names_the_actual_problem` loads the real key from `.env` and calls the API;
  a logging test expects `/` in a path and gets `\` on Windows.
- `examples/route2_demo.py` imports `route2`, which the router change removed.
- `app/runtime.py` contains two `system_prompt()` definitions; the second (with skill loading)
  is the one used. The `prompt` parameter of `agent_turns()` is no longer used.
- `load_dotenv` does not override existing environment variables: an `ANTHROPIC_MODEL` set in
  the shell wins over `.env`.
