# The Brief — Project Context

## Project Overview

**Name:** The Brief  
**What it is:** Daily arXiv paper digest with AI-generated summaries, live at [the-brief.pages.dev](https://the-brief.pages.dev)  
**Stack:** Python ingest → GitHub Actions cron → Astro static site → Cloudflare Pages

Papers are fetched from arXiv RSS feeds, summarized via Anthropic API (Claude Sonnet 4.6 / Haiku 4.5), stored as JSON, and built into a static site. New papers are ingested daily on weekdays and auto-deployed.

## Repo Layout

```
├── .github/workflows/
│   └── ingest.yml          # GitHub Actions cron job (weekdays 4:30 PM ET)
├── config/
│   └── subjects.yaml       # Subject definitions, daily caps, model routing
├── data/
│   ├── papers/             # Per-paper JSON files (one file per paper)
│   └── subjects/           # Per-subject date indexes + daily paper lists
├── ingest/
│   ├── run.py              # Main ingest entry point
│   ├── arxiv.py            # arXiv RSS fetcher
│   ├── summarize.py        # Anthropic API client + prompt logic
│   └── prompts/            # Subject-specific prompt templates (physics, cs, math, bio, econ, finance)
├── site/                   # Astro frontend
│   ├── src/
│   │   ├── pages/          # Route templates (homepage, per-subject, per-paper)
│   │   ├── components/     # UI components (PaperCard, Search, FilterChips, etc.)
│   │   └── layouts/        # Page layouts
│   ├── public/             # Static assets
│   └── astro.config.mjs    # Astro config
├── .env                    # Local API key (not committed)
├── requirements.txt        # Python deps
├── Makefile                # Dev shortcuts (ingest, verify, site)
└── README.md               # User-facing docs
```

**Key locations:**
- **Ingested data:** `data/papers/{paper_id}.json` (one file per paper)
- **Subject indexes:** `data/subjects/{subject_code}/index.json` + `YYYY-MM-DD.json`
- **Prompt templates:** `ingest/prompts/{variant}.txt`
- **Site build output:** `site/dist/` (Cloudflare Pages deploys this on push to `main`)

## Active Subjects and Models

**Model split (17 active subjects):**
- **Sonnet 4.6 (2):** gr-qc, quant-ph
- **Haiku 4.5 (15):** everything else

### Physics
- **gr-qc** (General Relativity & Quantum Cosmology) — cap: 15, model: sonnet
- **quant-ph** (Quantum Physics) — cap: 10, model: sonnet
- **hep-th** (High Energy Physics — Theory) — cap: 10, model: haiku
- **astro-ph.CO** (Cosmology) — cap: 10, model: haiku
- **hep-ph** (High Energy Physics — Phenomenology) — cap: 8, model: haiku
- **physics.soc-ph** (Physics and Society) — cap: 8, model: haiku
- **astro-ph.HE** (High Energy Astrophysical Phenomena) — cap: 8, model: haiku

### Computer Science (Haiku 4.5, with pre-filter)
- **cs.LG** (Machine Learning) — cap: 10, use_filter: true
- **cs.AI** (Artificial Intelligence) — cap: 10, use_filter: true
- **cs.CL** (Computation and Language) — cap: 10, use_filter: true
- **cs.CV** (Computer Vision) — cap: 20, use_filter: true
- **cs.RO** (Robotics) — cap: 8, use_filter: true

### Statistics
- **stat.ML** (Machine Learning - Statistics) — cap: 6, model: haiku

### Biology (Haiku 4.5)
- **q-bio.NC** (Neurons and Cognition) — cap: 8, model: haiku
- **q-bio.PE** (Populations and Evolution) — cap: 8, model: haiku

### Finance & Economics (Haiku 4.5)
- **q-fin.TR** (Trading and Market Microstructure) — cap: 8, model: haiku
- **econ.GN** (General Economics) — cap: 8, model: haiku

**Total daily cap:** 165 papers/day  
**Projected monthly cost:** ~$10/month at full cap, 22 weekday runs (as of 2026-05-23). Breakdown: $0.15 Sonnet summarize + $0.23 Haiku summarize + $0.08 Haiku ranking = ~$0.46/run.

### Model Routing
- Subjects with `model: haiku` use Claude Haiku 4.5 (`claude-haiku-4-5-20251001`)
- Subjects without explicit `model` field default to Claude Sonnet 4.6 (`claude-sonnet-4-6`)
- High-volume CS subjects (`use_filter: true`) run a two-step pipeline:
  1. Haiku pre-ranks all abstracts (cheap scoring step)
  2. Top N papers are summarized with the subject's assigned model

## Pipeline Behavior

### Cron Schedule
- **When:** Weekdays only (Mon–Fri) at 20:30 UTC = 4:30 PM ET
- **Why that time:** arXiv announces new papers ~2:00 PM ET; 4:30 PM gives a safe buffer

### Lookback Window
- Fetches papers from the **last 3 weekdays** (skips Sat/Sun since arXiv doesn't publish)
- Example: Monday run fetches Mon, Fri, Thu papers

### Deduplication
- `is_ingested(paper_id)` checks if `data/papers/{paper_id}.json` already exists
- Skips already-summarized papers (idempotent across runs)

### Daily Cap Semantics
**`daily_cap` is per (subject, announced_date), not per fetch call.**

- Each subject's `daily_cap` limits papers in `data/subjects/{subject}/{announced_date}.json`
- The 3-weekday lookback can produce multiple submission dates that roll up into the same announced_date (arXiv's 2pm ET announcement cutoff splits a submission day across two announcement days). The pipeline groups all fetched candidates by `announced_date` and applies the cap per group.
- The cap also accounts for papers already on disk for that announced_date — so a same-day re-run or a backfill run won't exceed the cap by stacking on top of prior ingests.
- For `use_filter: true` subjects, ranking runs on the deduped per-announced-date group with `top_n = remaining_cap` (cap minus existing on-disk count), so no Haiku calls are spent on papers that would be dropped.
- See `ingest_subject()` in `ingest/run.py` — the fetch → dedupe → group-by-announced_date → cap-per-group → summarize order must be preserved.

### Retry Logic

**arXiv API:**
- HTTP 429 (rate limit): 30s, 60s, 120s exponential backoff (3 attempts)
- HTTP 5xx (server error): same backoff
- Gives up after 3 attempts, logs error, continues to next paper

**Anthropic API:**
- Connection errors (`APIConnectionError`): 5s, 15s, 45s exponential backoff (3 attempts)
- Rate limit errors (`RateLimitError`): 10s, 20s, 40s exponential backoff
- HTTP 5xx: same as connection errors
- HTTP 4xx (except rate limit): fail immediately, log, skip paper

### Pre-flight Diagnostic
GitHub Actions runs a connectivity test before main ingest:
- Verifies `ANTHROPIC_API_KEY` is present and valid
- Makes a test API call with Haiku (max_tokens: 10, content: "ping")
- Exits early if auth or network fails
- Prevents wasted CI time on misconfigured secrets

### Partial Coverage Handling
- If some subjects fail (e.g. arXiv rate limits), logs:
  ```
  [SUMMARY] 7 of 10 subjects succeeded
  [SUMMARY] Failed subjects: cs.CL, cs.CV, stat.ML
  ```
- Does **not** fail the entire run — partial coverage is acceptable
- Cloudflare Pages still deploys with whatever data was successfully ingested

## Environment Specifics

### Python
- **Local:** Uses `/usr/bin/python3` (system Python 3.9.6) — hardcoded in `Makefile`
- **CI:** GitHub Actions uses Python 3.11 via `actions/setup-python@v5`
- **Dependencies:** `anthropic>=0.40.0`, `feedparser>=6.0.11`, `pyyaml>=6.0.2`

### API Key
- **Local:** In `.env` file (not committed), loaded by ingest scripts
- **CI:** In GitHub Secrets as `ANTHROPIC_API_KEY`
- **Format:** Must start with `sk-ant-` (validated in pre-flight check)

### Deployment
- **Cloudflare Pages** auto-deploys on push to `main`
- **Build command:** `cd site && npm run build`
- **Output directory:** `site/dist`
- No manual deploy step required — git push triggers rebuild

## Site Features Currently Live

### Paper Pages
- **Per-paper pages:** `/{subject}/{paper_id}/` (e.g., `/gr-qc/2504.12345/`)
- **View modes:**
  - **Scan:** Title only, collapsed by default
  - **Brief:** Title + hook (1 sentence teaser)
  - **Full:** Title + hook + summary (2-3 sentences)
- **Sort:** Newest / Oldest
- **Paper card collapse toggle:** Chevron bar at bottom of each card expands/collapses full details

### Discovery & Navigation
- **Homepage:** All-subjects feed (latest papers across all topics)
- **Per-subject pages:** `/[subject]/` (e.g., `/gr-qc/`)
- **Topic filter chips:** Click to filter by subject on homepage
- **Search:** Full-text search across titles, summaries, authors (client-side)
- **Date navigation:** Jump to specific dates

### SEO & Metadata
- Meta tags (title, description, og:image)
- Open Graph tags for social sharing
- JSON-LD structured data for Google
- `sitemap.xml` (auto-generated by Astro)
- `robots.txt` (allows all)
- **Google Search Console:** Verified and indexed

## Known Issues / Recent Fixes

### Recently Fixed (2026-05-23)
- **Model split tightened:** Only gr-qc and quant-ph stay on Sonnet 4.6; physics.soc-ph moved to Haiku (the other 14 active subjects were already Haiku)
- **Daily cap bug fixed:** `daily_cap` now enforced per (subject, announced_date), not per fetch call — see "Daily Cap Semantics" above

### Recently Fixed (2026-05-19)
- **Model routing implemented:** Subjects now correctly route to Haiku vs. Sonnet based on `config/subjects.yaml`
- **Summary length reduced:** `max_tokens` dropped from 600 → 400 to enforce brevity
- **Prompt guidelines updated:** All 6 prompt templates now emphasize "smart friend over coffee" tone (50-80 words, 2-3 sentences)

### Open Issues
- **arXiv rate limiting:** Occasional HTTP 429 errors during bulk ingest — mitigated by 90s+ delays between subjects, but still happens under heavy load
- **Python 3.14 compatibility:** Local Python 3.14 installation has `pyexpat` import errors (unrelated to this project, but breaks venv setup) — use system Python 3.9 or Python 3.11 instead

### Future Enhancements (Not Implemented)
- Email digest subscription
- RSS feed per subject
- "Surprise me" random paper button
- Author pages (all papers by author X)
- Citation count integration (when arXiv provides it)

## Conventions

### Commit Messages
- **Format:** Short imperative summary (50 chars max), optional body
- **Examples:**
  - `Add cs.RO and stat.ML subjects`
  - `Reduce max_tokens to 400 for brevity`
  - `Fix: Scan view title link now opens paper page`
- **Co-authoring:** When Claude Code makes changes, append:
  ```
  Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>
  ```

### Pre-flight Checks
- Before assuming pipeline issues are network-related, **check the pre-flight diagnostic step** in GitHub Actions logs
- If pre-flight passes but main ingest fails, it's likely an arXiv rate limit or subject-specific issue

### Logging & Observability
- Partial-coverage runs must surface loudly:
  ```
  [SUMMARY] N of M subjects succeeded
  [SUMMARY] Failed subjects: ...
  ```
- Cost tracking: each paper includes `cost_usd` field in JSON
- Use `make cost` to see projected monthly spend based on `data/` directory

### Code Style
- **Python:** Standard library style, type hints encouraged
- **Prompts:** Markdown plaintext, no code fences inside prompt files
- **Config:** YAML with comments explaining each field

## Running Locally

### First-time setup
```bash
# Install Python deps
pip3 install -r requirements.txt

# Install site deps
cd site && npm install

# Set API key
cp .env.example .env
# Edit .env and add your key
```

### Ingest a single subject
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
make ingest-subject SUBJECT=gr-qc
```

### Ingest all active subjects
```bash
make ingest
```

### Dev server
```bash
cd site && npm run dev
# Visit http://localhost:4321
```

### Verify environment
```bash
make verify
# Checks API key, arXiv connectivity, Anthropic API
```

## When Things Break

### "HTTP 429 from arXiv"
- Normal behavior under load
- Wait 2-5 minutes, try again
- Or reduce `daily_cap` in `config/subjects.yaml`

### "ANTHROPIC_API_KEY not set"
- Locally: check `.env` file exists and has valid key
- CI: verify GitHub Secret is set at repo settings → Secrets and variables → Actions

### "No new papers"
- Check if arXiv published today (no papers on Sat/Sun)
- Verify subject code is correct and active in `config/subjects.yaml`
- Check `data/subjects/{subject}/` — if recent date exists, papers were already ingested

### "Cloudflare Pages build failed"
- Check `site/dist/` was created (Astro build succeeded)
- Verify `astro.config.mjs` output directory is correct
- Check Cloudflare Pages dashboard for build logs

## Quick Reference

| Task | Command |
|------|---------|
| Ingest all active subjects | `make ingest` |
| Ingest one subject | `make ingest-subject SUBJECT=gr-qc` |
| Dry run (fetch only, no API) | `make ingest-dry` |
| Check environment | `make verify` |
| Start dev server | `cd site && npm run dev` |
| Build site | `cd site && npm run build` |
| Check cost estimate | `make cost` |

---

**Last updated:** 2026-05-23  
**Claude Code session:** Ready to assist with ingest pipeline, site features, or config changes.
