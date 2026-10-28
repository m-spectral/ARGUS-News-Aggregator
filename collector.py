#!/usr/bin/env python3
"""Public RSS/Atom collector, conservative deduplication, and delivery queue.

Python 3.9+, standard library only. No model calls or secret values in this file.
"""
import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from settings import load_env

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
LANES = ("business", "practice", "news", "primary", "research")
TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi"}
_last_request = {}


def now():
    return datetime.now(UTC).isoformat()


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean(value):
    parser = TextOnly()
    parser.feed(value or "")
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def normalized(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def canonical_url(value):
    """Remove known tracking parameters, preserving parameters that change content."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
        raise ValueError("Not a public HTTP article URL")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.casefold().startswith("utm_") and k.casefold() not in TRACKING]
    host = parts.hostname.lower().removeprefix("www.")
    port = parts.port
    if port and port not in (80, 443):
        host += f":{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def arxiv_identity(url):
    parts = urlsplit(url)
    if parts.hostname not in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        return None
    match = re.match(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?$", parts.path)
    if not match:
        return None
    return re.sub(r"v\d+$", "", match.group(1))


def identities(article):
    keys = ["url:" + canonical_url(article["url"])]
    paper = arxiv_identity(article["url"])
    if paper:
        keys.append("arxiv:" + paper)
    # RSS IDs are scoped to the publication, never globally trusted.
    if article.get("guid"):
        keys.append("guid:" + article["publisher"] + ":" + article["guid"])
    # Exact title AND substantive exact excerpt, not fuzzy headline matching.
    title, excerpt = normalized(article["title"]), normalized(article["excerpt"])
    if len(title) >= 40 and len(excerpt) >= 200:
        digest = hashlib.sha256((title + "\n" + excerpt).encode()).hexdigest()
        keys.append("content:" + digest)
    return keys


def date_iso(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC).isoformat()


def tag_name(element):
    return element.tag.rsplit("}", 1)[-1].lower()


def field(element, *names):
    for name in names:
        for child in element:
            if tag_name(child) == name:
                return "".join(child.itertext()).strip()
    return ""


def parse_feed(data, source):
    root = ET.fromstring(data)
    if tag_name(root) not in ("rss", "rdf", "feed"):
        raise ValueError("Response is not an RSS or Atom feed")
    articles = []
    rejected = 0
    for entry in root.iter():
        if tag_name(entry) not in ("item", "entry"):
            continue
        url = ""
        for child in entry:
            if tag_name(child) == "link" and child.get("rel", "alternate") == "alternate":
                url = child.get("href") or (child.text or "").strip()
                if url:
                    break
        title = clean(field(entry, "title"))
        if not title or not url:
            rejected += 1
            continue
        url = urljoin(source["url"], url)
        try:
            canonical_url(url)
        except ValueError:
            rejected += 1
            continue
        articles.append({
            "title": title, "url": url, "publisher": source["publisher"],
            "lane": source["lane"], "guid": field(entry, "guid", "id"),
            "published": date_iso(field(entry, "published", "pubdate", "date", "updated")),
            "excerpt": clean(field(entry, "encoded", "content", "description", "summary")),
            "source_id": source["id"], "feed_url": source["url"]
        })
    return articles, rejected


def fetch(url, *, headers=None, params=None, retries=2):
    """Bound GET retries; never expose request URLs containing API credentials."""
    if params:
        url += "?" + urlencode(params)
    host = urlsplit(url).hostname
    delay = 3.1 if host and host.endswith("arxiv.org") else 1.0
    time.sleep(max(0, delay - (time.monotonic() - _last_request.get(host, 0))))
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": "StandardNewsBot/1.0", **(headers or {})})
            _last_request[host] = time.monotonic()
            with urlopen(request, timeout=30) as response:
                body = response.read(10_000_001)
                if len(body) > 10_000_000:
                    raise ValueError("Response exceeded 10 MB")
                return body, dict(response.headers), response.geturl()
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise RuntimeError(f"HTTP {error.code}") from None
            wait = error.headers.get("Retry-After", "")
            if wait.isdigit() and int(wait) > 30:
                raise RuntimeError(f"HTTP {error.code}; retry later") from None
            time.sleep(int(wait) if wait.isdigit() else 2 ** (attempt + 1))
        except (URLError, TimeoutError, OSError):
            if attempt == retries:
                raise RuntimeError("Connection or timeout error") from None
            time.sleep(2 ** (attempt + 1))


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript('''
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
            publisher TEXT NOT NULL, lane TEXT NOT NULL, excerpt TEXT NOT NULL,
            published TEXT, collected TEXT NOT NULL, sent TEXT
        );
        CREATE TABLE IF NOT EXISTS aliases (
            alias TEXT PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id)
        );
        CREATE TABLE IF NOT EXISTS sightings (
            article_id INTEGER REFERENCES articles(id), source_id TEXT, feed_url TEXT,
            observed TEXT, PRIMARY KEY(article_id, source_id)
        );
    ''')
    return db


def store_article(db, article):
    keys = identities(article)
    placeholders = ",".join("?" * len(keys))
    matches = db.execute(
        f"SELECT DISTINCT article_id FROM aliases WHERE alias IN ({placeholders})", keys
    ).fetchall()
    with db:
        if matches:
            article_id = min(row[0] for row in matches)
            # Merge previously separate records if a new observation bridges aliases.
            for row in matches:
                other = row[0]
                if other == article_id:
                    continue
                db.execute("UPDATE aliases SET article_id=? WHERE article_id=?", (article_id, other))
                db.execute("INSERT OR IGNORE INTO sightings SELECT ?,source_id,feed_url,observed FROM sightings WHERE article_id=?", (article_id, other))
                db.execute("UPDATE articles SET sent=COALESCE(sent,(SELECT sent FROM articles WHERE id=?)) WHERE id=?", (other, article_id))
                db.execute("DELETE FROM sightings WHERE article_id=?", (other,))
                db.execute("DELETE FROM articles WHERE id=?", (other,))
            # Preserve first identity; retain the longest available feed excerpt.
            db.execute("UPDATE articles SET excerpt=CASE WHEN length(excerpt)<? THEN ? ELSE excerpt END, published=COALESCE(published,?) WHERE id=?", (len(article["excerpt"]), article["excerpt"], article["published"], article_id))
        else:
            result = db.execute("INSERT INTO articles(title,url,publisher,lane,excerpt,published,collected) VALUES(?,?,?,?,?,?,?)", (article["title"], article["url"], article["publisher"], article["lane"], article["excerpt"], article["published"], now()))
            article_id = result.lastrowid
        for key in keys:
            db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (key, article_id))
        db.execute("INSERT OR REPLACE INTO sightings VALUES(?,?,?,?)", (article_id, article["source_id"], article["feed_url"], now()))
    return not bool(matches)


def api_articles(provider, config):
    if provider == "newsapi_api":
        raise RuntimeError("NewsAPI.API needs its official endpoint and authentication documentation; no request was sent")
    key = os.environ.get(config["env"])
    if not key:
        raise RuntimeError(f"Missing environment variable {config['env']}")
    result, pages, incomplete = [], 0, False
    token = None
    for page in range(1, config.get("max_pages", 1) + 1):
        query = config["query"]
        if provider == "newsapi":
            url = "https://newsapi.org/v2/everything"
            params = {"q": query, "language": "en", "sortBy": "publishedAt", "pageSize": 100, "page": page}
            headers = {"X-Api-Key": key}
        elif provider == "gnews":
            url = "https://gnews.io/api/v4/search"
            params = {"q": query, "lang": "en", "sortby": "publishedAt", "max": 10, "page": page, "apikey": key}
            headers = {}
        elif provider == "newsdata":
            url = "https://newsdata.io/api/1/latest"
            params = {"q": query, "language": "en", "apikey": key}
            if token:
                params["page"] = token
            headers = {}
        else:
            raise ValueError("Unknown API provider")
        body, _, _ = fetch(url, headers=headers, params=params, retries=0)
        data = json.loads(body)
        if provider == "newsapi" and data.get("status") != "ok":
            raise RuntimeError("Provider returned an API error")
        if provider == "newsdata" and data.get("status") != "success":
            raise RuntimeError("Provider returned an API error")
        if provider == "gnews" and "articles" not in data:
            raise RuntimeError("Provider returned an API error")
        rows = data.get("results" if provider == "newsdata" else "articles", [])
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected API response shape")
        pages += 1
        for item in rows:
            link = item.get("url") or item.get("link") or ""
            title = clean(item.get("title"))
            if not title or not link:
                continue
            try:
                publisher = urlsplit(canonical_url(link)).hostname
            except ValueError:
                continue
            result.append({"title": title, "url": link, "publisher": publisher,
                "lane": "news", "excerpt": clean(item.get("description") or item.get("content")),
                "published": date_iso(item.get("publishedAt") or item.get("pubDate")),
                "guid": "", "source_id": "api:" + provider, "feed_url": url})
        total = data.get("totalResults", data.get("totalArticles", 0))
        token = data.get("nextPage")
        incomplete = bool(token) if provider == "newsdata" else total > page * (100 if provider == "newsapi" else 10)
        if not rows or not incomplete:
            break
    return result, {"pages": pages, "pagination_capped": incomplete}


def selected_sources(config, lanes=None):
    return [s for s in config["feeds"] if s.get("enabled", True) and (not lanes or s["lane"] in lanes)]


def collect(config, db, args):
    report = {"started": now(), "sources": [], "inserted": 0, "duplicates": 0, "older_skipped": 0}
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    def accept(articles):
        for article in articles:
            if article["published"] and datetime.fromisoformat(article["published"]) < cutoff:
                report["older_skipped"] += 1
                continue
            if store_article(db, article):
                report["inserted"] += 1
            else:
                report["duplicates"] += 1
    for source in selected_sources(config, args.lane):
        status = {"source": source["id"], "url": source["url"]}
        try:
            body, _, final_url = fetch(source["url"])
            articles, rejected = parse_feed(body, source)
            status.update(status="ok" if articles else "empty", entries=len(articles), rejected=rejected, resolved_url=final_url)
            if not args.check:
                accept(articles)
        except (RuntimeError, ValueError, ET.ParseError) as error:
            status.update(status="error", error=type(error).__name__ + ": " + str(error)[:160])
        report["sources"].append(status)
        print(source["id"], status["status"], status.get("entries", ""), status.get("error", ""), flush=True)
    if args.apis and not args.check:
        for provider, api in config.get("apis", {}).items():
            if not api.get("enabled"):
                continue
            status = {"source": "api:" + provider}
            try:
                articles, details = api_articles(provider, api)
                accept(articles)
                status.update(status="ok", entries=len(articles), **details)
            except (RuntimeError, ValueError) as error:
                status.update(status="error", error=str(error)[:160])
            report["sources"].append(status)
            print(status["source"], status["status"], flush=True)
    report["finished"] = now()
    return report


def pending(db, days):
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    # Undated entries remain visible, explicitly labelled in every output.
    rows = db.execute("SELECT * FROM articles WHERE sent IS NULL AND (published IS NULL OR published>=?) ORDER BY COALESCE(published,collected) DESC", (cutoff,)).fetchall()
    return [dict(row) for row in rows]


def balanced_digest(rows, per_lane=3, per_publisher=2):
    counts, lanes = Counter(), defaultdict(int)
    selected = []
    for lane in LANES:
        for row in rows:
            if row["lane"] != lane or lanes[lane] >= per_lane or counts[row["publisher"]] >= per_publisher:
                continue
            selected.append(row)
            lanes[lane] += 1
            counts[row["publisher"]] += 1
    return selected


def related_candidates(rows):
    # Review only. Similarity never changes deduplication or delivery state.
    result = []
    for i, left in enumerate(rows):
        for right in rows[i + 1:]:
            if left["publisher"] == right["publisher"]:
                continue
            score = SequenceMatcher(None, normalized(left["title"]), normalized(right["title"])).ratio()
            if score >= 0.82:
                result.append({"left_id": left["id"], "right_id": right["id"], "title_similarity": round(score, 3), "decision": "retain_both_review_only"})
    return result


def render_digest(rows):
    lines = ["AI Reading Brief", "Feed excerpts only. Research entries are preprints and are not treated as peer reviewed.", ""]
    for lane in LANES:
        items = [row for row in rows if row["lane"] == lane]
        if not items:
            continue
        lines.extend([lane.upper(), ""])
        for row in items:
            lines.extend([row["title"], f"{row['publisher']} | {row['published'] or 'Publication date unavailable'}", row["url"]])
            excerpt = row["excerpt"]
            if excerpt:
                lines.append("Feed excerpt: " + excerpt[:900] + ("..." if len(excerpt) > 900 else ""))
            else:
                lines.append("No excerpt available; open the source.")
            lines.append("")
    return "\n".join(lines)


def export(db, output, args, report=None):
    output.mkdir(parents=True, exist_ok=True)
    rows = [dict(row) for row in db.execute("SELECT * FROM articles ORDER BY COALESCE(published,collected) DESC")]
    for row in rows:
        row["sightings"] = [dict(s) for s in db.execute("SELECT source_id,feed_url,observed FROM sightings WHERE article_id=?", (row["id"],))]
    (output / "articles.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    selected = balanced_digest(pending(db, args.days), args.per_lane, args.per_publisher)
    (output / "digest.txt").write_text(render_digest(selected), encoding="utf-8")
    (output / "digest_selection.json").write_text(json.dumps([row["id"] for row in selected]), encoding="utf-8")
    (output / "related_candidates.json").write_text(json.dumps(related_candidates(selected), indent=2), encoding="utf-8")
    if report:
        (output / "feed_health.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return selected


def slack_chunks(text, maximum=3000):
    # Split at paragraph boundaries while staying comfortably below message limits.
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > maximum:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:maximum])
            line = line[maximum:]
        if len(current) + len(line) > maximum:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def send(db, rows, webhook):
    """One article per bounded group, saved immediately after acknowledged delivery."""
    for row in rows:
        text = render_digest([row])
        for chunk in slack_chunks(text):
            payload = json.dumps({"text": chunk, "mrkdwn": False, "unfurl_links": False, "unfurl_media": False}).encode()
            request = Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urlopen(request, timeout=30) as response:
                    if response.status != 200 or response.read().strip() != b"ok":
                        raise RuntimeError("Slack did not acknowledge delivery")
            except (URLError, TimeoutError, OSError):
                raise RuntimeError("Slack delivery unconfirmed; unsent items remain queued") from None
            time.sleep(1.1)
        with db:
            db.execute("UPDATE articles SET sent=? WHERE id=?", (now(), row["id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "collect", "preview", "send"))
    parser.add_argument("--config", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--db", type=Path, default=ROOT / "data/news.sqlite3")
    parser.add_argument("--out", type=Path, default=ROOT / "reports")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--lane", action="append", choices=LANES)
    parser.add_argument("--apis", action="store_true", help="Call enabled API adapters; may consume plan credits")
    parser.add_argument("--per-lane", type=int, default=3)
    parser.add_argument("--per-publisher", type=int, default=2)
    args = parser.parse_args()
    load_env(ROOT / ".env")
    if min(args.days, args.per_lane, args.per_publisher) < 1:
        parser.error("Days and digest limits must be positive")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    db = connect(args.db)
    try:
        if args.command in ("check", "collect"):
            args.check = args.command == "check"
            report = collect(config, db, args)
            if args.check:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / "feed_health.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            else:
                export(db, args.out, args, report)
            failures = sum(s["status"] != "ok" for s in report["sources"])
            print(f"New: {report['inserted']} | duplicate observations: {report['duplicates']} | unhealthy sources: {failures}")
            if not report["sources"] or all(s["status"] == "error" for s in report["sources"]):
                raise RuntimeError("No source succeeded; inspect feed_health.json")
        else:
            rows = export(db, args.out, args)
            if args.command == "preview":
                print(render_digest(rows))
                print(f"Selected {len(rows)} articles. Full archive: {args.out / 'articles.json'}")
            elif not rows:
                print("No eligible unsent articles")
            else:
                webhook = os.environ.get("SLACK_WEBHOOK_URL")
                if not webhook:
                    raise RuntimeError("Set SLACK_WEBHOOK_URL before sending")
                send(db, rows, webhook)
                print(f"Delivered {len(rows)} articles. Additional items remain queued.")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        raise SystemExit(f"Stopped: {error}")
