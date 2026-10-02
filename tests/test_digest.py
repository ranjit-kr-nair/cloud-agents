import json
from datetime import datetime, timedelta, timezone

import pytest

import digest

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
PERSON = {"name": "Ada", "category": "AI", "x": "ada", "bluesky": "ada.bsky.social"}


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def make_post(hours_ago: float, url: str, person: str = "Ada") -> digest.Post:
    return digest.Post(person, "AI", "Bluesky", NOW - timedelta(hours=hours_ago), "hi", url)


def test_parse_bluesky_builds_links_and_marks_reposts():
    data = {
        "feed": [
            {"post": {"uri": "at://did:plc:x/app.bsky.feed.post/abc", "author": {"handle": "ada.bsky.social"},
                      "record": {"text": "Hello <world>", "createdAt": iso(1)}}},
            {"post": {"uri": "at://did:plc:y/app.bsky.feed.post/def", "author": {"handle": "bob.bsky.social"},
                      "record": {"text": "Bob's post", "createdAt": iso(30)}},
             "reason": {"$type": "app.bsky.feed.defs#reasonRepost", "indexedAt": iso(2)}},
        ]
    }
    posts = digest.parse_bluesky(data, PERSON)
    assert posts[0].url == "https://bsky.app/profile/ada.bsky.social/post/abc"
    assert posts[0].published == NOW - timedelta(hours=1)
    assert posts[1].text == "[Repost of @bob.bsky.social] Bob's post"
    assert posts[1].published == NOW - timedelta(hours=2), "reposts are dated by when they were reposted"


def test_parse_feed_combines_title_and_summary_and_strips_html():
    rss = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>Big News</title><link>https://example.com/a</link>
        <description>&lt;p&gt;Details &amp;amp; more&lt;/p&gt;</description>
        <pubDate>{(NOW - timedelta(hours=3)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
    </channel></rss>""".encode()
    [post] = digest.parse_feed(rss, PERSON, "Newsletter/Blog")
    assert post.text == "Big News: Details & more"
    assert post.url == "https://example.com/a"
    assert post.published == NOW - timedelta(hours=3)


def test_parse_x_syndication_reads_embedded_timeline():
    payload = {"props": {"pageProps": {"timeline": {"entries": [
        {"content": {"tweet": {"id_str": "42", "full_text": "Ship it", "user": {"screen_name": "ada"},
                               "created_at": "Fri Oct 02 06:00:00 +0000 2026"}}},
        {"content": {}},
    ]}}}}
    page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'
    [post] = digest.parse_x_syndication(page, PERSON)
    assert (post.platform, post.text, post.url) == ("X", "Ship it", "https://x.com/ada/status/42")
    assert post.published == NOW - timedelta(hours=2)


def test_parse_x_syndication_raises_when_blocked():
    with pytest.raises(ValueError, match="blocked"):
        digest.parse_x_syndication("<html>Rate limit exceeded</html>", PERSON)


def test_mastodon_feed_url():
    assert digest.mastodon_feed_url("@simon@fedi.simonwillison.net") == "https://fedi.simonwillison.net/@simon.rss"
    with pytest.raises(ValueError):
        digest.mastodon_feed_url("simon")


def test_collect_filters_window_dedupes_caps_and_isolates_failures(monkeypatch):
    def fake_bluesky(person):
        return [make_post(1, "u1"), make_post(2, "u2"), make_post(3, "u3"), make_post(25, "old")]

    def broken_x(person):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(digest, "fetch_bluesky", fake_bluesky)
    monkeypatch.setattr(digest, "fetch_x", broken_x)
    monkeypatch.setattr(digest, "fetch_feed", lambda url, person, platform: [make_post(0.5, "u1")])

    config = {"window_hours": 24, "max_posts_per_person": 2,
              "people": [{**PERSON, "rss": ["https://example.com/feed"]}]}
    posts, results = digest.collect(config, NOW)

    assert [p.url for p in posts] == ["u1", "u2"], "newest first, duplicates dropped, capped at 2"
    errors = [r for r in results if r.error]
    assert len(errors) == 1 and "429" in errors[0].error


def test_render_markdown_escapes_pipes_and_lists_quiet_people_and_failures():
    post = digest.Post("Ada", "AI", "X", NOW - timedelta(hours=1), "a | b", "https://x.com/ada/status/1")
    config = {"window_hours": 24, "display_timezone": "UTC",
              "people": [{"name": "Ada"}, {"name": "Grace"}]}
    failure = digest.SourceResult("Grace", "X @grace", error="HTTPError: 429")
    out = digest.render_markdown([post], [failure], config, NOW)

    assert "| Oct 02 07:00 | Ada | AI | X | a \\| b | [open](https://x.com/ada/status/1) |" in out
    assert "**No posts in window:** Grace" in out
    assert "Grace / X @grace" in out


def test_main_writes_markdown_csv_and_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "collect", lambda config, now: ([make_post(1, "https://e.com/1")], []))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("people:\n  - name: Ada\n")
    assert digest.main(["--config", str(cfg), "--out", str(tmp_path / "out")]) == 0

    files = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert "latest.md" in files and any(f.endswith(".csv") for f in files)
    csv_text = next((tmp_path / "out").glob("*.csv")).read_text()
    assert csv_text.splitlines()[0] == "time,person,category,platform,post,url"
    assert "https://e.com/1" in csv_text
