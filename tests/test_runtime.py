"""Runtime stop/resume and write guards, with a scripted model and the real MCP domain."""

import asyncio
import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from app.contracts import ROOT
from app.domain import DomainStore
from app.evaluation import EvaluationLog
from app.runtime import (ResumeError, admit, authorization_required, blocked_write, reimbursement,
                         resume_request, run_request)

ADDRESS = {"street": "8 Orchard Road", "city": "Turin", "postal_code": "10100", "country": "Italy"}


def answer(agent_state, request_outcome, **fields):
    return {"request_category": "contact_update", "agent_state": agent_state, "request_outcome": request_outcome,
            "reason": f"Scripted {request_outcome}.", "message": f"Scripted {request_outcome}.",
            "question": None, "proposal": None, **fields}


class ScriptedModel:
    """Stands in for AsyncAnthropic: returns the scripted turns and keeps the messages it was sent.
    A callable turn is built from the messages so far, e.g. to use an ID a tool just returned."""

    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, *, messages, **_):
        self.seen.append(copy.deepcopy(messages))
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(messages)
        if isinstance(turn, dict):
            turn = [TextBlock(type="text", text=json.dumps(turn))]
        return SimpleNamespace(content=turn)


@pytest.fixture
def start(tmp_path, monkeypatch):
    """Runs a training ticket's initial request in a fresh domain run with the given scripted model."""
    monkeypatch.setenv("EURAPCO_DATA_DIR", str(tmp_path / "domain"))
    monkeypatch.setenv("EURAPCO_WORKFLOW_DIR", str(tmp_path / "workflows"))
    store = DomainStore()

    def run(ticket, model, prepare=None):
        run_id = store.start_run()
        if prepare:
            prepare(store, run_id)
        request = json.loads((ROOT / f"examples/training/{ticket}.json").read_text())
        log = EvaluationLog(request, run_id, "TEAM-1", tmp_path / "logs")
        final = asyncio.run(run_request(request, run_id, log, model))
        return store, run_id, log, final
    return run


@pytest.fixture
def waiting(start):
    """T04 suspended on its postal-code question."""
    model = ScriptedModel(answer("awaiting_clarification", "information_requested",
                                 question="What is your postal code?"))
    store, run_id, log, _ = start("T04", model)
    return store, run_id, log


def decision(ticket, verdict, **changes):
    event = {"event_type": "insurer_decision", "event_id": f"EV-{ticket}-02", "ticket_id": ticket,
             "timestamp": "2026-10-22T10:02:00+02:00", "decision": verdict}
    if verdict == "reject":
        event["reason"] = "The proposed change is declined for this training execution."
    return {**event, **changes}


def reply(**changes):
    return {**json.loads((ROOT / "examples/events/clarification_reply.json").read_text()), **changes}


def test_clarification_reply_resumes_same_run_conversation_and_log(waiting):
    store, run_id, log = waiting
    assert log.record["agent_state"] == "awaiting_clarification"
    workflow = admit(reply())
    assert workflow["run_id"] == run_id
    update = ToolUseBlock(type="tool_use", id="toolu_1", name="update_contact_details",
                          input={"policy_id": "POL-001", "changes": {"address": ADDRESS}})
    model = ScriptedModel([update], answer("completed", "updated"))
    final = asyncio.run(resume_request(reply(), workflow, EvaluationLog.load(log.path), model))

    assert final["agent_state"] == "completed"
    # The model resumed with its own question and the reply in context, not a fresh request.
    first_call = model.seen[0]
    assert "What is your postal code?" in json.dumps(first_call[-2]) and "10100" in first_call[-1]["content"]
    record = EvaluationLog.load(log.path).record
    assert record["run_id"] == run_id
    assert [e["data"]["event"]["event_type"] for e in record["events"] if e["kind"] == "input"] == \
        ["request", "clarification_reply"]
    assert [e["data"]["agent_state"] for e in record["events"] if e["kind"] == "status"] == \
        ["awaiting_clarification", "processing", "completed"]
    assert record["human_rounds"] == {"clarification": 1, "authorization": 0}
    assert store.snapshot(run_id)["policies"]["POL-001"]["contacts"]["address"] == ADDRESS
    with pytest.raises(ResumeError, match="completed, not waiting"):
        admit(reply(event_id="EV-T04-03"))


def test_second_clarification_is_closed_as_insufficient_information(waiting):
    _, _, log = waiting
    model = ScriptedModel(answer("awaiting_clarification", "information_requested",
                                 question="And the street number?"))
    final = asyncio.run(resume_request(reply(), admit(reply()), EvaluationLog.load(log.path), model))
    assert (final["agent_state"], final["request_outcome"]) == ("closed_without_execution", "insufficient_information")
    record = EvaluationLog.load(log.path).record
    assert record["agent_state"] == "closed_without_execution"
    assert record["human_rounds"]["clarification"] == 1


def test_refused_replies_are_logged_in_their_run_without_a_transition(waiting):
    _, _, log = waiting
    before = EvaluationLog.load(log.path).record
    with pytest.raises(ResumeError, match="accepts only clarification_reply events"):
        admit(decision("T04", "approve"))
    with pytest.raises(ResumeError, match="already processed"):
        admit(reply(event_id="EV-T04-01"))
    with pytest.raises(ResumeError, match="No workflow found for ticket T99"):
        admit(reply(ticket_id="T99"))  # No run to log it in.
    with pytest.raises(ResumeError, match="in run RUN-other"):
        admit(reply(), "RUN-other")
    after = EvaluationLog.load(log.path).record
    added = after["events"][len(before["events"]):]
    assert [(e["kind"], e["data"]["error_type"], e["data"]["event"]["event_type"]) for e in added] == \
        [("error", "ReplyRejected", "insurer_decision"), ("error", "ReplyRejected", "clarification_reply")]
    assert (after["agent_state"], after["human_rounds"]) == (before["agent_state"], before["human_rounds"])
    assert admit(reply())["agent_state"] == "awaiting_clarification"  # Still waiting for its one reply.


@pytest.mark.parametrize("name, arguments, blocked", [
    ("update_coverage", {"changes": {"annual_limit": "5000.00", "deductible": "200.00"}}, False),  # T05
    ("update_coverage", {"changes": {"annual_limit": "10000.00"}}, True),
    ("update_coverage", {"changes": {"annual_limit": "5000.00", "deductible": "500.00"}}, True),
    ("update_coverage", {"changes": {"annual_limit": "a lot"}}, False),  # Left to the tool's format check.
    ("terminate_policy", {"policy_id": "POL-001"}, True),
    ("pay_claim", {"amount": "1500.00"}, False),
    ("pay_claim", {"amount": "1500.01"}, True),
    ("update_contact_details", {"changes": {"phone": "+393339998888"}}, False),
])
def test_writes_above_the_authority_thresholds_are_blocked(name, arguments, blocked):
    assert bool(authorization_required(name, {"policy_id": "POL-001", **arguments})) is blocked


def test_unauthorized_coverage_write_never_reaches_the_domain(start):
    changes = {"annual_limit": "10000.00", "deductible": "500.00"}
    write = ToolUseBlock(type="tool_use", id="toolu_1", name="update_coverage",
                         input={"policy_id": "POL-001", "changes": changes})
    proposal = {"operation": "update_coverage", "arguments": {"policy_id": "POL-001", "changes": changes}}
    model = ScriptedModel([write], answer("awaiting_authorization", "authorization_requested",
                                          request_category="coverage_change", proposal=proposal))
    store, run_id, log, final = start("T06", model)

    assert final["agent_state"] == "awaiting_authorization"
    assert "Blocked by the harness" in model.seen[1][-1]["content"][0]["content"]
    terms = store.snapshot(run_id)["policies"]["POL-001"]["financial_terms"]
    assert (terms["annual_limit"], terms["deductible"]) == ("3000.00", "50.00")
    events = log.record["events"]
    assert not [e for e in events if e["kind"] == "tool_call" and e["data"]["name"] == "update_coverage"]
    assert [e["data"]["tool"] for e in events if e["data"].get("error_type") == "AuthorizationRequired"] ==         ["update_coverage"]
    assert log.record["human_rounds"] == {"clarification": 0, "authorization": 1}


def coverage_write(changes):
    return ToolUseBlock(type="tool_use", id="toolu_1", name="update_coverage",
                        input={"policy_id": "POL-001", "changes": changes})


def test_coverage_decrease_is_blocked_before_any_authorization(start):
    def raised(store, run_id):  # Fixture values are the lowest in the catalogue: raise them first.
        store.call("update_coverage", {"run_id": run_id, "policy_id": "POL-001",
                                       "changes": {"annual_limit": "10000.00", "deductible": "200.00"}})

    # Deductible 500 alone would need the insurer; the limit decrease makes the whole change unsupported.
    model = ScriptedModel([coverage_write({"annual_limit": "5000.00", "deductible": "500.00"})],
                          answer("closed_without_execution", "invalid_input", request_category="coverage_change"))
    store, run_id, log, final = start("T05", model, raised)

    assert final["request_outcome"] == "invalid_input"
    assert "Only increases are supported" in model.seen[1][-1]["content"][0]["content"]
    terms = store.snapshot(run_id)["policies"]["POL-001"]["financial_terms"]
    assert (terms["annual_limit"], terms["deductible"]) == ("10000.00", "200.00")
    events = log.record["events"]
    assert [e["data"]["name"] for e in events if e["kind"] == "tool_call"] == ["get_policy"]  # The guard's read.
    assert [e["data"]["error_type"] for e in events if e["kind"] == "error"] == ["UnsupportedDecrease"]


def test_increase_with_an_unchanged_part_still_reaches_the_domain(start):
    model = ScriptedModel([coverage_write({"annual_limit": "5000.00", "deductible": "50.00"})],
                          answer("completed", "updated", request_category="coverage_change"))
    store, run_id, log, _ = start("T05", model)

    terms = store.snapshot(run_id)["policies"]["POL-001"]["financial_terms"]
    assert (terms["annual_limit"], terms["deductible"]) == ("5000.00", "50.00")
    assert not [e for e in log.record["events"] if e["kind"] == "error"]


@pytest.mark.parametrize("expense, remaining, final", [
    ("180.00", "2896.00", "104.00"), ("1925.00", "2896.00", "1500.00"), ("2000.00", "2896.00", "1560.00"),
    ("2000.00", "900.00", "900.00"), ("40.00", "2896.00", "0.00"), ("50.03", "2896.00", "0.02"),  # 0.024 -> 0.02
    ("50.07", "2896.00", "0.06"),  # 0.056 -> 0.06
])
def test_reimbursement_follows_the_policy_formula(expense, remaining, final):
    assert reimbursement(expense, "50.00", remaining)[0] == Decimal(final)


class ClaimDomain:
    """Read results for one registered claim on a policy with deductible 50.00."""

    def __init__(self, expense, remaining):
        self.results = {
            "get_claim": {"ok": True, "data": {"invoice": {"expense": expense}, "payment": None}},
            "get_policy": {"ok": True, "data": {"financial_terms": {"deductible": "50.00", "remaining_limit": remaining}}},
        }

    async def call(self, name, arguments):
        return self.results[name]


@pytest.mark.parametrize("paid, expense, remaining, blocked", [
    ("104.00", "180.00", "2896.00", None),
    ("144.00", "180.00", "2896.00", "AmountMismatch"),  # 80% of the expense, deductible forgotten.
    ("1500.00", "1925.00", "2896.00", None),
    ("1560.00", "2000.00", "2896.00", "AuthorizationRequired"),
    ("1560.00", "2000.00", "900.00", "AmountMismatch"),  # Capped by the remaining limit.
    ("900.00", "2000.00", "900.00", None),
    ("0.00", "40.00", "2896.00", "ZeroReimbursement"),
])
def test_payment_must_be_the_calculated_amount(paid, expense, remaining, blocked):
    arguments = {"policy_id": "POL-001", "claim_id": "CL-1", "amount": paid}
    result = asyncio.run(blocked_write(ClaimDomain(expense, remaining), "pay_claim", arguments))
    assert (result[0] if result else None) == blocked


def claim_call(invoice_text):
    """create_claim with the facts of a training invoice, as the model would normalize them."""
    facts = dict(line.split(": ", 1) for line in invoice_text.splitlines())
    invoice = {"provider_id": facts["Provider ID"], "invoice_number": facts["Invoice number"],
               "invoice_date": facts["Invoice date"], "service_date": facts["Service date"],
               "expense": facts["Amount"].removeprefix("EUR "), "currency": "EUR"}
    return ToolUseBlock(type="tool_use", id="toolu_create", name="create_claim", input={
        "policy_id": "POL-001", "patient_name": facts["Patient"], "service_category": "specialist_consultation",
        "invoice": invoice, "evidence_refs": ["request/invoice_text"]})


def created_claim_id(messages):
    """The claim ID returned to the scripted create_claim call."""
    result = next(r["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], list)
                  for r in m["content"] if r.get("tool_use_id") == "toolu_create")
    return json.loads(result)["data"]["claim_id"]


def test_duplicate_invoice_is_not_registered_again(start):
    ticket = json.loads((ROOT / "examples/training/T13.json").read_text())  # Same invoice as CL-H001.
    model = ScriptedModel([claim_call(ticket["invoice_text"])],
                          answer("closed_without_execution", "duplicate", request_category="reimbursement_request"))
    store, run_id, log, _ = start("T13", model)

    assert "CL-H001" in model.seen[1][-1]["content"][0]["content"]
    assert set(store.snapshot(run_id)["claims"]) == {"CL-H001", "CL-H002"}
    assert [e["data"]["error_type"] for e in log.record["events"] if e["kind"] == "error"] == ["DuplicateInvoice"]


def test_wrong_payment_is_blocked_and_the_calculated_one_is_paid(start):
    ticket = json.loads((ROOT / "examples/training/T08.json").read_text())

    def pay(amount, call_id):
        return lambda messages: [ToolUseBlock(type="tool_use", id=call_id, name="pay_claim", input={
            "policy_id": "POL-001", "claim_id": created_claim_id(messages), "amount": amount})]

    model = ScriptedModel([claim_call(ticket["invoice_text"])], pay("144.00", "toolu_pay1"), pay("104.00", "toolu_pay2"),
                          answer("completed", "paid", request_category="reimbursement_request"))
    store, run_id, log, _ = start("T08", model)

    assert "calculated 104.00" in model.seen[2][-1]["content"][0]["content"]
    claims = [c for c in store.snapshot(run_id)["claims"].values() if c["invoice"]["invoice_number"] == "INV-2026-008"]
    assert [(c["status"], c["payment"]["amount"]) for c in claims] == [("paid", "104.00")]
    assert store.snapshot(run_id)["policies"]["POL-001"]["financial_terms"]["paid_consumption"] == "208.00"
    assert [e["data"]["error_type"] for e in log.record["events"] if e["kind"] == "error"] == ["AmountMismatch"]


COVERAGE_PROPOSAL = {"operation": "update_coverage", "arguments": {
    "policy_id": "POL-001", "changes": {"annual_limit": "10000.00", "deductible": "500.00"}}}


@pytest.fixture
def awaiting_insurer(start):
    """T06 suspended on its combined coverage proposal."""
    model = ScriptedModel(answer("awaiting_authorization", "authorization_requested",
                                 request_category="coverage_change", proposal=COVERAGE_PROPOSAL))
    store, run_id, log, _ = start("T06", model)
    return store, run_id, log


def statuses(record):
    return [(e["data"]["agent_state"], e["data"]["request_outcome"]) for e in record["events"] if e["kind"] == "status"]


def test_insurer_approval_executes_exactly_the_saved_proposal(awaiting_insurer):
    store, run_id, log = awaiting_insurer
    event = decision("T06", "approve")
    final = asyncio.run(resume_request(event, admit(event), EvaluationLog.load(log.path)))  # No model involved.

    assert (final["agent_state"], final["request_outcome"]) == ("completed", "updated")
    terms = store.snapshot(run_id)["policies"]["POL-001"]["financial_terms"]
    assert (terms["annual_limit"], terms["deductible"]) == ("10000.00", "500.00")
    record = EvaluationLog.load(log.path).record
    writes = [e["data"]["arguments"] for e in record["events"]
              if e["kind"] == "tool_call" and e["data"]["name"] == "update_coverage"]
    assert writes == [{"run_id": run_id, **COVERAGE_PROPOSAL["arguments"]}]
    assert statuses(record) == [("awaiting_authorization", "authorization_requested"),
                                ("processing", "in_progress"), ("completed", "updated")]
    assert [e["data"]["event"]["event_type"] for e in record["events"] if e["kind"] == "input"] == \
        ["request", "insurer_decision"]
    assert record["human_rounds"] == {"clarification": 0, "authorization": 1}
    with pytest.raises(ResumeError, match="completed, not waiting"):
        admit(decision("T06", "approve", event_id="EV-T06-03"))  # No second round.


def test_insurer_rejection_executes_nothing(awaiting_insurer):
    store, run_id, log = awaiting_insurer
    event = decision("T06", "reject")
    final = asyncio.run(resume_request(event, admit(event), EvaluationLog.load(log.path)))

    assert (final["agent_state"], final["request_outcome"]) == ("closed_without_execution", "insurer_rejected")
    assert event["reason"] in final["reason"] and event["reason"] in final["message"]
    terms = store.snapshot(run_id)["policies"]["POL-001"]["financial_terms"]
    assert (terms["annual_limit"], terms["deductible"]) == ("3000.00", "50.00")
    record = EvaluationLog.load(log.path).record
    assert not [e for e in record["events"] if e["kind"] == "tool_call" and e["data"]["name"] == "update_coverage"]


@pytest.mark.parametrize("verdict, status, paid, consumption", [
    ("approve", "paid", "1560.00", "1664.00"), ("reject", "rejected", None, "104.00")])
def test_insurer_decision_on_a_reimbursement_completes_the_claim_record(start, verdict, status, paid, consumption):
    ticket = json.loads((ROOT / "examples/training/T11.json").read_text())  # 2000.00 -> 1560.00.

    def pending(messages):
        return [ToolUseBlock(type="tool_use", id="toolu_pending", name="record_claim_outcome", input={
            "policy_id": "POL-001", "claim_id": created_claim_id(messages), "outcome": "pending_authorization",
            "reason": "Final reimbursement 1560.00 is above 1500.00."})]

    def wait(messages):
        return answer("awaiting_authorization", "authorization_requested", request_category="reimbursement_request",
                      proposal={"operation": "pay_claim", "arguments": {
                          "policy_id": "POL-001", "claim_id": created_claim_id(messages), "amount": "1560.00"}})

    store, run_id, log, final = start("T11", ScriptedModel([claim_call(ticket["invoice_text"])], pending, wait))
    assert final["agent_state"] == "awaiting_authorization"
    claim_id = final["proposal"]["arguments"]["claim_id"]

    event = decision("T11", verdict)
    final = asyncio.run(resume_request(event, admit(event), EvaluationLog.load(log.path)))

    assert final["request_outcome"] == ("paid" if verdict == "approve" else "insurer_rejected")
    snapshot = store.snapshot(run_id)
    assert snapshot["claims"][claim_id]["status"] == status
    assert (snapshot["claims"][claim_id]["payment"] or {}).get("amount") == paid
    assert snapshot["policies"]["POL-001"]["financial_terms"]["paid_consumption"] == consumption


def test_a_proposal_the_harness_cannot_execute_goes_back_to_the_model(start):
    # 5000/200 needs no insurer: asking would waste the one authorization round.
    needless = {"operation": "update_coverage", "arguments": {
        "policy_id": "POL-001", "changes": {"annual_limit": "5000.00", "deductible": "200.00"}}}
    model = ScriptedModel(answer("awaiting_authorization", "authorization_requested",
                                 request_category="coverage_change", proposal=needless),
                          [coverage_write(needless["arguments"]["changes"])],
                          answer("completed", "updated", request_category="coverage_change"))
    store, run_id, log, final = start("T05", model)

    assert final["request_outcome"] == "updated"
    assert "needs no insurer authorization" in model.seen[1][-1]["content"]
    assert [e["data"]["error_type"] for e in log.record["events"] if e["kind"] == "error"] == ["InvalidProposal"]
    assert log.record["human_rounds"] == {"clarification": 0, "authorization": 0}
    assert store.snapshot(run_id)["policies"]["POL-001"]["financial_terms"]["annual_limit"] == "5000.00"
