"""Fictional domain storage and nine narrow operations. No authorization harness."""

import copy
import json
import os
import re
import threading
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.contracts import ROOT, TOOLS, atomic_json, validator

_LOCK = threading.RLock()


class DomainError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def error(code, message, **details):
    return {"ok": False, "error": {"code": code, "message": message, "retryable": False,
                                     "effect_applied": False, **details}}


class DomainStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get("EURAPCO_DATA_DIR", ROOT / ".runtime" / "domain"))

    def path(self, run_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("run_id must contain only letters, digits, underscores or hyphens (max 100).")
        return self.directory / f"{run_id}.json"

    def start_run(self, run_id=None):
        """Setup API, never exposed to the model. New initial ticket, new copy."""
        run_id = run_id or "RUN-" + uuid4().hex
        with _LOCK:
            path = self.path(run_id)
            if path.exists():
                raise ValueError("Run already exists. Resume it, or choose a new run ID for a fresh execution.")
            fixtures = json.loads((ROOT / "data" / "initial.json").read_text(encoding="utf-8"))
            atomic_json(path, fixtures)
        return run_id

    def snapshot(self, run_id):
        path = self.path(run_id)
        if not path.exists():
            raise DomainError("NOT_FOUND", "Run not found. Create an isolated run using the setup API.")
        return json.loads(path.read_text(encoding="utf-8"))

    def call(self, name, arguments):
        if name not in TOOLS:
            return error("INVALID_ARGUMENT", "Unknown provided tool.")
        prefix = TOOLS[name][0]
        failures = list(validator("tools.schema.json", prefix + "Input").iter_errors(arguments))
        if failures:
            failure = failures[0]
            path = list(failure.absolute_path)
            if failure.validator == "required":
                path.append(next(name for name in failure.validator_value if name not in failure.instance))
            details = {"retryable": True}
            if path:
                details["field"] = ".".join(map(str, path))
            if "enum" in failure.schema:
                details["allowed_values"] = failure.schema["enum"]
            return error("INVALID_ARGUMENT", failure.message, **details)
        try:
            with _LOCK:
                state = self.snapshot(arguments["run_id"])
                if arguments["policy_id"] not in state["policies"]:
                    raise DomainError("NOT_FOUND", "Policy not found in this run.")
                result = {"ok": True, "data": self._execute(name, state, arguments)}
                validator("tools.schema.json", prefix + "Result").validate(result)
                if name not in ("get_policy", "get_claim", "list_claims"):
                    atomic_json(self.path(arguments["run_id"]), state)
                return result
        except DomainError as exc:
            return error(exc.code, exc.message)
        except ValueError as exc:
            return error("INVALID_ARGUMENT", str(exc), retryable=True)

    @staticmethod
    def _claim(state, args):
        claim = state["claims"].get(args["claim_id"])
        if claim is None:
            raise DomainError("NOT_FOUND", "Medical reimbursement record not found.")
        if claim["policy_id"] != args["policy_id"]:
            raise DomainError("REFERENCE_MISMATCH", "The reimbursement record belongs to another policy.")
        return claim

    def _execute(self, name, state, a):
        policy = state["policies"][a["policy_id"]]
        terms = policy["financial_terms"]
        if name == "get_policy":
            return copy.deepcopy(policy)
        if name in ("update_contact_details", "update_coverage"):
            target = policy["contacts"] if name == "update_contact_details" else terms
            before = copy.deepcopy(target)
            target.update(copy.deepcopy(a["changes"]))
            if name == "update_coverage":
                target["remaining_limit"] = f"{max(Decimal('0'), Decimal(target['annual_limit']) - Decimal(target['paid_consumption'])):.2f}"
            fields = [field for field in a["changes"] if before[field] != target[field]]
            return {"policy_id": a["policy_id"], "changed": bool(fields), "changed_fields": fields,
                    "before": before, "after": copy.deepcopy(target)}
        if name == "terminate_policy":
            before = policy["status"]
            if before not in ("active", "terminated"):
                raise DomainError("INVALID_STATE", "Only active policies can transition to terminated.")
            policy["status"] = "terminated"
            return {"policy_id": a["policy_id"], "changed": before != "terminated",
                    "before_status": before, "after_status": "terminated"}
        if name == "get_claim":
            return copy.deepcopy(self._claim(state, a))
        if name == "list_claims":
            return {"policy_id": a["policy_id"], "claims": [copy.deepcopy(c) for c in state["claims"].values()
                                                          if c["policy_id"] == a["policy_id"]]}
        if name == "create_claim":
            if a["invoice"]["invoice_date"] < a["invoice"]["service_date"]:
                raise DomainError("INVALID_ARGUMENT", "Invoice date cannot precede service date in this kit.")
            claim_id = "CL-" + uuid4().hex[:12]
            claim = {"claim_id": claim_id, **{k: copy.deepcopy(a[k]) for k in
                     ("policy_id", "patient_name", "service_category", "invoice", "evidence_refs")},
                     "status": "registered", "reason": None, "payment": None}
            state["claims"][claim_id] = claim
            return copy.deepcopy(claim)
        claim = self._claim(state, a)
        if name == "pay_claim":
            if claim["payment"]:
                if claim["payment"]["amount"] != a["amount"]:
                    raise DomainError("INVALID_STATE", "This record already has a payment for a different amount.")
                return {**claim["payment"], "created": False}
            if claim["status"] not in ("registered", "pending_authorization", "payment_failed"):
                raise DomainError("INVALID_STATE", "This reimbursement record is already closed without payment.")
            payment = {"payment_id": "PAY-" + uuid4().hex[:12], "claim_id": claim["claim_id"],
                       "policy_id": a["policy_id"], "amount": a["amount"], "currency": "EUR"}
            claim.update(payment=payment, status="paid", reason="Simulated payment completed.")
            terms["paid_consumption"] = f"{Decimal(terms['paid_consumption']) + Decimal(a['amount']):.2f}"
            terms["remaining_limit"] = f"{max(Decimal('0'), Decimal(terms['annual_limit']) - Decimal(terms['paid_consumption'])):.2f}"
            return {**payment, "created": True}
        if name == "record_claim_outcome":
            if (a["outcome"] == "paid") != bool(claim["payment"]):
                raise DomainError("INVALID_STATE", "The outcome must agree with actual payment effects.")
            claim.update(status=a["outcome"], reason=a["reason"])
            return copy.deepcopy(claim)
        raise AssertionError(name)
