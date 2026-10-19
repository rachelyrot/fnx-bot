"""Stage B+C — the RAG pipeline as an event-driven LlamaIndex Workflow, with routing.

validate -> condense -> understand -+-> structured --(found)------------------------+-> verify -> Stop
                                    |   (nothing found: use prefetched evidence)    |
                                    +-> evaluate -(ok)-> synthesize (streams tokens)
                                          ^    |
                       retrieve <- rewrite <---+ (weak, attempts left)

`understand` runs classification, routing and semantic retrieval concurrently
(latency: the three are independent; the retrieval is simply discarded when the
question stops at a boundary or goes to the structured layer). When the first retrieval
finds evidence, `understand` also drafts the answer itself while classification is still
running, and goes straight to verify; `synthesize` handles the answers after a rewrite.

Stops early: empty / too-long input (validate), advice / off-topic (understand, fixed
referral), no evidence after retries (evaluate: "not on the site", no answer-LLM call).
Personal-policy questions are answered from the site and get the referral appended.

State (ctx.store): question, attempts, category.  Events: only what the next step needs.
TokenEvent is written to the event stream so a UI can show the answer as it is generated.

    uv run python -m rag.workflow "יש לי נזילה בבית, איך אני מגישה תביעה?"
"""
from __future__ import annotations

import asyncio
import re
import sys

from llama_index.core.schema import NodeWithScore
from llama_index.core.workflow import Context, Event, StartEvent, StopEvent, Workflow, step

from rag import prompts, settings, structured
from rag import query_engine as qe
from rag.query_engine import sources_of
from rag.router import choose_route

MAX_QUESTION_CHARS = 1000
MAX_RETRIEVE_ATTEMPTS = 2
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


# --- Events --------------------------------------------------------------------
class CondenseEvent(Event):
    question: str
    history: list[dict]
    page: str = ""  # title of the fnx.co.il page the customer is reading (browser extension)


class ClassifyEvent(Event):
    query: str  # standalone question (after condense)
    original: str  # the customer's own words: "my policy" must survive condensing


class StructuredEvent(Event):
    query: str
    nodes: list[NodeWithScore]  # prefetched semantic evidence, used if the structured layer has nothing


class RetrieveEvent(Event):
    query: str


class EvaluateEvent(Event):
    query: str
    nodes: list[NodeWithScore]


class RewriteEvent(Event):
    query: str


class SynthesizeEvent(Event):
    query: str
    nodes: list[NodeWithScore]


class VerifyEvent(Event):
    text: str
    sources: list[dict]  # [{url, title, score}] the answer was allowed to cite
    route: str


class TokenEvent(Event):
    """Streamed to the caller (not routed to a step): a piece of the answer being written."""

    delta: str


def result(text: str, route: str, sources: list[dict] | None = None, **extra) -> StopEvent:
    return StopEvent(result={"text": text, "route": route, "sources": sources or [], **extra})


# --- Prompts for the small control-flow LLM calls --------------------------------
CONDENSE_PROMPT = """\
בהינתן היסטוריית שיחה ושאלת המשך, נסח מחדש את שאלת ההמשך כשאלה עצמאית ומלאה בעברית
(למשל להשלים במה מדובר: "ותוך כמה זמן?" -> "תוך כמה זמן מקבלים תשובה על תביעת נזקי מים בביטוח דירה?").
אם השאלה כבר עצמאית — החזר אותה כמות שהיא. החזר רק את השאלה.

היסטוריה:
{history}

שאלת המשך: {question}
שאלה עצמאית:"""

CLASSIFY_PROMPT = """\
סווג את שאלת הלקוח של חברת ביטוח לאחת מהקטגוריות, והחזר רק את שם הקטגוריה:
site_question — שאלה כללית על מוצרים, כיסויים, תביעות, טפסים, תהליכים או שירות (גם אם היא מנוסחת בגוף ראשון, כמו "יש לי נזילה, איך מגישים תביעה?")
personal_policy — שאלה שהתשובה עליה תלויה במידע אישי של הלקוח: הפוליסה שלו, הכיסוי שלו, הסטטוס של התביעה שלו, כמה כסף יש לו
advice — בקשה להמלצה אישית: מה כדאי לי לבחור, האם לקנות, איזה מסלול עדיף לי
off_topic — לא קשור לביטוח, פיננסים או לחברת הפניקס
שים לב לניסוח המקורי של הלקוח: "הפוליסה שלי", "התביעה שלי", "כמה אני אקבל" = personal_policy.

הניסוח המקורי של הלקוח: {original}
השאלה בהקשר השיחה: {query}
קטגוריה:"""

REWRITE_PROMPT = """\
החיפוש באתר הפניקס לא מצא תוצאות טובות לשאלה הבאה. נסח אותה מחדש כשאילתת חיפוש קצרה בעברית,
במונחים שחברת ביטוח משתמשת בהם באתר (למשל "נזילה" -> "נזקי מים וצנרת בביטוח דירה"). החזר רק את השאילתה.

שאלה: {query}
שאילתה:"""


CATEGORIES = ("personal_policy", "advice", "off_topic", "site_question")


def parse_category(text: str) -> str:
    """Models sometimes answer '**personal_policy**', 'personal_policy.' or a short sentence;
    pick the first category name that appears."""
    found = [(text.find(c), c) for c in CATEGORIES if c in text]
    return min(found)[1] if found else "site_question"


CONTEXT_WORDS = re.compile(r"(?<!\w)(?:זה|זו|זאת|הזה|הזו|הזאת|הזאתי|כאן|פה|הביטוח|המוצר|הכיסוי)(?!\w)")


def refers_to_context(question: str) -> bool:
    """Does a first question lean on the page being read ("מה זה כולל?", "כמה עולה הביטוח?")?
    Self-contained questions skip the condense call, so they pay no extra latency."""
    return len(question.split()) <= 4 or bool(CONTEXT_WORDS.search(question))


def history_text(history: list[dict], turns: int = 4) -> str:
    lines = []
    for msg in history[-turns * 2:]:
        role = "לקוח" if msg["role"] == "user" else "בוט"
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        lines.append(f"{role}: {content.split('<div')[0].strip()}")  # drop rendered source links
    return "\n".join(lines)


async def _draft(query: str, nodes: list[NodeWithScore], out: asyncio.Queue) -> None:
    """Stream the answer into `out`, ending with None (also on failure, so the reader never hangs)."""
    try:
        async for delta in qe.astream_synthesize(query, nodes):
            await out.put(delta)
    finally:
        out.put_nowait(None)


async def _drain(queue: asyncio.Queue):
    while (delta := await queue.get()) is not None:
        yield delta


async def _collect(ctx: Context, deltas) -> list[str]:
    """Stream answer deltas to the caller and return them. After a RESTART (broken stream)
    the answer arrives whole; it reaches the UI as the final text, which replaces the draft."""
    parts, live = [], True
    async for delta in deltas:
        if delta is qe.RESTART:
            parts, live = [], False
            continue
        parts.append(delta)
        if live:
            ctx.write_event_to_stream(TokenEvent(delta=delta))
    return parts


class FnxWorkflow(Workflow):
    @step
    async def validate_input(self, ctx: Context, ev: StartEvent) -> CondenseEvent | StopEvent:
        question = (ev.get("question") or "").strip()
        if not question:
            return result(prompts.EMPTY_INPUT_ANSWER, route="empty")
        if len(question) > MAX_QUESTION_CHARS:
            return result("השאלה ארוכה מדי — אפשר לקצר לשאלה אחת ממוקדת?", route="too_long")
        await ctx.store.set("question", question)
        await ctx.store.set("attempts", 0)
        return CondenseEvent(question=question, history=ev.get("history") or [], page=ev.get("page") or "")

    @step
    async def condense(self, ev: CondenseEvent) -> ClassifyEvent:
        # "מה זה כולל?" on the travel-insurance page is about travel insurance
        use_page = bool(ev.page) and refers_to_context(ev.question)
        if not ev.history and not use_page:  # nothing to resolve, no LLM call
            return ClassifyEvent(query=ev.question, original=ev.question)
        history = history_text(ev.history)
        if use_page:
            history = f"(הלקוח קורא כעת באתר את העמוד: {ev.page})\n{history}".strip()
        resp = await settings.llm(fast=True).acomplete(
            CONDENSE_PROMPT.format(history=history, question=ev.question)
        )
        return ClassifyEvent(query=resp.text.strip() or ev.question, original=ev.question)

    @step
    async def understand(
        self, ctx: Context, ev: ClassifyEvent
    ) -> StructuredEvent | EvaluateEvent | VerifyEvent | StopEvent:
        """Classify + route + retrieve, concurrently.

        Classification sometimes finishes after retrieval, so the answer is drafted
        speculatively as soon as the evidence is in, and only shown once the question
        is confirmed to be one we answer (an advice / off-topic draft is discarded)."""

        async def classify() -> str:
            resp = await settings.llm(fast=True).acomplete(
                CLASSIFY_PROMPT.format(original=ev.original, query=ev.query)
            )
            return parse_category(resp.text)

        label_task = asyncio.create_task(classify())
        route, nodes = await asyncio.gather(
            asyncio.to_thread(choose_route, ev.query),
            asyncio.to_thread(lambda: qe.postprocess(ev.query, qe.retrieve(ev.query))),
        )
        draft = asyncio.Queue()
        draft_task = asyncio.create_task(_draft(ev.query, nodes, draft)) if route == "semantic" and nodes else None
        try:
            label = await label_task
        except BaseException:
            if draft_task:
                draft_task.cancel()
            raise
        await ctx.store.set("category", label)
        await ctx.store.set("attempts", 1)
        if label in ("advice", "off_topic"):
            if draft_task:
                draft_task.cancel()
            answer = prompts.ADVICE_ANSWER if label == "advice" else prompts.OFF_TOPIC_ANSWER
            return result(answer, route=label, query=ev.query)
        # site_question and personal_policy both search the site (personal gets a referral appended)
        if route == "structured":
            return StructuredEvent(query=ev.query, nodes=nodes)
        if not draft_task:  # no evidence: evaluate decides between rewrite and "not on the site"
            return EvaluateEvent(query=ev.query, nodes=nodes)
        parts = await _collect(ctx, _drain(draft))
        await draft_task  # re-raises a failed synthesis
        text = "".join(parts).strip()
        return VerifyEvent(text=text, sources=sources_of(nodes, text), route="semantic")

    @step
    async def structured_lookup(self, ev: StructuredEvent) -> VerifyEvent | EvaluateEvent:
        text, items, _ = await structured.answer(ev.query)
        if not items or prompts.is_no_info(text):
            # structured layer has nothing: fall back to the semantic evidence fetched in `understand`
            return EvaluateEvent(query=ev.query, nodes=ev.nodes)
        seen, sources = set(), []
        for item in items:
            src = item["source"]
            if src["url"] not in seen:
                seen.add(src["url"])
                sources.append({"url": src["url"], "title": src["title"], "score": 1.0})
        return VerifyEvent(text=text, sources=sources, route="structured")

    @step
    async def retrieve(self, ctx: Context, ev: RetrieveEvent) -> EvaluateEvent:
        await ctx.store.set("attempts", await ctx.store.get("attempts") + 1)
        nodes = await asyncio.to_thread(lambda: qe.postprocess(ev.query, qe.retrieve(ev.query)))
        return EvaluateEvent(query=ev.query, nodes=nodes)

    @step
    async def evaluate(self, ctx: Context, ev: EvaluateEvent) -> SynthesizeEvent | RewriteEvent | StopEvent:
        if ev.nodes:
            return SynthesizeEvent(query=ev.query, nodes=ev.nodes)
        if await ctx.store.get("attempts") < MAX_RETRIEVE_ATTEMPTS:
            return RewriteEvent(query=ev.query)
        if await ctx.store.get("category") == "personal_policy":
            return result(prompts.PERSONAL_POLICY_ANSWER, route="personal_policy", query=ev.query)
        return result(prompts.NO_INFO_ANSWER, route="no_info", query=ev.query)

    @step
    async def rewrite(self, ev: RewriteEvent) -> RetrieveEvent:
        resp = await settings.llm(fast=True).acomplete(REWRITE_PROMPT.format(query=ev.query))
        return RetrieveEvent(query=resp.text.strip() or ev.query)

    @step
    async def synthesize(self, ctx: Context, ev: SynthesizeEvent) -> VerifyEvent:
        parts = await _collect(ctx, qe.astream_synthesize(ev.query, ev.nodes))
        text = "".join(parts).strip()
        return VerifyEvent(text=text, sources=sources_of(ev.nodes, text), route="semantic")

    @step
    async def verify(self, ctx: Context, ev: VerifyEvent) -> StopEvent:
        """Every cited URL must come from the evidence; no citation -> cite the top source."""
        personal = await ctx.store.get("category") == "personal_policy"
        if prompts.is_no_info(ev.text):
            if personal:
                return result(prompts.PERSONAL_POLICY_ANSWER, route="personal_policy")
            return result(prompts.NO_INFO_ANSWER, route="no_info")

        allowed = {s["url"] for s in ev.sources}
        text = ev.text
        for url in set(URL_RE.findall(text)):
            if url.rstrip(".,") not in allowed:
                text = text.replace(url, "").rstrip()  # cited something it was not given
        if not any(u.rstrip(".,") in allowed for u in URL_RE.findall(text)):
            text = f"{text}\n\nהמקור: {ev.sources[0]['url']}"
        if personal:
            # models copy the line loosely (\u200E mark), so match it without the phone number
            text = re.sub(rf"\n*{prompts.MORE_INFO_LINE.split(':')[0]}:[^\n]*", "", text)
            text = f"{text}\n\n{prompts.PERSONAL_POLICY_ANSWER}"
        return result(text, route="personal_policy" if personal else ev.route, sources=ev.sources)


workflow = FnxWorkflow(timeout=120, verbose=False)


async def ask(question: str, history: list[dict] | None = None, page: str = "") -> dict:
    return await workflow.run(question=question, history=history or [], page=page)


async def ask_stream(question: str, history: list[dict] | None = None, page: str = ""):
    """Yields ("token", delta) while the answer is written, then ("final", result dict).

    The final text is the verified one (citations checked), so a UI should replace
    the streamed text with it."""
    handler = workflow.run(question=question, history=history or [], page=page)
    async for ev in handler.stream_events():
        if isinstance(ev, TokenEvent):
            yield "token", ev.delta
    yield "final", await handler


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    out = asyncio.run(ask(" ".join(sys.argv[1:]) or "יש לי נזילה בבית, איך אני מגישה תביעה?"))
    print(f"[{out['route']}]\n{out['text']}")
    for s in out["sources"]:
        print(f"  [{s['score']}] {s['title']} — {s['url']}")
