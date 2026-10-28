# Minimum information

Use this as requirements documentation; implementing checks and targeted questions
is team work. Read existing input/evidence/policy/history before asking. Ask once
for all missing information relevant to the recognized operation. Do not ask for
unchanged contact fields, deductible/limit already retrievable or an optional reason.

| Operation | Minimum request information | Retrieve from tools |
|---|---|---|
| Policy enquiry | Identified policy (in envelope), question | Actual policy facts |
| Contact update | Fields to change and their new values; full address if replacing address | Current contacts, active status |
| Coverage change | Intended final annual limit and/or deductible | Current values, catalog, active status, thresholds |
| Termination | Explicit instruction (policy already identified) | Current status |
| Claim enquiry | Intended existing record; claim_id when needed | Record/status/payment and history |
| New reimbursement | Intent and necessary invoice/prescription contents | Policy, insured people, coverage dates/services, deductible, residual cover, history |

Every initial JSON includes event_id, ticket_id, policy_id, author_name, timestamp
and text. The author is always the policyholder. The patient can be another person
listed in the same policy. The envelope is not a document to parse with OCR.

## Invoice text

For covered services, invoice_text must contain provider ID, invoice number,
invoice date, service date, patient, service description, EUR amount and paid status.
Identify facts from the text rather than assuming a populated field is complete.
In this kit, invoice date must be on or after service date.

## Prescription text

Only diagnostic tests and physiotherapy require prescription_text, identifying the
patient and requested service. Specialist consultations need no prescription.
Missing, null, empty or substantively incomplete required contents warrant the one
clarification. A reply can add missing facts/text without changing existing facts.

Dental, cosmetic treatment and medicines: once exclusion is recognized, request
no further invoice/prescription contents. No approval overrides that exclusion.

We supply coherent text/fixture facts. Resolving contradictory sources, verifying
real signatures or detecting forged documents is outside the required exercise.
