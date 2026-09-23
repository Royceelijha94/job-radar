# job-radar

Scans the career boards of 50 target companies every 2 hours for new
SRE / DevOps / Cloud / Platform jobs in Bengaluru, India and Dubai, sends a push
alert to your phone, and publishes a live dashboard. Runs entirely on GitHub's
free tier: no server, no laptop needed.

```
GitHub Actions (every 2 hours) -> radar.py scans boards -> new job?
    -> push alert (ntfy / Telegram)
    -> state/state.json committed (remembers what you've seen)
    -> dashboard deployed to GitHub Pages
```

## Setup (about 15 minutes)

1. **Create a public repo** on GitHub named `job-radar` and upload every file in
   this folder (keep the `.github/workflows/` path). Public is what makes Actions
   minutes and Pages free.
2. **Turn on Pages:** repo Settings -> Pages -> Source: **GitHub Actions**.
3. **Set up phone alerts** (pick one or both):
   - **ntfy (easiest):** install the ntfy app (Android/iOS), subscribe to a long random
     topic name, e.g. `royce-radar-7f3k9q2x`. Anyone who knows the name can read it,
     so make it unguessable.
   - **Telegram:** message @BotFather -> `/newbot` -> copy the token. Send your bot
     a message, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy `chat.id`.
4. **Add secrets:** Settings -> Secrets and variables -> Actions -> New repository secret:
   `NTFY_TOPIC`, and/or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.
5. **Check the boards:** Actions tab -> job-radar -> Run workflow -> mode `validate`.
   The run summary shows which company boards work. Fix or set failing ones to
   `ats: manual` in `companies.yaml`.
6. **Test the alert:** Run workflow -> mode `test-alert`. Your phone should buzz.
7. **Start it:** Run workflow -> mode `run`. After that it runs by itself.

Dashboard: `https://<your-username>.github.io/job-radar/`

The first scan records the jobs that are already open without alerting, so you only
get pinged for jobs posted after setup.

## Tuning
Everything is in `companies.yaml`: title keywords, excluded words, locations, and the
company list. Commit the change and the next scan uses it.

## Run locally (optional)
```bash
pip install -r requirements.txt
python radar.py validate
NTFY_TOPIC=your-topic python radar.py run
```

## Limits
- GitHub's scheduler can run a few minutes late at busy times.
- If a repo has no activity for 60 days GitHub pauses scheduled workflows. The
  state commits normally prevent this; if it ever pauses, GitHub emails you and one
  click re-enables it.
- Some career sites may block GitHub's servers. `validate` shows this as FAIL.
