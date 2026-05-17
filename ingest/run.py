"""
ingest/run.py

Main entry point for the ingest pipeline. Run this daily via GitHub Actions.
Reads config/subjects.yaml, fetches new papers from arXiv, summarizes them
with Claude, and writes JSON files that the Astro site builds against.

Usage:
    python ingest/run.py                  # ingest all active subjects
    python ingest/run.py --subject gr-qc  # ingest one subject
    python ingest/run.py --dry-run        # fetch only, no API calls
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import yaml

# Allow running from repo root or ingest/ directory
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from arxiv import fetch_new_papers
from summarize import summarize_paper, rank_papers, SONNET, HAIKU

DATA_DIR     = ROOT / "data"
PAPERS_DIR   = DATA_DIR / "papers"
SUBJECTS_DIR = DATA_DIR / "subjects"
CONFIG_PATH  = ROOT / "config" / "subjects.yaml"

# Safety ceiling — warn loudly if a single run would cost more than this
COST_CEILING_PER_RUN = 0.70   # ~$21/month at 30 runs


# ── Data helpers ──────────────────────────────────────────────────────────

def is_ingested(paper_id: str) -> bool:
    return (PAPERS_DIR / f"{paper_id}.json").exists()


def save_paper(paper: dict) -> None:
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    paper["ingested_at"] = datetime.now(timezone.utc).isoformat()
    path = PAPERS_DIR / f"{paper['id']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(paper, f, indent=2, ensure_ascii=False)


def update_subject_index(subject_code: str, papers: list[dict]) -> None:
    """Update per-date JSON files and the subject's index.json."""
    if not papers:
        return

    subject_dir = SUBJECTS_DIR / subject_code
    subject_dir.mkdir(parents=True, exist_ok=True)

    # Group new papers by announced date
    by_date: dict[str, list[str]] = {}
    for p in papers:
        d = p["announced_date"]
        by_date.setdefault(d, []).append(p["id"])

    # Merge into daily files (idempotent)
    for d, ids in by_date.items():
        daily_path = subject_dir / f"{d}.json"
        existing_ids: list[str] = []
        if daily_path.exists():
            existing_ids = json.loads(daily_path.read_text()).get("paper_ids", [])

        merged = list(dict.fromkeys(existing_ids + ids))  # dedup, preserve order
        daily_path.write_text(
            json.dumps({"date": d, "subject": subject_code, "paper_ids": merged}, indent=2)
        )

    # Rebuild subject index (sorted descending)
    all_dates = sorted(
        [f.stem for f in subject_dir.glob("????-??-??.json")],
        reverse=True,
    )
    index_path = subject_dir / "index.json"
    index_path.write_text(
        json.dumps({"subject": subject_code, "dates": all_dates}, indent=2)
    )


# ── Core pipeline ─────────────────────────────────────────────────────────

def ingest_subject(subject: dict, client: anthropic.Anthropic, dry_run: bool, target_date: str = None) -> tuple[int, float]:
    """
    Run the full ingest pipeline for one subject.
    Returns (papers_ingested, cost_usd).
    """
    code          = subject["code"]
    daily_cap     = subject.get("daily_cap", 20)
    prompt_variant = subject.get("prompt_variant", "physics")
    use_filter    = subject.get("use_filter", False)
    model         = HAIKU if subject.get("model") == "haiku" else SONNET

    # 1. Fetch
    all_papers = fetch_new_papers(code, target_date)
    source_msg = f"for date {target_date}" if target_date else "from RSS"
    print(f"  Fetched {len(all_papers)} {source_msg}")

    # 2. Skip already-ingested
    new_papers = [p for p in all_papers if not is_ingested(p["id"])]
    print(f"  New: {len(new_papers)}")

    if not new_papers:
        return 0, 0.0

    # 3. Pre-rank high-volume subjects with cheap Haiku filter
    if use_filter and len(new_papers) > daily_cap:
        print(f"  Ranking {len(new_papers)} → top {daily_cap} with Haiku filter...")
        new_papers = rank_papers(new_papers, prompt_variant, client, top_n=daily_cap)

    # 4. Apply daily cap
    to_process = new_papers[:daily_cap]
    print(f"  Processing {len(to_process)} (cap: {daily_cap})")

    if dry_run:
        for p in to_process:
            print(f"    [dry-run] {p['id']}: {p['title_original'][:70]}")
        return 0, 0.0

    # 5. Summarize
    ingested = []
    run_cost = 0.0

    for paper in to_process:
        short_title = paper["title_original"][:65]
        print(f"  → {paper['id']}: {short_title}…")

        result = summarize_paper(paper, prompt_variant, client, model=model)
        if result:
            save_paper(result)
            ingested.append(result)
            run_cost += result.get("cost_usd", 0.0)
            time.sleep(0.3)  # gentle rate spacing

    # 6. Update subject date index
    update_subject_index(code, ingested)

    print(f"  ✓ {len(ingested)} ingested  (${run_cost:.4f})")
    return len(ingested), run_cost


# ── Entry point ───────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest arXiv papers for The Brief")
    parser.add_argument("--subject", help="Ingest only this subject code (e.g. gr-qc)")
    parser.add_argument("--date", help="Fetch papers from a specific date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but skip Claude API calls")
    args = parser.parse_args()

    # API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set")
        return 1

    # Config
    if not CONFIG_PATH.exists():
        print(f"ERROR: Config not found at {CONFIG_PATH}")
        return 1

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    subjects = config.get("subjects", [])
    if args.subject:
        subjects = [s for s in subjects if s["code"] == args.subject]
        if not subjects:
            print(f"ERROR: Subject '{args.subject}' not found in config/subjects.yaml")
            return 1
    else:
        subjects = [s for s in subjects if s.get("active", False)]

    if not subjects:
        print("No active subjects found. Set active: true in config/subjects.yaml")
        return 0

    client = anthropic.Anthropic(api_key=api_key)

    total_papers = 0
    total_cost   = 0.0

    # Generate date range (last 3 days by default, or single date if specified)
    if args.date:
        date_range = [args.date]
    else:
        today = datetime.now(timezone.utc).date()
        date_range = [(today - timedelta(days=i)).isoformat() for i in range(3)]

    print(f"\n{'─' * 48}")
    print(f"  The Brief — arXiv ingest")
    print(f"  Subjects: {', '.join(s['code'] for s in subjects)}")
    print(f"  Date range: {date_range[0]} to {date_range[-1]}")
    if args.dry_run:
        print("  Mode: DRY RUN (no API calls)")
    print(f"{'─' * 48}")

    for subject in subjects:
        print(f"\n── {subject['code']} ─────────────────────────")
        for target_date in date_range:
            papers, cost = ingest_subject(subject, client, dry_run=args.dry_run, target_date=target_date)
            total_papers += papers
            total_cost   += cost

    # ── Cost report ───────────────────────────────────────────────────────
    projected_monthly = total_cost * 30
    print(f"\n{'─' * 48}")
    print(f"  Papers today:      {total_papers}")
    print(f"  Cost today:        ${total_cost:.4f}")
    print(f"  Est. monthly:      ${projected_monthly:.2f}")

    if total_cost > COST_CEILING_PER_RUN:
        print(f"\n  ⚠️  WARNING: This run cost ${total_cost:.4f}, above the")
        print(f"     per-run ceiling of ${COST_CEILING_PER_RUN:.2f}.")
        print(f"     Reduce daily_cap values in config/subjects.yaml.")

    if projected_monthly > 20:
        print(f"\n  ⚠️  WARNING: Projected monthly cost (${projected_monthly:.2f})")
        print(f"     exceeds the $20 budget target.")

    print(f"{'─' * 48}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
