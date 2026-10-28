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

## Starting behavior

Policy and reimbursement enquiries retrieve and explain existing records. A small
keyword router chooses an agent. Both agents have monolithic prompts and their
domain tools. Required human interactions are described in those prompts; the
initial loop can ask/stop but **does not receive or resume human replies**.
The CLI rejects reply events explicitly rather than pretending to have resumed.

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
