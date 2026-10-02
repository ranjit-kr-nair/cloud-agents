# Daily PM & AI influencer digest

A small agent that, every morning, collects what a list of Product Management and AI influencers
posted in the last 24 hours and writes it into a table (`digests/YYYY-MM-DD.md` plus a `.csv`).
Optionally, Claude adds a short "Highlights" summary on top. It runs on free infrastructure and
uses no paid APIs.

```
GitHub Actions cron (free)
  └─ digest.py ── Bluesky public API ─┐
                ├─ RSS: Substack, blogs, Mastodon, YouTube
                └─ X/Twitter (best effort)
                         ▼
        digests/2026-10-02.md + .csv, latest.md, latest.csv
                         ▼
  optional: Claude Code adds "## Highlights" (billed to your Claude Pro plan, not the API)
                         ▼
          committed to the repo + shown in the Actions run summary
```

## What it costs

| Piece | Cost |
|---|---|
| Scheduler: GitHub Actions | Free. Public repos are unlimited; private repos get 2,000 min/month and this uses about 1 min/day. |
| Data: Bluesky API, RSS feeds, X embed endpoint | Free, no keys. |
| Summary: Claude Code with a `claude setup-token` OAuth token | Counts against your Claude Pro usage limits. No API key or per-token billing. |
| Alternative: Cursor Automation | Each run is billed at API rates against your Cursor usage (see "Option B"). |

## Setup (Option A, recommended: GitHub Actions)

1. Edit `influencers.yaml`: add or remove people and set `display_timezone`.
2. Set the time in `.github/workflows/daily-digest.yml`. GitHub cron is in UTC, and the file lists
   8 AM for common timezones. GitHub can start scheduled runs a few minutes late, especially on
   the hour.
3. Push to GitHub, open **Actions → Daily influencer digest → Run workflow** to try it immediately.
4. Optional AI highlights with Claude Pro:
   - Install Claude Code locally (`npm install -g @anthropic-ai/claude-code`) and run `claude setup-token`.
     Log in with your Claude Pro account; it prints a long-lived OAuth token.
   - In GitHub: **Settings → Secrets and variables → Actions → New repository secret**, name it
     `CLAUDE_CODE_OAUTH_TOKEN`, and paste the token. Do **not** set `ANTHROPIC_API_KEY`, or Claude Code
     will bill the API instead.
   - The step is `continue-on-error`, so if you hit your Pro limit the plain digest still gets published.

Each run commits `digests/<date>.md`, `digests/<date>.csv`, `digests/latest.md` and `digests/latest.csv`, and
renders the table on the run's summary page. Watch the repo (Custom → Actions) to get notified.

### Getting it into a Google Sheet or Doc

- Sheet that refreshes on its own (public repo): in cell A1, put
  `=IMPORTDATA("https://raw.githubusercontent.com/<you>/<repo>/main/digests/latest.csv")`.
- Doc: open `digests/latest.md` on GitHub, which renders the table, or paste the Markdown into Google Docs
  (Edit → Paste from Markdown, if enabled in Tools → Preferences).

## Option B: Cursor Automation (uses your Cursor Pro plan)

Cursor Pro includes Automations: scheduled Cloud Agents. Create one at
[cursor.com/automations](https://cursor.com/automations), attach this repo, choose a schedule such as
`CRON_TZ=America/New_York 0 8 * * *`, and paste the prompt from `prompts/cursor-automation.md`. Turn on the
Slack tool if you want the highlights posted to a channel.

Caveat: each run is billed at the selected model's API price against your Cursor usage. To guarantee no
charges beyond your plan, set the on-demand spend limit to $0 in the Cursor dashboard. Pick a cheaper model,
since the job is small.

## About X/Twitter

X has no free API. The script tries X's public embed endpoint, which usually works from a home
connection but is often rate limited (HTTP 429) from cloud IPs such as GitHub Actions. When that happens,
the digest still publishes and lists X under "Sources that could not be fetched". To get reliable X coverage
for free:

- **Run it on your own computer.** `python digest.py` from a home IP usually gets X posts. Schedule it with
  `cron`/launchd (macOS) or Task Scheduler (Windows), then `git push`, or just read `digests/latest.md`.
- **Plug in an RSS bridge.** If you self-host [RSSHub](https://docs.rsshub.app) or Nitter, set the Actions
  variable `X_RSS_TEMPLATE` (for example `https://rsshub.example.com/twitter/user/{handle}`) or a per-person
  `x_rss`, and X is read from there.
- Many of these people also post on Bluesky, Substack, or YouTube, which are covered reliably.

YouTube's channel RSS feeds sometimes return 404 for hours at a time. The script retries and then
reports the source as failed for that day.

LinkedIn has no free public feed, so LinkedIn-only creators can't be covered without a paid scraper.

## Run locally

```bash
pip install -r requirements.txt
python digest.py                 # writes digests/<today>.md and .csv
python digest.py --hours 48      # wider window
pip install pytest && pytest     # offline unit tests
```
