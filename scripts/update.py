#!/usr/bin/env python3
"""Refresh the auto-generated sections of index.html.

- Writing: your latest Medium posts, read from your Medium RSS feed.
- Open-Source Projects: every public, non-fork, non-archived repo you own,
  sorted by stars (high to low), then most recently updated, followed by
  other people's repos where you have merged pull requests (Contributor).
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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
SITEMAP = ROOT / "sitemap.xml"

GH_USER = os.environ.get("GH_USER") or "kampji"
MEDIUM_HANDLE = (os.environ.get("MEDIUM_HANDLE") or "jillkamperides").lstrip("@")
RESEARCH_REPO = os.environ.get("RESEARCH_REPO") or f"{GH_USER}/My-Security-Research"
RESEARCH_BRANCH = os.environ.get("RESEARCH_BRANCH") or "main"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "6"))

# Other people's repos to leave out of the contributions list, as "owner/name".
SKIP_CONTRIB = set()

# Your repos that publish a website (GitHub Pages). Their tile links to the
# live site and shows the latest version and release from the site's own pages,
# instead of linking to the repo. Repo name -> site URL.
SITES = {
    "finside": f"https://{GH_USER}.github.io/finside/",
}
SITE_RELEASES_PAGE = "releases.html"

# Type badges, from each repo's GitHub topics. Checked top to bottom; the first
# badge with a matching topic wins, so each tile gets at most one. A repo with
# none of these topics gets no badge. Edit freely: badge label -> topics.
TOPIC_BADGES = [
    ("Security", {"security", "cybersecurity", "cybersecurity-tools", "infosec",
                  "pentesting", "penetration-testing", "pentest", "red-team",
                  "vulnerability-scanner", "web-app-security", "offensive-security"}),
    ("Game", {"game", "game-development", "gamedev", "idle-game"}),
    ("RF & Satellites", {"satellite", "satellites", "satellite-tracking", "sdr",
                         "rtl-sdr", "ham-radio", "amateur-radio", "rf", "radio"}),
    ("Tool", {"tool", "tools", "cli", "utility", "automation", "pdf"}),
]


def type_badge(r):
    topics = {t.lower() for t in r.get("topics") or []}
    for label, wanted in TOPIC_BADGES:
        if topics & wanted:
            return label
    return ""

# Repos not worth listing as projects: the site itself, the profile README,
# and the research repo (it has its own Security Research section).
SKIP_REPOS = {f"{GH_USER}.github.io".lower(), GH_USER.lower(),
              RESEARCH_REPO.split("/")[-1].lower()}

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
    for r in repos:
        site = SITES.get(r["name"].lower())
        if site:
            r["_site"] = site
            try:
                enrich_site(r)
            except Exception as e:  # still show a basic tile linking the site
                print(f"Site {r['name']}: details skipped ({e})")
    return sort_repos(repos)


def get_contributions():
    """Other people's repos where you have merged pull requests.

    Found automatically with GitHub search, so a new merged PR shows up on its
    own. Each repo gets a "Contributor" badge and a link to your PRs there.
    """
    q = urllib.parse.quote(f"type:pr author:{GH_USER} is:merged -user:{GH_USER}")
    res = json.loads(fetch(f"https://api.github.com/search/issues?q={q}&per_page=100",
                           "application/vnd.github+json"))
    counts = {}
    for item in res.get("items", []):
        full = item["repository_url"].split("/repos/", 1)[1]
        counts[full] = counts.get(full, 0) + 1
    repos = []
    for full, n in counts.items():
        if full.lower() in SKIP_CONTRIB:
            continue
        r = json.loads(fetch(f"https://api.github.com/repos/{full}",
                             "application/vnd.github+json"))
        if r.get("private"):
            continue
        r["_contrib"] = n
        r["_pr_url"] = (f"https://github.com/{full}/pulls?q="
                        + urllib.parse.quote(f"is:pr author:{GH_USER} is:merged"))
        repos.append(r)
    return sort_repos(repos)


def sort_repos(repos):
    # Stars (high to low), then most recently pushed, then name.
    # Python's sort is stable, so sort by the least important key first.
    repos.sort(key=lambda r: r["name"].lower())
    repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    repos.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
    return repos


def raw_file(r, path):
    branch = r.get("default_branch") or "main"
    url = f"https://raw.githubusercontent.com/{r['full_name']}/{branch}/{path}"
    return fetch(url, "text/html").decode("utf-8", "replace")


def enrich_site(r):
    """Read the site's title, plus the newest release from its release notes.

    Release notes are expected to look like:
        <h2>Release name</h2>
        <p class="sub">Version 0.3.0 · October 25, 2026</p>
    """
    page = raw_file(r, "index.html")
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    if m:
        r["_title"] = html_to_text(m.group(1))
    notes = raw_file(r, SITE_RELEASES_PAGE)
    m = re.search(
        r"<h2[^>]*>((?:(?!</h2>).)*)</h2>\s*<p[^>]*>\s*Version\s+([\w.\-]+)\s*"
        r"(?:·|&middot;|&#183;|-|\|)\s*([^<]+?)\s*</p>", notes, re.S | re.I)
    if m:
        r["_release"] = html_to_text(m.group(1))
        r["_version"] = m.group(2)
        try:
            d = dt.datetime.strptime(html_to_text(m.group(3)), "%B %d, %Y")
            r["_release_date"] = f"{d:%b} {d.day}, {d.year}"
        except ValueError:
            r["_release_date"] = html_to_text(m.group(3))
        r["_releases_url"] = r["_site"].rstrip("/") + "/" + SITE_RELEASES_PAGE


def render_repos(repos):
    if not repos:
        return [f'{INDENT}<p class="lede">Projects coming soon.</p>']
    out = []
    for r in repos:
        contrib = r.get("_contrib")
        site = r.get("_site")
        # Show owner/name for other people's repos so it's clear whose project it is.
        if site:
            name = html.escape(r.get("_title") or r["name"])
        else:
            name = html.escape(r["full_name"] if contrib else r["name"])
        url = html.escape(site or r["html_url"])
        desc = (r.get("description") or "").strip()
        meta = []
        if r.get("_version"):
            v = f"v{html.escape(r['_version'])}"
            if r.get("_release_date"):
                v += f" · {html.escape(r['_release_date'])}"
            meta.append(v)
            meta.append(f'<a href="{html.escape(r["_releases_url"])}">Release notes</a>')
        elif r.get("language") and not site:
            meta.append(html.escape(r["language"]))
        stars = r.get("stargazers_count", 0)
        if stars:
            meta.append(f"★ {stars}")
        if contrib:
            label = f"{contrib} merged PR" + ("s" if contrib != 1 else "")
            meta.append(f'<a href="{html.escape(r["_pr_url"])}">{label}</a>')
        badge = ""
        kind = type_badge(r)
        if kind:
            badge += f' <span class="badge">{html.escape(kind)}</span>'
        if contrib:  # your role, not the project type: filled style
            badge += ' <span class="badge solid">Contributor</span>'

        out.append(f'{INDENT}<div class="card">')
        out.append(f'{INDENT}  <h3><a href="{url}">{name}</a>{badge}</h3>')
        if desc:
            out.append(f"{INDENT}  <p>{html.escape(desc)}</p>")
        if meta:
            out.append(f'{INDENT}  <p class="meta">{" · ".join(meta)}</p>')
        out.append(f"{INDENT}</div>")
    return out


CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
DESC_MAX = 220


def html_to_text(fragment):
    text = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def article_description(content):
    """Medium puts the article's subtitle in an <h4> at the top of the content.
    Fall back to the first paragraph, trimmed, if there's no subtitle."""
    if not content:
        return ""
    head = content.lstrip()[:600]
    m = re.match(r"(?:<h3[^>]*>.*?</h3>\s*)?<h4[^>]*>(.*?)</h4>", head, re.S)
    if m:
        return html_to_text(m.group(1))
    m = re.search(r"<p[^>]*>(.*?)</p>", content, re.S)
    if not m:
        return ""
    text = html_to_text(m.group(1))
    if len(text) > DESC_MAX:
        text = text[:DESC_MAX].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return text


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
            d = dt.datetime.strptime(pub[:16], "%a, %d %b %Y")
            date = f"{d:%b} {d.day}, {d.year}"
        except ValueError:
            date = ""
        desc = article_description(item.findtext(CONTENT_NS) or "")
        if title and link:
            items.append((title, link, date, desc))
    return items


def render_articles(items):
    if not items:
        return [f'{INDENT}<p class="lede">Articles coming soon.</p>']
    out = []
    for title, link, date, desc in items:
        out.append(f'{INDENT}<div class="card">')
        out.append(f'{INDENT}  <h3><a href="{html.escape(link)}">{html.escape(title)}</a></h3>')
        if desc:
            out.append(f"{INDENT}  <p>{html.escape(desc)}</p>")
        meta = " · ".join(x for x in ("Medium", date) if x)
        out.append(f'{INDENT}  <p class="meta">{meta}</p>')
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
        try:
            contribs = get_contributions()
        except Exception as e:  # still show your own repos if search fails
            contribs = []
            print(f"Contributions: skipped ({e})")
        # Your own projects first, then repos you've contributed to.
        text = replace_block(text, "PROJECTS", render_repos(repos + contribs))
        print(f"Projects: {len(repos)} repos, {len(contribs)} contributions")
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
