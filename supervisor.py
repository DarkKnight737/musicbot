"""GitHub Actions supervisor: the bot stays online across rotating runner jobs.

GitHub caps a single hosted-runner job at 6 hours, so a job cannot host the
bot forever by itself. This script makes many short jobs behave like one
permanent server:

  guard mode (step 1)
      * waits a random 20-80 s (breaks ties when two runs start together)
      * if a FRESH (<340 min) run is in progress, someone else is hosting:
        exit 3 -> workflow steps stop -> this run idles at zero bot load
      * if a STALE (>=340 min, near its 6h cap) run is in progress, wait
        for it to finish, then claim the hosting slot
  main mode (step 2)
      * keeps a bot.py child alive with instant-local restarts
      * at ~5h48m asks GitHub to start the replacement run (the replacement
        waits for us to leave, so there is never a second poller)
      * exits before the hard 6h cap; hand-off gap is ~30-60 s and messages
        sent meanwhile wait in the Telegram queue (drop_pending_updates is
        off in the bot) and are delivered immediately on reconnect
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
REPO = os.environ["GITHUB_REPOSITORY"]
RUN_ID = int(os.environ.get("GITHUB_RUN_ID", "0"))
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
FRESH_LIMIT_MIN = 340.0  # older than this = someone preparing to hand off


def _gh(path: str, token: str | None = None, data: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode() if data else None,
        method="POST" if data else "GET",
    )
    req.add_header("Authorization",
                   "Bearer " + (token or os.environ.get("GITHUB_TOKEN", "")))
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 422) and data is not None:
            print("API error %s on %s: %s" % (e.code, path, e.read()[:200]), flush=True)
        raise
    return json.loads(raw) if raw else {}


def _other_runs() -> list[tuple[int, float]]:
    """In-progress runs that are not this one: (id, age_minutes)."""
    try:
        d = _gh(f"/repos/{REPO}/actions/runs?per_page=30&status=in_progress")
    except urllib.error.URLError as e:
        print("runs list failed (%s) — assuming none" % e, flush=True)
        return []
    now = datetime.now(timezone.utc)
    out = []
    for r in d.get("workflow_runs", []):
        if int(r["id"]) == RUN_ID:
            continue
        created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        out.append((int(r["id"]), (now - created).total_seconds() / 60.0))
    return out


def do_guard() -> None:
    time.sleep(random.uniform(20, 80))
    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        others = _other_runs()
        fresh = [o for o in others if o[1] < FRESH_LIMIT_MIN]
        stale = [o for o in others if o[1] >= FRESH_LIMIT_MIN]
        if not fresh:
            print("guard: no fresh live run — claiming the hosting slot", flush=True)
            sys.exit(0)
        if stale and not fresh:
            print("guard: waiting for handed-off run %s to leave" % stale[0][0], flush=True)
            time.sleep(20)
            continue
        print("guard: run %s (age %.0f min) is hosting — this run steps aside" % (fresh[0][0], fresh[0][1]), flush=True)
        sys.exit(3)
    print("guard: waited too long for the hand-off — stepping aside", flush=True)
    sys.exit(3)


def _dispatch_replacement() -> None:
    token = os.environ.get("DISPATCH_TOKEN", "")
    if not token:
        raise RuntimeError("DISPATCH_TOKEN secret missing")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    _gh(f"/repos/{REPO}/actions/workflows/bot.yml/dispatches",
        token=token, data={"ref": ref})
    print("main: replacement run dispatched", flush=True)


def do_main() -> None:
    started = time.time()
    handoff_done = False
    crashes = 0
    child: subprocess.Popen | None = None
    env = dict(os.environ, PYTHONUNBUFFERED="1")

    while (time.time() - started) < 352 * 60:
        if not handoff_done and (time.time() - started) >= 348 * 60:
            try:
                _dispatch_replacement()
            except Exception as e:
                print("main: dispatch failed (%s) — next schedule tick will take over" % e, flush=True)
            handoff_done = True
        if child is None or child.poll() is not None:
            if child is not None:
                crashes += 1
                if crashes > 90:
                    print("main: %d consecutive crashes — giving up" % crashes, flush=True)
                    sys.exit(1)
                print("main: bot exited (code %s) — restart #%d in 10 s"
                      % (child.returncode, crashes), flush=True)
                time.sleep(10)
            child = subprocess.Popen([sys.executable, "-X", "utf8", "bot.py"],
                                     cwd=THIS_DIR, env=env)
        time.sleep(5)
    if child is not None and child.poll() is None:
        child.terminate()
    print("main: hand-off complete (replacement is taking it from here)", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "main"
    (do_guard if mode == "guard" else do_main)()