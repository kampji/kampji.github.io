#!/usr/bin/env python3
"""Refresh the auto-generated sections of index.html.

- Writing: your latest Medium posts, read from your Medium RSS feed.
- Open-Source Projects: every public, non-fork, non-archived repo you own,
  sorted by stars (high to low), then name (A to Z).
- Security Research: the table in your My-Security-Research README,
  in the same order as the table.

Standard library only, so it runs on a stock GitHub Actions runner
or on your own machine with no installs:

    python3 scripts/update.py

If a fetch fails, that section is left exactly as it was, so a GitHub or
Medium outage never wipes the page.
"""

import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
SITEMAP = ROOT / "sitemap.xml"

GH_USER = os.environ.get("GH_USER") or "kampji"
MEDIUM_HANDLE = (os.environ.get("MEDIUM_HANDLE") or "j.kamperides").lstrip("@")
RESEARCH_REPO = os.environ.get("RESEARCH_REPO") or f"{GH_USER}/My-Security-Research"
RESEARCH_BRANCH = os.environ.get("RESEARCH_BRANCH") or "main"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "6"))

# Repos that are about the site itself, not projects worth listing.
SKIP_REPOS = {f"{GH_USER}.github.io".lower(), GH_USER.lower()}

INDENT = "      "


def fetch(url, accept="application/json"):
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": f"{GH_USER}-portfolio-updater",
    })
    if TOKEN and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def get_repos():
    repos, page = [], 1
    while True:
        url = (f"https://api.github.com/users/{GH_USER}/repos"
               f"?type=owner&per_page=100&page={page}")
        batch = json.loads(fetch(url, "application/vnd.github+json"))
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    repos = [r for r in repos
             if not r.get("fork") and not r.get("archived")
             and not r.get("private")
             and r["name"].lower() not in SKIP_REPOS]
    repos.sort(key=lambda r: (-r.get("stargazers_count", 0), r["name"].lower()))
    return repos


def render_repos(repos):
    if not repos:
        return [f'{INDENT}<p class="lede">Projects coming soon.</p>']
    out = []
    for r in repos:
        name = html.escape(r["name"])
        url = html.escape(r["html_url"])
        desc = (r.get("description") or "").strip()
        meta = []
        if r.get("language"):
            meta.append(html.escape(r["language"]))
        stars = r.get("stargazers_count", 0)
        if stars:
            meta.append(f"★ {stars}")
        out.append(f'{INDENT}<div class="card">')
        out.append(f'{INDENT}  <h3><a href="{url}">{name}</a></h3>')
        if desc:
            out.append(f"{INDENT}  <p>{html.escape(desc)}</p>")
        if meta:
            out.append(f'{INDENT}  <p class="meta">{" · ".join(meta)}</p>')
        out.append(f"{INDENT}</div>")
    return out


def get_articles():
    raw = fetch(f"https://medium.com/feed/@{MEDIUM_HANDLE}",
                "application/rss+xml, application/xml")
    channel = ET.fromstring(raw).find("channel")
    items = []
    for item in channel.findall("item")[:MAX_ARTICLES]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").split("?")[0]  # drop tracking params
        pub = item.findtext("pubDate") or ""
        try:
            date = dt.datetime.strptime(pub[:16], "%a, %d %b %Y").strftime("%b %d, %Y")
        except ValueError:
            date = ""
        if title and link:
            items.append((title, link, date))
    return items


def render_articles(items):
    if not items:
        return [f'{INDENT}<p class="lede">Articles coming soon.</p>']
    out = []
    for title, link, date in items:
        out.append(f'{INDENT}<div class="card">')
        out.append(f'{INDENT}  <h3><a href="{html.escape(link)}">{html.escape(title)}</a></h3>')
        if date:
            out.append(f'{INDENT}  <p class="meta">{date} · Medium</p>')
        out.append(f"{INDENT}</div>")
    return out


MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def abs_url(url):
    """Turn a link relative to the research repo into a full GitHub URL."""
    if re.match(r"^[a-z]+://", url) or url.startswith("#"):
        return url
    return f"https://github.com/{RESEARCH_REPO}/blob/{RESEARCH_BRANCH}/{url.lstrip('./')}"


def md_inline(text):
    """Escape a table cell, keeping `code` spans."""
    parts = re.split(r"(`[^`]+`)", text)
    return "".join(
        f"<code>{html.escape(p[1:-1])}</code>" if p.startswith("`") and p.endswith("`")
        else html.escape(p)
        for p in parts)


def split_row(line):
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip().replace("\\|", "|") for c in cells]


def get_research():
    raw = fetch(f"https://raw.githubusercontent.com/{RESEARCH_REPO}/"
                f"{RESEARCH_BRANCH}/README.md", "text/plain").decode("utf-8")
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        header = [h.lower() for h in split_row(line)]
        if "identifier" not in header or i + 1 >= len(lines):
            continue
        if not re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            continue
        rows = []
        for row_line in lines[i + 2:]:
            if not row_line.lstrip().startswith("|"):
                break
            cells = split_row(row_line)
            cells += [""] * (len(header) - len(cells))
            rows.append(dict(zip(header, cells)))
        return rows
    raise ValueError("no table with an 'Identifier' column found in README")


def render_research(rows):
    if not rows:
        return [f'{INDENT}<p class="lede">Research coming soon.</p>']
    out = []
    for r in rows:
        ident = r.get("identifier", "")
        m = MD_LINK.search(ident)
        if m:
            id_text, id_url = m.group(1), abs_url(m.group(2))
            id_html = f'<a class="cve" href="{html.escape(id_url)}">{html.escape(id_text)}</a>'
        else:
            id_html = f'<span class="cve">{html.escape(ident)}</span>'
        name = r.get("name", "")
        title = f"{id_html} · {md_inline(name)}" if name else id_html

        meta = [md_inline(x) for x in (r.get("disclosed", ""), r.get("impact", "")) if x]
        refs = [(t, abs_url(u)) for t, u in MD_LINK.findall(r.get("references", ""))]

        out.append(f'{INDENT}<div class="card">')
        out.append(f"{INDENT}  <h3>{title}</h3>")
        if r.get("summary"):
            out.append(f"{INDENT}  <p>{md_inline(r['summary'])}</p>")
        if meta:
            out.append(f'{INDENT}  <p class="meta">{" · ".join(meta)}</p>')
        if refs:
            links = "".join(f'<a href="{html.escape(u)}">{html.escape(t)}</a>' for t, u in refs)
            out.append(f'{INDENT}  <p class="refs">{links}</p>')
        out.append(f"{INDENT}</div>")
    return out


def replace_block(text, marker, lines):
    pattern = re.compile(
        rf"(<!-- {marker}:START[^>]*-->)(.*?)([ \t]*<!-- {marker}:END -->)", re.S)
    if not pattern.search(text):
        sys.exit(f"Marker {marker}:START / {marker}:END not found in index.html")
    body = "\n".join(lines)
    return pattern.sub(
        lambda m: f"{m.group(1)}\n{body}\n{INDENT}<!-- {marker}:END -->", text, count=1)


def main():
    original = INDEX.read_text(encoding="utf-8")
    text = original

    try:
        repos = get_repos()
        text = replace_block(text, "PROJECTS", render_repos(repos))
        print(f"Projects: {len(repos)} repos")
    except Exception as e:  # leave the section untouched on failure
        print(f"Projects: skipped ({e})")

    if MEDIUM_HANDLE:
        try:
            items = get_articles()
            text = replace_block(text, "WRITING", render_articles(items))
            print(f"Writing: {len(items)} articles")
        except Exception as e:
            print(f"Writing: skipped ({e})")
    else:
        print("Writing: skipped (MEDIUM_HANDLE not set)")

    try:
        rows = get_research()
        text = replace_block(text, "RESEARCH", render_research(rows))
        print(f"Research: {len(rows)} entries")
    except Exception as e:
        print(f"Research: skipped ({e})")

    if text == original:
        print("No changes.")
        return

    INDEX.write_text(text, encoding="utf-8")
    today = dt.date.today().isoformat()
    sm = SITEMAP.read_text(encoding="utf-8")
    sm = re.sub(r"<lastmod>[^<]*</lastmod>", f"<lastmod>{today}</lastmod>", sm, count=1)
    SITEMAP.write_text(sm, encoding="utf-8")
    print(f"Updated index.html and sitemap.xml ({today}).")


if __name__ == "__main__":
    main()
