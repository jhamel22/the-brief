"""
ingest/summarize.py

Calls the Anthropic API to generate a relatable title, hook, summary,
and lead classification for each paper. Includes JSON extraction,
retry logic, and per-paper cost tracking.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import anthropic

# ── Models & pricing ──────────────────────────────────────────────────────

SONNET = "claude-sonnet-4-6"   # primary — quality summaries
HAIKU  = "claude-haiku-4-5-20251001"  # filter step — cheap abstract ranking

# USD per million tokens
_COSTS = {
    SONNET: {"input": 3.00,  "output": 15.00},
    HAIKU:  {"input": 0.80,  "output":  4.00},
}

PROMPTS_DIR = Path(__file__).parent / "prompts"


# ── Public API ────────────────────────────────────────────────────────────

def summarize_paper(
    paper: dict,
    prompt_variant: str,
    client: anthropic.Anthropic,
    model: str = SONNET,
    max_retries: int = 3,
) -> Optional[dict]:
    """
    Summarize a single paper. Returns the paper dict enriched with
    title_relatable, hook, summary, is_lead, and token/cost metadata.
    Returns None if summarization fails after all retries.
    """
    system_prompt = _load_prompt(prompt_variant)
    user_content = _format_input(paper)

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=400,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )

            text = response.content[0].text.strip()
            parsed = _extract_json(text)

            if parsed and _is_valid(parsed):
                tokens_in  = response.usage.input_tokens
                tokens_out = response.usage.output_tokens
                return {
                    **paper,
                    "title_relatable": parsed["title_relatable"].strip(),
                    "hook":    parsed["hook"].strip(),
                    "summary": parsed["summary"].strip(),
                    "is_lead": bool(parsed.get("is_lead", False)),
                    "model":          model,
                    "prompt_variant": prompt_variant,
                    "tokens_in":  tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd":   _estimate_cost(tokens_in, tokens_out, model),
                }

            print(f"    [WARN] JSON parse failed (attempt {attempt + 1}/{max_retries})")

        except anthropic.RateLimitError:
            wait = 10 * (2 ** attempt)
            print(f"    [WAIT] Rate limited — sleeping {wait}s")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                wait = 5 * (attempt + 1)
                print(f"    [WAIT] Server error {e.status_code} — retrying in {wait}s")
                time.sleep(wait)
            else:
                print(f"    [ERROR] API error {e.status_code}: {e.message}")
                return None

        except anthropic.APIConnectionError as e:
            # Network-level connection errors — retry with exponential backoff
            if attempt < max_retries - 1:
                wait = 5 * (3 ** attempt)  # 5s, 15s, 45s
                print(f"    [WAIT] Connection error — retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                print(f"    [ERROR] Connection failed after {max_retries} attempts: {e}")
                return None

        except Exception as e:
            print(f"    [ERROR] Unexpected: {e}")
            return None

    print(f"    [FAIL] {paper['id']} — gave up after {max_retries} attempts")
    return None


def rank_papers(
    papers: list[dict],
    prompt_variant: str,
    client: anthropic.Anthropic,
    top_n: int = 20,
) -> list[dict]:
    """
    Cheap pre-ranking for high-volume subjects (cs.LG etc.).
    Uses Haiku to score each abstract and returns the top_n most interesting.
    Only used when subjects.yaml sets use_filter: true.
    """
    if len(papers) <= top_n:
        return papers

    system = (
        "You are a research editor. Given a paper title and abstract, "
        "output a single integer from 1 (narrow/incremental) to 10 (broad impact/striking result). "
        "Output only the integer, nothing else."
    )

    scored = []
    for paper in papers:
        score = 5  # default mid-score
        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=HAIKU,
                    max_tokens=5,
                    system=system,
                    messages=[{
                        "role": "user",
                        "content": f"Title: {paper['title_original']}\nAbstract: {paper['abstract'][:500]}"
                    }],
                )
                score_text = response.content[0].text.strip()
                score = int(re.search(r"\d+", score_text).group())
                break
            except anthropic.APIConnectionError:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))  # 2s, 4s
                    continue
            except Exception:
                break  # give up on other errors

        scored.append((score, paper))
        time.sleep(0.1)

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:top_n]]


# ── Helpers ───────────────────────────────────────────────────────────────

def _load_prompt(variant: str) -> str:
    path = PROMPTS_DIR / f"{variant}.txt"
    if not path.exists():
        path = PROMPTS_DIR / "physics.txt"  # safe fallback
    return path.read_text().strip()


def _format_input(paper: dict) -> str:
    return (
        f"Title: {paper['title_original']}\n"
        f"Authors: {paper['authors']}\n"
        f"Subject: {paper['subject']}\n"
        f"Abstract: {paper['abstract']}"
    )


def _extract_json(text: str) -> Optional[dict]:
    """Try several strategies to extract valid JSON from Claude's response."""

    # Strategy 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown fences
    stripped = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Strategy 3: find the outermost {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _is_valid(obj: dict) -> bool:
    required = {"title_relatable", "hook", "summary"}
    return required.issubset(obj.keys()) and all(
        isinstance(obj[k], str) and len(obj[k]) > 5 for k in required
    )


def _estimate_cost(tokens_in: int, tokens_out: int, model: str) -> float:
    rates = _COSTS.get(model, _COSTS[SONNET])
    return (tokens_in * rates["input"] + tokens_out * rates["output"]) / 1_000_000
