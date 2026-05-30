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
import random
import subprocess
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


def existing_paper_count(subject_code: str, announced_date: str) -> int:
    """Number of papers already on disk for this subject + announced_date."""
    daily_path = SUBJECTS_DIR / subject_code / f"{announced_date}.json"
    if not daily_path.exists():
        return 0
    try:
        return len(json.loads(daily_path.read_text()).get("paper_ids", []))
    except (json.JSONDecodeError, OSError):
        return 0


def commit_and_push_subject(code: str, announced_dates: list[str]) -> None:
    """Commit and push this subject's new data files (CI only).

    Per-subject commits make the run durable: a timeout mid-loop preserves
    every completed subject's work instead of discarding the whole run.
    Local runs are no-ops.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return

    try:
        subprocess.run(["git", "add", "data/"], check=True, cwd=ROOT)
        check = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=ROOT)
        if check.returncode == 0:
            return  # nothing staged

        dates_str = ",".join(sorted(announced_dates, reverse=True))
        msg = f"ingest: {code} {dates_str}"
        subprocess.run(["git", "commit", "-m", msg], check=True, cwd=ROOT)

        # Rebase-on-failure retry so a concurrent push (or auto-deploy ref bump)
        # doesn't drop the commit. Tries: push → rebase → push.
        push = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if push.returncode == 0:
            print(f"  [GIT] pushed: {msg}")
            return

        err = (push.stderr or push.stdout or "").strip()[:200]
        print(f"  [GIT] push failed, rebasing: {err}")
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=ROOT, capture_output=True, text=True,
        )
        push = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if push.returncode == 0:
            print(f"  [GIT] pushed after rebase: {msg}")
        else:
            err = (push.stderr or push.stdout or "").strip()[:200]
            print(f"  [GIT] push gave up after rebase: {err}")
    except subprocess.CalledProcessError as e:
        print(f"  [GIT] subprocess error: {e}")


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

def ingest_subject(
    subject: dict,
    client: anthropic.Anthropic,
    dry_run: bool,
    date_range: list[str],
) -> tuple[int, float, list[str]]:
    """
    Run the full ingest pipeline for one subject across the lookback window.

    Fetches papers for each date in date_range (these are arXiv *submission*
    dates), dedupes by paper ID across fetches, groups candidates by
    *announced_date*, then enforces daily_cap per announced_date —
    accounting for papers already on disk for that date from prior runs.

    daily_cap is per (subject, announced_date), not per fetch call. Without
    this, the 3-weekday lookback would 2× the cap whenever multiple
    submission days roll up into the same announcement day.

    Returns (papers_ingested, cost_usd, ingested_dates).
    """
    code           = subject["code"]
    daily_cap      = subject.get("daily_cap", 20)
    prompt_variant = subject.get("prompt_variant", "physics")
    use_filter     = subject.get("use_filter", False)
    model          = HAIKU if subject.get("model") == "haiku" else SONNET

    # 1. Fetch across all dates, dedupe by paper ID
    candidates: list[dict] = []
    seen_ids: set[str] = set()
    for target_date in date_range:
        fetched = fetch_new_papers(code, target_date)
        print(f"  Fetched {len(fetched)} for {target_date}")
        for p in fetched:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                candidates.append(p)

    # 2. Drop papers already on disk from prior runs
    candidates = [p for p in candidates if not is_ingested(p["id"])]
    print(f"  Deduped new candidates: {len(candidates)}")

    if not candidates:
        return 0, 0.0, []

    # 3. Group by announced_date
    by_announced: dict[str, list[dict]] = {}
    for p in candidates:
        by_announced.setdefault(p["announced_date"], []).append(p)

    # 4. Per announced_date: subtract existing-on-disk count from cap, rank, cap, summarize
    total_ingested  = 0
    run_cost        = 0.0
    ingested_dates: list[str] = []

    for announced_date in sorted(by_announced.keys(), reverse=True):
        group = by_announced[announced_date]
        existing = existing_paper_count(code, announced_date)
        remaining = max(0, daily_cap - existing)

        if remaining == 0:
            print(
                f"  [{announced_date}] cap reached on disk "
                f"({existing}/{daily_cap}) — skipping {len(group)} candidates"
            )
            continue

        # Pre-rank high-volume subjects with cheap Haiku filter
        if use_filter and len(group) > remaining:
            print(
                f"  [{announced_date}] Ranking {len(group)} → top {remaining} "
                f"with Haiku filter..."
            )
            group = rank_papers(group, prompt_variant, client, top_n=remaining)

        to_process = group[:remaining]
        print(
            f"  [{announced_date}] Processing {len(to_process)} "
            f"(cap: {daily_cap}, existing: {existing}, remaining: {remaining})"
        )

        if dry_run:
            for p in to_process:
                print(f"    [dry-run] {p['id']}: {p['title_original'][:70]}")
            continue

        ingested = []
        for paper in to_process:
            short_title = paper["title_original"][:65]
            print(f"  → {paper['id']}: {short_title}…")

            result = summarize_paper(paper, prompt_variant, client, model=model)
            if result:
                save_paper(result)
                ingested.append(result)
                run_cost += result.get("cost_usd", 0.0)
                time.sleep(0.3)  # gentle rate spacing

        update_subject_index(code, ingested)
        total_ingested += len(ingested)
        if ingested:
            ingested_dates.append(announced_date)
        print(f"  [{announced_date}] ✓ {len(ingested)} ingested")

    print(f"  ── subject total: {total_ingested} ingested  (${run_cost:.4f})")
    return total_ingested, run_cost, ingested_dates


# ── Date helpers ──────────────────────────────────────────────────────────

def get_last_n_weekdays(n: int = 3, start_date=None) -> list[str]:
    """
    Get the last N weekdays (Mon-Fri), excluding weekends.
    arXiv does not publish on Sat/Sun, so this avoids wasted API calls.

    Examples:
    - Monday run: returns [Mon, Fri, Thu]
    - Tuesday run: returns [Tue, Mon, Fri]
    - Friday run: returns [Fri, Thu, Wed]
    """
    if start_date is None:
        start_date = datetime.now(timezone.utc).date()

    weekdays = []
    current = start_date

    while len(weekdays) < n:
        # Monday=0, Sunday=6. Include Mon-Fri only (0-4)
        if current.weekday() < 5:
            weekdays.append(current.isoformat())
        current -= timedelta(days=1)

    return weekdays


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

    # Shuffle subject order with a date-derived seed so tail subjects don't
    # always lose when a run gets cancelled. Reproducible within a UTC day.
    if not args.subject:
        seed = datetime.now(timezone.utc).date().isoformat()
        random.Random(seed).shuffle(subjects)

    client = anthropic.Anthropic(api_key=api_key)

    total_papers = 0
    total_cost   = 0.0
    subject_results = {}  # Track success/failure per subject

    # Generate date range (last 3 weekdays by default, or single date if specified)
    if args.date:
        date_range = [args.date]
    else:
        date_range = get_last_n_weekdays(n=3)

    print(f"\n{'─' * 48}")
    print(f"  The Brief — arXiv ingest")
    print(f"  Subjects: {', '.join(s['code'] for s in subjects)}")
    print(f"  Date range: {date_range[0]} to {date_range[-1]}")
    if args.dry_run:
        print("  Mode: DRY RUN (no API calls)")
    print(f"{'─' * 48}")

    for subject in subjects:
        print(f"\n── {subject['code']} ─────────────────────────")
        papers, cost, ingested_dates = ingest_subject(
            subject, client, dry_run=args.dry_run, date_range=date_range
        )
        total_papers += papers
        total_cost   += cost

        # Track whether this subject succeeded (got any papers)
        subject_results[subject['code']] = papers > 0

        if papers > 0 and not args.dry_run:
            commit_and_push_subject(subject['code'], ingested_dates)

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

    # ── Subject coverage summary ──────────────────────────────────────────
    succeeded = [s for s, ok in subject_results.items() if ok]
    failed = [s for s, ok in subject_results.items() if not ok]

    if failed:
        print(f"\n  [SUMMARY] {len(succeeded)} of {len(subjects)} subjects succeeded")
        print(f"  [SUMMARY] Failed subjects: {', '.join(failed)}")
    elif succeeded:
        print(f"\n  [SUMMARY] All {len(subjects)} subjects succeeded")

    print(f"{'─' * 48}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
