"""Polite crawler for the public fnx.co.il pages.

sitemap.xml -> filter customer-facing sections + robots.txt -> fetch (~1 req/s)
-> clean -> data/pages/<id>.json

Incremental: a page is re-fetched only if the sitemap <lastmod> is newer than
our crawled_at (or with --force).

Usage:
    uv run python -m crawl.crawler                 # all included sections
    uv run python -m crawl.crawler --limit 50
    uv run python -m crawl.crawler --sections claims-customer-service apartment-insurance
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.robotparser import RobotFileParser

import httpx

import config
from crawl.clean import clean_html, section_of

SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def page_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def page_path(url: str):
    return config.PAGES_DIR / f"{page_id(url)}.json"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_sitemap(client: httpx.Client) -> list[tuple[str, str | None]]:
    resp = client.get(config.SITEMAP_URL)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    out = []
    for url_el in root.findall("sm:url", SM_NS):
        loc = url_el.findtext("sm:loc", namespaces=SM_NS)
        lastmod = url_el.findtext("sm:lastmod", namespaces=SM_NS)
        if loc:
            out.append((loc.strip(), lastmod.strip() if lastmod else None))
    return out


def load_robots(client: httpx.Client) -> RobotFileParser:
    rp = RobotFileParser()
    resp = client.get(f"{config.BASE_URL}/robots.txt")
    rp.parse(resp.text.splitlines())
    return rp


def load_redirects() -> dict[str, str]:
    if config.REDIRECTS_PATH.exists():
        return json.loads(config.REDIRECTS_PATH.read_text(encoding="utf-8"))
    return {}


def needs_fetch(url: str, lastmod: str | None, force: bool, redirects: dict[str, str]) -> bool:
    path = page_path(redirects.get(url, url))
    if force or not path.exists():
        return True
    try:
        crawled_at = parse_dt(json.loads(path.read_text(encoding="utf-8")).get("crawled_at"))
    except (json.JSONDecodeError, OSError):
        return True
    site_dt = parse_dt(lastmod)
    return bool(site_dt and crawled_at and site_dt > crawled_at)


RETRY_STATUSES = {418, 429, 500, 502, 503, 504}  # 418 = the site's WAF throttling us


def get_with_backoff(client: httpx.Client, url: str, attempts: int = 3) -> httpx.Response:
    for i in range(attempts):
        resp = client.get(url)
        if resp.status_code not in RETRY_STATUSES or i == attempts - 1:
            return resp
        time.sleep(10 * (i + 1))
    return resp


def fetch_page(client: httpx.Client, url: str, lastmod: str | None) -> dict | None:
    resp = get_with_backoff(client, url)
    if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
        print(f"  skip {resp.status_code} {url}", file=sys.stderr)
        return None
    cleaned = clean_html(resp.text, url)
    if len(cleaned["markdown"]) < 80:
        print(f"  skip (empty) {url}", file=sys.stderr)
        return None
    final_url = str(resp.url)
    return {
        "id": page_id(final_url),
        "url": final_url,
        "lastmod": lastmod,
        "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hash": "sha256:" + hashlib.sha256(cleaned["markdown"].encode("utf-8")).hexdigest(),
        **cleaned,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="max pages to fetch this run (0 = no limit)")
    ap.add_argument("--sections", nargs="*", help="override config.INCLUDE_SECTIONS")
    ap.add_argument("--force", action="store_true", help="re-fetch even if unchanged")
    args = ap.parse_args()

    sections = set(args.sections) if args.sections else config.INCLUDE_SECTIONS
    config.PAGES_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        headers={"User-Agent": config.USER_AGENT, "Accept-Language": "he-IL,he;q=0.9"},
        timeout=config.REQUEST_TIMEOUT_SEC,
        follow_redirects=True,
    ) as client:
        robots = load_robots(client)
        entries = load_sitemap(client)
        targets = [
            (u, lm) for u, lm in entries
            if section_of(u) in sections and robots.can_fetch(config.USER_AGENT, u)
        ]
        redirects = load_redirects()
        todo = [(u, lm) for u, lm in targets if needs_fetch(u, lm, args.force, redirects)]
        if args.limit:
            todo = todo[: args.limit]
        print(f"sitemap: {len(entries)} urls | in scope: {len(targets)} | to fetch: {len(todo)}")

        ok, seen = 0, set()
        for i, (url, lastmod) in enumerate(todo, 1):
            try:
                page = fetch_page(client, url, lastmod)
            except httpx.HTTPError as e:
                print(f"  error {url}: {e}", file=sys.stderr)
                page = None
            if page:
                if page["url"] != url:
                    redirects[url] = page["url"]
                if page["url"] not in seen:  # several sitemap urls may redirect to one page
                    seen.add(page["url"])
                    page_path(page["url"]).write_text(
                        json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    ok += 1
            if i % 25 == 0:
                print(f"  {i}/{len(todo)} fetched, {ok} saved")
            time.sleep(config.CRAWL_DELAY_SEC)

    config.REDIRECTS_PATH.write_text(json.dumps(redirects, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: {ok} pages saved to {config.PAGES_DIR}")


if __name__ == "__main__":
    main()
