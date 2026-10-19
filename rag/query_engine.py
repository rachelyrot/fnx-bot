"""Stage A — querying: Retrieve -> Postprocess -> Synthesize.

    uv run python -m rag.query_engine "יש לי נזילה בבית, איך מגישים תביעה?"
"""
from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import lru_cache

from llama_index.core import get_response_synthesizer
from llama_index.core.base.response.schema import AsyncStreamingResponse
from llama_index.core.postprocessor import SimilarityPostprocessor
from llama_index.core.schema import NodeWithScore

import config
from rag import settings
from rag.prompts import NO_INFO_ANSWER, QA_TEMPLATE, REFINE_TEMPLATE


@dataclass
class Answer:
    text: str
    sources: list[dict] = field(default_factory=list)  # [{url, title, score}]
    nodes: list[NodeWithScore] = field(default_factory=list)


# 1. Retrieve ------------------------------------------------------------------
def retrieve(query: str, top_k: int = config.RETRIEVE_TOP_K) -> list[NodeWithScore]:
    retriever = settings.vector_index().as_retriever(similarity_top_k=top_k)
    return retriever.retrieve(query)


# 2. Postprocess -----------------------------------------------------------------
def postprocess(query: str, nodes: list[NodeWithScore]) -> list[NodeWithScore]:
    """Drop weak vector matches, rerank with Cohere, drop weak rerank matches."""
    nodes = SimilarityPostprocessor(similarity_cutoff=config.SIMILARITY_CUTOFF).postprocess_nodes(nodes)
    if not nodes:
        return []
    try:
        # top_n = all candidates: the cutoff and diversify() below decide what stays
        reranked = settings.reranker(config.RETRIEVE_TOP_K).postprocess_nodes(nodes, query_str=query)
        nodes = SimilarityPostprocessor(similarity_cutoff=config.RERANK_CUTOFF).postprocess_nodes(reranked)
    except Exception as e:  # e.g. trial-key rate limit: answer now from vector ranking, don't stall
        print(f"rerank unavailable ({type(e).__name__}); using vector ranking", file=sys.stderr)
        nodes = SimilarityPostprocessor(
            similarity_cutoff=config.VECTOR_FALLBACK_CUTOFF
        ).postprocess_nodes(nodes)
    return diversify(nodes)


def diversify(nodes: list[NodeWithScore]) -> list[NodeWithScore]:
    """Keep the best-ranked chunks, at most MAX_CHUNKS_PER_PAGE per page, up to RERANK_TOP_N."""
    per_page: dict[str, int] = {}
    out = []
    for n in nodes:  # already sorted by rerank score
        url = n.node.metadata.get("url", "")
        if per_page.get(url, 0) >= config.MAX_CHUNKS_PER_PAGE:
            continue
        per_page[url] = per_page.get(url, 0) + 1
        out.append(n)
        if len(out) == config.RERANK_TOP_N:
            break
    return out


# 3. Synthesize ------------------------------------------------------------------
@lru_cache
def synthesizer(streaming: bool = False):
    return get_response_synthesizer(
        llm=settings.llm(),
        response_mode="compact",
        text_qa_template=QA_TEMPLATE,
        refine_template=REFINE_TEMPLATE,
        streaming=streaming,
    )


def synthesize(query: str, nodes: list[NodeWithScore]) -> Answer:
    if not nodes:  # nothing relevant -> do not let the LLM improvise
        return Answer(NO_INFO_ANSWER)
    response = synthesizer().synthesize(query, nodes=nodes)
    return Answer(text=str(response).strip(), sources=sources_of(nodes), nodes=nodes)


RESTART = object()  # yielded by astream_synthesize: discard what was streamed, the full answer follows


async def astream_synthesize(query: str, nodes: list[NodeWithScore]) -> AsyncIterator[str | object]:
    """Same synthesis, yielding text deltas as the LLM produces them.

    Behind NetFree about 1 in 3 streamed answers arrives corrupted (a broken Hebrew
    character, then an event with no data -> JSONDecodeError); non-streamed answers
    arrive intact. On a broken stream: yield RESTART, then the whole answer in one piece."""
    if not nodes:
        yield NO_INFO_ANSWER
        return
    try:
        async for delta in _stream(query, nodes):
            yield delta
    except Exception as e:
        print(f"answer stream broken ({type(e).__name__}); fetching it in one piece", file=sys.stderr)
        yield RESTART
        yield str(await synthesizer().asynthesize(query, nodes=nodes)).strip()


async def _stream(query: str, nodes: list[NodeWithScore]) -> AsyncIterator[str]:
    response = await synthesizer(streaming=True).asynthesize(query, nodes=nodes)
    if isinstance(response, AsyncStreamingResponse):
        async for delta in response.async_response_gen():
            yield delta
        return
    # Some LLM integrations only stream synchronously: pull the generator in a worker thread.
    gen = iter(response.response_gen)
    done = object()
    while (delta := await asyncio.to_thread(next, gen, done)) is not done:
        yield delta


def sources_of(nodes: list[NodeWithScore], answer: str = "") -> list[dict]:
    """One entry per page. With `answer`, each entry also gets `quote`: the sentence on that
    page that best supports the answer (the UI highlights it on the live site)."""
    seen, out = set(), []
    for n in nodes:
        url = n.node.metadata.get("url")
        if url and url not in seen:
            seen.add(url)
            src = {"url": url, "title": n.node.metadata.get("title", ""), "score": round(n.score or 0, 3)}
            if answer:
                chunks = [m.node.get_content() for m in nodes if m.node.metadata.get("url") == url]
                src["quote"] = best_quote(chunks, answer)
            out.append(src)
    return out


SENTENCE_SPLIT = re.compile(r"(?<=[.!?:])\s+|\n+")
MARKDOWN = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)|[*_#>`|]+")
LIST_MARK = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+")
WORD = re.compile(r"\w{2,}")
MIN_SHARED_WORDS = 3


def best_quote(chunks: list[str], answer: str) -> str | None:
    """The chunk sentence sharing the most words with the answer (normalised for length),
    as plain text the way it reads on the page; None when nothing clearly matches."""
    answer_words = set(WORD.findall(answer))
    best, best_score = None, 0.0
    for chunk in chunks:
        for raw in SENTENCE_SPLIT.split(chunk):
            sentence = " ".join(LIST_MARK.sub("", MARKDOWN.sub(lambda m: m.group(1) or "", raw)).split())
            words = set(WORD.findall(sentence))
            shared = len(words & answer_words)
            if shared < MIN_SHARED_WORDS or not 5 <= len(words) <= 60:
                continue
            score = shared / len(words) ** 0.5
            if score > best_score:
                best, best_score = sentence, score
    return best


def answer(query: str) -> Answer:
    return synthesize(query, postprocess(query, retrieve(query)))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = answer(" ".join(sys.argv[1:]) or "איך מגישים תביעה על נזקי מים?")
    print(a.text)
    for s in a.sources:
        print(f"  [{s['score']}] {s['title']} — {s['url']}")
