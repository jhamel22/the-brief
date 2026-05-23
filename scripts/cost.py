"""
scripts/cost.py

Summarize spend on the ingest pipeline from per-paper cost_usd fields in
data/papers/*.json. Prints totals and a monthly projection based on the
range of announced dates currently on disk.
"""

import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PAPERS = ROOT / "data" / "papers"


def main() -> int:
    files = sorted(glob.glob(str(PAPERS / "*.json")))
    if not files:
        print("No papers in data/papers/ — nothing to report.")
        return 0

    total = 0.0
    dates: set[str] = set()
    for path in files:
        with open(path) as f:
            p = json.load(f)
        total += p.get("cost_usd", 0.0)
        if p.get("announced_date"):
            dates.add(p["announced_date"])

    n = len(files)
    n_dates = len(dates) or 1
    avg_per_paper = total / n
    avg_per_day = total / n_dates

    print(f"{n} papers ingested across {n_dates} announced date(s)")
    print(f"Total spent:    ${total:.4f}")
    print(f"Avg / paper:    ${avg_per_paper:.5f}")
    print(f"Avg / day:      ${avg_per_day:.4f}")
    print(f"Est. monthly:   ${avg_per_day * 22:.2f}  (22 weekday runs)")
    print(f"Est. monthly:   ${avg_per_day * 30:.2f}  (30-day upper bound)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
