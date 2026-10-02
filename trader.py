"""
trader.py — stage 1: suggestions only.

Every TRADER_INTERVAL seconds during the session it rebuilds the suggestion list
from the bridge's files (suggest.py) for EVERY account the dashboard shows,
pushes anything new to your phone through ntfy (notify.py) with the account
named, and writes data/trade-suggestions.json for the dashboard's Trader page.
It never places an order; there is no broker client in here.

Timing (the backtest traded once a day at 11:00 ET; the user wants it hourly):
  * closes at 50% are checked on every pass, all session, every account;
  * new puts, covered calls and notes are built once per entry slot: the top of
    each hour in TRADER_ENTRY_HOURS (default 11,12,13,14,15 ET = 9:00 to 1:00
    Mountain), with TRADER_ENTRY_SLOT_MINUTES after the hour to get it done. At
    the first pass of a slot the trader asks the bridge for a fresh Quant scan
    (task_inbox/quant_scan) and waits for one no older than TRADER_SCAN_MAX_AGE
    seconds before the slot opened. If a day ends with no slot served, a note
    says so.
  * the dashboard's "Run now" drops data/trader-run: a full pass, any time, any
    day, against whatever scan is on disk.

Accounts: every account in the merged snapshot (base data/, data/acct2/,
data/manual/ …) except the Auto Trader paper account, which paper.py runs on
the same slots. TRADER_ACCOUNTS=id1,id2 limits it.

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
import paper
import suggest

DATA_DIR = os.environ.get("APP_DATA_DIR", "/app/data")
STATE_DIR = os.environ.get("TRADER_STATE_DIR", ".")
INBOX_DIR = os.environ.get("TRADER_INBOX_DIR", "/app/task_inbox")  # the bridge's task_inbox, if mounted
OUT_FILE = "trade-suggestions.json"
FEEDBACK_FILE = "trade-feedback.json"  # the app writes: {key: {"status": "good"|"bad"|"done"|"skip", "at": iso}}
RUN_FILE = "trader-run"  # the app writes it; a pass runs within POLL seconds and removes it
SENT_FILE = os.path.join(STATE_DIR, "sent.json")
DAY_FILE = os.path.join(STATE_DIR, "day.json")  # {"scanRequested": slot, "entriesBuilt": slot, "entriesDay": date}
PAUSE_FILE = os.path.join(STATE_DIR, "paused")
INTERVAL = int(os.environ.get("TRADER_INTERVAL", "900"))
POLL = 5  # seconds between looks for the run marker
ENTRY_HOURS = [int(h) for h in (os.environ.get("TRADER_ENTRY_HOURS") or "11,12,13,14,15").split(",") if h.strip()]  # Eastern
SLOT_MINUTES = int(os.environ.get("TRADER_ENTRY_SLOT_MINUTES", "50"))  # how long after the hour a slot stays open
SCAN_MAX_AGE = int(os.environ.get("TRADER_SCAN_MAX_AGE", "1800"))  # a scan this much older than the slot start is stale
ACCOUNTS = [a.strip() for a in (os.environ.get("TRADER_ACCOUNTS") or "").split(",") if a.strip()]  # empty = every account
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


def current_slot(now: datetime | None = None) -> tuple[str, datetime] | None:
    """The entry slot `now` falls in: (key "YYYY-MM-DDTHH", the slot's start), or None."""
    et = to_et(now)
    if et.weekday() >= 5 or et.hour not in ENTRY_HOURS or et.minute >= SLOT_MINUTES:
        return None
    start = et.replace(minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H"), start


def in_entry_window(now: datetime | None = None) -> bool:
    return current_slot(now) is not None


def last_slot_of_day(slot_key: str) -> bool:
    return slot_key.endswith(f"T{max(ENTRY_HOURS):02d}")


def scan_fresh(scan: dict | None, slot_start: datetime) -> bool:
    """True when the Quant scan was produced for this slot: no older than SCAN_MAX_AGE before it opened."""
    as_of = ((scan or {}).get("meta") or {}).get("asOf")
    if not as_of:
        return False
    try:
        t = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t >= slot_start - timedelta(seconds=SCAN_MAX_AGE)


def request_scan(slot_key: str) -> bool:
    """Drop the bridge's task_inbox/quant_scan marker once per slot. False when the inbox isn't mounted."""
    day = _read(DAY_FILE, {}) or {}
    if day.get("scanRequested") == slot_key:
        return False
    if not os.path.isdir(INBOX_DIR):
        return False
    try:
        with open(os.path.join(INBOX_DIR, "quant_scan"), "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except OSError:
        return False
    _write(DAY_FILE, {**day, "scanRequested": slot_key})
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


def accounts_in(ctx: dict) -> list[dict]:
    """The accounts the rules run for, in the dashboard's order; the paper account runs on its own."""
    snap = ctx.get("snapshot") or {}
    listed = [a for a in snap.get("accounts") or [] if isinstance(a, dict) and a.get("id")]
    if not listed:
        listed = [{"id": k} for k in (snap.get("data") or {})]
    return [a for a in listed if a["id"] != paper.PAPER_ID and (not ACCOUNTS or a["id"] in ACCOUNTS)]


def _migrate_keys(keys: list[str], account_ids: list[str], primary: str | None) -> dict[str, str]:
    """Keys written before accounts were named (kind-first) belong to the first account."""
    out: dict[str, str] = {}
    for k in keys:
        if primary and not any(k.startswith(a + "|") for a in account_ids) and k.split("|", 1)[0] in ("csp", "close", "cc", "note"):
            out[k] = f"{primary}|{k}"
    return out


def run_once(force: bool = False, now: float | None = None, entries: bool | None = None, when: datetime | None = None,
             requested: bool = False) -> dict:
    """One pass. `entries` forces the entry half on/off (None = follow the slots). Returns a summary for the log and the self-test."""
    now = now or time.time()
    when = when or datetime.fromtimestamp(now, timezone.utc)
    if not force and not market_open(when):
        return {"skipped": "market closed"}
    ctx = suggest.load_context(DATA_DIR)
    today = to_et(when).date().isoformat()
    day = _read(DAY_FILE, {}) or {}
    slot = current_slot(when)
    waiting = False
    if entries is None:
        entries = False
        if slot and day.get("entriesBuilt") != slot[0]:
            if request_scan(slot[0]):
                _log(f"entry slot {slot[0]} open — asked the bridge for a fresh Quant scan")
                day = _read(DAY_FILE, {}) or {}
            if scan_fresh(ctx.get("scan"), slot[1]):
                entries = True
            else:
                waiting = True

    accts = accounts_in(ctx)
    ids = [a["id"] for a in accts]
    fresh: list[dict] = []
    for a in accts:
        label = suggest.account_label(a)
        try:
            built = suggest.build(ctx, a["id"], today=to_et(when).date(), entries=entries)
        except Exception as exc:  # noqa: BLE001 — one account's bad data must not silence the others
            _log(f"{label}: ERROR - {exc}")
            continue
        for s in built:
            s["key"] = f"{a['id']}|{s['key']}"
            s["account"] = label
            fresh.append(s)
    if waiting and slot and last_slot_of_day(slot[0]) and not in_entry_window(when + timedelta(seconds=INTERVAL)) and day.get("entriesDay") != today:
        # the day's last slot is closing and no slot was served: say so instead of staying silent
        fresh.append({"key": f"note|scan|{today}", "kind": "note", "symbol": "—", "accountId": "",
                      "title": "No fresh Quant scan today",
                      "detail": "Every entry slot passed without a scan newer than its start, so no new puts were evaluated today. Check the bridge's quant scan.",
                      "amount": None, "rule": "entry slots"})
    if entries:
        day = {**day, "entriesBuilt": slot[0] if slot else f"{today}Trun", "entriesDay": today}
        _write(DAY_FILE, day)
    evaluated = set(ENTRY_KINDS) | {"close"} if entries else {"close"}

    # The Auto Trader paper account: same rules, booked as trades, on the same slots.
    paper_meta = None
    if paper.ENABLED:
        try:
            paper_meta = paper.run(ctx, DATA_DIR, entries=entries, today=to_et(when).date(),
                                   vix=((ctx.get("vix") or {}).get("inputs") or {}).get("vix"),
                                   market_open=market_open(when))  # a Run now at 3 a.m. suggests; it does not fill
        except Exception as exc:  # noqa: BLE001 — the paper book must never block the live suggestions
            _log(f"paper: ERROR - {exc}")

    out_path = os.path.join(DATA_DIR, OUT_FILE)
    existing = {s["key"]: s for s in (_read(out_path, {}) or {}).get("suggestions", [])}
    feedback = _read(os.path.join(DATA_DIR, FEEDBACK_FILE), {}) or {}
    sent = _read(SENT_FILE, {}) or {}
    # Rows and receipts from before accounts were named: keep them, under the first account.
    primary = ids[0] if ids else None
    for old, new in _migrate_keys(list(existing), ids, primary).items():
        row = existing.pop(old)
        existing[new] = {**row, "key": new}
    for old, new in _migrate_keys(list(sent), ids, primary).items():
        sent[new] = sent.pop(old)
    for old, new in _migrate_keys(list(feedback), ids, primary).items():
        feedback[new] = feedback.pop(old)
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
    # Only kinds this pass evaluated can expire: between slots, the morning's puts stand.
    for key, prior in existing.items():
        if key in merged:
            continue
        if prior.get("status") == "new" and prior.get("kind") in evaluated:
            prior = {**prior, "status": "expired", "expiredAt": prior.get("expiredAt") or stamp}
        merged[key] = prior

    rows = sorted(merged.values(), key=lambda r: r.get("lastSeen", ""), reverse=True)[:KEEP]
    _write(out_path, {
        "meta": {"asOf": stamp, "interval": INTERVAL, "paused": paused, "ntfy": bool(cfg["topic"]),
                 "window": f"{ENTRY_HOURS[0]:02d}:00–{ENTRY_HOURS[-1]:02d}:00 ET hourly" if ENTRY_HOURS else "off",
                 "slots": ENTRY_HOURS, "entriesBuilt": day.get("entriesBuilt"),
                 "accounts": [suggest.account_label(a) for a in accts],
                 "lastPass": "run now" if requested else ("entries" if entries else "closes"),
                 "paper": paper_meta, "build": (os.environ.get("BUILD_SHA") or "")[:7] or None,
                 "active": sum(1 for r in rows if r["status"] == "new"), "pushed": pushed},
        "suggestions": rows,
    })
    _write(SENT_FILE, sent)
    return {"active": sum(1 for r in rows if r["status"] == "new"), "pushed": pushed, "paused": paused,
            "entries": entries, "accounts": len(accts), **({"waiting": "scan"} if waiting else {})}


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
    hours = ", ".join(f"{h:02d}:00" for h in ENTRY_HOURS) or "none"
    _log(f"trader started — closes every {INTERVAL}s during the session, entries at {hours} ET for every account; ntfy "
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
