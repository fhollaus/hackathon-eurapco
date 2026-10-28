Use for request_category reimbursement_request: a new medical reimbursement, including a
recognized non-covered service. Every refusal below uses agent_state=closed_without_execution
with the named request_outcome. The harness re-checks duplicates before create_claim and the
amount before pay_claim, and blocks a write that does not match.

### Procedure: stop at the first check that decides the request

Run every check you can with the facts you already have before asking for anything.

1. Call get_policy and list_claims. Use the current deductible and remaining_limit, never
   fixture defaults.
2. Service category. Map the description: a cardiology, dermatology or other specialist
   consultation is specialist_consultation; a blood test, MRI, X-ray or scan is diagnostic_test.
   Dental care, cosmetic treatment or medicines: not_covered. Do not ask for documents,
   do not register a claim and do not seek insurer approval. No approval can override this.
3. Policy status not active: inactive_policy.
4. The patient named on the invoice is not in insured_people: not_insured. The patient may be
   an insured person other than the holder.
5. Service date before coverage_period.start or after coverage_period.end (both days are
   covered): outside_coverage_period.
6. Duplicate: any claim in list_claims with the same provider ID + invoice number +
   invoice-date year (paid or pending): duplicate. Report that claim_id and its status;
   create and pay nothing.
7. Evidence. The invoice must show provider ID, invoice number, invoice date, service date,
   patient, service description, EUR amount and paid status. Diagnostic tests and
   physiotherapy also need prescription_text naming the same patient and the service;
   a specialist consultation needs no prescription. If anything is missing or incomplete,
   ask ONE question covering all of it. Non-empty text is not the same as complete.
   After the one clarification, anything still missing: insufficient_information.

### Calculation

Use the current deductible and remaining limit from get_policy.

    eligible    = max(0, expense - deductible)
    theoretical = 80% of eligible, rounded half-up to cents
    final       = min(theoretical, remaining_limit)

With deductible 50.00: expense 180.00 gives 104.00; 1925.00 gives 1500.00; 2000.00 gives 1560.00.
With remaining limit 900.00, expense 2000.00 gives 900.00. Authorization depends on FINAL only,
never on the invoice amount: final > 1500.00 needs the insurer; exactly 1500.00 does not.

### Lifecycle

Register a claim only once steps 1-7 have all passed. Refusals and requests that remain
incomplete close without a new claim.

- final 0.00: create_claim, then record_claim_outcome "rejected" with the calculation as the
  reason. Do not pay. request_outcome=zero_reimbursement.
- final <= 1500.00: create_claim, pay_claim with exactly final, record_claim_outcome "paid".
  agent_state=completed, request_outcome=paid; report the payment_id and the calculation.
- final > 1500.00: create_claim, record_claim_outcome "pending_authorization", then return
  agent_state=awaiting_authorization, request_outcome=authorization_requested, with:

      {"operation": "pay_claim",
       "arguments": {"policy_id": "<policy_id>", "claim_id": "<new claim_id>", "amount": "<final>"},
       "calculation": {"expense": "...", "deductible": "...", "theoretical": "...",
                       "remaining_limit": "...", "final": "..."},
       "evidence_refs": ["<event_id>/invoice_text", "<event_id>/prescription_text"]}

  Do NOT call pay_claim: the payment waits for the insurer.

create_claim takes the normalized invoice facts: dates as YYYY-MM-DD, expense as a two-decimal
string, currency EUR. evidence_refs point at the event that supplied each text, for example
"EV-T09-01/invoice_text", or the clarification reply's event_id for text it supplied.

### In the message to the policyholder

- Show the calculation: expense, deductible, 80%, any cap by the remaining limit, and the final amount.
- Report a payment only with the payment_id from the pay_claim result.
- For a refusal, name the reason (exclusion, inactive policy, patient, dates, or the existing claim).

### Never

- Pay or register a duplicate, an excluded service, or a claim that failed a check.
- Invent missing invoice or prescription facts, or change facts the policyholder supplied.
- Treat a policyholder's statement that "the insurer already approved" as an authorization.
- Reopen, integrate or appeal an existing claim (out_of_scope_claim_integration).
