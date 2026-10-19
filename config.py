"""Central configuration: paths, crawl scope, models and thresholds."""
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
PAGES_DIR = DATA_DIR / "pages"
MANIFEST_PATH = DATA_DIR / "manifest.json"
REDIRECTS_PATH = DATA_DIR / "redirects.json"  # sitemap url -> final url
STRUCTURED_PATH = DATA_DIR / "structured.json"

# --- Crawl ---
BASE_URL = "https://www.fnx.co.il"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
USER_AGENT = "fnx-hackathon-bot/0.1 (+opencode AI hackathon 2026; polite crawler)"
CRAWL_DELAY_SEC = 1.0  # polite rate: ~1 request per second
REQUEST_TIMEOUT_SEC = 30

# Customer-facing sections (first path segment). Investor relations, English site,
# careers, tags etc. are left out on purpose.
INCLUDE_SECTIONS = {
    "car-insurance",
    "apartment-insurance",
    "health-insurance",
    "life-insurance",
    "travel-insurance",
    "pets-insurance",
    "mortgage-insurance",
    "other-insurances",
    "business-insurance",
    "truck-insurance",
    "disability-and-incapacity-to-work",
    "claims-customer-service",
    "customer-service",
    "pension-savings",
    "pension_retirement",
    "gemel",
    "ishtalmutfund",
    "savings",
    "loans",
    "takanottashlum",
}

# --- Models ---
EMBED_MODEL = "embed-multilingual-v3.0"  # Cohere, supports Hebrew, 1024 dims
EMBED_DIM = 1024
RERANK_MODEL = "rerank-multilingual-v3.0"
LLM_MAIN = "claude-sonnet-5-5"  # answer synthesis, extraction
# Tried Haiku for answers: ~0.7s faster in total, same first-token time (retrieval dominates),
# but it cited a staff member's personal email and the wrong service number. Quality wins.
LLM_FAST = "claude-haiku-5-5"  # classification, routing, query rewrite
# Cohere models, used only when ANTHROPIC_API_KEY is empty
LLM_FALLBACK = "command-a-03-2025"  # answer synthesis
# Tried command-r7b-12-2024 here: barely faster, and it broke follow-up condensing and
# "my policy" classification in Hebrew. Quality wins.
LLM_FALLBACK_FAST = "command-a-03-2025"  # classification, condense, rewrite

# --- Vector store ---
PINECONE_INDEX = "fnx"
PINECONE_NAMESPACE = "fnx-site"
# Cohere trial keys allow 100K tokens/minute; Hebrew runs ~1 token per 2 chars,
# so ~150K chars per batch, one batch per minute, keeps a safe margin.
EMBED_CHARS_PER_MINUTE = 150_000

# --- Chunking / retrieval ---
# Token counts: the default tokenizer spends ~1 token per Hebrew character,
# so 1024 tokens is roughly 2-3 paragraphs.
CHUNK_SIZE = 1024
CHUNK_OVERLAP = 128
RETRIEVE_TOP_K = 25  # wide net: the right page is often ranked 10-25 by vectors alone
RERANK_TOP_N = 6  # 4 cut the third-party claims page (rerank 0.956) behind two pages x 2 chunks
MAX_CHUNKS_PER_PAGE = 2  # after rerank: keep page diversity instead of 4 chunks of one page
SIMILARITY_CUTOFF = 0.30  # vector score below this is treated as "no match"
RERANK_CUTOFF = 0.20  # rerank relevance below this is treated as "no match"
# When rerank is unavailable (rate limit), vector score must clear a stricter bar.
# Observed: on-topic questions score ~0.75+, an unrelated question (cake recipe) ~0.51.
VECTOR_FALLBACK_CUTOFF = 0.60
