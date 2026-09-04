"""Pulls today's startup/VC news from several sources and writes an LLM-analyzed digest.

Sources: TechCrunch, Crunchbase News, AVC, The Verge, Ars Technica, and NYT Technology (RSS);
Hacker News (keyword search via the Algolia HN Search API); SEC EDGAR (S-1 IPO registration
filings); and 20VC's YouTube channel (recent episode titles, for trend context only).

The digest has two parts, both written by Claude in one pass: a ~3-minute-read overview of
the day's climate and how it ties to the trailing month's larger trends, followed by the
day's most significant headlines grouped by theme with a short analysis and link each. A raw,
unfiltered link list for the day is appended at the end for reference.

Usage:
    python news_tracker.py
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import feedparser
from anthropic import Anthropic
from dotenv import load_dotenv

RSS_FEEDS = {
    "TechCrunch": "https://techcrunch.com/category/startups/feed/",
    "Crunchbase News": "https://news.crunchbase.com/feed/",
    "AVC": "https://avc.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "NYT Technology": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
}
YOUTUBE_20VC_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCf0PBRjhf0rF8fWBIxTuoWA"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_KEYWORDS = ["raises", "seed", "Series A", "growth", "fund"]
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_IPO_FORM = "S-1"

LIGHT_NEWS_THRESHOLD = 10  # below this many today-headlines, search the open web for more
WEB_SEARCH_MAX_USES = 5

RECENT_CONTEXT_DAYS = 30
RECENT_RSS_LIMIT = 10  # per feed; RSS feeds don't support date-range queries, so this is
                       # "however far back the feed's latest N items happen to reach" — for
                       # high-volume outlets that's often just the last day or two.
RECENT_YOUTUBE_LIMIT = 15
RECENT_HN_LIMIT = 15
RECENT_EDGAR_LIMIT = 15

OUTPUT_DIR = Path(__file__).parent / "output"
MODEL = "claude-sonnet-5"


class Headline(TypedDict):
    """A single news item pulled from a source."""

    source: str
    title: str
    link: str
    summary: str


def strip_html(text: str) -> str:
    """Remove HTML tags from an RSS summary field."""
    return re.sub("<[^<]+?>", "", text).strip()


def _fetch_json(url: str, headers: dict[str, str] | None = None) -> dict | None:
    """Fetch and parse a JSON endpoint, returning None (with a warning) on failure."""
    try:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except Exception as error:
        print(f"Warning: request to {url} failed: {error}", file=sys.stderr)
        return None


def fetch_rss_headlines(feed_url: str, source: str) -> list[Headline]:
    """Fetch entries from an RSS feed published in the trailing 24 hours."""
    feed = feedparser.parse(feed_url)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    headlines: list[Headline] = []
    for entry in feed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published is None:
            continue
        published_dt = datetime(*published[:6], tzinfo=timezone.utc)
        if published_dt >= cutoff:
            headlines.append({
                "source": source,
                "title": entry.get("title", "").strip(),
                "link": entry.get("link", ""),
                "summary": strip_html(entry.get("summary", "")),
            })
    return headlines


def fetch_all_rss_headlines(feeds: dict[str, str]) -> list[Headline]:
    """Fetch today's headlines from each RSS feed in `feeds`."""
    headlines: list[Headline] = []
    for source, feed_url in feeds.items():
        headlines.extend(fetch_rss_headlines(feed_url, source))
    return headlines


def fetch_recent_rss_items(feed_url: str, source: str, limit: int) -> list[Headline]:
    """Fetch the most recent entries from an RSS feed, regardless of publish date.

    Used for trend context, not "today's headlines" — RSS feeds only expose their latest
    entries with no archive/date-range access, so this is best-effort recency, not a
    guaranteed date window.
    """
    feed = feedparser.parse(feed_url)
    return [
        {
            "source": source,
            "title": entry.get("title", "").strip(),
            "link": entry.get("link", ""),
            "summary": "",
        }
        for entry in feed.entries[:limit]
    ]


def fetch_hn_headlines(keywords: list[str], days: int, limit: int | None = None) -> list[Headline]:
    """Search Hacker News (via Algolia) for stories in the last `days` matching any keyword."""
    window_start = datetime.now(timezone.utc) - timedelta(days=days)
    start_ts = int(window_start.timestamp())
    numeric_filters = f"created_at_i>={start_ts}"

    stories_by_id: dict[str, dict] = {}
    for keyword in keywords:
        query = urllib.parse.urlencode({
            "tags": "story",
            "query": keyword,
            "numericFilters": numeric_filters,
        })
        data = _fetch_json(f"{HN_SEARCH_URL}?{query}")
        if data is None:
            continue
        for hit in data.get("hits", []):
            # Algolia's search is typo-tolerant (e.g. "fund" ~ "Finding"), which is too loose
            # for these short keywords — require the keyword to actually appear in the title.
            if keyword.lower() in hit.get("title", "").lower():
                stories_by_id[hit["objectID"]] = hit

    stories = sorted(stories_by_id.values(), key=lambda h: h.get("created_at_i", 0), reverse=True)
    if limit is not None:
        stories = stories[:limit]

    headlines: list[Headline] = []
    for story in stories:
        discussion_link = f"https://news.ycombinator.com/item?id={story['objectID']}"
        headlines.append({
            "source": "Hacker News",
            "title": story.get("title", "").strip(),
            "link": story.get("url") or discussion_link,
            "summary": f"{story.get('points', 0)} points, {story.get('num_comments', 0)} "
                       f"comments — {discussion_link}",
        })
    return headlines


def fetch_edgar_ipo_filings(
    contact: str, start_date: str, end_date: str, limit: int | None = None
) -> list[Headline]:
    """Fetch S-1 (IPO registration) filings from SEC EDGAR full-text search in a date range."""
    query = urllib.parse.urlencode({
        "forms": EDGAR_IPO_FORM,
        "startdt": start_date,
        "enddt": end_date,
    })
    headers = {"User-Agent": f"Daily VC News Tracker {contact}"}
    data = _fetch_json(f"{EDGAR_SEARCH_URL}?{query}", headers=headers)
    if data is None:
        return []

    hits = data.get("hits", {}).get("hits", [])
    # Skip amendments (S-1/A); only surface original filings.
    hits = [h for h in hits if h["_source"].get("form") == EDGAR_IPO_FORM]
    if limit is not None:
        hits = hits[:limit]

    headlines: list[Headline] = []
    for hit in hits:
        source_data = hit["_source"]
        cik = source_data["ciks"][0].lstrip("0")
        accession_no_dashes = source_data["adsh"].replace("-", "")
        filename = hit["_id"].split(":", 1)[1]
        company = re.sub(r"\s*\(CIK \d+\)\s*$", "", source_data["display_names"][0]).strip()

        headlines.append({
            "source": "SEC EDGAR",
            "title": f"{company} files for IPO (Form S-1)",
            "link": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{filename}",
            "summary": f"Form S-1 registration statement filed {source_data['file_date']}.",
        })
    return headlines


def fetch_recent_youtube_items(feed_url: str, source: str, limit: int) -> list[Headline]:
    """Fetch the most recent video titles from a YouTube channel's RSS feed.

    Titles/descriptions only — no transcripts — so this contributes surface-level trend
    context, not deep analysis of episode content.
    """
    feed = feedparser.parse(feed_url)
    return [
        {
            "source": source,
            "title": entry.get("title", "").strip(),
            "link": entry.get("link", ""),
            "summary": strip_html(entry.get("summary", ""))[:200],
        }
        for entry in feed.entries[:limit]
    ]


def format_headlines_for_prompt(headlines: list[Headline]) -> str:
    """Render headlines as a grouped, readable block of text for the LLM prompt."""
    if not headlines:
        return "(none)"
    grouped: dict[str, list[Headline]] = {}
    for h in headlines:
        grouped.setdefault(h["source"], []).append(h)
    lines: list[str] = []
    for source, items in grouped.items():
        lines.append(f"{source}:")
        for h in items:
            snippet = f" — {h['summary']}" if h["summary"] else ""
            lines.append(f"  - {h['title']}{snippet} ({h['link']})")
    return "\n".join(lines)


def _extract_json(text: str) -> object | None:
    """Pull the first JSON array or object out of a model response, tolerating code fences."""
    match = re.search(r"\[.*\]|\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def dedupe_exact_headlines(headlines: list[Headline]) -> list[Headline]:
    """Drop exact-duplicate headlines: same link, or same title once punctuation/case is stripped."""
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[Headline] = []
    for h in headlines:
        link_key = h["link"].split("?")[0].rstrip("/")
        title_key = re.sub(r"[^a-z0-9]", "", h["title"].lower())
        if link_key in seen_links or (title_key and title_key in seen_titles):
            continue
        seen_links.add(link_key)
        if title_key:
            seen_titles.add(title_key)
        deduped.append(h)
    return deduped


def search_additional_headlines(client: Anthropic, existing_headlines: list[Headline]) -> list[Headline]:
    """When the plugged-in sources are light on news, have Claude search the open web for more.

    Uses Anthropic's server-side web search tool, which only returns publicly indexed results —
    it never logs into anything or accesses paywalled/authenticated content, so this can't pull
    in sources that require personal credentials.
    """
    existing_titles = "\n".join(f"- {h['title']}" for h in existing_headlines) or "(none)"
    prompt = f"""Today's plugged-in news sources turned up relatively little. Search the public \
web for significant venture capital, startup, and AI-industry news from the trailing 24 hours \
that ISN'T already covered by the headlines listed below under "ALREADY COVERED".

Rules:
- Only use publicly accessible web pages. Never use, cite, or attempt to access anything that \
would require a personal login, subscription, or paywall bypass to view.
- Only include events you can verify from actual search results — don't invent anything.
- Skip anything that duplicates (even if worded differently) an item in ALREADY COVERED.
- If you don't find any genuinely new, significant items, return an empty array.

Respond with ONLY a JSON array (no prose, no code fence), where each item is \
{{"source": "<publication name>", "title": "<headline>", "link": "<url>", "summary": \
"<1-2 sentence factual summary>"}}. Include at most 8 items.

ALREADY COVERED:
{existing_titles}
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}],
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    parsed = _extract_json(text)
    if not isinstance(parsed, list):
        print("Warning: additional-source search returned no parseable results.", file=sys.stderr)
        return []

    headlines: list[Headline] = []
    for item in parsed:
        if not isinstance(item, dict) or not item.get("title") or not item.get("link"):
            continue
        headlines.append({
            "source": item.get("source") or "Web Search",
            "title": str(item["title"]).strip(),
            "link": str(item["link"]).strip(),
            "summary": str(item.get("summary", "")).strip(),
        })
    return headlines


def deduplicate_headlines(client: Anthropic, headlines: list[Headline]) -> list[Headline]:
    """Ask Claude to find headlines describing the same underlying event from different outlets.

    Catches semantic duplicates (same funding round covered by two publications with different
    wording/URLs) that exact link/title matching can't — exact duplicates are already removed
    by `dedupe_exact_headlines` before this runs.
    """
    if len(headlines) < 2:
        return headlines

    numbered = "\n".join(f"{i}. [{h['source']}] {h['title']}" for i, h in enumerate(headlines))
    prompt = f"""Below is a numbered list of today's news headlines from different sources. Find \
groups of items that describe the exact same real-world event (e.g. the same funding round, \
filing, or acquisition reported by two different outlets) — not just the same general topic.

Respond with ONLY a JSON object (no prose, no code fence): \
{{"duplicate_groups": [[i, j, ...], ...]}}, where each inner list is the indices (from the list \
below) of items that are duplicates of each other. Only include groups of 2 or more genuine \
duplicates. If there are none, respond with {{"duplicate_groups": []}}.

HEADLINES:
{numbered}
"""

    response = client.messages.create(model=MODEL, max_tokens=1024, messages=[{"role": "user", "content": prompt}])
    text = "".join(block.text for block in response.content if block.type == "text")
    parsed = _extract_json(text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("duplicate_groups"), list):
        print("Warning: duplicate check returned no parseable result; skipping dedup.", file=sys.stderr)
        return headlines

    to_drop: set[int] = set()
    for group in parsed["duplicate_groups"]:
        if not isinstance(group, list) or len(group) < 2:
            continue
        valid_indices = sorted(i for i in group if isinstance(i, int) and 0 <= i < len(headlines))
        to_drop.update(valid_indices[1:])  # keep the first of each group, drop the rest

    return [h for i, h in enumerate(headlines) if i not in to_drop]


def generate_digest_body(
    client: Anthropic, today_headlines: list[Headline], recent_context: list[Headline]
) -> str:
    """Ask Claude to write the full digest: climate/trend analysis + curated top headlines."""
    if not today_headlines:
        return "No headlines found for today."

    today_block = format_headlines_for_prompt(today_headlines)
    recent_block = format_headlines_for_prompt(recent_context)

    prompt = f"""You are writing the daily briefing for a venture capital investment analyst. \
Write the digest in Markdown with exactly two sections, using the material provided below. \
Be factual: only state facts present in the material, and never invent numbers, dates, deals, \
or outcomes not shown there.

Heading levels matter: a document title (`#`) is added separately above your output, so your \
two top-level sections must use `##` exactly as written below, and thematic groupings inside \
"Today's Top Headlines" must use `###`.

## Today's Climate & Trends

A roughly three-minute read (~500-650 words): an overview of today's startup/VC/tech news \
climate, then an analysis of how today's headlines reflect or connect to broader trends in the \
venture market and across the AI stack. Explicitly tie today's news to larger developments from \
major tech/venture players over the trailing month, using the RECENT CONTEXT material below for \
that grounding — do not rely on outside/background knowledge for trend claims, since some of \
that material may predate your training data or be otherwise unknown to you.

Note on RECENT CONTEXT coverage: the Hacker News and SEC EDGAR items below span the full \
trailing {RECENT_CONTEXT_DAYS} days. The RSS-sourced and YouTube items only reflect however far \
back each feed's most recent items happen to reach (often just the last few days for \
high-volume outlets) — don't imply broader historical coverage from those sources than what's \
actually listed.

## Today's Top Headlines

From TODAY'S HEADLINES below (the trailing 24 hours only — do not pull items from RECENT \
CONTEXT into this section), select exactly the 10 most significant events. If fewer than 10 \
genuinely significant events exist, list only those — don't pad the count with minor items. \
Group your selections under clear thematic `###` headings that fit the day's actual news (e.g. \
Funding Rounds, IPOs & Public Markets, AI Infrastructure, M&A, Product Launches — invent \
headings that fit rather than forcing these).

Format each of the 10 as:
1. A bolded headline stating the specific event itself in plain, concrete language — who did \
what, with the real numbers/parties/terms involved — not the title of the article that reported \
it. For example, write "**Nvidia acquires Hugging Face for $12.9B**" or "**Thinking Machines in \
talks with Accel to raise $1B at a $40B pre-money valuation**," not a rephrased article headline.
2. On the next line, a ~30-second-read (60-90 words) description and analysis of the event and \
why it matters. For a fundraise specifically, state exactly what the company or fund said it \
raised the capital for (e.g. "Crusoe raises $3B to accelerate its AI data center buildout") if \
that's stated in the material below — only if it's actually stated; don't guess or invent a \
use of proceeds that isn't there.
3. Only if one specific source is clearly and directly the origin of that event, a markdown \
link to it on its own line below the analysis (e.g. "Source: [TechCrunch](url)"). If no single \
source is a clean match, omit the link entirely rather than attaching a loosely related one.

If there isn't enough material for a full three-minute read in the "Today's Climate & Trends" \
section above, write a shorter, honest piece rather than padding it with speculation.

TODAY'S HEADLINES:
{today_block}

RECENT CONTEXT (trailing ~{RECENT_CONTEXT_DAYS} days where available — for trend grounding \
only, not to be listed as "today's" news):
{recent_block}
"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    if message.stop_reason == "max_tokens":
        print("Warning: digest generation hit the max_tokens cap and was truncated.", file=sys.stderr)
    return "".join(block.text for block in message.content if block.type == "text")


def write_digest(today_headlines: list[Headline], digest_body: str) -> Path:
    """Write the day's digest, with a raw link appendix, to a dated markdown file."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    out_path = OUTPUT_DIR / f"{today:%Y_%m_%d}_venture_news.md"

    lines = [f"# Startup & VC News Digest — {today}", "", digest_body.strip(), "", "## All Headlines (Raw)"]
    headlines_by_source: dict[str, list[Headline]] = {}
    for h in today_headlines:
        headlines_by_source.setdefault(h["source"], []).append(h)
    for source, source_headlines in headlines_by_source.items():
        lines.extend(["", f"### {source}", ""])
        for h in source_headlines:
            lines.append(f"- [{h['title']}]({h['link']})")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def main() -> None:
    """Entry point of script. Expects to be run as CLI program."""
    load_dotenv(".env.local")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set. Copy .env.local.example to .env.local and fill it in.")
        sys.exit(1)

    client = Anthropic(api_key=api_key)

    today_headlines = fetch_all_rss_headlines(RSS_FEEDS)
    today_headlines.extend(fetch_hn_headlines(HN_KEYWORDS, days=1))

    edgar_contact = os.environ.get("SEC_EDGAR_CONTACT")
    if edgar_contact:
        today = datetime.now(timezone.utc).date()
        today_headlines.extend(fetch_edgar_ipo_filings(edgar_contact, today.isoformat(), today.isoformat()))
    else:
        print(
            "Warning: SEC_EDGAR_CONTACT not set, skipping SEC EDGAR (required by SEC's fair "
            "access policy — set it in .env.local).",
            file=sys.stderr,
        )

    today_headlines = dedupe_exact_headlines(today_headlines)

    if len(today_headlines) < LIGHT_NEWS_THRESHOLD:
        print(
            f"Only {len(today_headlines)} headline(s) from the plugged-in sources — "
            "searching the open web for more.",
            file=sys.stderr,
        )
        today_headlines.extend(search_additional_headlines(client, today_headlines))
        today_headlines = dedupe_exact_headlines(today_headlines)

    today_headlines = deduplicate_headlines(client, today_headlines)

    recent_context: list[Headline] = []
    for source, feed_url in RSS_FEEDS.items():
        recent_context.extend(fetch_recent_rss_items(feed_url, source, RECENT_RSS_LIMIT))
    recent_context.extend(fetch_recent_youtube_items(YOUTUBE_20VC_FEED, "20VC (YouTube)", RECENT_YOUTUBE_LIMIT))
    recent_context.extend(fetch_hn_headlines(HN_KEYWORDS, days=RECENT_CONTEXT_DAYS, limit=RECENT_HN_LIMIT))
    if edgar_contact:
        today = datetime.now(timezone.utc).date()
        month_ago = (today - timedelta(days=RECENT_CONTEXT_DAYS)).isoformat()
        recent_context.extend(
            fetch_edgar_ipo_filings(edgar_contact, month_ago, today.isoformat(), limit=RECENT_EDGAR_LIMIT)
        )

    digest_body = generate_digest_body(client, today_headlines, recent_context)
    out_path = write_digest(today_headlines, digest_body)
    print(f"Wrote digest with {len(today_headlines)} today's headline(s) to {out_path}")


if __name__ == "__main__":
    main()
