"""HTML -> clean Markdown with title and section metadata.

Primary extractor is trafilatura (keeps headings, drops menus and "related
articles" blocks). Hub/landing pages are mostly link lists that trafilatura
drops, so when its output is much shorter than the <main> text we fall back
to a heading-aware walk over <main>.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

import trafilatura
from bs4 import BeautifulSoup

MIN_CHARS = 400  # below this the trafilatura result is considered too thin
MIN_RATIO = 0.4  # trafilatura length / <main> text length
DEDUPE_MIN_CHARS = 60  # only paragraphs at least this long are de-duplicated

_NOISE_TAGS = ["script", "style", "noscript", "svg", "form", "iframe", "button"]
_TITLE_SUFFIX = re.compile(r"\s*[|\-–]\s*הפניקס\s*$")


def _title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return " ".join(h1.get_text(" ", strip=True).split())  # <br> inside h1 -> single line
    if soup.title and soup.title.string:
        return _TITLE_SUFFIX.sub("", soup.title.string.strip())
    return ""


def _description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    return (meta.get("content") or "").strip() if meta else ""


def _main_to_markdown(main) -> str:
    """Heading-aware text walk over <main>, used for hub pages."""
    for tag in main(_NOISE_TAGS):
        tag.decompose()
    lines: list[str] = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "a", "span", "div"]):
        # only leaf-ish elements: skip containers whose text is fully held by children we also visit
        if el.find(["h1", "h2", "h3", "h4", "p", "li", "a", "div"]):
            continue
        text = el.get_text(" ", strip=True)
        if not text or len(text) < 2:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            text = "#" * int(el.name[1]) + " " + text
        elif el.name == "li":
            text = "- " + text
        if lines and lines[-1].lstrip("#- ") == text.lstrip("#- "):
            continue  # drop repeated labels (mobile/desktop duplicates)
        lines.append(text)
    return "\n\n".join(lines)


_CUT_HEADINGS = ("אולי יעניין אותך גם", "מאמרים נוספים", "כתבות נוספות")
_DISCLAIMER_PREFIXES = ("מידע זה הינו כללי", "החברה עושה מאמצים", "האמור בכתבה אינו מהווה")
_NOISE_LINES = {"דף הבית", "מה חדש", "טיפים", "מושגים", "|", "טיפים |", "מושגים |"}


def _postprocess(markdown: str, title: str) -> str:
    """Drop breadcrumbs, related-articles tail, legal boilerplate and duplicate paragraphs."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", markdown) if p.strip()]

    # Breadcrumbs: if the title heading appears twice, everything before the second one is chrome.
    title_idx = [i for i, p in enumerate(paras) if p.lstrip("# ").strip() == title]
    if len(title_idx) >= 2:
        paras = paras[title_idx[1]:]

    out, seen = [], set()
    for p in paras:
        bare = p.lstrip("#- ").strip()
        if any(bare.startswith(h) for h in _CUT_HEADINGS):
            break  # related-articles block runs to the end of <main>
        if bare in _NOISE_LINES or bare.startswith(_DISCLAIMER_PREFIXES):
            continue
        if len(bare) >= DEDUPE_MIN_CHARS:  # short lines (phones, hours) legitimately repeat per section
            if bare in seen:
                continue
            seen.add(bare)
        out.append(p)
    return "\n\n".join(out)


# Phone numbers (054-9893455, 03-7141000, *5070, 3455*) and emails: the facts customers
# most often ask for, and the ones trafilatura tends to drop from card layouts.
_CONTACT_RE = re.compile(r"\b0\d{1,2}-?\d{3}-?\d{4}\b|\*\d{4}\b|\b\d{4}\*|[\w.+-]+@[\w-]+\.[\w.]+")


def _contact_facts(text: str) -> set[str]:
    return set(_CONTACT_RE.findall(text))


def section_of(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return unquote(path.split("/")[0]) if path else ""


def clean_html(html: str, url: str) -> dict:
    """Return {title, description, section, markdown, extractor}."""
    soup = BeautifulSoup(html, "html.parser")
    title = _title(soup)
    description = _description(soup)

    traf = trafilatura.extract(
        html,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        favor_recall=True,
        url=url,
    ) or ""
    traf = _postprocess(traf, title)

    main = soup.find("main")
    fallback = _postprocess(_main_to_markdown(main), title) if main else ""

    traf_ok = len(traf) >= MIN_CHARS and (not fallback or len(traf) >= MIN_RATIO * len(fallback))
    lost_contacts = _contact_facts(fallback) - _contact_facts(traf)
    if traf_ok and not lost_contacts:
        markdown, extractor = traf, "trafilatura"
    else:
        markdown, extractor = (fallback or traf), "main-walk"

    # Make sure the page title is the top heading so chunks keep context.
    if title and not markdown.lstrip().startswith("# "):
        markdown = f"# {title}\n\n{markdown}"
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    return {
        "title": title,
        "description": description,
        "section": section_of(url),
        "markdown": markdown,
        "extractor": extractor,
    }
