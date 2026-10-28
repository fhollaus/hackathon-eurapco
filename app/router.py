"""Intentionally small starter router. Teams improve it."""

import re


def route(text):
    return "claims" if re.search(r"\b(claim|claims|invoice|invoices|reimbursement|reimburse)\b", text, re.I) else "administration"
