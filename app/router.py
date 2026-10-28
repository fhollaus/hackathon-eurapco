"""Intentionally small starter router. Teams improve it."""

import re


_ADMIN_INTENT = re.compile(
    r"\b(?:"
    r"(?:change|update|correct|replace)\b.{0,80}\b(?:email|phone|telephone|mobile|address|contact details)\b"
    r"|(?:increase|raise|decrease|lower|change|update|set)\b.{0,100}\b(?:annual\s+)?(?:reimbursement\s+)?(?:limit|deductible|excess|coverage|cover)\b"
    r"|(?:terminate|cancel|end)\b.{0,50}\b(?:policy|cover(?:age)?)\b"
    r")",
    re.IGNORECASE,
)
_POLICY_ENQUIRY = re.compile(
    r"\b(?:"
    r"(?:is|are)\b.{0,80}\b(?:covered|coverage|policy)\b"
    r"|(?:what|which|how much)\b.{0,80}\b(?:policy|coverage|cover|deductible|annual\s+limit)\b"
    r")",
    re.IGNORECASE,
)
_CLAIMS_INTENT = re.compile(
    r"\b(?:claims?|reimbursements?|reimburse|invoices?|pay\s+me\s+back|paid\s+bill)\b",
    re.IGNORECASE,
)


def route(text):
    """Route by request intent, keeping policy changes ahead of claims keywords."""
    if _ADMIN_INTENT.search(text) or _POLICY_ENQUIRY.search(text):
        return "administration"
    if _CLAIMS_INTENT.search(text):
        return "claims"
    return "administration"
