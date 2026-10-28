"""Checks for supplied infrastructure, not a solved business harness or final rubric."""

import asyncio
import copy
import json
from decimal import Decimal

import pytest
from jsonschema import ValidationError

from app.contracts import ROOT, TOOLS, document, validator
from app.domain import DomainStore
from app.evaluation import EvaluationLog, IncompleteEvaluationLog
from app.mcp_client import DomainClient


@pytest.fixture
def domain(tmp_path):
    store = DomainStore(tmp_path / "domain")
    run_id = store.start_run()
    return store, run_id


def call(domain, name, **args):
    store, run_id = domain
    result = store.call(name, {"run_id": run_id, "policy_id": "POL-001", **args})
    validator("tools.schema.json", TOOLS[name][0] + "Result").validate(result)
    return result


def new_claim(domain):
    return call(domain, "create_claim", patient_name="Taylor Example", service_category="specialist_consultation",
                invoice={"provider_id": "PROVIDER-002", "invoice_number": "TEST-01", "invoice_date": "2026-10-22",
                         "service_date": "2026-10-21", "expense": "180.00", "currency": "EUR"},
                evidence_refs=["request.invoice_text"])["data"]["claim_id"]


def test_fixture_records_match_published_contracts(domain):
    state = domain[0].snapshot(domain[1])
    for policy in state["policies"].values():
        validator("tools.schema.json", "Policy").validate(policy)
    for claim in state["claims"].values():
        validator("tools.schema.json", "Claim").validate(claim)
    for policy in state["policies"].values():
        consumption = sum((Decimal(c["payment"]["amount"]) for c in state["claims"].values()
                           if c["policy_id"] == policy["policy_id"] and c["payment"]), Decimal("0"))
        assert consumption == Decimal(policy["financial_terms"]["paid_consumption"])
    assert call(domain, "get_claim", claim_id="CL-H001")["data"]["payment"]["amount"] == "104.00"


def test_isolated_runs_preserve_fixtures_and_reject_accidental_reset(domain):
    store, run_id = domain
    before = json.loads((ROOT / "data/initial.json").read_text())
    call(domain, "update_contact_details", changes={"phone": "+393339998888"})
    other = store.start_run()
    assert store.snapshot(other) == before
    assert store.snapshot(run_id) != before
    with pytest.raises(ValueError, match="already exists"):
        store.start_run(run_id)
    assert json.loads((ROOT / "data/initial.json").read_text()) == before


@pytest.mark.parametrize("changes", [
    {"email": "changed@example.com", "phone": "bad"},
    {"address": {"city": "Rome"}},
    {"email": "changed@example.com", "iban": "not-a-supported-field"},
])
def test_contact_validation_is_atomic_and_narrow(domain, changes):
    before = domain[0].snapshot(domain[1])
    result = call(domain, "update_contact_details", changes=changes)
    assert not result["ok"] and not result["error"]["effect_applied"]
    assert domain[0].snapshot(domain[1]) == before


def test_coverage_validation_is_atomic_and_gives_catalog_feedback(domain):
    before = domain[0].snapshot(domain[1])
    result = call(domain, "update_coverage", changes={"annual_limit": "5000.00", "deductible": "75.00"})
    assert not result["ok"] and result["error"]["retryable"]
    assert "100.00" in result["error"]["allowed_values"]
    assert domain[0].snapshot(domain[1]) == before
    changed = call(domain, "update_coverage", changes={"annual_limit": "5000.00", "deductible": "100.00"})
    assert changed["data"]["after"]["remaining_limit"] == "4896.00"


def test_payment_is_observed_once_and_cannot_be_erased(domain):
    claim_id = new_claim(domain)
    first = call(domain, "pay_claim", claim_id=claim_id, amount="104.00")
    after = domain[0].snapshot(domain[1])
    second = call(domain, "pay_claim", claim_id=claim_id, amount="104.00")
    assert first["data"]["created"] is True and second["data"]["created"] is False
    assert first["data"]["payment_id"] == second["data"]["payment_id"]
    assert domain[0].snapshot(domain[1]) == after
    assert not call(domain, "pay_claim", claim_id=claim_id, amount="200.00")["ok"]
    assert not call(domain, "record_claim_outcome", claim_id=claim_id, outcome="rejected", reason="Cannot erase payment")["ok"]
    assert after["policies"]["POL-001"]["financial_terms"]["paid_consumption"] == "208.00"


def test_unpaid_record_cannot_be_claimed_paid(domain):
    claim_id = new_claim(domain)
    assert not call(domain, "record_claim_outcome", claim_id=claim_id, outcome="paid", reason="No payment occurred")["ok"]


def test_reference_mismatch_has_no_effect(domain):
    store, run_id = domain
    result = store.call("get_claim", {"run_id": run_id, "policy_id": "POL-002", "claim_id": "CL-H001"})
    assert result["error"]["code"] == "REFERENCE_MISMATCH"


def test_history_outcome_persistence_and_no_payment_after_recorded_rejection(domain):
    history = call(domain, "list_claims")["data"]["claims"]
    assert [c["claim_id"] for c in history] == ["CL-H001"]
    claim_id = new_claim(domain)
    pending = call(domain, "record_claim_outcome", claim_id=claim_id,
                   outcome="pending_authorization", reason="Awaiting a decision")
    assert pending["data"]["payment"] is None
    rejected = call(domain, "record_claim_outcome", claim_id=claim_id,
                    outcome="rejected", reason="Insurer declined")
    assert rejected["data"]["status"] == "rejected"
    assert not call(domain, "pay_claim", claim_id=claim_id, amount="104.00")["ok"]


def test_termination_reports_actual_transition_and_repeat_is_noop(domain):
    # Technical simulator test, not an implementation of the team's approval gate.
    first = call(domain, "terminate_policy")
    second = call(domain, "terminate_policy")
    assert first["data"]["before_status"] == "active"
    assert first["data"]["after_status"] == "terminated"
    assert first["data"]["changed"] is True
    assert second["data"]["changed"] is False


def test_all_public_events_validate_and_rejection_requires_reason():
    for path in sorted((ROOT / "examples").rglob("*.json")):
        validator("events.schema.json").validate(json.loads(path.read_text()))
    rejection = json.loads((ROOT / "examples/events/insurer_reject.json").read_text())
    rejection.pop("reason")
    with pytest.raises(ValidationError):
        validator("events.schema.json").validate(rejection)
    assert len(list((ROOT / "examples/training").glob("*.json"))) == 15


def test_cumulative_logger_survives_load_and_preserves_request_events(tmp_path):
    request = json.loads((ROOT / "examples/training/T04.json").read_text())
    log = EvaluationLog(request, "RUN-test", "TEAM-1", tmp_path)
    log.route("administration")
    log.human_requested("clarification", {"question": "What postal code?"})
    log.status(agent_state="awaiting_clarification", request_outcome="information_requested", reason="Postal code missing", request_category="contact_update")
    restored = EvaluationLog.load(log.path)
    restored.input(json.loads((ROOT / "examples/events/clarification_reply.json").read_text()))
    assert len(restored.record["events"]) == 5
    assert restored.record["human_rounds"]["clarification"] == 1
    original = copy.deepcopy(restored.record)
    wrong = json.loads((ROOT / "examples/events/clarification_reply.json").read_text())
    wrong["ticket_id"] = "OTHER"
    with pytest.raises(ValueError, match="different ticket"):
        restored.input(wrong)
    assert restored.record == original


@pytest.mark.parametrize("state", ["completed", "closed_without_execution", "awaiting_clarification", "awaiting_authorization"])
def test_classification_required_at_completion_or_human_wait_without_losing_events(tmp_path, state):
    request = json.loads((ROOT / "examples/training/T04.json").read_text())
    log = EvaluationLog(request, "RUN-classification", "TEAM-1", tmp_path)
    previous = copy.deepcopy(log.record["events"])
    external = {**log.record, "agent_state": state}
    with pytest.raises(ValidationError):
        validator("evaluation.schema.json").validate(external)
    with pytest.raises(IncompleteEvaluationLog, match="handling_agent, request_category"):
        log.status(agent_state=state, request_outcome="answered", reason="Attempted transition")
    restored = EvaluationLog.load(log.path)
    assert restored.record["agent_state"] == "failed"
    assert restored.record["request_outcome"] == "technical_failure"
    assert restored.record["events"][:len(previous)] == previous
    assert restored.record["handling_agent"] is None and restored.record["request_category"] is None
    assert restored.record["events"][-1]["data"]["attempted_agent_state"] == state


@pytest.mark.parametrize("agent, category, invalid_field", [
    (None, "contact_update", "handling_agent"),
    ("administration", None, "request_category"),
    ("unknown_agent", "contact_update", "handling_agent"),
    ("administration", "unknown_category", "request_category"),
])
def test_missing_or_invalid_classification_reports_the_specific_field(tmp_path, agent, category, invalid_field):
    request = json.loads((ROOT / "examples/training/T04.json").read_text())
    log = EvaluationLog(request, "RUN-field", "TEAM-1", tmp_path)
    if agent is not None:
        # Test validation of the public log record, including integrations bypassing route().
        log.record["handling_agent"] = agent
    with pytest.raises(IncompleteEvaluationLog) as error:
        log.status(agent_state="completed", request_outcome="updated", reason="Attempted completion", request_category=category)
    assert error.value.fields == [invalid_field]
    assert EvaluationLog.load(log.path).record["agent_state"] == "failed"


def test_failure_before_classification_is_saved_without_fabricated_metadata(tmp_path):
    request = json.loads((ROOT / "examples/training/T04.json").read_text())
    log = EvaluationLog(request, "RUN-early-error", "TEAM-1", tmp_path)
    assert log.record["agent_state"] == "processing" and log.record["request_category"] is None
    log.event("error", error_type="ConnectionError")
    log.status(agent_state="failed", request_outcome="technical_failure", reason="ConnectionError")
    restored = EvaluationLog.load(log.path)
    assert restored.record["handling_agent"] is None and restored.record["request_category"] is None
    assert restored.record["reason"] == "ConnectionError"


def test_validate_log_cli_reports_missing_fields_without_rewriting_input(tmp_path, monkeypatch, capsys):
    import app.__main__ as cli
    request = json.loads((ROOT / "examples/training/T04.json").read_text())
    log = EvaluationLog(request, "RUN-cli-validation", "TEAM-1", tmp_path)
    monkeypatch.setattr("sys.argv", ["app", "validate-log", str(log.path)])
    assert cli.main() == 0  # Unclassified processing snapshot is legitimate.
    capsys.readouterr()
    invalid = {**log.record, "agent_state": "completed"}
    log.path.write_text(json.dumps(invalid))
    before = log.path.read_bytes()
    assert cli.main() == 1
    message = capsys.readouterr().err
    assert "handling_agent" in message and "request_category" in message
    assert log.path.read_bytes() == before


def test_cli_preserves_domain_effects_and_logging_error_on_incomplete_metadata(tmp_path, monkeypatch):
    import app.__main__ as cli
    import app.runtime as runtime
    store = DomainStore(tmp_path / "domain")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    monkeypatch.setattr(cli, "DomainStore", lambda: store)
    monkeypatch.setattr(cli, "EvaluationLog", lambda request, run_id, team_id:
                        EvaluationLog(request, run_id, team_id, tmp_path / "logs"))

    async def incomplete_integration(request, run_id, log):
        log.route("administration")
        result = store.call("update_contact_details", {"run_id": run_id, "policy_id": "POL-001",
                                                       "changes": {"phone": "+393339998888"}})
        log.event("tool_result", name="update_contact_details", result=result)
        log.status(agent_state="completed", request_outcome="updated", reason="Updated, but integration omitted category")

    monkeypatch.setattr(runtime, "run_request", incomplete_integration)
    with pytest.raises(IncompleteEvaluationLog, match="request_category"):
        asyncio.run(cli.run(ROOT / "examples/training/T03.json"))
    saved = EvaluationLog.load(next((tmp_path / "logs").glob("*.json"))).record
    assert saved["agent_state"] == "failed" and "request_category" in saved["reason"]
    assert saved["domain_snapshot"]["policies"]["POL-001"]["contacts"]["phone"] == "+393339998888"
    assert any(e["kind"] == "tool_result" and e["data"]["result"]["ok"] for e in saved["events"])


def test_real_stdio_mcp_discovery_reads_errors_and_bound_context(tmp_path, monkeypatch):
    monkeypatch.setenv("EURAPCO_DATA_DIR", str(tmp_path / "domain"))
    store = DomainStore()
    run_id = store.start_run()

    async def scenario():
        async with DomainClient(run_id, "administration") as client:
            assert len(client.model_tools()) == 4
            assert "run_id" not in client.model_tools()[0]["input_schema"]["properties"]
            assert (await client.call("get_policy", {"policy_id": "POL-001"}))["ok"]
            missing = await client.call("get_policy", {})
            validator("tools.schema.json", "GetPolicyResult").validate(missing)
            assert missing["error"]["field"] == "policy_id"
            bad = await client.call("update_coverage", {"policy_id": "POL-001", "changes": {"annual_limit": "6000.00"}})
            assert not bad["ok"] and bad["error"]["effect_applied"] is False
            forbidden = await client.call("pay_claim", {"policy_id": "POL-001", "claim_id": "CL-H001", "amount": "104.00"})
            assert not forbidden["ok"]
            injected = await client.call("get_policy", {"policy_id": "POL-001", "run_id": "OTHER"})
            assert not injected["ok"]

    asyncio.run(scenario())


def test_cli_names_the_actual_problem(monkeypatch, capsys):
    import app.__main__ as cli
    monkeypatch.setattr("sys.argv", ["app", "run", str(ROOT / "examples/events/clarification_reply.json")])
    assert cli.main() == 1
    assert "Reply events are not admitted" in capsys.readouterr().err
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["app", "run", str(ROOT / "examples/training/T01.json")])
    assert cli.main() == 1
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_cli_records_the_real_failure_not_the_exception_group(tmp_path, monkeypatch, capsys):
    import app.__main__ as cli
    import app.runtime as runtime
    store = DomainStore(tmp_path / "domain")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    monkeypatch.setattr(cli, "DomainStore", lambda: store)
    monkeypatch.setattr(cli, "EvaluationLog", lambda request, run_id, team_id:
                        EvaluationLog(request, run_id, team_id, tmp_path / "logs"))

    async def wrapped_failure(request, run_id, log):
        raise ExceptionGroup("task group", [ExceptionGroup("inner", [json.JSONDecodeError("bad", "x", 0)])])

    monkeypatch.setattr(runtime, "run_request", wrapped_failure)
    monkeypatch.setattr("sys.argv", ["app", "run", str(ROOT / "examples/training/T01.json")])
    assert cli.main() == 1
    assert "JSONDecodeError" in capsys.readouterr().err
    saved = EvaluationLog.load(next((tmp_path / "logs").glob("*.json"))).record
    assert saved["agent_state"] == "failed" and saved["reason"] == "JSONDecodeError"
    assert [e for e in saved["events"] if e["kind"] == "error"][0]["data"]["error_type"] == "JSONDecodeError"


def test_tool_call_log_keeps_a_model_supplied_run_id_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("EURAPCO_DATA_DIR", str(tmp_path / "domain"))
    store = DomainStore()
    run_id = store.start_run()
    request = json.loads((ROOT / "examples/training/T01.json").read_text())
    log = EvaluationLog(request, run_id, "TEAM-1", tmp_path / "logs")

    async def scenario():
        async with DomainClient(run_id, "administration", log) as client:
            return await client.call("get_policy", {"policy_id": "POL-001", "run_id": "OTHER"})

    assert not asyncio.run(scenario())["ok"]
    call = [e for e in log.record["events"] if e["kind"] == "tool_call"][0]["data"]
    assert call["arguments"]["run_id"] == "OTHER"
