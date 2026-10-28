# Eurapco · Medical policy harness challenge

Build a reliable harness around two working agents: **Administration** and **Claims**. 
You start with real agent/tool connections, fictional data and a supplied evaluation logger.

Start with the [challenge guide](docs/challenge.html).
Then read:
- [the fictional policy](docs/policy.md)
- [minimum information](docs/minimum-information.md)
- [tool contracts](docs/tools.md)
- [events and evaluation logs](docs/contracts.md).

## Quick start

Use **Python 3.12, 3.13 or 3.14** and a separate environment for this repository.
Do not reuse an environment from the live coding session or the standalone pretests.
The commands below use 3.12; if you use 3.13 or 3.14, replace `3.12` with your version.

macOS / Linux:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m app check
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m app check
```

If PowerShell blocks activation, use `.venv\Scripts\python.exe` in place of
`python`; changing the computer's execution policy is not required.

`check` starts the local MCP server, discovers the nine tools, and tests a read
and simulated contact write in a fresh isolated copy. It needs no API key.

Edit `.env` with your Anthropic key and team ID, then run:

```sh
python -m app run examples/training/T01.json
python -m app run examples/training/T02.json
```

The default model is `claude-haiku-5-5`. You can also use Sonnet (`claude-sonnet-5-5`)
by setting `ANTHROPIC_MODEL` in `.env`. Both have been tried with this baseline, but
results can differ between the two: for example, on T07 Sonnet may terminate the policy
without waiting for the insurer, where Haiku asks first.
Each invocation is a **new independent
execution**, with a fresh domain copy, run ID and cumulative log under `logs/`.
The CLI prints both the response and evaluation-file path. Never commit `.env`.

A waiting request resumes in the **same** run and log, on a clarification reply or an
insurer decision:

```sh
python -m app run examples/training/T04.json
python -m app resume examples/events/clarification_reply.json   # --run RUN_ID to pick the run
python -m app run examples/training/T06.json
python -m app resume examples/events/insurer_approve.json       # or insurer_reject.json
```

The reply is matched to the latest waiting run of its `ticket_id`. Workflow state
(conversation, waiting state, saved proposal, human rounds, processed event IDs) is kept
in `.runtime/workflows/RUN_ID.json`. A reply to a run that is not waiting, of the wrong
type or already processed is refused: it is recorded in that run's log as a
`ReplyRejected` error event, without any state change. A second clarification is not
asked: the request closes with `insufficient_information`.

An insurer decision is applied without the model (and without an API key). Approval runs
exactly the saved proposal once and reports the actual tool result; rejection runs nothing
and closes with `insurer_rejected`. For a reimbursement the claim record is updated to
`paid` or `rejected`. A proposal is only sent to the insurer if the harness can execute it
exactly: `operation` and `arguments`, valid tool input, passing the guards below and really
needing authorization. Otherwise it goes back to the model (`InvalidProposal` error event).

Writes that need the insurer never reach the domain from the model: `terminate_policy`,
`update_coverage` with a final limit above 5000.00 or deductible above 200.00, and
`pay_claim` above 1500.00 are blocked in `app/runtime.py` (logged as an
`AuthorizationRequired` error event). The model must return a proposal instead.
Before any `update_coverage`, the harness reads the policy (an extra `get_policy` in the
log) and blocks a value below the current one (`UnsupportedDecrease`), before any
authorization: the request closes as `invalid_input`.
Before `create_claim` it checks `list_claims` for the duplicate key (provider ID + invoice
number + invoice-date year; `DuplicateInvoice`). Before `pay_claim` it recomputes the
reimbursement from the claim's expense and the current deductible and remaining limit, and
blocks any other amount (`AmountMismatch`) or a zero payment (`ZeroReimbursement`).

## Starting behavior

Policy and reimbursement enquiries retrieve and explain existing records. A small
keyword router chooses an agent. Both agents have monolithic prompts and their
domain tools. Required human interactions are described in those prompts; the
loop can ask/stop and resume on a clarification reply or an insurer decision.

Business checks mostly depend on prompt instructions. There is no finished
clarification/authorization state machine, deterministic reimbursement calculator,
deterministic duplicate check or completed domain skill set.
All effects stay inside an isolated fictional JSON data store.

## Repository map

```text
agents/administration/   Administration code and monolithic prompt
agents/claims/           Claims code and monolithic prompt
app/                    Router, baseline loop, MCP client, logger and skill loader
mcp_server/             Nine provided domain tools over stdio or Streamable HTTP
data/initial.json       Shared initial domain fixtures; copied for each new run
contracts/              Events, tools, response, vocabulary and evaluation schemas
skills/                 Empty domain folders; write your procedures here
examples/training/      15 public tickets, easy through advanced
examples/events/        Clarification and insurer-decision format examples
docs/                   Challenge, policy, minimum information and API contracts
tests/                  Supplied infrastructure checks, not the final scoring suite
```

## Development checks

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m app validate-log logs/YOUR_RUN_ID.json
```

The dependency constraints pin the preparation environment. The primary baseline
uses Anthropic directly and MCP SDK **2.2.0**, matching the participant MCP pretest.
Teams may use another framework while preserving contracts. 

## Optional explicit HTTP server

The default stdio transport launches the server automatically. To use the same
SDK over local HTTP, start a separate terminal in this repository/environment:

```sh
python -m mcp_server --transport streamable-http --port 8765
```

Set `MCP_URL=http://127.0.0.1:8765/mcp` in `.env`, then run `python -m app check`
or an agent in a second terminal. Both must use the same repository/data directory.
Stop the server with Ctrl+C. The local server is for this fictional exercise,
not a production or publicly hosted service.
