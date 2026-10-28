"""Agent loop with persisted workflow state, so a waiting request resumes on its clarification reply or
insurer decision. Writes that need the insurer are blocked in the loop: the model can only propose them,
and only the harness executes an approved proposal."""

import json
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from anthropic import AsyncAnthropic

from agents.administration.agent import system_prompt as admin_prompt
from agents.claims.agent import system_prompt as claims_prompt
from app.contracts import ROOT, TOOLS, atomic_json, document, validator
from app.domain import error
from app.evaluation import EvaluationLog
from app.mcp_client import DomainClient
from app.router import route
from app.skills import load_skills

# The only event type that may resume each waiting state.
RESUMED_BY = {"awaiting_clarification": "clarification_reply", "awaiting_authorization": "insurer_decision"}
# Final values strictly above these need insurer authorization; equality is autonomous (docs/policy.md).
COVERAGE_THRESHOLDS = {"annual_limit": Decimal("5000.00"), "deductible": Decimal("200.00")}
REIMBURSEMENT_THRESHOLD = Decimal("1500.00")
# The writes an insurer approval can release, per agent, and how the policyholder reads them.
AUTHORIZABLE = {"administration": ("update_coverage", "terminate_policy"), "claims": ("pay_claim",)}
DESCRIBED = {"update_coverage": "coverage change", "terminate_policy": "policy termination",
             "pay_claim": "reimbursement"}
# What the model must do instead of a write the harness blocked.
INSTEAD = {
    "UnsupportedDecrease": "Only increases are supported and nothing was changed: close with "
                           "agent_state=closed_without_execution and request_outcome=invalid_input.",
    "DuplicateInvoice": "Nothing was created: close with agent_state=closed_without_execution and "
                        "request_outcome=duplicate, reporting the existing claim_id.",
    "ZeroReimbursement": "Nothing was paid: record the outcome on the claim and close with "
                         "request_outcome=zero_reimbursement.",
    "AmountMismatch": "Nothing was paid: use exactly the calculated amount (above 1500.00 it needs "
                      "insurer authorization).",
    "AuthorizationRequired": "Return agent_state=awaiting_authorization with the exact proposal "
                             "(operation and arguments), or report that nothing needs to change.",
}


class ResumeError(ValueError):
    """A reply that cannot resume its run. Neither workflow nor domain changed; a known run logs the refusal."""


def workflow_dir():
    return Path(os.environ.get("EURAPCO_WORKFLOW_DIR", ROOT / ".runtime" / "workflows"))


def save_workflow(workflow):
    workflow["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(workflow_dir() / f"{workflow['run_id']}.json", workflow)


@contextmanager
def marks_failure(workflow):
    """A crash leaves the workflow failed, so no later reply can resume a half-handled request."""
    try:
        yield
    except Exception:
        workflow["agent_state"] = "failed"
        save_workflow(workflow)
        raise


@asynccontextmanager
async def model_provider(provider):
    if provider is not None:
        yield provider
        return
    owned = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0, timeout=45)
    try:
        yield owned
    finally:
        await owned.close()


def system_prompt(agent):
    prompt = admin_prompt() if agent == "administration" else claims_prompt()
    prompt += "\nReturn ONLY one JSON object matching this response schema when done or waiting:\n"
    prompt += json.dumps(document("agent-response.schema.json"))
    # skills = load_skills()
    # prompt += "\n\n"
    # for skill in skills:
    #     prompt += f"\nSkill: {skill['name']}\nDescription: {skill['description']}\n"
    # No prewritten skills or solved skill integration in the initial agent.
    return prompt


async def run_request(request, run_id, log, provider=None):
    validator("events.schema.json").validate(request)
    if request["event_type"] != "request":
        raise ValueError("A reply resumes its waiting run: use admit() and resume_request().")
    workflow = {
        "run_id": run_id, "ticket_id": request["ticket_id"], "agent": None, "log_path": str(log.path),
        "request": request, "messages": [{"role": "user", "content": json.dumps(request)}],
        "agent_state": "processing", "request_category": None, "question": None, "proposal": None,
        "human_rounds": {"clarification": 0, "authorization": 0}, "event_ids": [request["event_id"]],
    }
    save_workflow(workflow)
    with marks_failure(workflow):
        workflow["agent"] = route(request["text"])
        log.route(workflow["agent"])
        return await advance(workflow, log, provider)


def admit(event, run_id=None):
    """Correlate a reply with the suspended workflow of its ticket and check it may resume it."""
    validator("events.schema.json").validate(event)
    if event["event_type"] == "request":
        raise ResumeError("An initial request starts a new run; it cannot resume one.")
    paths = [workflow_dir() / f"{run_id}.json"] if run_id else workflow_dir().glob("*.json")
    candidates = [workflow for workflow in (json.loads(p.read_text(encoding="utf-8")) for p in paths if p.exists())
                  if workflow["ticket_id"] == event["ticket_id"]]
    if not candidates:
        raise ResumeError(f"No workflow found for ticket {event['ticket_id']}"
                          + (f" in run {run_id}." if run_id else "."))
    waiting = [workflow for workflow in candidates if workflow["agent_state"] in RESUMED_BY]
    if not waiting:
        latest = max(candidates, key=lambda workflow: workflow["updated_at"])
        refuse(event, latest, f"Run {latest['run_id']} of ticket {event['ticket_id']} is {latest['agent_state']}, "
                              "not waiting; a reply cannot reopen or restart it.")
    workflow = max(waiting, key=lambda workflow: workflow["updated_at"])
    expected = RESUMED_BY[workflow["agent_state"]]
    if event["event_type"] != expected:
        refuse(event, workflow, f"Run {workflow['run_id']} is {workflow['agent_state']} and accepts only "
                                f"{expected} events, not {event['event_type']}.")
    if event["event_id"] in workflow["event_ids"]:
        refuse(event, workflow, f"Event {event['event_id']} was already processed in run {workflow['run_id']}.")
    return workflow


def refuse(event, workflow, reason):
    """Record the refused reply in its run's log, as evidence, without any state transition."""
    EvaluationLog.load(workflow["log_path"]).event("error", error_type="ReplyRejected", reason=reason, event=event)
    raise ResumeError(reason)


async def resume_request(event, workflow, log, provider=None):
    """Continue a workflow returned by admit() in the same run, conversation and evaluation log."""
    workflow["agent_state"] = "processing"
    workflow["event_ids"].append(event["event_id"])
    save_workflow(workflow)  # Claim it before any work: a second delivery is now rejected by admit().
    with marks_failure(workflow):
        log.input(event)
        if event["event_type"] == "insurer_decision":
            log.status(agent_state="processing", request_outcome="in_progress",
                       reason=f"Insurer decision received ({event['decision']}); applying it to the saved proposal.")
            return await decide(event, workflow, log)
        log.status(agent_state="processing", request_outcome="in_progress",
                   reason="Clarification reply received; resuming the waiting request.")
        workflow["messages"].append({"role": "user", "content": (
            "The policyholder's clarification reply to your question. It is the only clarification "
            "for this request:\n" + json.dumps(event) + "\n"
            "The facts of the original request are fixed: use the reply only for the information that "
            "was missing. Do not ask another question. If required information is still missing, close "
            "with agent_state=closed_without_execution and request_outcome=insufficient_information. "
            "If the operation now needs insurer authorization, request it."
        )})
        return await advance(workflow, log, provider)


async def advance(workflow, log, provider):
    """Run the agent on the workflow's conversation until it finishes or waits for a person."""
    agent = workflow["agent"]
    async with model_provider(provider) as client, DomainClient(workflow["run_id"], agent, log) as domain:
        final = await agent_turns(client, domain, system_prompt(agent), workflow["messages"], agent, log,
                                  workflow["request"]["policy_id"])
    return finish(workflow, log, final)


async def decide(event, workflow, log):
    """Apply the insurer decision without the model: approval runs exactly the saved proposal, once."""
    proposal = workflow["proposal"]
    operation, arguments = proposal.get("operation"), proposal.get("arguments") or {}
    async with DomainClient(workflow["run_id"], workflow["agent"], log) as domain:
        if event["decision"] == "reject":
            if operation == "pay_claim":
                await domain.call("record_claim_outcome", {
                    "policy_id": arguments["policy_id"], "claim_id": arguments["claim_id"],
                    "outcome": "rejected", "reason": "Insurer rejected: " + event["reason"]})
            return finish(workflow, log, decided(
                workflow, "closed_without_execution", "insurer_rejected",
                f"The insurer rejected the proposal: {event['reason']}",
                f"The insurer did not approve the {DESCRIBED.get(operation, 'request')}. Reason: {event['reason']} "
                f"Nothing was {'paid' if operation == 'pay_claim' else 'changed'}."))
        # An approval cannot make an unsupported write allowed: check the proposal again as it is executed.
        problem = await proposal_problem(domain, workflow["agent"], workflow["request"]["policy_id"], proposal)
        if problem:
            return finish(workflow, log, decided(
                workflow, "failed", "technical_failure", f"The approved proposal cannot be executed as saved: {problem}",
                "The insurer approved your request, but it could not be carried out as proposed. Nothing was "
                "changed; the case needs a manual check."))
        result = await domain.call(operation, arguments)
        if result["ok"] and operation == "pay_claim":
            await domain.call("record_claim_outcome", {
                "policy_id": arguments["policy_id"], "claim_id": arguments["claim_id"],
                "outcome": "paid", "reason": f"Paid {arguments['amount']} after insurer approval."})
    return finish(workflow, log, executed(workflow, operation, result))


def decided(workflow, agent_state, request_outcome, reason, message):
    """A harness-made final response, in the agent response shape, for the saved proposal."""
    return {"request_category": workflow["request_category"], "agent_state": agent_state,
            "request_outcome": request_outcome, "reason": reason, "message": message,
            "question": None, "proposal": workflow["proposal"]}


def executed(workflow, operation, result):
    """The outcome of an approved proposal, reported from the actual tool result only."""
    if not result["ok"]:
        failure = result["error"]
        return decided(workflow, "failed", "technical_failure",
                       f"Approved {operation} failed: {failure['code']}: {failure['message']}",
                       "The insurer approved your request, but carrying it out failed, so nothing is reported as "
                       "done. The case needs a manual check.")
    data = result["data"]
    if operation == "pay_claim":
        return decided(workflow, "completed", "paid",
                       f"Insurer approved; payment {data['payment_id']} of {data['amount']} for claim {data['claim_id']}.",
                       f"The insurer approved your reimbursement: EUR {data['amount']} was paid for claim "
                       f"{data['claim_id']} (payment {data['payment_id']}).")
    if not data["changed"]:
        return decided(workflow, "completed", "no_change", f"Insurer approved; {operation} reported no change.",
                       "The insurer approved your request. Your policy already had these values, so nothing changed.")
    if operation == "terminate_policy":
        return decided(workflow, "completed", "terminated",
                       f"Insurer approved; policy {data['policy_id']} changed from {data['before_status']} to terminated.",
                       f"The insurer approved the termination: policy {data['policy_id']} is now terminated.")
    changes = [(field.replace("_", " "), data["before"][field], data["after"][field]) for field in data["changed_fields"]]
    return decided(workflow, "completed", "updated",
                   "Insurer approved; " + "; ".join(f"{f} {old} -> {new}" for f, old, new in changes) + ".",
                   "The insurer approved your coverage change: "
                   + " and ".join(f"{f} from EUR {old} to EUR {new}" for f, old, new in changes) + ".")


def finish(workflow, log, final):
    """Enforce the human-round rules, then record the observed transition in the log and the workflow."""
    if workflow["request_category"]:
        # A reply supplies missing facts; it does not change the operation already classified.
        final["request_category"] = workflow["request_category"]
    if final["agent_state"] == "awaiting_clarification" and workflow["human_rounds"]["clarification"]:
        final.update(
            agent_state="closed_without_execution", request_outcome="insufficient_information",
            reason="Required information still missing after the one clarification; the agent wanted to ask: "
                   + final["question"],
            message="Thank you for your reply. Some required information is still missing and we can ask only "
                    "once, so your request was closed without carrying out the requested operation.",
            question=None,
        )
    if final["agent_state"] == "awaiting_clarification":
        log.human_requested("clarification", {"question": final["question"]})
        workflow["human_rounds"]["clarification"] += 1
    elif final["agent_state"] == "awaiting_authorization":
        log.human_requested("authorization", final["proposal"])
        workflow["human_rounds"]["authorization"] += 1
    log.status(agent_state=final["agent_state"], request_outcome=final["request_outcome"],
               reason=final["reason"], message=final["message"], request_category=final["request_category"])
    workflow.update(agent_state=final["agent_state"], request_category=final["request_category"],
                    question=final["question"], proposal=final["proposal"])
    save_workflow(workflow)
    return final

def system_prompt(agent):
    prompt = admin_prompt() if agent == "administration" else claims_prompt()
    for name, text in load_skills(agent):
        prompt += f"\n\n## Skill: {name}\n{text}"
    prompt += "\nReturn ONLY one JSON object matching this response schema when done or waiting:\n"
    prompt += json.dumps(document("agent-response.schema.json"))
    return prompt

async def agent_turns(provider, domain, prompt, messages, agent, log, policy_id):
    """Model/tool loop. Appends every turn to messages, so a resumed run keeps its full context."""
    for _ in range(12):
        response = await provider.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"),
            max_tokens=1800,
            system=system_prompt(agent),
            messages=messages,
            tools=domain.model_tools(),
        )
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in response.content]})

        calls = [b for b in response.content if b.type == "tool_use"]
        if calls:
            results = []
            for call in calls:
                blocked = await blocked_write(domain, call.name, call.input)
                if blocked:
                    # Never reaches the domain. Not INVALID_ARGUMENT, so it is not retried as a format error.
                    error_type, reason = blocked
                    log.event("error", error_type=error_type, tool=call.name, arguments=call.input, reason=reason)
                    result = error("INVALID_STATE", f"Blocked by the harness: {reason} Do not call {call.name}. "
                                   + INSTEAD[error_type])
                else:
                    result = await domain.call(call.name, call.input)
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": json.dumps(result), "is_error": not result["ok"]})
            messages.append({"role": "user", "content": results})
            continue
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        final = json.loads(text)
        validator("agent-response.schema.json").validate(final)
        if final["request_category"] not in document("vocabulary.json")["request_categories"][agent]:
            raise ValueError("Agent returned a category from the other domain.")
        log.event("model_response", response=final)
        if final["agent_state"] == "awaiting_authorization":
            # Only a proposal the harness can execute exactly is sent to the insurer.
            problem = await proposal_problem(domain, agent, policy_id, final["proposal"])
            if problem:
                log.event("error", error_type="InvalidProposal", reason=problem)
                messages.append({"role": "user", "content": "The harness cannot send this proposal to the "
                                 f"insurer: {problem} Return a corrected response."})
                continue
        return final
    raise RuntimeError("Baseline turn budget exhausted. Stop and hand over; do not repeat successful writes.")


async def proposal_problem(domain, agent, policy_id, proposal):
    """Why an approval of this proposal could not be executed exactly as saved, or None."""
    operation, arguments = proposal.get("operation"), proposal.get("arguments")
    if operation not in AUTHORIZABLE[agent] or not isinstance(arguments, dict):
        return ("The proposal needs operation (one of " + ", ".join(AUTHORIZABLE[agent])
                + ") and arguments (its exact tool input, without run_id).")
    if arguments.get("policy_id") != policy_id or "run_id" in arguments:
        return f"The arguments must be for policy {policy_id} and must not contain run_id."
    invalid = next(validator("tools.schema.json", TOOLS[operation][0] + "Input")
                   .iter_errors({**arguments, "run_id": domain.run_id}), None)
    if invalid:
        return f"The arguments are not a valid {operation} input: {invalid.message}."
    blocked = await blocked_write(domain, operation, arguments)
    if blocked is None:
        return f"{operation} with these arguments needs no insurer authorization: run it yourself."
    if blocked[0] != "AuthorizationRequired":
        return f"{blocked[1]} {INSTEAD[blocked[0]]}"
    return None


async def blocked_write(domain, name, arguments):
    """(error_type, reason) when the model may not run this write itself, else None."""
    # Checked before authorization: an unsupported or wrong write must close or be corrected,
    # not become a proposal to the insurer.
    if name == "update_coverage":
        decreases = await coverage_decreases(domain, arguments)
        if decreases:
            return "UnsupportedDecrease", "Coverage can only be increased: " + " and ".join(decreases) + "."
    if name == "create_claim":
        existing = await duplicate_claim(domain, arguments)
        if existing:
            return "DuplicateInvoice", (f"This invoice already belongs to claim {existing['claim_id']} "
                                        f"(status {existing['status']}).")
    if name == "pay_claim":
        calculated = await calculated_reimbursement(domain, arguments)
        if calculated:
            final, working = calculated
            if final == 0:
                return "ZeroReimbursement", f"The calculated reimbursement is 0.00 ({working})."
            requested = amount(arguments.get("amount"))
            if requested is not None and requested != final:
                return "AmountMismatch", (f"Amount {arguments['amount']} differs from the calculated "
                                          f"{final:.2f} ({working}).")
    reason = authorization_required(name, arguments)
    return ("AuthorizationRequired", reason) if reason else None


async def duplicate_claim(domain, arguments):
    """An existing claim with the same provider ID + invoice number + invoice-date year (docs/policy.md)."""
    invoice = arguments.get("invoice")
    if not (isinstance(invoice, dict) and isinstance(arguments.get("policy_id"), str)):
        return None
    history = await domain.call("list_claims", {"policy_id": arguments["policy_id"]})
    if not history["ok"]:
        return None  # The write itself then reports the actual error.
    return next((claim for claim in history["data"]["claims"]
                 if duplicate_key(claim["invoice"]) == duplicate_key(invoice)), None)


def duplicate_key(invoice):
    return invoice.get("provider_id"), invoice.get("invoice_number"), str(invoice.get("invoice_date"))[:4]


async def calculated_reimbursement(domain, arguments):
    """(final, working) for the claim from its expense and the policy's current terms, or None."""
    if not all(isinstance(arguments.get(field), str) for field in ("policy_id", "claim_id")):
        return None
    claim = await domain.call("get_claim", {"policy_id": arguments["policy_id"], "claim_id": arguments["claim_id"]})
    if not claim["ok"] or claim["data"]["payment"]:
        return None  # The write itself reports the error, or returns the existing payment without paying again.
    policy = await domain.call("get_policy", {"policy_id": arguments["policy_id"]})
    if not policy["ok"]:
        return None
    terms = policy["data"]["financial_terms"]
    return reimbursement(claim["data"]["invoice"]["expense"], terms["deductible"], terms["remaining_limit"])


def reimbursement(expense, deductible, remaining_limit):
    """docs/policy.md: 80% of the expense above the deductible, half-up to cents, capped by the remaining limit."""
    eligible = max(Decimal("0.00"), Decimal(expense) - Decimal(deductible))
    theoretical = (eligible * Decimal("0.80")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    final = min(theoretical, Decimal(remaining_limit))
    return final, (f"expense {expense} - deductible {deductible} = {eligible:.2f}; 80% = {theoretical}; "
                   f"remaining limit {remaining_limit}; final {final:.2f}")


async def coverage_decreases(domain, arguments):
    """Requested values below the current ones, read fresh from the policy; equal values are a no-op."""
    changes = arguments.get("changes")
    if not (isinstance(changes, dict) and changes.keys() & COVERAGE_THRESHOLDS.keys()
            and isinstance(arguments.get("policy_id"), str)):
        return []
    policy = await domain.call("get_policy", {"policy_id": arguments["policy_id"]})
    if not policy["ok"]:
        return []  # The write itself then reports the actual error.
    terms = policy["data"]["financial_terms"]
    return [f"{field} {changes[field]} is below the current {terms[field]}" for field in COVERAGE_THRESHOLDS
            if field in changes and exceeds(terms[field], changes[field])]


def authorization_required(name, arguments):
    """Why the model may not run this write itself, or None. An approved proposal is run by the harness."""
    if name == "terminate_policy":
        return "Policy termination always needs insurer authorization."
    if name == "update_coverage" and isinstance(arguments.get("changes"), dict):
        changes = arguments["changes"]
        over = [f"final {field} {changes[field]} is above {limit}" for field, limit in COVERAGE_THRESHOLDS.items()
                if field in changes and exceeds(changes[field], limit)]
        if over:
            return "Coverage change needs insurer authorization: " + " and ".join(over) + "."
    if name == "pay_claim" and exceeds(arguments.get("amount"), REIMBURSEMENT_THRESHOLD):
        return f"Reimbursement {arguments['amount']} is above {REIMBURSEMENT_THRESHOLD} and needs insurer authorization."
    return None


def exceeds(value, limit):
    try:
        return Decimal(str(value)) > Decimal(str(limit))
    except ArithmeticError:
        return False  # Not an amount: the tool's own validation rejects it as a format error.


def amount(value):
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None  # Not an amount: left to the tool's own format check.
