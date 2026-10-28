"""Loading support only: teams write and integrate their domain procedures."""

from app.contracts import ROOT


def load_skills(agent):
    if agent not in ("administration", "claims"):
        raise ValueError("Unknown agent.")
    return [(p.stem, p.read_text(encoding="utf-8")) for p in sorted((ROOT / "skills" / agent).glob("*.md"))]
