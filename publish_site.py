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

STYLE = """
    :root { color-scheme: light dark; }
    body { max-width: 760px; margin: 2rem auto; padding: 0 1.25rem; font-family: -apple-system,
        BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.55; }
    h1 { font-size: 1.6rem; }
    h2 { font-size: 1.3rem; margin-top: 2rem; border-bottom: 1px solid currentColor; padding-bottom: .25rem; }
    h3 { font-size: 1.05rem; margin-top: 1.5rem; }
    nav { margin-bottom: 1.5rem; font-size: .9rem; }
    nav a { margin-right: 1rem; }
    .archive-list { list-style: none; padding: 0; }
    .archive-list li { padding: .35rem 0; border-bottom: 1px solid rgba(128,128,128,.25); }
    footer { margin-top: 3rem; font-size: .8rem; opacity: .7; }
"""


def page_shell(title: str, body_html: str, nav_html: str = "") -> str:
    """Wrap rendered HTML body content in a minimal page template."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{STYLE}</style>
</head>
<body>
{nav_html}
{body_html}
<footer>Generated automatically by <a href="https://github.com/nickoster123103/vc-news-tracker">vc-news-tracker</a>.</footer>
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
    return markdown.markdown(md_text, extensions=["extra", "sane_lists"])


def build_site() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    files = digest_files_by_date()
    if not files:
        print("No digests found in output/ — nothing to publish.")
        return

    nav = '<nav><a href="index.html">← All Digests</a></nav>'
    for iso_date, path in files:
        body_html = render_markdown(path.read_text(encoding="utf-8"))
        page = page_shell(f"Venture News — {iso_date}", body_html, nav)
        (DOCS_DIR / f"{iso_date}.html").write_text(page, encoding="utf-8")

    latest_date, latest_path = files[0]
    latest_html = render_markdown(latest_path.read_text(encoding="utf-8"))

    archive_items = "\n".join(
        f'<li><a href="{iso_date}.html">{iso_date}</a></li>' for iso_date, _ in files
    )
    index_body = f"""
<h1>Venture News Tracker</h1>
<p>Daily AI-generated VC/startup/tech news digest. Latest below; past digests in the archive.</p>
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
