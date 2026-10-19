"""Shared model / vector-store factories used by ingestion and querying."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

import config

load_dotenv()

# NetFree: same as the course starter. Set NETFREE_UNSTRICT_SSL=0 to disable (render.yaml does).
if os.getenv("NETFREE_UNSTRICT_SSL", "1") != "0":
    from netfree_unstrict_ssl import unstrict_ssl

    unstrict_ssl()


def require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()  # a pasted key often carries a trailing newline
    if not value:
        raise RuntimeError(f"Missing {name} — copy .env.example to .env and fill it in")
    return value


@lru_cache
def embed_model():
    from llama_index.embeddings.cohere import CohereEmbedding

    # input_type left unset: search_document for nodes, search_query for queries
    return CohereEmbedding(
        api_key=require_env("COHERE_API_KEY"),
        model_name=config.EMBED_MODEL,
        embed_batch_size=96,  # Cohere's per-call max; keeps trial-key call counts low
    )


@lru_cache
def llm(fast: bool = False):
    if not os.getenv("ANTHROPIC_API_KEY"):
        # Temporary fallback until a Claude key is available: Cohere Command A (supports Hebrew).
        return _cohere_chat_llm(config.LLM_FALLBACK_FAST if fast else config.LLM_FALLBACK)

    from llama_index.llms.anthropic import Anthropic

    # Both models think before answering by default; for short grounded answers and one-word
    # labels that only adds latency (measured: Sonnet first token ~6s -> ~1s, Haiku ~0.9s -> ~0.65s).
    # Sonnet 5.5 rejects {"type": "disabled"}; "between_tools" is its thinking-off mode.
    return Anthropic(
        model=config.LLM_FAST if fast else config.LLM_MAIN,
        api_key=require_env("ANTHROPIC_API_KEY"),
        temperature=0.0,
        max_tokens=1024,
        thinking_dict={"type": "disabled"} if fast else {"type": "between_tools"},
    )


def _cohere_chat_llm(model: str):
    """llama-index-llms-cohere routes complete() to Cohere's removed Generate API;
    send completions through the Chat API instead."""
    from llama_index.core.base.llms.types import ChatMessage, CompletionResponse, LLMMetadata
    from llama_index.llms.cohere import Cohere

    class CohereChat(Cohere):
        @property
        def metadata(self) -> LLMMetadata:
            return super().metadata.model_copy(update={"is_chat_model": True})

        def complete(self, prompt: str, formatted: bool = False, **kwargs) -> CompletionResponse:
            resp = self.chat([ChatMessage(role="user", content=prompt)], **kwargs)
            return CompletionResponse(text=resp.message.content or "", raw=resp.raw)

        async def acomplete(self, prompt: str, formatted: bool = False, **kwargs) -> CompletionResponse:
            resp = await self.achat([ChatMessage(role="user", content=prompt)], **kwargs)
            return CompletionResponse(text=resp.message.content or "", raw=resp.raw)

        def stream_chat(self, messages, **kwargs):
            # The integration buffers the whole reply before yielding; stream from Cohere's v2 API directly.
            import cohere
            from llama_index.core.base.llms.types import ChatResponse

            client = cohere.ClientV2(api_key=require_env("COHERE_API_KEY"))
            payload = [{"role": m.role.value, "content": m.content or ""} for m in messages]
            text = ""
            for ev in client.chat_stream(model=self.model, messages=payload,
                                         temperature=self.temperature, max_tokens=self.max_tokens):
                if ev.type == "content-delta":
                    delta = ev.delta.message.content.text
                    text += delta
                    yield ChatResponse(message=ChatMessage(role="assistant", content=text), delta=delta)

        async def astream_chat(self, messages, **kwargs):
            # The integration's astream_chat hands back a sync generator; wrap the sync stream instead.
            import asyncio

            gen = iter(self.stream_chat(messages, **kwargs))
            done = object()

            async def agen():
                while (chunk := await asyncio.to_thread(next, gen, done)) is not done:
                    yield chunk

            return agen()

    return CohereChat(model=model, api_key=require_env("COHERE_API_KEY"),
                      temperature=0.0, max_tokens=1024)


@lru_cache
def pinecone_index(create: bool = False):
    from pinecone import Pinecone, ServerlessSpec

    pc = Pinecone(api_key=require_env("PINECONE_API_KEY"))
    if not pc.has_index(config.PINECONE_INDEX):
        if not create:
            raise RuntimeError(f"Pinecone index '{config.PINECONE_INDEX}' not found — run ingest.prepare first")
        pc.create_index(
            name=config.PINECONE_INDEX,
            dimension=config.EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
    return pc.Index(config.PINECONE_INDEX)


def vector_store(create: bool = False):
    from llama_index.vector_stores.pinecone import PineconeVectorStore

    return PineconeVectorStore(pinecone_index=pinecone_index(create), namespace=config.PINECONE_NAMESPACE)


@lru_cache
def vector_index():
    """VectorStoreIndex over the existing Pinecone namespace (query time). Built once per process."""
    from llama_index.core import VectorStoreIndex

    return VectorStoreIndex.from_vector_store(vector_store(), embed_model=embed_model())


@lru_cache
def reranker(top_n: int):
    from llama_index.postprocessor.cohere_rerank import CohereRerank

    # max_retries=0: on a rate limit fail fast (query_engine falls back to vector ranking)
    return CohereRerank(model=config.RERANK_MODEL, top_n=top_n, api_key=require_env("COHERE_API_KEY"),
                        max_retries=0)


def warm_up() -> None:
    """Create every client up front so the first customer question is not the slow one."""
    llm(fast=True)
    llm()
    vector_index().as_retriever(similarity_top_k=1).retrieve("ביטוח")
