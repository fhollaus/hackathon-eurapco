# Fictional medical policy

These are exercise rules, not real insurance advice. Read current policy values
from get_policy; do not assume fixture defaults after a change within the same run.

## Administration

Contact updates permit only holder email, international-format telephone and postal
address (street, city, postal_code, country). Active policy required. Unspecified
contacts remain unchanged. Equal values mean no change. No insurer approval or
extra policyholder confirmation. Basic field-format validation is supplied.

Only increases of annual limit and/or per-claim deductible are supported. Annual
limit levels: EUR3000,5000,10000. Deductible levels: EUR50,100,200,500. Equality is
no-op; decreases/noncatalog values are unsupported. Compare requested FINAL values:
annual limit >5000 OR deductible >200 requires insurer approval; equality does not.
A combined change is one atomic proposal, with no partial write if inadmissible or
rejected. Active policy required. Underwriting, premium changes and effective-date
management are deliberately excluded.

Actual termination always needs insurer approval and an explicit holder instruction;
a question about how to terminate is an enquiry. Personal reason optional. Already
terminated means no change. No scheduling/refunds/renewals/pending-claim interaction.

## Medical reimbursement

Covered: specialist consultation, diagnostic tests, physiotherapy.
Excluded: dental care, cosmetic treatment, medicines. Recognized exclusions need
no further evidence collection or insurer approval; explain and close without payment.

For covered services require an active policy, an insured patient and service date
within the coverage period, first and last days included. In this exercise, the
invoice date must be the same as or later than the service date; this is a kit
simplification enforced by create_claim. Specialist: paid invoice
text. Diagnostic/physiotherapy: paid invoice and prescription text. Required contents
are in minimum-information.md; non-empty does not mean sufficient.

Formula: eligible base=max(0,expense-deductible); theoretical reimbursement=80% of
eligible base, rounded half-up to EUR cents; final=min(theoretical,remaining limit).
Authorization only if FINAL reimbursement >EUR1500, not invoice/theoretical amount.
Equality is autonomous. Zero-reimbursement completion is a team design choice;
correct calculation and truthful records remain required.

With deductible50: expense180 →104; expense1925 →1500; expense2000 →1560.
If remaining limit900, the last example pays at most900 and is autonomous.
This is different from the preceding live-coding formula.

Duplicate key: provider ID + invoice number + invoice-date year. Check supplied
history before creating another record or paying. Paid OR pending match means
report existing reference, with no duplicate registration/payment. No fuzzy matching
is required; identifiers are consistent in supplied evidence/history.

Initial annual limit3000 and deductible50. POL-001 has a historical104 payment and
remaining2896; POL-002 remaining900; POL-003 inactive. Full facts/people are in
initial.json/get_policy. Consumption changes only on successful simulated payment,
not proposal/approval. Each independent initial ticket gets the same initial state.

Technical tool validation does not implement the business permission checks for
you. Team workflow controls must govern when effects are allowed. Tool acceptance
of a value's syntax/catalog membership is not insurer authorization.
