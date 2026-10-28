"""Shared payload validation. Schemas describe contracts, not business gates."""

import copy
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    "get_policy": ("GetPolicy", "Read the current policy, contacts, insured people and financial terms."),
    "update_contact_details": ("UpdateContactDetails", "Update only requested email, phone or full postal address."),
    "update_coverage": ("UpdateCoverage", "Update annual limit and/or deductible atomically, using supported catalog values."),
    "terminate_policy": ("TerminatePolicy", "Simulate termination of the referenced policy."),
    "get_claim": ("GetClaim", "Read one existing medical reimbursement record; do not reopen it."),
    "list_claims": ("ListClaims", "Read the policy's complete initial and current-run reimbursement history."),
    "create_claim": ("CreateClaim", "Create a medical reimbursement record from actual invoice facts and evidence references."),
    "pay_claim": ("PayClaim", "Simulate a payment and return the actual result; never a real transfer."),
    "record_claim_outcome": ("RecordClaimOutcome", "Store an actual outcome on an existing reimbursement record; does not pay."),
}
ACCESS = {
    "administration": ("get_policy", "update_contact_details", "update_coverage", "terminate_policy"),
    "claims": ("get_policy", "get_claim", "list_claims", "create_claim", "pay_claim", "record_claim_outcome"),
}


@lru_cache
def document(filename):
    return json.loads((ROOT / "contracts" / filename).read_text(encoding="utf-8"))


def validator(filename, definition=None):
    schema = document(filename)
    if definition:
        schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"}
    return Draft202012Validator(schema, format_checker=FormatChecker())


def tool_schema(name, *, model_facing=False):
    """Expand local refs for provider APIs; bind run_id in the client, not model."""
    definitions = document("tools.schema.json")["$defs"]

    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {k: expand(v) for k, v in value.items()}
        return [expand(v) for v in value] if isinstance(value, list) else copy.deepcopy(value)

    schema = expand(definitions[TOOLS[name][0] + "Input"])
    if model_facing:
        schema["properties"].pop("run_id")
        schema["required"].remove("run_id")
    return schema


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
