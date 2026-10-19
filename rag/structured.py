"""Stage C — structured lookup over data/structured.json.

Claude turns the question into a query against the schema (StructuredQuery),
we run it in Python, and Claude writes the final answer from the matching items
(each item carries its source URL, so citations stay exact).
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal, Optional

from llama_index.core import PromptTemplate
from pydantic import BaseModel, Field

import config
from rag import settings
from rag.prompts import NO_INFO_ANSWER

MAX_ITEMS_TO_LLM = 25


class StructuredQuery(BaseModel):
    item_type: Literal["claim_procedures", "service_actions", "contacts"]
    product: Optional[str] = Field(None, description="מוצר לסינון, למשל 'רכב', 'דירה', 'נסיעות'. ריק = כל המוצרים")
    keywords: list[str] = Field(
        default_factory=list,
        description="מילות מפתח קצרות (מילה אחת או שתיים) לסינון, למשל ['ביטול'] או ['גרר']. ריק = הכל",
    )


QUERY_PROMPT = PromptTemplate(
    """\
יש מאגר JSON של פריטים שחולצו מאתר הפניקס, עם הסוגים:
- claim_procedures: {{product, claim_type, steps, required_documents, channels, stated_timeframe}} — איך מגישים תביעה
- service_actions: {{product, action, how, channels}} — פעולות שירות עצמי (ביטול, משעבד, חידוש, עדכון אשראי, אישורים...)
- contacts: {{department, phone, whatsapp, email, hours}} — דרכי יצירת קשר
בנה שאילתה שמחזירה את הפריטים הדרושים לשאלה. מילות מפתח — רק אם הן מצמצמות באמת; לשאלת "רשימה של כל..." השאר ריק.

שאלה: {query}"""
)

ANSWER_PROMPT = PromptTemplate(
    """\
אתה הבוט של אתר הפניקס. ענה לשאלת הלקוח רק מתוך הפריטים הבאים (JSON שחולץ מהאתר).
- עברית ברורה וקצרה; לשאלת רשימה — רשימה מסודרת.
- בסוף: שורה "המקור:" עם כתובות ה-url (מתוך שדה source.url) של הפריטים שעליהם התבססת.
- אם הפריטים לא עונים על השאלה — כתוב בדיוק: "{no_info}"

פריטים:
{items}

שאלה: {query}
תשובה:"""
).partial_format(no_info=NO_INFO_ANSWER)


@lru_cache
def load_items() -> dict[str, list[dict]]:
    if not config.STRUCTURED_PATH.exists():
        return {}
    return json.loads(config.STRUCTURED_PATH.read_text(encoding="utf-8"))["items"]


def run_query(sq: StructuredQuery) -> list[dict]:
    items = load_items().get(sq.item_type, [])

    def text_of(item: dict) -> str:
        return json.dumps({k: v for k, v in item.items() if k not in ("source", "id", "observed_at")},
                          ensure_ascii=False) + " " + item["source"]["title"]

    if sq.product:
        items = [i for i in items if sq.product in text_of(i)]
    if sq.keywords:
        items = [i for i in items if any(k in text_of(i) for k in sq.keywords)]
    return items


async def answer(query: str) -> tuple[str, list[dict], StructuredQuery]:
    """Returns (answer text, matched items, the generated query)."""
    sq = await settings.llm(fast=True).astructured_predict(StructuredQuery, QUERY_PROMPT, query=query)
    items = run_query(sq)
    if not items:
        return NO_INFO_ANSWER, [], sq
    compact = [{k: v for k, v in i.items() if k not in ("observed_at",) and v not in (None, [], "")}
               for i in items[:MAX_ITEMS_TO_LLM]]
    resp = await settings.llm().apredict(
        ANSWER_PROMPT, items=json.dumps(compact, ensure_ascii=False, indent=1), query=query
    )
    return resp.strip(), items, sq
