Paste this as the prompt of a scheduled Cursor Automation (cursor.com/automations) that is
attached to this repository. Suggested schedule: `CRON_TZ=America/New_York 0 8 * * *`
(replace the timezone with yours).

---

Produce today's influencer digest for this repository.

1. Run `pip install -r requirements.txt` and then `python digest.py --config influencers.yaml --out digests`.
2. Open the dated Markdown file it printed (for example `digests/2026-10-02.md`). Directly under the
   first paragraph and above the table, add a `## Highlights` section with 3 to 6 bullets that group
   the most substantive posts by theme, name who said what, and link the relevant post URL from the
   table. Then add a `**Worth reading in full:**` line with one or two links and a sentence each.
   Use only information from the file. Do not change anything else in it.
3. Copy the edited file over `digests/latest.md`.
4. Commit only the files under `digests/` with the message `Digest for <date>` and open a pull request.
5. If the Slack tool is enabled, post the Highlights section and a link to the file to the configured channel.
