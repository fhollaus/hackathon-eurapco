"""Supplied cumulative logger. Observe execution; never infer successful effects."""

import copy
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePath

from jsonschema import ValidationError

from app.contracts import ROOT, atomic_json, document, validator

CLASSIFIED_AGENT_STATES = {"awaiting_clarification", "awaiting_authorization", "completed", "closed_without_execution"}


class EvaluationLogError(ValueError):
    """A rejected update, with a safe diagnostic already saved when storage permits."""


class InvalidEvaluationLog(EvaluationLogError):
    def __init__(self, field, problem):
        self.field = field
        super().__init__(f"Evaluation logging failed at {field}: {problem}. "
                         "Earlier valid events are preserved in the failed record.")


class IncompleteEvaluationLog(EvaluationLogError):
    def __init__(self, fields):
        self.fields = fields
        super().__init__("Evaluation logging failed: missing or invalid " + ", ".join(fields)
                         + ". Earlier events are preserved in the failed record.")


def now():
    return datetime.now(timezone.utc).isoformat()


def json_value(value, field="record", ancestors=None):
    """Copy supported Python values to JSON; reject ambiguous/lossy conversions."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidEvaluationLog(field, "non-finite numbers are not JSON values")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidEvaluationLog(field, "non-finite Decimal is not supported")
        return str(value)  # Never round or pass through float.
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise InvalidEvaluationLog(field, "datetime requires an explicit timezone")
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    if not isinstance(value, (dict, list, tuple)):
        raise InvalidEvaluationLog(field, f"unsupported value type {type(value).__name__}")
    ancestors = set() if ancestors is None else ancestors
    if id(value) in ancestors:
        raise InvalidEvaluationLog(field, "circular references are not supported")
    ancestors.add(id(value))
    try:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise InvalidEvaluationLog(field, "JSON object keys must be strings")
                result[key] = json_value(item, f"{field}.{key}", ancestors)
            return result
        return [json_value(item, f"{field}[{index}]", ancestors) for index, item in enumerate(value)]
    finally:
        ancestors.remove(id(value))


class EvaluationLog:
    def __init__(self, request, run_id, team_id, directory=None):
        self.path = Path(directory or ROOT / "logs") / f"{run_id}.json"
        self.record = {
            "schema_version": "1", "team_id": team_id, "run_id": run_id,
            "ticket_id": request["ticket_id"], "policy_id": request["policy_id"],
            "handling_agent": None, "request_category": None, "agent_state": "processing",
            "request_outcome": "in_progress", "reason": "", "message": "",
            "human_rounds": {"clarification": 0, "authorization": 0},
            "events": [], "domain_snapshot": None,
        }
        self._last_saved = None
        self.input(request)

    @classmethod
    def load(cls, path):
        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance.record = json.loads(instance.path.read_text(encoding="utf-8"))
        validator("evaluation.schema.json").validate(instance.record)
        instance._last_saved = copy.deepcopy(instance.record)
        return instance

    def save(self):
        """Validate a candidate before replacing either disk or in-memory evidence."""
        self._commit(self.record)

    def _write(self, candidate):
        validator("evaluation.schema.json").validate(candidate)
        # Check the actual JSON encoding too, before touching the saved record.
        json.dumps(candidate, ensure_ascii=False, allow_nan=False).encode("utf-8")
        atomic_json(self.path, candidate)
        self.record = candidate
        self._last_saved = copy.deepcopy(candidate)

    def _commit(self, candidate):
        # Also recover safely when an integration mutated the public record before save().
        if self._last_saved is not None:
            self.record = copy.deepcopy(self._last_saved)
        try:
            candidate = json_value(candidate)
            failure = self._classification_failure(candidate)
            self._write(candidate)
        except (InvalidEvaluationLog, ValidationError, UnicodeEncodeError, RecursionError) as exc:
            if isinstance(exc, ValidationError):
                field = ".".join(map(str, exc.absolute_path)) or "record"
                failure = InvalidEvaluationLog(field, f"does not match the evaluation schema ({exc.validator})")
            elif isinstance(exc, InvalidEvaluationLog):
                failure = exc
            else:
                failure = InvalidEvaluationLog("record", "text encoding or nesting cannot be serialized")
            if self._last_saved is not None:
                diagnostic = copy.deepcopy(self._last_saved)
                diagnostic.update(agent_state="failed", request_outcome="technical_failure",
                                  reason=str(failure), message=str(failure))
                self._append(diagnostic, "error", error_type=type(failure).__name__,
                             field=failure.field, message=str(failure))
                self._write(diagnostic)
            if failure is exc:
                raise
            raise failure from exc
        if failure:
            raise failure

    @staticmethod
    def _classification_failure(candidate):
        request_categories = document("vocabulary.json")["request_categories"]
        allowed = {"handling_agent": list(request_categories),
                   "request_category": [label for labels in request_categories.values() for label in labels]}
        required = isinstance(candidate.get("agent_state"), str) and candidate["agent_state"] in CLASSIFIED_AGENT_STATES
        invalid = [field for field, values in allowed.items()
                   if (candidate.get(field) is None and required)
                   or (candidate.get(field) is not None and candidate[field] not in values)]
        if invalid:
            attempted_agent_state = candidate.get("agent_state")
            attempted_request_outcome = candidate.get("request_outcome")
            failure = IncompleteEvaluationLog(invalid)
            for field in invalid:
                candidate[field] = None  # Unavailable, never a fabricated classification.
            candidate.update(agent_state="failed", request_outcome="technical_failure",
                             reason=str(failure), message=str(failure))
            EvaluationLog._append(candidate, "error", error_type=type(failure).__name__, fields=invalid,
                                  attempted_agent_state=attempted_agent_state, attempted_request_outcome=attempted_request_outcome)
            return failure
        return None

    @staticmethod
    def _append(candidate, kind, **data):
        candidate["events"] = [*candidate["events"], {
            "sequence": len(candidate["events"]) + 1, "timestamp": now(), "kind": kind, "data": data,
        }]

    def event(self, kind, **data):
        candidate = dict(self.record)
        self._append(candidate, kind, **data)
        self._commit(candidate)

    def input(self, event):
        validator("events.schema.json").validate(event)
        if event["ticket_id"] != self.record["ticket_id"]:
            raise ValueError("Input event belongs to a different ticket.")
        self.event("input", event=event)

    def route(self, agent):
        candidate = {**self.record, "handling_agent": agent}
        self._append(candidate, "routing", handling_agent=agent)
        self._commit(candidate)

    def status(self, *, agent_state, request_outcome, reason, message="", request_category=None):
        """Teams call this after an observed transition; does not execute it."""
        candidate = {**self.record, "agent_state": agent_state, "request_outcome": request_outcome,
                     "reason": reason, "message": message}
        if request_category is not None:
            candidate["request_category"] = request_category
        self._append(candidate, "status", agent_state=agent_state, request_outcome=request_outcome, reason=reason)
        self._commit(candidate)

    def human_requested(self, interaction, payload):
        if interaction not in ("clarification", "authorization"):
            raise ValueError("Unknown human interaction.")
        # Measurement, deliberately not a supplied round-limit gate.
        candidate = {**self.record, "human_rounds": dict(self.record["human_rounds"])}
        candidate["human_rounds"][interaction] += 1
        self._append(candidate, "model_response", interaction=interaction, payload=payload)
        self._commit(candidate)

    def snapshot(self, domain):
        self._commit({**self.record, "domain_snapshot": domain})
