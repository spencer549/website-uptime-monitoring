# Weekly Uptime Monitor

Checks the sites in `sites.txt` every Friday morning (Asia/Manila) and posts the
result to Slack. Runs twice, at 01:17 UTC and again at 04:17 UTC, because GitHub
sometimes skips a scheduled run.

## Setup

1. Create a Slack incoming webhook at https://api.slack.com/apps
2. Add it as a repo secret named `SLACK_WEBHOOK_URL`
3. Test it: Actions tab, "Weekly uptime check", Run workflow

## Editing the site list

Edit `sites.txt`, one URL per line. Lines starting with `#` are ignored, so a
site that is temporarily dead can be commented out with a dated reason instead
of being deleted.

## What counts as a failure

A site is only reported after it fails **twice**, with a short pause between
attempts, so a single blip does not raise an alert.

- HTTP status 400 or higher
- DNS failure (the hostname does not resolve)
- Connection refused, reset, or another connection error
- SSL error
- Connect timeout or read timeout (over 20s)
- Redirect loop
- Slow response (over 10s)

Each site's hostname is resolved before the request, so a genuine DNS failure is
reported as one instantly rather than being mislabelled after a 20s timeout.
Requests are also serialised per server IP, because several monitored sites share
a host and hitting them at once can create the slowness being measured.

## Reading the run status

**Green does not mean every site is up.** Slack is the alert channel; the run
status reports the health of the monitor itself.

| Exit | Meaning |
|---|---|
| 0 | The check ran and the Slack message was delivered, whether or not sites are down |
| 2 | `SLACK_WEBHOOK_URL` is not set |
| 3 | Slack rejected the message |
| 1 | The script crashed |

So a red run means the monitor needs attention. This is deliberate: the previous
version exited 1 whenever any site failed, and one long-dead domain kept every
run red from 2026-06-05 onward, which made the status carry no information.

## Note on GitHub's schedule

GitHub disables scheduled workflows on public repositories after 60 days without
repository activity. Any commit resets that clock. Scheduled runs are also
routinely delayed by 30 to 90 minutes, which is normal and not a fault.

## Where the workflow lives

`.github/workflows/uptime.yml`, and only there. GitHub does not read a
`workflows/` folder at the repository root, so a copy placed there does nothing.
