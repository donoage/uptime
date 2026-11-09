#!/usr/bin/env python3
"""External uptime probe: GET each target in targets.json, alert Discord after 2 consecutive failures.

A target fails when it does not answer 2xx within TIMEOUT seconds (redirects are followed). The consecutive-failure
count per target is kept in a small JSON state file that the workflow carries from run to run (actions/cache). One
alert is sent when a target reaches THRESHOLD failures in a row, and one recovery note when it answers again.

The Discord webhook comes from the DISCORD_WEBHOOK_URL environment variable and is never printed. If an alert is due
and the webhook is missing or Discord refuses it, the run exits 1 so GitHub shows it failed, and the alert is retried
on the next run.

One process can probe several rounds (UPTIME_ROUNDS, default 1) UPTIME_INTERVAL_SEC apart (default 300): GitHub runs
schedules late or not at all on a low-activity repo, so the workflow starts hourly and each run covers ~55 min of
5-minute rounds. State is written after every round, so a cancelled run (a newer one took over) loses nothing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

THRESHOLD = 2
TIMEOUT = 20
USER_AGENT = "donoage-uptime/1 (+https://github.com/donoage/uptime)"


def load_targets(path: Path) -> list[dict]:
    targets = json.loads(path.read_text())
    for t in targets:
        if not (t.get("name") and str(t.get("url", "")).startswith("https://")):
            raise ValueError(f"bad target {t!r}")
    return targets


def probe(url: str, opener=urllib.request.urlopen, now=time.time) -> tuple[bool, str]:
    """(ok, detail). ``{ts}`` in the url becomes the current time, so a cache cannot answer for the origin."""
    req = urllib.request.Request(url.replace("{ts}", str(int(now()))), headers={"User-Agent": USER_AGENT})
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"no answer ({type(reason).__name__}: {reason})"[:200]
    return 200 <= code < 300, f"HTTP {code}"


def step(state: dict, results: dict[str, tuple[bool, str]]) -> tuple[dict, list[tuple[str, str]]]:
    """New state and the (target, message) pairs due. Targets no longer probed drop out of the state."""
    new, messages = {}, []
    for name, (ok, detail) in results.items():
        before = state.get(name, 0)
        count = 0 if ok else before + 1
        new[name] = count
        if count == THRESHOLD:
            messages.append((name, f":red_circle: **{name} is DOWN**: {detail} ({THRESHOLD} checks in a row, 5 min apart)"))
        elif ok and before >= THRESHOLD:
            messages.append((name, f":green_circle: **{name} is back up** after {before} failed checks"))
    return new, messages


def send(webhook: str, content: str, opener=urllib.request.urlopen) -> str:
    """Post to Discord and return the message id (wait=true makes Discord confirm the message exists)."""
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    sep = "&" if "?" in webhook else "?"
    req = urllib.request.Request(
        webhook + sep + "wait=true",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            return str(json.loads(resp.read() or b"{}").get("id", ""))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # the URL holds the webhook token: report only the exception type and HTTP code
        raise RuntimeError(f"Discord refused the alert ({type(exc).__name__} {getattr(exc, 'code', '')})") from None


def main(argv: list[str] | None = None, opener=urllib.request.urlopen, environ=os.environ, sleep=time.sleep,
         clock=time.monotonic) -> int:
    """Run UPTIME_ROUNDS rounds; exit 1 if any round could not send an alert that was due."""
    rounds = max(1, int(environ.get("UPTIME_ROUNDS", "1")))
    interval = float(environ.get("UPTIME_INTERVAL_SEC", "300"))
    status = 0
    for i in range(rounds):
        started = clock()
        if rounds > 1:
            print(f"--- round {i + 1}/{rounds} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}", flush=True)
        status = max(status, run_once(opener, environ))
        if i + 1 < rounds:
            sleep(max(0.0, interval - (clock() - started)))
    return status


def run_once(opener=urllib.request.urlopen, environ=os.environ) -> int:
    root = Path(__file__).resolve().parent
    state_path = Path(environ.get("UPTIME_STATE", root / "state" / "state.json"))
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    results = {t["name"]: probe(t["url"], opener) for t in load_targets(root / "targets.json")}
    for name, (ok, detail) in results.items():
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail} (in a row: {0 if ok else state.get(name, 0) + 1})", flush=True)
    new, messages = step(state, results)
    webhook = environ.get("DISCORD_WEBHOOK_URL", "")
    status = 0
    for name, text in messages:
        try:
            if not webhook.startswith("https://"):
                raise RuntimeError("alert due but DISCORD_WEBHOOK_URL is not set")
            print(f"alert sent for {name}, Discord message id {send(webhook, text, opener)}")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            status = 1
            # unsent: keep the target one step short of the alert (or still down), so the next run sends it again
            new[name] = THRESHOLD - 1 if new[name] else state.get(name, 0)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(new, sort_keys=True))
    return status


if __name__ == "__main__":
    sys.exit(main())
