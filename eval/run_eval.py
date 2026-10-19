"""Evaluate the bot on eval/questions.yaml.

    uv run python -m eval.run_eval --retrieval-only   # Cohere + Pinecone only: is the right page found?
    uv run python -m eval.run_eval                    # full workflow: answer / refuse / referral + citation

Writes eval/results.md.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import yaml

QUESTIONS = Path(__file__).with_name("questions.yaml")
RESULTS = Path(__file__).with_name("results.md")

ANSWER_ROUTES = {"semantic", "structured", "personal_policy"}
REFERRAL_ROUTES = {"personal_policy", "advice", "off_topic"}
ALLOWED_ROUTES = {
    "answer": ANSWER_ROUTES,
    "refuse": {"no_info"},
    "referral": REFERRAL_ROUTES,
    "answer_or_refuse": ANSWER_ROUTES | {"no_info"},
    "refuse_or_referral": REFERRAL_ROUTES | {"no_info"},
}


def url_hit(urls: list[str], expected: list[str]) -> bool:
    decoded = [unquote(u) for u in urls]
    return any(e in u for e in expected for u in decoded)


def retrieval_only(items: list[dict], delay: float) -> list[dict]:
    from rag import query_engine as qe

    rows = []
    for it in items:
        if not it.get("expected_urls") or it.get("follow_up"):
            continue  # follow-ups need conversation context; refusals have no target page
        time.sleep(delay)
        nodes = qe.postprocess(it["q"], qe.retrieve(it["q"]))
        urls = [s["url"] for s in qe.sources_of(nodes)]
        rows.append({"id": it["id"], "q": it["q"], "ok": url_hit(urls, it["expected_urls"]),
                     "detail": ", ".join(unquote(u).split("fnx.co.il/")[-1] for u in urls[:3]) or "—"})
    return rows


async def full(items: list[dict], delay: float) -> list[dict]:
    from rag.workflow import ask

    rows, history = [], []
    for it in items:
        if not it.get("follow_up"):
            history = []
        await asyncio.sleep(delay)
        out = await ask(it["q"], history)
        cited = [s["url"] for s in out["sources"] if s["url"] in out["text"]]
        route_ok = out["route"] in ALLOWED_ROUTES[it["expect"]]
        page_ok = True
        if it["expect"] == "answer" and it.get("expected_urls"):
            page_ok = url_hit(cited or [s["url"] for s in out["sources"]], it["expected_urls"])
        rows.append({"id": it["id"], "q": it["q"], "ok": route_ok and page_ok,
                     "detail": f"{out['route']} | {out['text'][:140].replace(chr(10), ' ')}"})
        history += [{"role": "user", "content": it["q"]}, {"role": "assistant", "content": out["text"]}]
    return rows


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--delay", type=float, default=7.0, help="seconds between questions (Cohere trial: 10 rerank calls/min)")
    args = ap.parse_args()

    items = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    rows = retrieval_only(items, args.delay) if args.retrieval_only else asyncio.run(full(items, args.delay))
    passed = sum(r["ok"] for r in rows)
    mode = "retrieval-only (right page in reranked top results)" if args.retrieval_only else "full workflow"

    lines = [f"# Eval — {mode}", "", f"**{passed}/{len(rows)} passed**", "",
             "| | id | question | detail |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {'✅' if r['ok'] else '❌'} | {r['id']} | {r['q']} | {r['detail']} |")
    RESULTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{passed}/{len(rows)} passed — see {RESULTS}")


if __name__ == "__main__":
    main()
