"""Stage C — Router: semantic search vs. structured lookup.

Uses LlamaIndex's LLMSingleSelector (the selector behind RouterQueryEngine)
over two tool descriptions; the workflow's `route` step calls choose_route().
"""
from __future__ import annotations

from llama_index.core.selectors import LLMSingleSelector
from llama_index.core.tools import ToolMetadata

import config
from rag import settings

ROUTES = [
    ToolMetadata(
        name="semantic",
        description=(
            "חיפוש סמנטי בכל תוכן האתר: הסברים, כיסויים, מה כלול ומה לא, מאמרים, שאלות נפוצות, "
            "תנאים, הגדרות ומושגים. מתאים לרוב השאלות."
        ),
    ),
    ToolMetadata(
        name="structured",
        description=(
            "שליפה ממאגר מובנה של: תהליכי הגשת תביעה (שלבים, מסמכים נדרשים, ערוצים, זמני טיפול), "
            "פעולות שירות עצמי (ביטול, הוספת/הסרת משעבד, חידוש, עדכון אשראי, אישורים) "
            "ודרכי יצירת קשר (טלפונים, וואטסאפ, מייל, שעות). מתאים לשאלות רשימה "
            "('אילו/כל ה...'), למספרי טלפון, ולשאלות 'איך מבצעים פעולה X'."
        ),
    ),
]


def choose_route(query: str) -> str:
    if not config.STRUCTURED_PATH.exists():
        return "semantic"
    selector = LLMSingleSelector.from_defaults(llm=settings.llm(fast=True))
    result = selector.select(ROUTES, query=query)
    return ROUTES[result.ind].name
