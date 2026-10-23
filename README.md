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

## How a verdict is reached

**One GitHub runner is not a reliable witness.** Its egress IP is random per
job, and some servers firewall parts of the Azure range, so a runner can fail to
open a connection to a site that is serving everyone else perfectly. Measured
2026-08-21: five parallel jobs got five distinct egress IPs, and three reached
`34.174.188.233` while two were blocked at the same instant.

The asymmetry that matters: **reaching a site proves it is serving, but failing
to reach it proves nothing on its own.** So the check runs in three phases.

1. `check`, one runner tests every site.
2. `verify`, if anything failed, four more runners retest only the failures.
3. `report`, the verdicts are merged and posted to Slack.

A site is called **DOWN only when every vantage point failed.** If any single
one reached it, the site is up and the failure belonged to that runner.

Each site is also tried twice within a vantage, with a pause between, so a
single blip never reaches the alert.

### The four verdicts

| Verdict | Meaning | Renders as |
|---|---|---|
| DOWN | Every vantage point failed | Red alert |
| Could not check: bot protection | Every vantage point got a firewall challenge page (Cloudflare, Sucuri or Imperva) | Not an outage, but not confirmed up either. Allowlist the monitor in that site's firewall to check it again |
| Blocked from some runners | Some reached it, the others could not open a connection or got a challenge | Not an outage, informational |
| Intermittent | Some reached it, the others got a slow response or an error | Warning |

A challenge is recognised only by the firewall's own markers (Cloudflare's
`cf-mitigated: challenge` header or its challenge and block pages, and the
Sucuri and Imperva block pages; their headers alone do not count, since they
sit on every proxied response). A plain 403, a Cloudflare 1xxx configuration
error, or a 503 or 52x passed through from a failing origin is still an error
and can still make a site DOWN. A challenge mixed only with runner connection
failures is also "could not check"; mixed with any HTTP error it stays DOWN. Added
2026-10-23 after Cloudflare Bot Fight Mode made rebounderz.com read as DOWN
from every runner on 10-16 and 10-23 while it was serving visitors normally.

## What counts as a failure at a single vantage

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
