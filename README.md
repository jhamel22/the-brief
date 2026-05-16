# The Brief

A daily digest of new arXiv preprints — relatable headlines, plain-English hooks, AI-generated summaries. Built to scale from one subject to the full arXiv catalog.

**Stack:** Python ingest → GitHub Actions cron → Astro static site → Cloudflare Pages

---

## How it works

1. GitHub Actions runs the ingest script daily at 4:30 PM ET (after arXiv's ~2:00 PM announcement)
2. The script fetches new papers from arXiv RSS, summarizes them with Claude, and commits the JSON files back to the repo
3. Cloudflare Pages detects the commit and rebuilds the static site in ~30 seconds
4. No server, no database, no uptime to babysit

**Cost at launch (gr-qc only):** ~$4/month at Sonnet 4.6 rates (~$0.007/paper × 20 papers/day × 30 days). The `daily_cap` in `config/subjects.yaml` is the cost control lever.

---

## Deploy in ~15 minutes

### Prerequisites
- Python 3.11+, Node 20+
- An [Anthropic API key](https://console.anthropic.com/settings/keys) with a few dollars of credit
- A [GitHub account](https://github.com/signup)
- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (free)

---

### 1 — Verify locally (5 min)

```bash
cp .env.example .env
# Edit .env and paste your sk-ant-... key

export $(cat .env | xargs)
make setup
make verify
```

`make verify` checks Python packages, your API key, arXiv RSS connectivity, and a live Claude ping. If it prints **"All systems go"** — deployment will work. Fix any errors before continuing.

---

### 2 — Push to GitHub (2 min)

```bash
git init && git add . && git commit -m "initial"

# With GitHub CLI:
gh repo create the-brief --public --source=. --push

# Or create the repo at github.com, then:
# git remote add origin https://github.com/YOUR_USERNAME/the-brief.git
# git push -u origin main
```

---

### 3 — Add your API key as a repo secret (1 min)

GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | your `sk-ant-...` key |

---

### 4 — ⚠️ Enable workflow write permissions (30 sec — easy to miss)

GitHub repo → **Settings** → **Actions** → **General** → scroll to **Workflow permissions** → select **"Read and write permissions"** → **Save**

Without this, the cron job summarizes papers but silently fails to commit them back. The workflow YAML requests `contents: write` but the repository setting must allow it.

---

### 5 — Trigger the first ingest (2 min)

GitHub repo → **Actions** tab → **Ingest arXiv** → **Run workflow**

Watch the logs. A successful run ends with a commit like `ingest: 2026-05-16`. The `data/` directory will now have JSON files.

---

### 6 — Connect Cloudflare Pages (5 min)

Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** → **Connect to Git** → select your repo

| Setting | Value |
|---------|-------|
| Framework preset | **Astro** |
| Build command | `npm run build` |
| Build output directory | `dist` |
| Root directory | `site` |

Under **Environment variables**:

| Variable | Value |
|----------|-------|
| `NODE_VERSION` | `20` |

Click **Save and Deploy**. Your site will be live at `https://the-brief-XXX.pages.dev`.

---

## Adding a subject

Open `config/subjects.yaml` and set `active: true` on any subject. Push the change — the next cron run will begin ingesting it. Each additional subject adds ~$3-7/month depending on `daily_cap`.

```yaml
- code: hep-th
  name: High Energy Physics - Theory
  domain: Physics
  slug: hep-th
  daily_cap: 20
  prompt_variant: physics
  active: true   # ← flip this
```

High-volume subjects (cs.LG, cs.AI) already have `use_filter: true` in the config, which enables a cheap Haiku pre-ranking step before the Sonnet summarization — keeping costs flat regardless of how many papers arXiv announces.

---

## Local development

```bash
# Ingest one subject manually
make ingest-subject SUBJECT=gr-qc

# Dry run (fetch RSS, no API calls)
make ingest-dry

# Run the site locally
make site

# Show estimated monthly cost from ingested data
make cost
```

---

## Repo structure

```
the-brief/
├── config/
│   └── subjects.yaml       ← all subject config, active flags, daily caps
├── ingest/
│   ├── arxiv.py            ← arXiv RSS fetcher
│   ├── summarize.py        ← Claude API + cost tracking
│   ├── run.py              ← daily orchestrator
│   └── prompts/            ← per-domain prompt variants
│       ├── physics.txt
│       ├── cs.txt
│       └── math.txt
├── data/
│   ├── papers/             ← one JSON file per paper (committed by bot)
│   └── subjects/           ← per-subject date indexes (committed by bot)
├── site/                   ← Astro static site
│   └── src/
│       ├── lib/data.js     ← reads data/ at build time
│       └── pages/
│           ├── index.astro          ← homepage / subject selector
│           └── [subject]/index.astro ← full interactive Brief page
└── .github/workflows/
    └── ingest.yml          ← daily cron + manual trigger
```

---

## Troubleshooting

**The cron runs but no files are committed**
→ Check step 4 (workflow write permissions). This is the most common issue.

**`make verify` fails on arXiv RSS**
→ arXiv doesn't publish on weekends. Run verify on a weekday, or check https://status.arxiv.org.

**Claude returns invalid JSON**
→ The summarizer retries 3 times and logs failures. A handful of failures per run is normal. If all papers fail, check your API key balance.

**Cloudflare build fails**
→ Confirm `Root directory` is set to `site` (not the repo root). Confirm `NODE_VERSION=20` is in the environment variables.
