"""
trader.py — stage 1: suggestions only.

Every TRADER_INTERVAL seconds during the session it rebuilds the suggestion list
from the bridge's files (suggest.py), pushes anything new to your phone through
ntfy (notify.py), and writes data/trade-suggestions.json for the dashboard's
Trader page. It never places an order; there is no broker client in here.

Dedupe: a suggestion is pushed once, and again only if it is still standing a
day later or its price has moved more than 15%. What was sent lives in
/state/sent.json so a restart doesn't re-push everything.

Pause: touch /state/paused to stop pushes (the file is still written).
"""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone

import notify
import suggest

DATA_DIR = os.environ.get("APP_DATA_DIR", "/app/data")
STATE_DIR = os.environ.get("TRADER_STATE_DIR", ".")
OUT_FILE = "trade-suggestions.json"
FEEDBACK_FILE = "trade-feedback.json"  # the app writes: {key: {"status": "good"|"bad"|"done"|"skip", "at": iso}}
SENT_FILE = os.path.join(STATE_DIR, "sent.json")
PAUSE_FILE = os.path.join(STATE_DIR, "paused")
INTERVAL = int(os.environ.get("TRADER_INTERVAL", "900"))
RESEND_AFTER_SEC = 24 * 3600
RESEND_MOVE_PCT = 15.0
KEEP = 300  # suggestions kept in the file

socket.setdefaulttimeout(30)


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: str, payload) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def market_open(now: datetime | None = None) -> bool:
    try:
        from zoneinfo import ZoneInfo
        et = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        return True
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 570 <= mins < 960


def should_push(s: dict, sent: dict, now: float) -> bool:
    prev = sent.get(s["key"])
    if not prev:
        return True
    if now - prev.get("at", 0) >= RESEND_AFTER_SEC:
        return True
    p0, p1 = prev.get("price"), s.get("price")
    if p0 and p1 and abs(p1 - p0) / p0 * 100 >= RESEND_MOVE_PCT:
        return True
    return False


def run_once(force: bool = False, now: float | None = None) -> dict:
    """One pass. Returns a small summary for the log and the self-test."""
    now = now or time.time()
    if not force and not market_open():
        return {"skipped": "market closed"}
    ctx = suggest.load_context(DATA_DIR)
    fresh = suggest.build(ctx, os.environ.get("TRADER_ACCOUNT") or None)

    out_path = os.path.join(DATA_DIR, OUT_FILE)
    existing = {s["key"]: s for s in (_read(out_path, {}) or {}).get("suggestions", [])}
    feedback = _read(os.path.join(DATA_DIR, FEEDBACK_FILE), {}) or {}
    sent = _read(SENT_FILE, {}) or {}
    paused = os.path.exists(PAUSE_FILE)
    cfg = notify.config()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    pushed = 0
    merged: dict[str, dict] = {}
    for s in fresh:
        prior = existing.get(s["key"])
        row = {**s, "firstSeen": (prior or {}).get("firstSeen") or stamp, "lastSeen": stamp, "status": "new"}
        fb = feedback.get(s["key"])
        if fb and fb.get("status"):
            row["status"] = fb["status"]  # the app's verdict wins
        if row["status"] in ("new",) and not paused and should_push(s, sent, now):
            if notify.push(s, cfg):
                pushed += 1
                sent[s["key"]] = {"at": now, "price": s.get("price")}
                row["pushedAt"] = stamp
        elif prior and prior.get("pushedAt"):
            row["pushedAt"] = prior["pushedAt"]
        merged[s["key"]] = row
    # Suggestions that stopped applying stay in the log, marked, so the page can show history.
    for key, prior in existing.items():
        if key in merged:
            continue
        if prior.get("status") == "new":
            prior = {**prior, "status": "expired", "expiredAt": prior.get("expiredAt") or stamp}
        merged[key] = prior

    rows = sorted(merged.values(), key=lambda r: r.get("lastSeen", ""), reverse=True)[:KEEP]
    _write(out_path, {
        "meta": {"asOf": stamp, "interval": INTERVAL, "paused": paused, "ntfy": bool(cfg["topic"]),
                 "active": sum(1 for r in rows if r["status"] == "new"), "pushed": pushed},
        "suggestions": rows,
    })
    _write(SENT_FILE, sent)
    return {"active": sum(1 for r in rows if r["status"] == "new"), "pushed": pushed, "paused": paused}


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    cfg = notify.config()
    _log(f"trader started — suggestions every {INTERVAL}s during the session; ntfy "
         + (f"{cfg['url']}/{cfg['topic'][:4]}…" if cfg["topic"] else "OFF (set NTFY_TOPIC)"))
    first = True
    while True:
        try:
            r = run_once(force=first)
            first = False
            _log(f"pass: {r}")
        except Exception as exc:  # noqa: BLE001 — never let one bad pass stop the loop
            _log(f"ERROR - {exc}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
