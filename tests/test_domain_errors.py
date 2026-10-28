"""Tool errors must themselves satisfy the published result contracts."""

import pytest

from app.contracts import TOOLS, validator
from app.domain import DomainStore


@pytest.mark.parametrize("tool, arguments, field", [
    ("get_policy", {"run_id": "RUN-test"}, "policy_id"),
    ("get_policy", {"policy_id": "POL-001"}, "run_id"),
    ("get_policy", {}, "run_id"),
    ("update_contact_details", {"run_id": "RUN-test", "policy_id": "POL-001",
                                "changes": {"address": {"city": "Rome"}}}, "changes.address.street"),
    ("get_policy", {"run_id": "RUN-test", "policy_id": ""}, "policy_id"),
    ("get_policy", {"run_id": "RUN-test", "policy_id": "POL-001", "unexpected": True}, None),
    ("get_policy", [], None),
])
def test_invalid_inputs_return_valid_actionable_tool_errors(tmp_path, tool, arguments, field):
    result = DomainStore(tmp_path).call(tool, arguments)
    validator("tools.schema.json", TOOLS[tool][0] + "Result").validate(result)
    assert result["ok"] is False and result["error"]["retryable"] is True
    assert result["error"]["effect_applied"] is False
    assert result["error"].get("field") == field
    if field:
        assert field.rsplit(".", 1)[-1] in result["error"]["message"] or arguments.get(field) == ""
    assert not list(tmp_path.iterdir())
