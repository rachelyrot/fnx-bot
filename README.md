# הבוט שיודע את האתר של הפניקס

צ'אט שעונה בעברית על שאלות לקוחות **רק מתוך האתר הציבורי fnx.co.il**, מצרף קישור לעמוד המקור, ויודע להגיד "זה לא כתוב באתר".
האקתון AI קוד פתוח 2026 — אתגר בשיתוף הפניקס.

**סטאק:** LlamaIndex · Cohere (embed-multilingual-v3.0 + rerank-multilingual-v3.0) · Pinecone · Claude (Sonnet לתשובות, Haiku לסיווג/ניתוב) · FastAPI + ממשק ווב בעיצוב אתר הפניקס

תרשים הזרימה של ה-Workflow: [docs/workflow.html](docs/workflow.html)

## הרצה

```bash
uv sync
cp .env.example .env          # ANTHROPIC_API_KEY, COHERE_API_KEY, PINECONE_API_KEY

uv run python -m crawl.crawler      # 1. איסוף: sitemap -> ~930 עמודים ציבוריים -> data/pages/
uv run python -m ingest.prepare     # 2. אינדוקס: חיתוך -> embeddings -> Pinecone
uv run python -m ingest.extract     # 3. חילוץ מובנה: data/structured.json
uv run python app.py                # 4. צ'אט: http://127.0.0.1:8000

uv run python -m eval.run_eval      # בדיקה על 31 שאלות -> eval/results.md
```

בלי מפתח Claude הבוט משתמש זמנית ב-Cohere Command A (אותו מפתח של ה-embeddings).

מאחורי NetFree: התיקון של `netfree_unstrict_ssl` פועל כברירת מחדל (`NETFREE_UNSTRICT_SSL=0` לכיבוי).

## איך זה בנוי

| שלב | קבצים | מה קורה |
|---|---|---|
| איסוף | [crawl/crawler.py](crawl/crawler.py), [crawl/clean.py](crawl/clean.py) | `sitemap.xml` → סינון לאזורים ללקוח (ביטוחים, תביעות, שירות, חיסכון ופנסיה; בלי קשרי משקיעים/אנגלית/קריירה) → `robots.txt` → קצב מנומס (~1 בקשה לשנייה) → trafilatura ל-Markdown (גיבוי: מעבר על `<main>` בעמודי מרכז) → ניקוי פירורי לחם, הסתייגויות משפטיות ו"אולי יעניין אותך" |
| א' — אינדקס | [ingest/prepare.py](ingest/prepare.py) | `Document` לכל עמוד (url, title, section, crawled_at, hash) → `MarkdownNodeParser` + `SentenceSplitter` → `CohereEmbedding` → `VectorStoreIndex` על `PineconeVectorStore` |
| א' — תשאול | [rag/query_engine.py](rag/query_engine.py), [rag/prompts.py](rag/prompts.py) | Retriever (top-10) → `SimilarityPostprocessor` → `CohereRerank` (top-4) → סף רלוונטיות → Response Synthesizer עם פרומפט מחייב |
| ב' — Workflow | [rag/workflow.py](rag/workflow.py) | אותו תהליך כ-Workflow מונחה-אירועים עם ולידציות וניתוב (ראו תרשים) |
| ג' — חילוץ + ניתוב | [ingest/extract.py](ingest/extract.py), [rag/structured.py](rag/structured.py), [rag/router.py](rag/router.py) | Structured Data Extraction לתהליכי תביעה, פעולות שירות ודרכי קשר → `LLMSingleSelector` מחליט בין חיפוש סמנטי לשליפה מובנית |
| ממשק | [app.py](app.py), [web/](web/) | שרת FastAPI (`POST /api/chat`) ועמוד צ'אט ב-HTML/CSS/JS בצבעי אתר הפניקס: RTL, היסטוריית שיחה לשאלות המשך, קישורים למקורות, הצעות לשאלות |

### איך נבחר הקטע
1. השאלה (אחרי השלמה מהיסטוריית השיחה) מוטמעת ב-Cohere עם `input_type=search_query`.
2. Pinecone מחזיר 10 קטעים; קטעים מתחת לסף דמיון נזרקים.
3. Cohere Rerank רב-לשוני מדרג מחדש ומשאיר 4; קטעים מתחת לסף רלוונטיות נזרקים — **זה המנגנון העיקרי שמונע המצאה**: שאלה על מתכון לעוגה מקבלת ציון rerank אפסי, והבוט עונה "לא מופיע באתר" בלי לקרוא ל-LLM בכלל.
4. אם לא נשאר כלום — ניסוח מחדש של השאילתה במונחי האתר וניסיון נוסף אחד.

### כללי הבוט
| כלל | איך נאכף |
|---|---|
| מקור יחיד | פרומפט מחייב + אין קריאה ל-LLM כשאין קטעים רלוונטיים |
| ציטוט | `verify` מוחק כתובות שלא הגיעו מהקטעים שאוחזרו ומוסיף מקור אם חסר |
| גבולות | `classify`: ייעוץ אישי ונושאים לא קשורים — הפניה קבועה; שאלה על פוליסה אישית — הסבר כללי מהאתר + הפניה לשירות הלקוחות |
| עברית | תשובות קצרות (2–4 משפטים), שאלות המשך דרך `condense` |

### שלב ב' — דיון
- **חלוקה ל-Steps:** כל step עושה החלטה אחת או פעולה אחת עם קלט/פלט ברור, כך שאפשר לנתב ממנו למקום אחר (למשל `evaluate` שולח ל-`rewrite`, ל-`synthesize` או לעצירה).
- **האם Step חייב LLM?** לא. `validate_input`, `retrieve`, `evaluate`, `verify` דטרמיניסטיים — והם בדיוק אלה שמבטיחים "לא להמציא".
- **State מול Event:** ב-State נמצא מה שכמה steps צריכים לאורך הריצה (השאלה המקורית, מספר הניסיונות, הקטגוריה). ב-Event — רק מה שה-step הבא צריך (השאילתה הנוכחית, הקטעים, טיוטת התשובה).

### שלב ג' — למה שכבה מובנית
חיפוש סמנטי מחזיר את 4 הקטעים הדומים ביותר — לא רשימה מלאה. לשאלות כמו "אילו פעולות אפשר לעשות בביטוח רכב באתר?" או "מה כל הדרכים ליצור קשר?" הסכמה ב-[ingest/extract.py](ingest/extract.py) (`claim_procedures`, `service_actions`, `contacts`) נותנת כיסוי מלא, וכל פריט שומר את ה-URL שלו לציטוט מדויק. אם השליפה המובנית לא מוצאת כלום — נופלים חזרה לחיפוש סמנטי.

## רענון
- `crawl.crawler` מוריד מחדש רק עמודים שה-`lastmod` שלהם ב-sitemap חדש מתאריך האיסוף.
- `ingest.prepare` משווה hash לכל עמוד מול `data/manifest.json`: רק עמודים חדשים/שהשתנו מוטמעים; קטעים ישנים נמחקים מ-Pinecone לפי prefix של מזהה העמוד.
- `ingest.extract` מחלץ מחדש רק עמודים שה-hash שלהם השתנה.

## מגבלות ידועות
- 11 עמודים נחסמו בסריקה על ידי סינון NetFree (קוד 418; הרשימה ב-`data/crawl.log`). הם נשמרו ידנית מהדפדפן ל-`data/manual/` ועברו את אותו ניקוי (`clean_html`) לתוך `data/pages/`. ב-JSON שלהם אין `lastmod`.
- קובצי PDF (תנאי פוליסות, טפסים) עדיין לא נאספים.
- trafilatura משמיט לפעמים את פסקת הפתיחה של מאמר.
- עמודי תובענות ייצוגיות ותנאי שימוש נאספים ועלולים להופיע כמקור לא אידיאלי.
- מפתח Cohere בגרסת ניסיון: 100K טוקנים לדקה ו-10 קריאות rerank לדקה — האינדוקס רץ באצוות של דקה.

## בדיקות
[eval/questions.yaml](eval/questions.yaml): 31 שאלות — תביעות, שירות, נסיעות, רכב, דירה, בריאות, שאלות המשך (תסריט הנזילה מהאתגר), ו-6 שאלות שהבוט חייב **לא** לענות עליהן מהאתר. תוצאות: [eval/results.md](eval/results.md).
