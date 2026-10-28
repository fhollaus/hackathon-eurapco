# Provided domain tools

Nine MCP tools are provided. This catalog documents their real payload contracts;
business permission checks belong to your harness. `run_id` is bound by the client
and omitted from model-facing schemas. `policy_id` comes from the initial request.

| Tool | Agent access | Input / complete result definitions |
|---|---|---|
| `get_policy` | administration, claims | `GetPolicyInput` / `GetPolicyResult` |
| `update_contact_details` | administration | `UpdateContactDetailsInput` / `UpdateContactDetailsResult` |
| `update_coverage` | administration | `UpdateCoverageInput` / `UpdateCoverageResult` |
| `terminate_policy` | administration | `TerminatePolicyInput` / `TerminatePolicyResult` |
| `get_claim` | claims | `GetClaimInput` / `GetClaimResult` |
| `list_claims` | claims | `ListClaimsInput` / `ListClaimsResult` |
| `create_claim` | claims | `CreateClaimInput` / `CreateClaimResult` |
| `pay_claim` | claims | `PayClaimInput` / `PayClaimResult` |
| `record_claim_outcome` | claims | `RecordClaimOutcomeInput` / `RecordClaimOutcomeResult` |

All definitions are in [tools.schema.json](../contracts/tools.schema.json). Select
the named `$defs` entry; the unselected root deliberately rejects arbitrary payloads.
Objects reject undeclared fields. Result: `{ "ok": true, "data": ... }` or
`{ "ok": false, "error": { "code": ..., "message": ..., "retryable": ...,
"effect_applied": false } }`. Errors may include `field` and `allowed_values`.
For a missing required property, `field` identifies that property, including its
nested path (for example, `changes.address.street`). Record-level errors without
a specific field omit `field`; it is never an empty string.

Codes: INVALID_ARGUMENT, NOT_FOUND, REFERENCE_MISMATCH, INVALID_STATE, OPERATION_FAILED.
Correct representation only when allowed; missing references/facts cannot be invented.

## Fixing invalid tool calls

A call can fail only because its format is wrong: `INVALID_ARGUMENT`, with `field` and
`allowed_values` saying what to correct. In that case your system may put the structured
error back in front of the model and let it correct the call.

- **At most three correction attempts** for the same error. Stop as soon as one works.
- **Log** the failing call and every attempt. The supplied client already records each
  tool call and its actual result.
- **If the third attempt also fails:** stop, and hand over to a human with a short report:
  a summary of the request, what was tried, the attempts and their results, why you
  stopped and what you suggest next. Record the outcome `handover_required`.
- **Only for format errors.** A business rejection (`NOT_FOUND`, `REFERENCE_MISMATCH`,
  `INVALID_STATE`) is not retried, a correction never changes the facts or the action that
  were requested, and it is not another clarification or insurer authorization.
- **Never repeat a write that already succeeded**, even if a later step failed.

The prompts already describe this rule to the model. Counting the attempts per error and
stopping at three is done by your harness.

## Effects and boundaries

- Contact and coverage updates validate every supplied field before writing any.
  Unspecified fields remain unchanged; identical input reports changed=false.
- Coverage values must belong to the catalog; tools do not enforce the complete
  increase-only/active-policy/authorization workflow for you.
- Termination simulates active→terminated; repeated termination is no change.
- create_claim accepts actual invoice facts and evidence references, generates a
  record ID and starts registered. Invoice date must be on or after service date
  in this kit; the tool checks this cross-field condition. Teams decide registration
  timing/lifecycle.
- list_claims returns history facts, not a solved duplicate decision.
- pay_claim returns an actual simulated payment reference and updates consumption
  once. Same claim/amount returns the prior payment with created=false; conflicting
  amount fails. It is not a real financial transfer or a solved authorization gate.
- record_claim_outcome cannot invent or erase a successful payment.
- The client scopes tools to the agent and binds run_id; teams govern business
  permissions/proposals. No document-reading, approval or logging MCP tool exists.

The calculation formula is in [policy.md](policy.md). Additional tools and their
integration are team choices. Router, human responses, state and logging are application
components, not automatically more MCP endpoints.
