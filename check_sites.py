import argparse
import json
import os
import socket
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import requests

TIMEOUT_SECONDS = 20
SLOW_THRESHOLD_SECONDS = 10
ATTEMPTS = 2                 # one retry before a site is called failing
RETRY_BACKOFF_SECONDS = 3
MAX_WORKERS = 6
USER_AGENT = "UptimeMonitor/1.0 (+https://github.com/)"
SITES_FILE = Path(__file__).parent / "sites.txt"

# One request at a time per server IP. Several monitored sites share a host,
# so hitting them concurrently can manufacture the slowness we report.
# Best-effort only: requests does its own DNS lookup, so a round-robin host or
# a cross-host redirect can still land on a server we hold no lock for. It
# removes the common case, it does not guarantee exclusivity. The lock is held
# across the request, so a shared host that goes dark serialises its sites;
# the workflow carries a timeout-minutes guard for that.
_host_locks = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def host_lock(key):
    with _locks_guard:
        return _host_locks[key]


def load_sites():
    urls = []
    for line in SITES_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def resolve(url):
    """Resolve up front so a real DNS failure is reported as one, instantly,
    and so we can serialise requests per server IP."""
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        return infos[0][4][0], None
    except socket.gaierror as e:
        return None, f"DNS failure ({e.strerror or e})"


def attempt(url, ip):
    start = time.monotonic()
    try:
        resp = requests.get(
            url,
            timeout=TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        elapsed = time.monotonic() - start
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}", elapsed
        if elapsed > SLOW_THRESHOLD_SECONDS:
            return False, f"slow ({elapsed:.1f}s)", elapsed
        return True, f"HTTP {resp.status_code}", elapsed

    # ConnectTimeout subclasses BOTH ConnectionError and Timeout, and SSLError
    # subclasses ConnectionError, so the specific cases must come first or a
    # timeout gets mislabelled as a DNS failure.
    except requests.exceptions.ConnectTimeout:
        return False, f"connect timeout (>{TIMEOUT_SECONDS}s)", time.monotonic() - start
    except requests.exceptions.ReadTimeout:
        return False, f"read timeout (>{TIMEOUT_SECONDS}s)", time.monotonic() - start
    except requests.exceptions.SSLError as e:
        return False, f"SSL error ({e.__class__.__name__})", time.monotonic() - start
    except requests.exceptions.TooManyRedirects:
        return False, "redirect loop", time.monotonic() - start
    except requests.exceptions.ConnectionError as e:
        text = str(e).lower()
        if "refused" in text:
            reason = "connection refused"
        elif "name or service not known" in text or "nodename nor servname" in text:
            reason = "DNS failure"
        elif "reset by peer" in text:
            reason = "connection reset"
        else:
            reason = "connection error"
        return False, reason, time.monotonic() - start
    except requests.exceptions.RequestException as e:
        return False, e.__class__.__name__, time.monotonic() - start


def check(url):
    ip, dns_error = resolve(url)
    if dns_error:
        return {"url": url, "ok": False, "reason": dns_error, "elapsed": 0.0,
                "ip": None, "attempts": 1}

    lock = host_lock(ip)
    reason = ""
    elapsed = 0.0
    for i in range(ATTEMPTS):
        with lock:
            ok, reason, elapsed = attempt(url, ip)
        if ok:
            return {"url": url, "ok": True, "reason": reason, "elapsed": elapsed,
                    "ip": ip, "attempts": i + 1}
        if i < ATTEMPTS - 1:
            time.sleep(RETRY_BACKOFF_SECONDS)
    return {"url": url, "ok": False, "reason": reason, "elapsed": elapsed,
            "ip": ip, "attempts": ATTEMPTS}


# ---------------------------------------------------------------------------
# Phases.
#
# A single GitHub runner is not a reliable witness. Its egress IP is random per
# job, and some servers firewall parts of the Azure range, so a runner can be
# unable to open a TCP connection to a site that is serving everyone else
# perfectly. Measured 2026-08-21: five parallel jobs received five distinct
# egress IPs, and three of the five reached 34.174.188.233 while two were
# blocked at the very same moment.
#
# So a verdict is a consensus, not one opinion:
#   check    one runner tests every site
#   verify   if anything failed, N more runners retest ONLY the failures
#   report   a site is DOWN only if EVERY vantage point failed. If any vantage
#            reached it, the site is up and the failure was local to a runner.
#
# The asymmetry matters: reaching a site proves it is serving, while failing to
# reach it proves nothing on its own. The old single-runner design treated both
# as equally conclusive, which is why it reported live sites as down.
# ---------------------------------------------------------------------------

CONNECTION_LEVEL = ("connect timeout", "read timeout", "connection refused",
                    "connection reset", "connection error", "DNS failure")


def runner_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception:
        return "unknown"


def run_checks(urls, label):
    print(f"[{label}] egress IP {runner_ip()}, checking {len(urls)} site(s), "
          f"up to {ATTEMPTS} attempts each")
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for r in pool.map(check, urls):
            results.append(r)
    results.sort(key=lambda r: r["url"])
    for r in results:
        state = "OK  " if r["ok"] else "FAIL"
        print(f"{state}  {r['url']}  [{r['reason']}, {r['elapsed']:.1f}s, "
              f"{r['ip'] or 'no ip'}]")
    return results


def write_json(path, label, results):
    payload = {"vantage": label, "ip": runner_ip(), "results": results}
    Path(path).write_text(json.dumps(payload, indent=1))


def gh_output(**kv):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def classify(verdicts):
    """verdicts is a list of (vantage_label, result) for one URL, across every
    vantage that tested it. Returns (status, detail)."""
    reached = [r for _, r in verdicts if r["ok"]]
    failed = [r for _, r in verdicts if not r["ok"]]
    n = len(verdicts)

    if not failed:
        return "ok", ""

    if not reached:
        reasons = sorted({r["reason"].split(" (")[0] for r in failed})
        where = f"all {n} vantage points" if n > 1 else "the only vantage point"
        return "down", f"{', '.join(reasons)}, failed from {where}"

    # Mixed verdict. Something reached it, so the site is serving.
    got = len(reached)
    base = failed[0]["reason"].split(" (")[0]

    if base in CONNECTION_LEVEL:
        return "unreachable_from_some", (
            f"reached from {got} of {n} vantage points. The other {n - got} "
            f"could not open a connection ({base}), so those runner IPs are "
            f"blocked by the server. The site itself is serving normally.")

    if base == "slow":
        slow = ", ".join(f"{r['elapsed']:.1f}s" for r in failed)
        fast = ", ".join(f"{r['elapsed']:.1f}s" for r in reached)
        return "intermittent", (
            f"slow on {len(failed)} of {n} checks ({slow}) but fine on {got} "
            f"({fast}). Intermittent, and it is server side.")

    return "intermittent", (
        f"{base} on {len(failed)} of {n} checks, fine on {got}. Intermittent.")


def build_message(rows, total, vantages):
    down = [r for r in rows if r["status"] == "down"]
    unreach = [r for r in rows if r["status"] == "unreachable_from_some"]
    flaky = [r for r in rows if r["status"] == "intermittent"]

    if not (down or unreach or flaky):
        return [f":white_check_mark: *Website Monitoring is done. "
                f"All {total} websites are Active.*"]

    lines = []
    if down:
        lines += [f":rotating_light: *{len(down)} of {total} sites DOWN*", ""]
        for r in down:
            lines.append(f"• <{r['url']}|{r['url']}> · {r['detail']}")
    else:
        lines += [f":white_check_mark: *No outage. All {total} sites are "
                  f"reachable.*"]

    if unreach:
        lines += ["", ":information_source: *Blocked from some GitHub runners, "
                  "NOT an outage*", ""]
        for r in unreach:
            lines.append(f"• <{r['url']}|{r['url']}> · {r['detail']}")

    if flaky:
        lines += ["", ":warning: *Intermittent, worth a look*", ""]
        for r in flaky:
            lines.append(f"• <{r['url']}|{r['url']}> · {r['detail']}")

    lines += ["", f"_Verdicts are a consensus of up to {vantages} independent "
              f"GitHub runners. A site is called DOWN only when every one of "
              f"them failed to reach it._"]
    return lines


def send_slack(webhook_url, lines):
    r = requests.post(webhook_url, json={"text": "\n".join(lines)}, timeout=15)
    r.raise_for_status()


def phase_check(args):
    urls = load_sites()
    if not urls:
        print("ERROR: sites.txt has no active URLs", file=sys.stderr)
        sys.exit(3)
    results = run_checks(urls, "check")
    write_json(args.out, "check", results)
    failed = [r["url"] for r in results if not r["ok"]]
    gh_output(has_failures=str(bool(failed)).lower(),
              failures=json.dumps(failed))
    tail = ", sending them for verification" if failed else ""
    print(f"\n{len(failed)} of {len(results)} failed this pass{tail}")


def phase_verify(args):
    urls = json.loads(args.urls)
    if not urls:
        write_json(args.out, args.label, [])
        return
    write_json(args.out, args.label, run_checks(urls, args.label))


def phase_report(args):
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("ERROR: SLACK_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(2)

    files = sorted(Path(args.results_dir).rglob("*.json"))
    payloads = [json.loads(f.read_text()) for f in files]
    base = next((p for p in payloads if p["vantage"] == "check"), None)
    if base is None:
        print("ERROR: the check-phase result file is missing, so there is "
              "nothing trustworthy to report", file=sys.stderr)
        sys.exit(3)

    per_url = defaultdict(list)
    for p in payloads:
        for r in p["results"]:
            per_url[r["url"]].append((p["vantage"], r))

    rows = []
    for r in base["results"]:
        status, detail = classify(per_url[r["url"]])
        rows.append({"url": r["url"], "status": status, "detail": detail})

    total = len(base["results"])
    vantages = len(payloads)
    print(f"vantage points reporting: {vantages} "
          f"({', '.join(p['vantage'] + '=' + p['ip'] for p in payloads)})\n")
    for row in rows:
        if row["status"] != "ok":
            print(f"{row['status'].upper():<22} {row['url']}  {row['detail']}")

    # Log the message verbatim so a past run can be read back from its own log
    # instead of being reconstructed from the code.
    message = build_message(rows, total, vantages)
    print("\n--- message sent to Slack ---")
    print("\n".join(message))
    print("--- end message ---")

    try:
        send_slack(webhook, message)
    except Exception as e:
        print(f"ERROR: could not post to Slack: {e.__class__.__name__}: {e}",
              file=sys.stderr)
        sys.exit(3)

    counts = {k: sum(1 for r in rows if r["status"] == k)
              for k in ("down", "unreachable_from_some", "intermittent")}
    print(f"\nReported across {vantages} vantage point(s): "
          f"{counts['down']} down, {counts['unreachable_from_some']} "
          f"blocked-runner, {counts['intermittent']} intermittent.")
    # Exit 0 regardless of what the sites did. Slack is the alert channel, so a
    # red run means the MONITOR is broken.
    sys.exit(0)


def main():
    ap = argparse.ArgumentParser(description="Consensus uptime monitor.")
    sub = ap.add_subparsers(dest="phase", required=True)

    c = sub.add_parser("check")
    c.add_argument("--out", default="result-check.json")

    v = sub.add_parser("verify")
    v.add_argument("--urls", required=True, help="JSON array of URLs")
    v.add_argument("--label", required=True)
    v.add_argument("--out", required=True)

    r = sub.add_parser("report")
    r.add_argument("--results-dir", default="results")

    args = ap.parse_args()
    {"check": phase_check,
     "verify": phase_verify,
     "report": phase_report}[args.phase](args)


if __name__ == "__main__":
    main()
