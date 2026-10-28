"""Show route2's result for every public training ticket."""

import json
from pathlib import Path

from app.router import route2


TRAINING_DIR = Path(__file__).parent / "training"
EXPECTED_AGENTS = {
    "T01": "administration",
    "T02": "claims",
    **{f"T{i:02}": "administration" for i in range(3, 8)},
    **{f"T{i:02}": "claims" for i in range(8, 16)},
}


def main():
    for ticket_path in sorted(TRAINING_DIR.glob("T*.json")):
        ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
        actual = route2(ticket["text"])
        expected = EXPECTED_AGENTS[ticket["ticket_id"]]
        print(f'{ticket["ticket_id"]}: {actual} (expected: {expected})')
        if actual != expected:
            raise SystemExit(f'Routing mismatch for {ticket["ticket_id"]}')


if __name__ == "__main__":
    main()
