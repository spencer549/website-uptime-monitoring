import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

TIMEOUT_SECONDS = 20
SLOW_THRESHOLD_SECONDS = 10
USER_AGENT = "UptimeMonitor/1.0 (+https://github.com/)"
SITES_FILE = Path(__file__).parent / "sites.txt"


def load_sites():
    urls = []
    for line in SITES_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def check(url):
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
            return {"url": url, "ok": False, "reason": f"HTTP {resp.status_code}", "elapsed": elapsed}
        if elapsed > SLOW_THRESHOLD_SECONDS:
            return {"url": url, "ok": False, "reason": f"slow ({elapsed:.1f}s)", "elapsed": elapsed}
        return {"url": url, "ok": True, "reason": f"HTTP {resp.status_code}", "elapsed": elapsed}
    except requests.exceptions.SSLError as e:
        return {"url": url, "ok": False, "reason": f"SSL error: {e.__class__.__name__}", "elapsed": time.monotonic() - start}
    except requests.exceptions.ConnectionError:
        return {"url": url, "ok": False, "reason": "connection refused / DNS failure", "elapsed": time.monotonic() - start}
    except requests.exceptions.Timeout:
        return {"url": url, "ok": False, "reason": f"timeout (>{TIMEOUT_SECONDS}s)", "elapsed": time.monotonic() - start}
    except requests.exceptions.RequestException as e:
        return {"url": url, "ok": False, "reason": f"{e.__class__.__name__}", "elapsed": time.monotonic() - start}


def send_slack(webhook_url, failures, total):
    lines = [f":rotating_light: *Uptime check: {len(failures)} of {total} sites failing*", ""]
    for f in failures:
        lines.append(f"• <{f['url']}|{f['url']}> — {f['reason']} ({f['elapsed']:.1f}s)")
    payload = {"text": "\n".join(lines)}
    r = requests.post(webhook_url, json=payload, timeout=10)
    r.raise_for_status()


def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("ERROR: SLACK_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(2)

    urls = load_sites()
    print(f"Checking {len(urls)} sites...")

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(check, u): u for u in urls}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: r["url"])
    for r in results:
        status = "OK  " if r["ok"] else "FAIL"
        print(f"{status}  {r['url']}  [{r['reason']}, {r['elapsed']:.1f}s]")

    failures = [r for r in results if not r["ok"]]
    if failures:
        send_slack(webhook, failures, len(results))
        print(f"\nAlert sent: {len(failures)} failing.")
        sys.exit(1)
    print("\nAll sites OK.")


if __name__ == "__main__":
    main()


