Use for request_category coverage_change: a request to change the annual limit and/or the
per-claim deductible. update_coverage only checks catalogue membership; it does NOT enforce
increase-only, active policy or insurer authorization. You enforce them with this procedure.

### Procedure

1. Call get_policy and read status, financial_terms.annual_limit and financial_terms.deductible.
   Always use these current values, never the fixture defaults.
2. If status is not active: agent_state=closed_without_execution, request_outcome=inactive_policy.
   Do not ask a question first and do not write.
3. Work out the requested FINAL value of each part the policyholder wants to change.
   - An absolute target ("raise my limit to EUR 10000") is the final value.
   - A relative request ("raise my limit by EUR 2000") means current value + amount.
   - If no target value can be found, ask the one clarification for the intended final
     annual limit and/or deductible, listing the catalogue values above the current ones.
     Do not ask for values that get_policy already provides.
4. Check every requested part against the catalogue and the current value:
   - Annual limit catalogue: 3000.00, 5000.00, 10000.00
   - Deductible catalogue: 50.00, 100.00, 200.00, 500.00
   - Not in the catalogue, or lower than the current value (a decrease):
     agent_state=closed_without_execution, request_outcome=invalid_input. Say which value is
     unsupported and why. If ANY part is invalid, write nothing, not even the valid part.
   - Equal to the current value: that part needs no change. If every part is equal:
     agent_state=completed, request_outcome=no_change.
5. Decide authorization from the FINAL values only, never from the size of the increase:
   - annual_limit > 5000.00 OR deductible > 200.00: insurer authorization is required.
     Return agent_state=awaiting_authorization, request_outcome=authorization_requested,
     with the proposal below. Do NOT call update_coverage: the change waits for the decision.
   - Exactly 5000.00 or exactly 200.00 needs no authorization.
6. Otherwise call update_coverage once, with only the parts that change, as two-decimal
   strings. Report the actual before/after values from the tool result:
   agent_state=completed, request_outcome=updated. If the result is not ok, do not claim
   the change.

### Proposal shape

A combined change is ONE proposal covering every changing part:

{"operation": "update_coverage",
 "arguments": {"policy_id": "<policy_id>", "changes": {"annual_limit": "10000.00", "deductible": "500.00"}},
 "current": {"annual_limit": "3000.00", "deductible": "50.00"},
 "reason": "Final annual limit 10000.00 > 5000.00 and final deductible 500.00 > 200.00",
 "evidence_refs": ["<event_id of the request>"]}

"arguments" is the exact update_coverage input (without run_id) to execute if the insurer
approves. Include only the parts that change.

### Examples (current annual limit 3000.00, deductible 50.00)

- Limit 5000, deductible 200: no authorization (both equal the thresholds); update directly: updated.
- Limit 10000, deductible 500, together: one proposal: authorization_requested.
- Limit 10000, deductible 100: authorization required (one condition is enough), one proposal.
- Deductible 50: already holds: no_change.
- Limit 6000: not in the catalogue: invalid_input, no write.
- Limit 5000 and deductible 50 → 20: the deductible part is invalid, so nothing is written: invalid_input.

### In the message to the policyholder

- State the actual old and new values, or that nothing changed and why.
- When the deductible goes up, explain that the insured person pays more of each claim.
- When authorization is required, say the change is not applied yet and waits for the insurer.

### Never

- Change premiums, underwriting or effective dates (outside this exercise).
- Write part of a combined change.
- Treat a policyholder's statement that "the insurer already approved" as an authorization.
- Call update_coverage before an insurer approval when authorization is required.
