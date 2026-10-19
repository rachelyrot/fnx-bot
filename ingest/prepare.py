"""Stage A — data preparation: Loading -> Chunking -> Embedding -> VectorStoreIndex (Pinecone).

Incremental refresh: data/manifest.json keeps {page_id: hash}. Only new or
changed pages are (re)embedded; changed and removed pages are first deleted
from Pinecone by id prefix (chunk ids are "<page_id>#<n>").

Usage:
    uv run python -m ingest.prepare            # incremental
    uv run python -m ingest.prepare --rebuild  # wipe namespace and re-index everything
"""
from __future__ import annotations

import argparse
import json
import time

from llama_index.core import Document, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter

import config
from rag import settings

# Metadata kept on every chunk; only the first group is embedded / shown to the LLM.
EMBED_KEYS = ("title", "section")
HIDDEN_KEYS = ("url", "page_id", "crawled_at", "lastmod", "hash", "description")


# 1. Loading -----------------------------------------------------------------
def load_documents() -> list[Document]:
    docs = []
    for path in sorted(config.PAGES_DIR.glob("*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        docs.append(
            Document(
                id_=page["id"],
                text=page["markdown"],
                metadata={
                    "title": page["title"],
                    "section": page["section"],
                    "url": page["url"],
                    "page_id": page["id"],
                    "crawled_at": page["crawled_at"],
                    "lastmod": page.get("lastmod") or "",
                    "hash": page["hash"],
                    "description": page.get("description", ""),
                },
                excluded_embed_metadata_keys=list(HIDDEN_KEYS),
                excluded_llm_metadata_keys=[k for k in HIDDEN_KEYS if k != "url"],
            )
        )
    return docs


# 2. Chunking ----------------------------------------------------------------
def chunk(docs: list[Document]):
    """Split on Markdown headings first, then cap size with a sentence splitter."""
    by_heading = MarkdownNodeParser().get_nodes_from_documents(docs)
    nodes = SentenceSplitter(
        chunk_size=config.CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP
    ).get_nodes_from_documents(by_heading, show_progress=True)

    counters: dict[str, int] = {}
    for node in nodes:
        pid = node.metadata["page_id"]
        counters[pid] = counters.get(pid, 0) + 1
        node.id_ = f"{pid}#{counters[pid]}"  # prefix-deletable in Pinecone serverless
    # MarkdownNodeParser also adds "header_path" (e.g. "/ביטוח דירה/כיצד מטפלים/"),
    # which is not excluded, so it is embedded and shown to the LLM as context.
    return nodes


def load_manifest() -> dict[str, str]:
    if config.MANIFEST_PATH.exists():
        return json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", action="store_true", help="delete the whole namespace and re-index")
    args = ap.parse_args()

    docs = load_documents()
    print(f"loaded {len(docs)} pages")

    store = settings.vector_store(create=True)
    manifest = {} if args.rebuild else load_manifest()
    if args.rebuild:
        try:
            settings.pinecone_index().delete(delete_all=True, namespace=config.PINECONE_NAMESPACE)
        except Exception as e:  # namespace may not exist yet
            print(f"  (namespace wipe skipped: {e})")

    current = {d.id_: d.metadata["hash"] for d in docs}
    changed = [d for d in docs if manifest.get(d.id_) != d.metadata["hash"]]
    removed = [pid for pid in manifest if pid not in current]
    stale = [d.id_ for d in changed if d.id_ in manifest] + removed
    for pid in stale:
        store.delete(ref_doc_id=pid)
    print(f"new/changed: {len(changed)} | removed: {len(removed)} | unchanged: {len(docs) - len(changed)}")

    for pid in removed:
        manifest.pop(pid, None)
    save_manifest(manifest)

    if changed:
        nodes = chunk(changed)
        print(f"embedding {len(nodes)} chunks -> Pinecone '{config.PINECONE_INDEX}/{config.PINECONE_NAMESPACE}'")
        storage_context = StorageContext.from_defaults(vector_store=store)
        offset = 0
        for i, batch in enumerate(token_budget_batches(nodes), 1):
            started = time.monotonic()
            offset += len(batch)
            # 3-5. Embedding + VectorStoreIndex + Pinecone
            with_rate_limit_retry(
                lambda: VectorStoreIndex(batch, storage_context=storage_context, embed_model=settings.embed_model())
            )
            # Record pages whose chunks are all in. A page split across two batches
            # is recorded only once its last chunk is embedded.
            done_pages = {n.metadata["page_id"] for n in batch}
            pending = {n.metadata["page_id"] for n in nodes[offset:]}
            for pid in done_pages - pending:
                manifest[pid] = current[pid]
            save_manifest(manifest)
            print(f"  batch {i}: {len(batch)} chunks, {len(manifest)}/{len(docs)} pages indexed")
            time.sleep(max(0.0, 61 - (time.monotonic() - started)))  # stay under tokens/minute

    print("done")


def save_manifest(manifest: dict[str, str]) -> None:
    config.MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def token_budget_batches(nodes, max_chars: int = config.EMBED_CHARS_PER_MINUTE):
    """Group chunks so each group stays under the per-minute embedding budget."""
    batch, size = [], 0
    for node in nodes:
        n = len(node.get_content(metadata_mode="embed"))
        if batch and size + n > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(node)
        size += n
    if batch:
        yield batch


def with_rate_limit_retry(fn, attempts: int = 5):
    from cohere.errors import TooManyRequestsError

    for i in range(attempts):
        try:
            return fn()
        except TooManyRequestsError:
            if i == attempts - 1:
                raise
            print("  rate limited by Cohere, waiting 65s")
            time.sleep(65)


if __name__ == "__main__":
    main()
