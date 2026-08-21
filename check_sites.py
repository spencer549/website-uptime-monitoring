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


def send_slack(webhook_url, failures, total):
    if failures:
        lines = [f":rotating_light: *Uptime check: {len(failures)} of {total} sites failing*", ""]
        for f in failures:
            # reason already carries the timing for slow/timeout cases
            timing = "" if "s)" in f["reason"] else f" ({f['elapsed']:.1f}s)"
            ip = f" · {f['ip']}" if f["ip"] else ""
            lines.append(f"• <{f['url']}|{f['url']}> · {f['reason']}{timing}{ip}")
        if len(failures) == total and total > 2:
            lines += ["", ":warning: _Every site failed. That usually means a "
                      "monitor-side network problem, not 20 real outages._"]
        else:
            by_ip = defaultdict(list)
            for f in failures:
                if f["ip"]:
                    by_ip[f["ip"]].append(f["url"])
            shared = {ip: u for ip, u in by_ip.items() if len(u) > 1}
            if shared:
                lines.append("")
                for ip, urls in shared.items():
                    lines.append(f"_Note: {len(urls)} of these share server {ip}, "
                                 f"so one host is likely the cause._")
        lines += ["", f"_Each site was tried {ATTEMPTS}x before being reported._"]
    else:
        lines = [f":white_check_mark: *Website Monitoring is done. All {total} websites are Active.*"]
    payload = {"text": "\n".join(lines)}
    r = requests.post(webhook_url, json=payload, timeout=15)
    r.raise_for_status()


def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("ERROR: SLACK_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(2)

    urls = load_sites()
    print(f"Checking {len(urls)} sites (up to {ATTEMPTS} attempts each)...")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for r in pool.map(check, urls):
            results.append(r)

    results.sort(key=lambda r: r["url"])
    for r in results:
        status = "OK  " if r["ok"] else "FAIL"
        retried = f", {r['attempts']} attempts" if r["attempts"] > 1 else ""
        ip = f", {r['ip']}" if r["ip"] else ""
        print(f"{status}  {r['url']}  [{r['reason']}, {r['elapsed']:.1f}s{ip}{retried}]")

    failures = [r for r in results if not r["ok"]]

    try:
        send_slack(webhook, failures, len(results))
    except Exception as e:
        print(f"\nERROR: could not post to Slack: {e.__class__.__name__}: {e}",
              file=sys.stderr)
        sys.exit(3)

    if failures:
        print(f"\nAlert sent: {len(failures)} of {len(results)} failing.")
    else:
        print(f"\nAll {len(results)} sites OK. Success notification sent.")

    # Exit 0 whether or not sites are down. Slack is the alert channel; a red
    # run here means the MONITOR is broken (no webhook, Slack rejected, crash),
    # which is the signal that was lost while every run failed on a dead domain.
    sys.exit(0)


if __name__ == "__main__":
    main()
