"""
Compare trimmed prompt outputs against existing on-disk summaries.

For each sample paper:
  - Read the existing summary (generated with the old prompt)
  - Call the API with the CURRENT prompt file (the trimmed version)
  - Print side-by-side for visual quality check
  - Print token usage so we can confirm the input-token reduction
"""

import json
import os
import sys
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "ingest"))

from summarize import _load_prompt, _format_input, _extract_json, SONNET, HAIKU

SAMPLES = {
    "physics": ("2605.14956", SONNET),  # gr-qc
    "cs":      ("2605.16087", HAIKU),   # cs.RO
    "bio":     ("2605.19333", HAIKU),   # q-bio
}


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return 1
    client = anthropic.Anthropic(api_key=api_key)

    for variant, (paper_id, model) in SAMPLES.items():
        paper_path = ROOT / "data" / "papers" / f"{paper_id}.json"
        if not paper_path.exists():
            print(f"\n[{variant}] {paper_id}: SKIP (not on disk)")
            continue

        with open(paper_path) as f:
            paper = json.load(f)

        print(f"\n{'═' * 72}")
        print(f"[{variant}] {paper_id} (model: {model})")
        print(f"Title: {paper['title_original'][:80]}")
        print(f"{'═' * 72}")

        system_prompt = _load_prompt(variant)
        user_content = _format_input(paper)

        response = client.messages.create(
            model=model,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

        text = response.content[0].text.strip()
        new = _extract_json(text)
        if not new:
            print("ERROR: could not parse JSON from response")
            print(text)
            continue

        print(f"\n── OLD (on disk; old prompt) ──")
        print(f"  title_relatable: {paper['title_relatable']}")
        print(f"  hook:    {paper['hook']}")
        print(f"  summary: {paper['summary']}")
        print(f"  is_lead: {paper['is_lead']}")
        print(f"  tokens:  in={paper['tokens_in']}  out={paper['tokens_out']}")

        print(f"\n── NEW (trimmed prompt, just generated) ──")
        print(f"  title_relatable: {new.get('title_relatable')}")
        print(f"  hook:    {new.get('hook')}")
        print(f"  summary: {new.get('summary')}")
        print(f"  is_lead: {new.get('is_lead')}")
        print(f"  tokens:  in={response.usage.input_tokens}  out={response.usage.output_tokens}")
        print(f"  Δ input: {response.usage.input_tokens - paper['tokens_in']:+d} tokens")

    return 0


if __name__ == "__main__":
    sys.exit(main())
