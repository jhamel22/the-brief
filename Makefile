.PHONY: setup verify ingest ingest-dry site clean

# ── Setup ──────────────────────────────────────────────────
setup:
	pip3 install -r requirements.txt
	@echo "✓ Python deps installed"
	@cd site && npm install && echo "✓ Node deps installed"

# ── Verify environment before first deploy ─────────────────
verify:
	@echo "Checking environment..."
	@/usr/bin/python3 -c "import anthropic, feedparser, yaml; print('✓ Python packages OK')"
	@/usr/bin/python3 -c "import os; key=os.environ.get('ANTHROPIC_API_KEY',''); \
		assert key.startswith('sk-ant-'), 'ANTHROPIC_API_KEY not set or invalid'" \
		&& echo "✓ API key present"
	@/usr/bin/python3 -c "\
import feedparser; \
f = feedparser.parse('https://rss.arxiv.org/rss/gr-qc'); \
n = len(f.entries); \
assert n > 0, 'RSS feed returned 0 entries'; \
print(f'✓ arXiv RSS OK ({n} papers in latest batch)')"
	@/usr/bin/python3 -c "\
import anthropic, os; \
c = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY']); \
r = c.messages.create(model='claude-haiku-4-5-20251001', max_tokens=5, messages=[{'role':'user','content':'Hi'}]); \
print('✓ Anthropic API OK')"
	@echo ""
	@echo "All systems go. Ready to deploy."

# ── Ingest ─────────────────────────────────────────────────
ingest:
	/usr/bin/python3 ingest/run.py

ingest-dry:
	/usr/bin/python3 ingest/run.py --dry-run

ingest-subject:
	@test -n "$(SUBJECT)" || (echo "Usage: make ingest-subject SUBJECT=gr-qc" && exit 1)
	/usr/bin/python3 ingest/run.py --subject $(SUBJECT)

# ── Site ───────────────────────────────────────────────────
site:
	cd site && npm run dev

site-build:
	cd site && npm run build

# ── Utilities ──────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -name "*.pyc" -delete 2>/dev/null; \
	rm -rf site/dist site/.astro; \
	echo "✓ Clean"

# Show estimated monthly cost based on data/ directory
cost:
	@/usr/bin/python3 scripts/cost.py
