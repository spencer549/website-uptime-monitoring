# Weekly Uptime Monitor

Checks websites every Friday at 09:00 Asia/Manila (01:00 UTC) and posts a Slack alert if any are down or slow.

## Setup

1. Create a Slack incoming webhook at https://api.slack.com/apps
2. Add it as a repo secret named `SLACK_WEBHOOK_URL`
3. Test it: Actions tab → Weekly uptime check → Run workflow

## Editing the site list

Edit `sites.txt` — one URL per line.

## Failure criteria

- HTTP status ≥ 400
- Connection/DNS error
- SSL error
- Timeout (> 20s)
- Slow response (> 10s)
