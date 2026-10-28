from app.contracts import ROOT


def system_prompt():
    return (ROOT / "agents" / "claims" / "prompt.md").read_text(encoding="utf-8")
