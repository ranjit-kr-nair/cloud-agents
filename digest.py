"""Collect the last N hours of posts from a list of influencers into a Markdown table and CSV.

Only free, key-less sources are used: Bluesky's public API, RSS/Atom feeds (Substack, blogs,
Mastodon, YouTube) and, best effort, X/Twitter.

Usage:
    python digest.py [--config influencers.yaml] [--out digests] [--hours 24]
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 20
BLUESKY_FEED_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
X_SYNDICATION_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"


@dataclass
class Post:
    person: str
    category: str
    platform: str
    published: datetime
    text: str
    url: str


@dataclass
class SourceResult:
    person: str
    source: str
    posts: list[Post] = field(default_factory=list)
    error: str | None = None


def http_get(url: str, attempts: int = 3, **kwargs) -> requests.Response:
    # YouTube feeds intermittently 404 and many hosts throw transient 5xx; 401/403/429 won't recover.
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            status = getattr(exc.response, "status_code", None)
            if attempt == attempts or status in (401, 403, 410, 429, 451):
                raise
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def parse_time(value: str) -> datetime:
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            dt = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_text(raw: str, limit: int = 280) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


# --- Bluesky ------------------------------------------------------------------------------


def parse_bluesky(data: dict, person: dict) -> list[Post]:
    posts = []
    for item in data.get("feed", []):
        post = item.get("post", {})
        record = post.get("record", {})
        author = post.get("author", {}).get("handle", "")
        rkey = post.get("uri", "").rsplit("/", 1)[-1]
        text = record.get("text", "")
        when = record.get("createdAt")
        reason = item.get("reason") or {}
        if reason.get("$type", "").endswith("reasonRepost"):
            text = f"[Repost of @{author}] {text}"
            when = reason.get("indexedAt", when)
        if not when:
            continue
        posts.append(
            Post(
                person=person["name"],
                category=person.get("category", ""),
                platform="Bluesky",
                published=parse_time(when),
                text=clean_text(text),
                url=f"https://bsky.app/profile/{author}/post/{rkey}",
            )
        )
    return posts


def fetch_bluesky(person: dict) -> list[Post]:
    resp = http_get(
        BLUESKY_FEED_URL,
        params={"actor": person["bluesky"], "limit": 50, "filter": "posts_no_replies"},
    )
    return parse_bluesky(resp.json(), person)


# --- RSS / Atom (blogs, Substack, Mastodon, YouTube, custom X bridges) --------------------


def parse_feed(content: bytes, person: dict, platform: str) -> list[Post]:
    feed = feedparser.parse(content)
    posts = []
    for entry in feed.entries:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not stamp:
            continue
        title = clean_text(entry.get("title", ""), 200)
        summary = clean_text(entry.get("summary", ""), 200)
        if platform in ("Mastodon", "X"):
            text = clean_text(entry.get("summary", "") or entry.get("title", ""))
        elif title and summary and not summary.startswith(title):
            text = clean_text(f"{title}: {summary}")
        else:
            text = title or summary
        posts.append(
            Post(
                person=person["name"],
                category=person.get("category", ""),
                platform=platform,
                published=datetime(*stamp[:6], tzinfo=timezone.utc),
                text=text,
                url=entry.get("link", ""),
            )
        )
    return posts


def fetch_feed(url: str, person: dict, platform: str) -> list[Post]:
    return parse_feed(http_get(url).content, person, platform)


def mastodon_feed_url(address: str) -> str:
    user, _, instance = address.lstrip("@").partition("@")
    if not instance:
        raise ValueError(f"Mastodon address must look like @user@instance, got {address!r}")
    return f"https://{instance}/@{user}.rss"


# --- X / Twitter (best effort) ------------------------------------------------------------


def parse_x_syndication(page: str, person: dict) -> list[Post]:
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if not match:
        raise ValueError("X embed page had no timeline data (likely blocked or rate limited)")
    data = json.loads(match.group(1))
    entries = data.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries", [])
    posts = []
    for entry in entries:
        tweet = entry.get("content", {}).get("tweet")
        if not tweet:
            continue
        handle = tweet.get("user", {}).get("screen_name", person["x"])
        text = tweet.get("full_text") or tweet.get("text", "")
        if tweet.get("retweeted_status"):
            text = f"[Repost] {text}"
        posts.append(
            Post(
                person=person["name"],
                category=person.get("category", ""),
                platform="X",
                published=parse_time(tweet["created_at"]),
                text=clean_text(text),
                url=f"https://x.com/{handle}/status/{tweet.get('id_str', '')}",
            )
        )
    return posts


def fetch_x(person: dict) -> list[Post]:
    """Prefer a user-supplied RSS bridge (X_RSS_TEMPLATE / x_rss); fall back to X's embed endpoint."""
    template = person.get("x_rss") or os.environ.get("X_RSS_TEMPLATE")
    if template:
        return fetch_feed(template.format(handle=person["x"]), person, "X")
    return parse_x_syndication(http_get(X_SYNDICATION_URL.format(handle=person["x"])).text, person)


# --- Orchestration ------------------------------------------------------------------------


def build_jobs(person: dict) -> list[tuple[str, Callable[[], list[Post]]]]:
    jobs: list[tuple[str, Callable[[], list[Post]]]] = []
    if person.get("x"):
        jobs.append((f"X @{person['x']}", lambda: fetch_x(person)))
    if person.get("bluesky"):
        jobs.append((f"Bluesky {person['bluesky']}", lambda: fetch_bluesky(person)))
    if person.get("mastodon"):
        url = mastodon_feed_url(person["mastodon"])
        jobs.append((f"Mastodon {person['mastodon']}", lambda: fetch_feed(url, person, "Mastodon")))
    if person.get("youtube"):
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={person['youtube']}"
        jobs.append(("YouTube", lambda: fetch_feed(url, person, "YouTube")))
    for url in person.get("rss") or []:
        jobs.append((url, lambda url=url: fetch_feed(url, person, "Newsletter/Blog")))
    return jobs


def run_job(person: dict, source: str, job: Callable[[], list[Post]]) -> SourceResult:
    try:
        return SourceResult(person["name"], source, posts=job())
    except Exception as exc:  # one broken source must not sink the whole digest
        return SourceResult(person["name"], source, error=f"{type(exc).__name__}: {exc}"[:200])


def collect(config: dict, now: datetime) -> tuple[list[Post], list[SourceResult]]:
    cutoff = now - timedelta(hours=config.get("window_hours", 24))
    per_person = config.get("max_posts_per_person", 8)
    tasks = [(p, src, job) for p in config["people"] for src, job in build_jobs(p)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: run_job(*t), tasks))

    by_person: dict[str, list[Post]] = {}
    seen_urls: set[str] = set()
    for result in results:
        for post in result.posts:
            if cutoff <= post.published <= now + timedelta(minutes=5) and post.url not in seen_urls:
                seen_urls.add(post.url)
                by_person.setdefault(post.person, []).append(post)

    posts = []
    for person_posts in by_person.values():
        person_posts.sort(key=lambda p: p.published, reverse=True)
        posts.extend(person_posts[:per_person])
    posts.sort(key=lambda p: (p.category, p.person, -p.published.timestamp()))
    return posts, results


# --- Output -------------------------------------------------------------------------------


def md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(posts: list[Post], results: list[SourceResult], config: dict, now: datetime) -> str:
    tz = ZoneInfo(config.get("display_timezone", "UTC"))
    hours = config.get("window_hours", 24)
    people_with_posts = len({p.person for p in posts})
    lines = [
        f"# Influencer digest: {now.astimezone(tz):%Y-%m-%d}",
        "",
        f"{len(posts)} posts from {people_with_posts} of {len(config['people'])} people "
        f"in the {hours} hours before {now.astimezone(tz):%Y-%m-%d %H:%M %Z}.",
        "",
    ]
    if posts:
        lines += [
            f"| Time ({tz.key}) | Person | Category | Platform | Post | Link |",
            "|---|---|---|---|---|---|",
        ]
        for p in posts:
            lines.append(
                f"| {p.published.astimezone(tz):%b %d %H:%M} | {md_cell(p.person)} | {md_cell(p.category)} "
                f"| {p.platform} | {md_cell(p.text)} | [open]({p.url}) |"
            )
    else:
        lines.append("_No posts found in this window._")

    quiet = sorted({p["name"] for p in config["people"]} - {p.person for p in posts})
    if quiet:
        lines += ["", f"**No posts in window:** {', '.join(quiet)}"]

    failed = [r for r in results if r.error]
    if failed:
        lines += ["", "<details><summary>Sources that could not be fetched "
                  f"({len(failed)} of {len(results)})</summary>", ""]
        lines += [f"- {r.person} / {r.source}: `{md_cell(r.error)}`" for r in failed]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def write_csv(posts: list[Post], path: Path, tz: ZoneInfo) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "person", "category", "platform", "post", "url"])
        for p in posts:
            writer.writerow(
                [p.published.astimezone(tz).isoformat(timespec="minutes"), p.person, p.category,
                 p.platform, p.text, p.url]
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="influencers.yaml")
    parser.add_argument("--out", default="digests")
    parser.add_argument("--hours", type=int, help="override window_hours from the config")
    args = parser.parse_args(argv)

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.hours:
        config["window_hours"] = args.hours
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(config.get("display_timezone", "UTC"))

    posts, results = collect(config, now)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(tz):%Y-%m-%d}"
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(render_markdown(posts, results, config, now), encoding="utf-8")
    write_csv(posts, out_dir / f"{stem}.csv", tz)
    write_csv(posts, out_dir / "latest.csv", tz)
    (out_dir / "latest.md").write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"markdown={md_path}\ncsv={out_dir / f'{stem}.csv'}\n")

    failed = sum(1 for r in results if r.error)
    print(f"Wrote {md_path} with {len(posts)} posts ({failed}/{len(results)} sources failed)")
    for r in results:
        if r.error:
            print(f"  ! {r.person} / {r.source}: {r.error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
