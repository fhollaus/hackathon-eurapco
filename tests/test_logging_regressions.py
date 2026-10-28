"""Regression checks for durable evaluation evidence and logger recovery."""

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts import ROOT
from app.domain import DomainStore
from app.evaluation import EvaluationLog


@pytest.fixture
def log(tmp_path):
    request = json.loads((ROOT / "examples/training/T04.json").read_text())
    return EvaluationLog(request, "RUN-regression", "TEAM-1", tmp_path / "logs")


@pytest.mark.parametrize("value, expected", [
    (Decimal("104.00"), "104.00"),
    (Decimal("123456789.123456789123456789"), "123456789.123456789123456789"),
    (datetime(2026, 10, 23, 16, 10, tzinfo=timezone(timedelta(hours=2))), "2026-10-23T16:10:00+02:00"),
    (Path("evidence/invoice.txt"), "evidence/invoice.txt"),
])
def test_nested_python_values_are_saved_without_poisoning_later_writes(log, value, expected):
    payload = {"items": [{"value": value}]}
    log.event("tool_result", payload=payload)
    payload["items"].clear()  # The caller cannot change recorded evidence afterwards.
    log.event("error", error_type="LaterFailure")
    log.status(agent_state="failed", request_outcome="technical_failure", reason="LaterFailure")
    log.snapshot({"observed": value})
    saved = EvaluationLog.load(log.path).record
    assert saved == log.record
    assert saved["events"][1]["data"]["payload"]["items"][0]["value"] == expected
    assert saved["events"][-1]["kind"] == "status"
    assert saved["agent_state"] == "failed"
    assert saved["domain_snapshot"] == {"observed": expected}


@pytest.mark.parametrize("value", [object(), float("nan"), float("inf"), Decimal("NaN"),
                                        Decimal("Infinity"), datetime(2026, 10, 23, 16, 10), {1: "not a string key"}])
def test_rejected_payload_keeps_prior_evidence_and_allows_recovery(log, value):
    log.event("tool_result", result={"ok": True})
    prior = copy.deepcopy(log.record["events"])
    with pytest.raises(ValueError, match="Evaluation logging failed"):
        log.event("model_response", payload={"nested": [value]})
    saved = EvaluationLog.load(log.path).record
    assert saved == log.record
    assert saved["events"][:len(prior)] == prior
    assert len(saved["events"]) == len(prior) + 1
    assert saved["agent_state"] == "failed" and saved["request_outcome"] == "technical_failure"
    assert saved["events"][-1]["kind"] == "error"
    assert "payload" in saved["events"][-1]["data"]["field"]
    restored = EvaluationLog.load(log.path)
    restored.event("error", error_type="HandledByTeam")
    restored.snapshot({"actual_effect": True})
    assert EvaluationLog.load(log.path).record == restored.record


def test_invalid_status_does_not_block_error_handler_or_snapshot(log):
    prior = copy.deepcopy(log.record["events"])
    with pytest.raises(ValueError, match="agent_state"):
        log.status(agent_state="typo", request_outcome="in_progress", reason="Invalid transition")
    assert log.record["events"][:len(prior)] == prior
    assert log.record["agent_state"] == "failed"
    log.event("error", error_type="HandledByTeam")
    log.snapshot({"actual_effect": True})
    assert EvaluationLog.load(log.path).record == log.record


def test_direct_save_rejects_bad_data_and_restores_a_writable_record(log):
    log.record["domain_snapshot"] = {"bad": object()}
    with pytest.raises(ValueError, match="domain_snapshot.bad"):
        log.save()
    log.event("error", error_type="HandledByTeam")
    assert EvaluationLog.load(log.path).record == log.record
    assert log.record["domain_snapshot"] is None


def test_circular_human_payload_is_rejected_without_consuming_a_round(log):
    payload = {}
    payload["self"] = payload
    with pytest.raises(ValueError, match="circular"):
        log.human_requested("clarification", payload)
    assert log.record["human_rounds"]["clarification"] == 0
    log.human_requested("clarification", {"question": "What postal code?"})
    assert log.record["human_rounds"]["clarification"] == 1
    assert EvaluationLog.load(log.path).record == log.record


def test_invalid_event_kind_preserves_snapshot_and_event_sequence(log):
    log.snapshot({"observed_effect": True})
    prior = copy.deepcopy(log.record["events"])
    with pytest.raises(ValueError, match="kind"):
        log.event("not_a_kind", payload="rejected")
    log.event("error", error_type="HandledByTeam")
    assert log.record["domain_snapshot"] == {"observed_effect": True}
    assert log.record["events"][:len(prior)] == prior
    assert [event["sequence"] for event in log.record["events"]] == list(range(1, len(log.record["events"]) + 1))
    assert EvaluationLog.load(log.path).record == log.record


def test_filesystem_failure_does_not_commit_the_attempted_event(log, monkeypatch):
    import app.evaluation as evaluation
    previous = copy.deepcopy(log.record)
    disk = log.path.read_bytes()
    real_write = evaluation.atomic_json

    def disk_full(*args):
        raise OSError("simulated disk full")

    monkeypatch.setattr(evaluation, "atomic_json", disk_full)
    with pytest.raises(OSError, match="disk full"):
        log.event("tool_result", amount=Decimal("104.00"))
    assert log.record == previous and log.path.read_bytes() == disk
    monkeypatch.setattr(evaluation, "atomic_json", real_write)
    log.event("error", error_type="StorageRecovered")
    assert EvaluationLog.load(log.path).record == log.record


@pytest.mark.parametrize("payload, fails", [(Decimal("104.00"), False), (object(), True)])
def test_cli_preserves_actual_domain_effects_after_logging_payload(tmp_path, monkeypatch, capsys, payload, fails):
    import app.__main__ as cli
    import app.runtime as runtime
    store = DomainStore(tmp_path / "domain")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    monkeypatch.setattr(cli, "DomainStore", lambda: store)
    monkeypatch.setattr(cli, "EvaluationLog", lambda request, run_id, team_id:
                        EvaluationLog(request, run_id, team_id, tmp_path / "logs"))

    async def integration(request, run_id, log):
        log.route("administration")
        result = store.call("update_contact_details", {"run_id": run_id, "policy_id": "POL-001",
                                                       "changes": {"phone": "+393339998888"}})
        log.event("tool_result", result=result)
        log.event("model_response", calculated_amount=payload)
        log.status(agent_state="completed", request_outcome="updated", reason="Contact updated", request_category="contact_update")
        return {"message": "Contact updated"}

    monkeypatch.setattr(runtime, "run_request", integration)
    monkeypatch.setattr("sys.argv", ["app", "run", str(ROOT / "examples/training/T03.json")])
    assert cli.main() == int(fails)
    output = capsys.readouterr()
    saved = EvaluationLog.load(next((tmp_path / "logs").glob("*.json"))).record
    assert saved["agent_state"] == ("failed" if fails else "completed")
    assert saved["domain_snapshot"]["policies"]["POL-001"]["contacts"]["phone"] == "+393339998888"
    assert any(e["kind"] == "tool_result" and e["data"]["result"]["ok"] for e in saved["events"])
    if fails:
        assert "calculated_amount" in saved["reason"] and "calculated_amount" in output.err
        assert any(e["kind"] == "error" for e in saved["events"])
    else:
        assert any(e["data"].get("calculated_amount") == "104.00" for e in saved["events"])
