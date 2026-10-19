"""Stage C — Structured Data Extraction -> data/structured.json.

Semantic search returns the top few chunks; it cannot guarantee a complete list
("all the ways to contact", "every claim type for car insurance") or an exact
detail buried in one page. So we extract a thin structured layer from the
procedural pages (claims + customer service) with Claude structured output.

Item types:
  claim_procedures — how to file a claim for a product / event
  service_actions  — self-service actions (cancel, add mortgagor, renew, update card, ...)
  contacts         — phone / WhatsApp / email / hours of a department

Usage:
    uv run python -m ingest.extract            # only pages whose hash changed
    uv run python -m ingest.extract --force
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from llama_index.core import PromptTemplate
from pydantic import BaseModel, Field

import config
from rag import settings

SCHEMA_VERSION = "1.0"
EXTRACT_SECTIONS = {"claims-customer-service", "customer-service"}
SKIP_URL_PARTS = ("class-actions", "terms-of-use", "former_settle")
MAX_PAGE_CHARS = 12_000
CONCURRENCY = 4


# --- Schema --------------------------------------------------------------------
class ClaimProcedure(BaseModel):
    product: str = Field(description="המוצר, למשל 'ביטוח דירה', 'ביטוח רכב', 'ביטוח בריאות'")
    claim_type: str = Field(description="סוג המקרה/התביעה, למשל 'נזקי צנרת', 'תאונה ברכב', 'מחלה קשה'")
    steps: list[str] = Field(default_factory=list, description="שלבי ההגשה כפי שמופיעים בעמוד")
    required_documents: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list, description="איפה מגישים: אתר, אפליקציה, טלפון, וואטסאפ, סוכן")
    stated_timeframe: Optional[str] = Field(None, description="זמן טיפול או מועד הגשה אם כתוב במפורש בעמוד")


class ServiceAction(BaseModel):
    product: str
    action: str = Field(description="הפעולה, למשל 'ביטול ביטוח', 'הוספת משעבד', 'חידוש', 'עדכון פרטי אשראי'")
    how: str = Field(description="איך מבצעים, בקצרה, לפי העמוד")
    channels: list[str] = Field(default_factory=list)


class ContactPoint(BaseModel):
    department: str = Field(description="המחלקה/השירות, למשל 'מוקד תביעות רכב', 'שירות לקוחות'")
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    email: Optional[str] = None
    hours: Optional[str] = None


class PageExtraction(BaseModel):
    """Everything extractable from one page. Empty lists when the page has none."""

    claim_procedures: list[ClaimProcedure] = Field(default_factory=list)
    service_actions: list[ServiceAction] = Field(default_factory=list)
    contacts: list[ContactPoint] = Field(default_factory=list)


EXTRACT_PROMPT = PromptTemplate(
    """\
חלץ מידע מובנה מעמוד באתר חברת הביטוח הפניקס.
חוקים: חלץ רק מה שכתוב במפורש בעמוד. אל תשלים, אל תנחש. אם אין פריט מסוג מסוים — רשימה ריקה.
טלפונים, מיילים וזמני טיפול — העתק בדיוק כפי שהם כתובים.

כותרת: {title}
כתובת: {url}
תוכן העמוד:
---------------------
{markdown}
---------------------"""
)


def eligible(page: dict) -> bool:
    return page["section"] in EXTRACT_SECTIONS and not any(p in page["url"] for p in SKIP_URL_PARTS)


async def extract_page(page: dict, sem: asyncio.Semaphore) -> PageExtraction:
    async with sem:
        return await settings.llm().astructured_predict(
            PageExtraction,
            EXTRACT_PROMPT,
            title=page["title"],
            url=page["url"],
            markdown=page["markdown"][:MAX_PAGE_CHARS],
        )


def load_existing() -> dict:
    if config.STRUCTURED_PATH.exists():
        return json.loads(config.STRUCTURED_PATH.read_text(encoding="utf-8"))
    return {"sources": [], "items": {}}


async def run(force: bool) -> None:
    pages = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(config.PAGES_DIR.glob("*.json"))]
    pages = [p for p in pages if eligible(p)]

    existing = load_existing()
    known_hash = {s["page_id"]: s["hash"] for s in existing["sources"]}
    todo = [p for p in pages if force or known_hash.get(p["id"]) != p["hash"]]
    keep_ids = {p["id"] for p in pages} - {p["id"] for p in todo}
    print(f"eligible pages: {len(pages)} | to extract: {len(todo)}")

    # Keep items of unchanged pages, re-extract the rest.
    items: dict[str, list] = {k: [i for i in v if i["source"]["page_id"] in keep_ids]
                              for k, v in existing["items"].items()}
    for key in PageExtraction.model_fields:
        items.setdefault(key, [])

    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(extract_page(p, sem) for p in todo), return_exceptions=True)
    for page, res in zip(todo, results):
        if isinstance(res, Exception):
            print(f"  failed {page['url']}: {res}")
            keep_ids.discard(page["id"])
            continue
        source = {"page_id": page["id"], "url": page["url"], "title": page["title"]}
        for key in PageExtraction.model_fields:
            for obj in getattr(res, key):
                items[key].append({**obj.model_dump(), "source": source, "observed_at": page["crawled_at"]})
        keep_ids.add(page["id"])

    # Stable ids per type
    prefixes = {"claim_procedures": "claim", "service_actions": "action", "contacts": "contact"}
    for key, lst in items.items():
        for n, item in enumerate(lst, 1):
            item["id"] = f"{prefixes[key]}-{n:03d}"

    out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [
            {"page_id": p["id"], "url": p["url"], "hash": p["hash"], "crawled_at": p["crawled_at"]}
            for p in pages if p["id"] in keep_ids
        ],
        "items": items,
    }
    config.STRUCTURED_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("items: " + ", ".join(f"{k}={len(v)}" for k, v in items.items()))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    asyncio.run(run(ap.parse_args().force))


if __name__ == "__main__":
    main()
