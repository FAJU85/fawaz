"""Scrape the titles of the top trending stories from Hacker News.

Self-healing scraper: retries transient failures, sends browser-like headers to
avoid user-agent blocks, tolerates missing/markup-changed tags, and raises a
clear error instead of returning silent garbage.

NOTE ON THE TARGET: the host the user asked for, ``https://ycombinator.com``,
is Y Combinator's *company marketing site* and contains no "trending stories".
The actual top-stories front page is Hacker News at ``news.ycombinator.com``,
which is the default here. Pass a different ``url`` if you really want another
page (the same ``tr.athing span.titleline > a`` markup applies to HN listings).
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

# Front page of Hacker News (the real "top stories" source).
HN_TOP = "https://news.ycombinator.com/"

# A real browser UA avoids naive bot blocks; many sites 403 the default
# "python-requests/x.y" user agent.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Status codes worth retrying (transient / rate-limit / server side).
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Ordered list of selectors; the first that matches wins. The second is a
# fallback in case HN drops the <span class="titleline"> wrapper.
TITLE_SELECTORS = ("span.titleline > a", "td.title > a", "td.title a")


class ScrapeError(RuntimeError):
    """Raised when the page cannot be fetched or parsed into stories."""


@dataclass
class Story:
    rank: int
    item_id: str | None
    title: str
    url: str


def fetch_top_stories(
    url: str = HN_TOP,
    limit: int | None = 30,
    timeout: float = 20.0,
    retries: int = 3,
    backoff: float = 1.5,
    session: requests.Session | None = None,
) -> list[Story]:
    """Return the top stories (rank, title, link) from an HN-style listing.

    Args:
        url: page to scrape (defaults to the Hacker News front page).
        limit: max stories to return; ``None`` returns all found.
        timeout: per-request timeout in seconds.
        retries: attempts for transient errors (timeouts, 429/5xx).
        backoff: base seconds for exponential backoff between retries.
        session: optional reusable ``requests.Session``.

    Raises:
        ScrapeError: if the page can't be fetched or contains no story rows.
    """
    http = session or requests.Session()
    http.headers.update(DEFAULT_HEADERS)

    last_error: Exception | None = None
    response: requests.Response | None = None

    for attempt in range(1, retries + 1):
        try:
            response = http.get(url, timeout=timeout, allow_redirects=True)
        except requests.exceptions.RequestException as exc:  # DNS, TCP, timeout...
            last_error = exc
            if attempt < retries:
                time.sleep(backoff ** attempt)
                continue
            raise ScrapeError(f"request to {url!r} failed: {exc}") from exc

        if response.status_code == 403:
            raise ScrapeError(
                f"{url!r} returned 403 Forbidden — the client was likely "
                "blocked (user-agent / bot protection)."
            )
        if response.status_code in RETRYABLE_STATUS:
            last_error = ScrapeError(f"HTTP {response.status_code}")
            if attempt < retries:
                time.sleep(backoff ** attempt)
                continue
            raise ScrapeError(f"{url!r} kept returning HTTP {response.status_code}.")
        if response.status_code >= 400:
            raise ScrapeError(f"{url!r} returned HTTP {response.status_code}.")
        break

    if response is None:  # pragma: no cover - defensive
        raise ScrapeError(f"no response from {url!r}: {last_error}")

    # --- Parse ---------------------------------------------------------------
    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("tr.athing")

    if not rows:
        raise ScrapeError(
            "no 'tr.athing' story rows found — the page markup may have changed, "
            "or the response was an interstitial/consent page instead of content."
        )

    stories: list[Story] = []
    for row in rows:
        anchor = next((row.select_one(sel) for sel in TITLE_SELECTORS if row.select_one(sel)), None)
        if anchor is None:
            continue  # tolerate a malformed/ad row rather than crashing

        title = anchor.get_text(" ", strip=True)
        if not title:
            continue

        href = anchor.get("href")
        if not href:
            continue
        # HN sometimes returns relative links (e.g. "item?id=123"); absolutize.
        href = urllib.parse.urljoin(url, href)

        rank_el = row.select_one("span.rank")
        rank = int(rank_el.get_text(strip=True).rstrip(".")) if rank_el else len(stories) + 1

        stories.append(Story(rank=rank, item_id=row.get("id"), title=title, url=href))

    if not stories:
        raise ScrapeError("story rows were found but none contained a usable title link.")

    return stories[:limit] if limit is not None else stories


if __name__ == "__main__":
    for s in fetch_top_stories():
        print(f"{s.rank:>2}. {s.title}\n     {s.url}")
