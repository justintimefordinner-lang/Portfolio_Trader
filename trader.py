"""
trader.py — stage 1: suggestions only.

Every TRADER_INTERVAL seconds during the session it rebuilds the suggestion list
from the bridge's files (suggest.py), pushes anything new to your phone through
ntfy (notify.py), and writes data/trade-suggestions.json for the dashboard's
Trader page. It never places an order; there is no broker client in here.

Timing follows the backtest, which traded once a day at 11:00 ET:
  * closes at 50% are checked on every pass, all session;
  * new puts, covered calls and notes are only built inside the entry window
    (TRADER_ENTRY_WINDOW, default 11:00-12:30 ET = 9:00-10:30 Mountain). At the
    first pass of the window the trader asks the bridge for a fresh Quant scan
    (task_inbox/quant_scan) and waits for one no older than TRADER_SCAN_MAX_AGE
    seconds before the window opened. The window is wide so a late scan still
    gets its turn; if none arrives, a note says so.
  * the dashboard's "Run now" drops data/trader-run: a full pass, any time, any
    day, against whatever scan is on disk.

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
from datetime import date, datetime, timedelta, timezone

import notify
import suggest

DATA_DIR = os.environ.get("APP_DATA_DIR", "/app/data")
STATE_DIR = os.environ.get("TRADER_STATE_DIR", ".")
INBOX_DIR = os.environ.get("TRADER_INBOX_DIR", "/app/task_inbox")  # the bridge's task_inbox, if mounted
OUT_FILE = "trade-suggestions.json"
FEEDBACK_FILE = "trade-feedback.json"  # the app writes: {key: {"status": "good"|"bad"|"done"|"skip", "at": iso}}
RUN_FILE = "trader-run"  # the app writes it; a pass runs within POLL seconds and removes it
SENT_FILE = os.path.join(STATE_DIR, "sent.json")
DAY_FILE = os.path.join(STATE_DIR, "day.json")  # {"scanRequested": "YYYY-MM-DD", "entriesBuilt": "YYYY-MM-DD"}
PAUSE_FILE = os.path.join(STATE_DIR, "paused")
INTERVAL = int(os.environ.get("TRADER_INTERVAL", "900"))
POLL = 5  # seconds between looks for the run marker
ENTRY_WINDOW = os.environ.get("TRADER_ENTRY_WINDOW", "11:00-12:30")  # Eastern, like the backtest
SCAN_MAX_AGE = int(os.environ.get("TRADER_SCAN_MAX_AGE", "1800"))  # a scan this much older than the window start is stale
RESEND_AFTER_SEC = 24 * 3600
RESEND_MOVE_PCT = 15.0
KEEP = 300  # suggestions kept in the file
ENTRY_KINDS = ("csp", "cc", "note")

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


# ---- Eastern time without a tz database (US rules since 2007) -----------------
def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _et_offset(d: date) -> int:
    return -4 if _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1) else -5


def to_et(now: datetime | None = None) -> datetime:
    """Any datetime (naive = UTC) as an aware Eastern-time datetime."""
    now = now or datetime.now(timezone.utc)
    utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    off = _et_offset((utc + timedelta(hours=_et_offset(utc.date()))).date())
    return utc.astimezone(timezone(timedelta(hours=off)))


def market_open(now: datetime | None = None) -> bool:
    et = to_et(now)
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 570 <= mins < 960


def _window() -> tuple[int, int]:
    try:
        a, b = ENTRY_WINDOW.split("-")
        h1, m1 = (int(x) for x in a.split(":"))
        h2, m2 = (int(x) for x in b.split(":"))
        return h1 * 60 + m1, h2 * 60 + m2
    except ValueError:
        return 11 * 60, 12 * 60 + 30


def in_entry_window(now: datetime | None = None) -> bool:
    et = to_et(now)
    if et.weekday() >= 5:
        return False
    start, end = _window()
    return start <= et.hour * 60 + et.minute < end


def window_start(now: datetime | None = None) -> datetime:
    """Today's window start as an aware datetime."""
    et = to_et(now)
    start, _ = _window()
    return et.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)


def scan_fresh(scan: dict | None, now: datetime | None = None) -> bool:
    """True when the Quant scan was produced for this window: no older than SCAN_MAX_AGE before it opened."""
    as_of = ((scan or {}).get("meta") or {}).get("asOf")
    if not as_of:
        return False
    try:
        t = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t >= window_start(now) - timedelta(seconds=SCAN_MAX_AGE)


def request_scan(today: str) -> bool:
    """Drop the bridge's task_inbox/quant_scan marker once per day. False when the inbox isn't mounted."""
    day = _read(DAY_FILE, {}) or {}
    if day.get("scanRequested") == today:
        return False
    if not os.path.isdir(INBOX_DIR):
        return False
    try:
        with open(os.path.join(INBOX_DIR, "quant_scan"), "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except OSError:
        return False
    _write(DAY_FILE, {**day, "scanRequested": today})
    return True


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


def run_once(force: bool = False, now: float | None = None, entries: bool | None = None, when: datetime | None = None,
             requested: bool = False) -> dict:
    """One pass. `entries` forces the entry half on/off (None = follow the window). Returns a summary for the log and the self-test."""
    now = now or time.time()
    when = when or datetime.fromtimestamp(now, timezone.utc)
    if not force and not market_open(when):
        return {"skipped": "market closed"}
    ctx = suggest.load_context(DATA_DIR)
    today = to_et(when).date().isoformat()
    day = _read(DAY_FILE, {}) or {}
    waiting = False
    if entries is None:
        entries = False
        if in_entry_window(when):
            if request_scan(today):
                _log("entry window open — asked the bridge for a fresh Quant scan")
                day = _read(DAY_FILE, {}) or {}
            if scan_fresh(ctx.get("scan"), when):
                entries = True
            else:
                waiting = True
    fresh = suggest.build(ctx, os.environ.get("TRADER_ACCOUNT") or None, entries=entries)
    if waiting and not in_entry_window(when + timedelta(seconds=INTERVAL)) and day.get("entriesBuilt") != today:
        # last pass of the window and still no scan for today: say so instead of staying silent
        fresh.append({"key": f"note|scan|{today}", "kind": "note", "symbol": "—", "accountId": "",
                      "title": "No fresh Quant scan this window",
                      "detail": "The entry window closed without a scan newer than its start, so no new puts were evaluated today. Check the bridge's quant scan.",
                      "amount": None, "rule": "entry window"})
    if entries:
        day = {**day, "entriesBuilt": today}
        _write(DAY_FILE, day)
    evaluated = set(ENTRY_KINDS) | {"close"} if entries else {"close"}

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
    # Only kinds this pass evaluated can expire: outside the window, the morning's puts stand.
    for key, prior in existing.items():
        if key in merged:
            continue
        if prior.get("status") == "new" and prior.get("kind") in evaluated:
            prior = {**prior, "status": "expired", "expiredAt": prior.get("expiredAt") or stamp}
        merged[key] = prior

    rows = sorted(merged.values(), key=lambda r: r.get("lastSeen", ""), reverse=True)[:KEEP]
    _write(out_path, {
        "meta": {"asOf": stamp, "interval": INTERVAL, "paused": paused, "ntfy": bool(cfg["topic"]),
                 "window": f"{ENTRY_WINDOW} ET", "entriesBuilt": day.get("entriesBuilt"),
                 "lastPass": "run now" if requested else ("entries" if entries else "closes"),
                 "active": sum(1 for r in rows if r["status"] == "new"), "pushed": pushed},
        "suggestions": rows,
    })
    _write(SENT_FILE, sent)
    return {"active": sum(1 for r in rows if r["status"] == "new"), "pushed": pushed, "paused": paused,
            "entries": entries, **({"waiting": "scan"} if waiting else {})}


def run_requested() -> bool:
    """The app's "Run now": a full pass, whatever the clock says. Removes the marker first so a failing pass can't loop."""
    path = os.path.join(DATA_DIR, RUN_FILE)
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
    except OSError:
        pass
    r = run_once(force=True, entries=True, requested=True)
    _log(f"run now: {r}")
    return True


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    cfg = notify.config()
    _log(f"trader started — closes every {INTERVAL}s during the session, entries in the {ENTRY_WINDOW} ET window; ntfy "
         + (f"{cfg['url']}/{cfg['topic'][:4]}…" if cfg["topic"] else "OFF (set NTFY_TOPIC)")
         + ("" if os.path.isdir(INBOX_DIR) else "; bridge inbox not mounted, relying on its scheduled scan"))
    first = True
    next_pass = 0.0
    while True:
        try:
            if time.time() >= next_pass:
                next_pass = time.time() + INTERVAL
                _log(f"pass: {run_once(force=first)}")
                first = False
            run_requested()
        except Exception as exc:  # noqa: BLE001 — never let one bad pass stop the loop
            _log(f"ERROR - {exc}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
