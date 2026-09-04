"""Renders the latest markdown digest (and the full archive) into a static site in docs/.

Reads every dated digest from output/, converts each to HTML, writes one page per day plus
an index page showing the latest digest with a list of past ones. Intended to run right after
news_tracker.py, in CI, with the docs/ folder served via GitHub Pages.

Usage:
    python publish_site.py
"""

import re
from pathlib import Path

import markdown

OUTPUT_DIR = Path(__file__).parent / "output"
DOCS_DIR = Path(__file__).parent / "docs"
FILENAME_RE = re.compile(r"(\d{4})_(\d{2})_(\d{2})_venture_news\.md$")
REPO_URL = "https://github.com/nickoster123103/vc-news-tracker"

FONT_LINKS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
"""

# A minimal, full-bleed skyline silhouette (buildings + a spired tower, à la the Empire State
# Building) used as a subtle divider under the page title. Renders in the current text color at
# low opacity, so it stays quiet in both light and dark mode instead of competing with the copy.
SKYLINE_SVG = """
<svg class="skyline" viewBox="0 0 800 110" preserveAspectRatio="none" aria-hidden="true">
  <rect x="0"   y="55" width="42" height="55"/>
  <rect x="48"  y="35" width="30" height="75"/>
  <rect x="84"  y="68" width="52" height="42"/>
  <rect x="142" y="20" width="26" height="90"/>
  <rect x="174" y="48" width="36" height="62"/>
  <rect x="216" y="62" width="46" height="48"/>
  <rect x="268" y="30" width="22" height="80"/>
  <polygon points="268,30 279,6 290,30"/>
  <rect x="296" y="58" width="40" height="52"/>
  <rect x="342" y="42" width="30" height="68"/>
  <rect x="378" y="66" width="50" height="44"/>
  <rect x="434" y="24" width="24" height="86"/>
  <rect x="464" y="52" width="38" height="58"/>
  <rect x="508" y="66" width="48" height="44"/>
  <rect x="562" y="14" width="20" height="96"/>
  <polygon points="562,14 572,-8 582,14"/>
  <rect x="588" y="58" width="42" height="52"/>
  <rect x="636" y="40" width="30" height="70"/>
  <rect x="672" y="64" width="52" height="46"/>
  <rect x="730" y="50" width="34" height="60"/>
  <rect x="770" y="70" width="30" height="40"/>
</svg>
"""

STYLE = """
    :root {
        --bg: #fbf8f2;
        --bg-alt: #f2ecdc;
        --text: #1b1b2f;
        --text-muted: #5b5c72;
        --accent: #00733e;
        --accent-ink: #00532c;
        --border: #e6ddc4;
        color-scheme: light dark;
    }
    @media (prefers-color-scheme: dark) {
        :root {
            --bg: #0f0f16;
            --bg-alt: #1a1a24;
            --text: #ece8dd;
            --text-muted: #a6a4b3;
            --accent: #2fbf71;
            --accent-ink: #6fe0a3;
            --border: #2b2b38;
        }
    }
    * { box-sizing: border-box; }
    body {
        max-width: 720px; margin: 0 auto; padding: 0 1.25rem 3rem;
        background: var(--bg); color: var(--text);
        font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-size: 1.05rem; line-height: 1.6;
    }
    header.masthead { padding-top: 2rem; }
    .kicker {
        font-family: "IBM Plex Mono", monospace; font-size: .78rem; letter-spacing: .12em;
        text-transform: uppercase; color: var(--accent-ink);
    }
    h1, h2, h3 { font-family: "Playfair Display", Georgia, serif; font-weight: 800; line-height: 1.25; }
    h1 { font-size: 2rem; margin: .3rem 0 .5rem; }
    .tagline { color: var(--text-muted); margin: 0 0 1rem; }
    .skyline {
        display: block; width: 100%; height: 42px; margin: .5rem 0 1.75rem;
        color: var(--accent); opacity: .55;
    }
    h2 {
        font-size: 1.4rem; margin-top: 2.5rem; padding-bottom: .4rem;
        border-bottom: 2px solid var(--accent);
    }
    h3 {
        font-size: 1.1rem; font-weight: 700; margin-top: 1.75rem;
        padding-left: .6rem; border-left: 4px solid var(--accent);
    }
    a { color: inherit; text-decoration-color: var(--accent); text-decoration-thickness: 2px; }
    a:hover { color: var(--accent-ink); }
    ol, ul { padding-left: 1.3rem; }
    ol > li { margin-bottom: 1.1rem; }
    nav.top { margin-bottom: .5rem; font-family: "IBM Plex Mono", monospace; font-size: .85rem; }
    nav.top a { text-decoration: none; border-bottom: 1px solid var(--accent); }
    .archive-list { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: .5rem; }
    .archive-list li { margin: 0; }
    .archive-list a {
        display: inline-block; font-family: "IBM Plex Mono", monospace; font-size: .85rem;
        padding: .35rem .7rem; border: 1px solid var(--border); border-radius: 999px;
        text-decoration: none; background: var(--bg-alt);
    }
    .archive-list a:hover { border-color: var(--accent); color: var(--accent-ink); }
    footer {
        margin-top: 3.5rem; padding-top: 1.25rem; border-top: 1px solid var(--border);
        font-family: "IBM Plex Mono", monospace; font-size: .78rem; color: var(--text-muted);
    }
    footer a { color: inherit; }
"""


def masthead(tagline: str) -> str:
    """Shared page header: kicker, title, tagline, and the skyline divider graphic."""
    return f"""
<header class="masthead">
  <div class="kicker">New York · Startups · Venture Capital</div>
  <h1>Venture News Tracker 🗽</h1>
  <p class="tagline">{tagline}</p>
  {SKYLINE_SVG}
</header>
"""


def page_shell(title: str, body_html: str, nav_html: str = "") -> str:
    """Wrap rendered HTML body content in the page template."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{FONT_LINKS}
<style>{STYLE}</style>
</head>
<body>
{nav_html}
{body_html}
<footer>Generated automatically by <a href="{REPO_URL}">vc-news-tracker</a>.</footer>
</body>
</html>
"""


def digest_files_by_date() -> list[tuple[str, Path]]:
    """Return (iso_date, path) for every digest in output/, newest first."""
    files = []
    for path in OUTPUT_DIR.glob("*_venture_news.md"):
        match = FILENAME_RE.search(path.name)
        if match:
            iso_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
            files.append((iso_date, path))
    return sorted(files, key=lambda pair: pair[0], reverse=True)


def render_markdown(md_text: str) -> str:
    # Drop the digest's own leading "# Startup & VC News Digest — date" title — the page's own
    # masthead/heading already covers it, so keeping both stacks two near-identical headlines.
    md_text = re.sub(r"^#[^\n]*\n+", "", md_text, count=1)
    # nl2br turns the single-newline-separated headline/analysis/source lines the digest prompt
    # produces into actual line breaks — without it they'd run together in one dense paragraph.
    return markdown.markdown(md_text, extensions=["extra", "sane_lists", "nl2br"])


def build_site() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    files = digest_files_by_date()
    if not files:
        print("No digests found in output/ — nothing to publish.")
        return

    nav = '<nav class="top"><a href="index.html">← All Digests</a></nav>'
    for iso_date, path in files:
        body_html = render_markdown(path.read_text(encoding="utf-8"))
        page_body = masthead(f"Daily digest for {iso_date}.") + body_html
        page = page_shell(f"Venture News — {iso_date}", page_body, nav)
        (DOCS_DIR / f"{iso_date}.html").write_text(page, encoding="utf-8")

    latest_date, latest_path = files[0]
    latest_html = render_markdown(latest_path.read_text(encoding="utf-8"))

    archive_items = "\n".join(
        f'<li><a href="{iso_date}.html">{iso_date}</a></li>' for iso_date, _ in files
    )
    index_body = f"""
{masthead("Daily AI-generated VC/startup/tech news, out of the city that never sleeps.")}
<h2>Latest — {latest_date}</h2>
{latest_html}
<h2>Archive</h2>
<ul class="archive-list">
{archive_items}
</ul>
"""
    (DOCS_DIR / "index.html").write_text(page_shell("Venture News Tracker", index_body), encoding="utf-8")
    print(f"Published {len(files)} digest(s) to {DOCS_DIR}, latest: {latest_date}")


if __name__ == "__main__":
    build_site()
