"""
paper.py — the "Auto Trader" paper account: the rules, traded for real-looking
money that isn't real.

A manual account (the dashboard's hand-tracked kind, priced by the bridge every
cycle) named Auto Trader is created in data/manual_positions.json with
PAPER_CASH (default $400,000). Every pass the trader applies the same rules it
applies to the live account, but to this one, and books the trades itself:

  * new CSP   → a short put row; cash drops by the collateral (the dashboard's
                manual model carries a short put as collateral + P/L, so the
                premium collected shows there, not in cash; the dashboard's
                uncommitted cash adds it back, and so does the sizing here)
  * close 50% → the row goes; cash gets the collateral back plus the realized
                credit − cost; a round-trip is written to data/manual/csp-closed.json
  * covered call → a short call row (its premium shows as P/L until it ends)
  * expiry    → settled on the first pass after: an in-the-money put is assigned
                (100 shares per contract at strike − premium, cash gets the
                premium); an out-of-the-money one expires (cash gets collateral +
                premium); an in-the-money call is called away at the strike;
                each writes the matching closed record

So the account's value, open P/L, realized P/L and history all appear in the
dashboard like any other account, filtered to Auto Trader. The dashboard's
Trader page shows the trade log (data/trader-paper.json). Nothing here touches
a broker.

Entries follow the live account's timing (the entry window, or Run now) and
happen at most once a day; closes and expiries are checked every pass.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone

import suggest

PAPER_ID = "manual-auto-trader"
LABEL = os.environ.get("PAPER_LABEL", "Auto Trader")
CASH = float(os.environ.get("PAPER_CASH", "400000"))
ENABLED = (os.environ.get("TRADER_PAPER", "1") or "").strip().lower() not in ("0", "false", "no", "off", "")
MANUAL_FILE = "manual_positions.json"
MANUAL_SNAPSHOT = os.path.join("manual", "snapshot.json")
MANUAL_DIR = "manual"
LOG_FILE = "trader-paper.json"
KEEP = 500
MULT = 100


def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _new_id() -> str:
    return f"{int(time.time() * 1000):x}-{os.urandom(3).hex()}"


def _r2(n: float) -> float:
    return round(n * 100) / 100


def _r4(n: float) -> float:
    return round(n * 10_000) / 10_000


def _days(opened: str | None, closed: str) -> int:
    try:
        d = (date.fromisoformat(closed) - date.fromisoformat(opened or closed)).days
    except ValueError:
        d = 1
    return max(1, d)


# ---- the manual file --------------------------------------------------------------
def ensure_account(data_dir: str) -> dict:
    """The Auto Trader account in data/manual_positions.json, created on first use."""
    path = os.path.join(data_dir, MANUAL_FILE)
    doc = _read(path, None) or {"version": 1, "accounts": []}
    if not isinstance(doc.get("accounts"), list):
        doc["accounts"] = []
    acct = next((a for a in doc["accounts"] if a.get("id") == PAPER_ID), None)
    if acct is None:
        acct = {"id": PAPER_ID, "label": LABEL, "cash": CASH, "positions": [], "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "paper": True}
        doc["accounts"].append(acct)
        _write(path, doc)
    return acct


def save_account(data_dir: str, acct: dict) -> None:
    """Write the account back, re-reading the file so other accounts' edits made meanwhile survive."""
    path = os.path.join(data_dir, MANUAL_FILE)
    doc = _read(path, None) or {"version": 1, "accounts": []}
    acct["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    acct["cash"] = _r2(acct["cash"])
    accounts = [a for a in doc.get("accounts", []) if a.get("id") != PAPER_ID]
    accounts.append(acct)
    _write(path, {"version": 1, "accounts": accounts})


def snapshot_account(data_dir: str) -> dict | None:
    """The bridge's priced view of the account (None before its first cycle)."""
    doc = _read(os.path.join(data_dir, MANUAL_SNAPSHOT), None) or {}
    return (doc.get("data") or {}).get(PAPER_ID)


def _synthetic(acct: dict) -> dict:
    """What the account looks like before the bridge has priced it: cash only."""
    return {"summary": {"totalValue": acct["cash"], "equityValue": 0, "cryptoValue": 0, "cash": acct["cash"]},
            "equities": [], "options": [], "valueHistory": []}


def credits(acct: dict) -> float:
    """Premium collected on the open short options. In the manual model it lives in
    the options' P/L, not in `cash`; at a broker it is cash, and the backtest spent it."""
    return sum(p["premium"] * MULT * p["qty"] for p in acct["positions"] if p.get("type") == "option" and p.get("side") == "short")


def capacity(acct: dict, snap_acct: dict | None, vix: float | None) -> dict:
    """Free cash the way the dashboard and the backtest count it: cash beyond the
    collateral, plus the premiums collected (the dashboard's uncommitted cash)."""
    total = float(((snap_acct or {}).get("summary") or {}).get("totalValue") or acct["cash"])
    margin = suggest.vix_margin(vix)
    collateral = sum(p["strike"] * MULT * p["qty"] for p in acct["positions"] if p.get("type") == "option" and p.get("side") == "short" and p.get("optionType") == "put")
    return {"totalValue": total, "margin": margin, "buyingPower": total * (1 + margin),
            "freeCash": float(acct["cash"]) + credits(acct) + margin * total, "putObligations": collateral}


# ---- closed records, in the dashboard's shapes ------------------------------------
def _append_closed(data_dir: str, file: str, rec: dict) -> None:
    path = os.path.join(data_dir, MANUAL_DIR, file)
    doc = _read(path, None)
    if not doc or not isinstance(doc.get("closed"), list):
        doc = {"meta": {"generatedAt": "", "source": "manual", "note": "Closed by the Auto Trader paper account"}, "closed": []}
    doc["closed"].append(rec)
    doc["meta"]["generatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write(path, doc)


def _closed_csp(data_dir: str, o: dict, close_price: float, closed_at: str, outcome: str) -> dict:
    credit = o["premium"] * MULT * o["qty"]
    cost = close_price * MULT * o["qty"]
    collateral = o["strike"] * MULT * o["qty"]
    days = _days(o.get("openedAt"), closed_at)
    realized = 0.0 if outcome == "assigned" else credit - cost
    roc = 0.0 if outcome == "assigned" or not collateral else realized / collateral
    rec = {"id": f"manual:{o['id']}", "symbol": o["symbol"], "name": f"{o['symbol']} · {LABEL}", "accountId": PAPER_ID,
           "strike": o["strike"], "expiration": o["expiration"], "openedAt": o.get("openedAt") or closed_at, "closedAt": closed_at,
           "contracts": o["qty"], "creditPerShare": _r2(o["premium"]), "creditReceived": _r2(credit), "costToClose": _r2(cost),
           "realizedPnl": _r2(realized), "outcome": outcome, "daysHeld": days, "collateral": _r2(collateral),
           "returnOnCollateral": _r4(roc), "annualized": _r4(roc * 365 / days)}
    _append_closed(data_dir, "csp-closed.json", rec)
    return rec


def _closed_call(data_dir: str, o: dict, close_price: float, closed_at: str, outcome: str) -> dict:
    credit = o["premium"] * MULT * o["qty"]
    cost = 0.0 if outcome in ("expired", "assigned") else close_price * MULT * o["qty"]
    notional = o["strike"] * MULT * o["qty"]
    days = _days(o.get("openedAt"), closed_at)
    realized = credit - cost
    ret = realized / notional if notional else 0.0
    rec = {"id": f"manual:{o['id']}", "symbol": o["symbol"], "name": f"{o['symbol']} · {LABEL}", "accountId": PAPER_ID,
           "strike": o["strike"], "expiration": o["expiration"], "openedAt": o.get("openedAt") or closed_at, "closedAt": closed_at,
           "contracts": o["qty"], "creditPerShare": _r2(o["premium"]), "creditReceived": _r2(credit), "costToClose": _r2(cost),
           "realizedPnl": _r2(realized), "outcome": "expired" if outcome == "assigned" else outcome, "daysHeld": days,
           "returnOnNotional": _r4(ret), "annualized": _r4(ret * 365 / days)}
    _append_closed(data_dir, "covered-closed.json", rec)
    return rec


def _closed_stock(data_dir: str, s: dict, shares: int, price: float, closed_at: str) -> dict:
    cost = s["avgCost"] * shares
    proceeds = price * shares
    realized = proceeds - cost
    days = _days(s.get("openedAt"), closed_at)
    ret = realized / cost if cost else 0.0
    rec = {"id": f"manual:{s['id']}:{closed_at}", "symbol": s["symbol"], "name": f"{s['symbol']} · {LABEL}", "accountId": PAPER_ID,
           "side": "long", "shares": shares, "avgOpen": _r4(s["avgCost"]), "avgClose": _r4(price), "costBasis": _r2(cost), "proceeds": _r2(proceeds),
           "realizedPnl": _r2(realized), "outcome": "closed_profit" if realized >= 0 else "closed_loss",
           "openedAt": s.get("openedAt") or closed_at, "closedAt": closed_at, "daysHeld": days, "returnPct": _r4(ret), "annualized": _r4(ret * 365 / days)}
    _append_closed(data_dir, "stocks-closed.json", rec)
    return rec


# ---- settlement and trades ---------------------------------------------------------
def _underlying(snap_acct: dict | None, o: dict) -> float | None:
    """The underlying's price for a manual option row, from the bridge's priced view."""
    want = f"{PAPER_ID}:{o['id']}"
    for row in (snap_acct or {}).get("options") or []:
        if row.get("id") == want and row.get("underlyingPrice") is not None:
            return float(row["underlyingPrice"])
    for e in (snap_acct or {}).get("equities") or []:
        if e.get("symbol") == o["symbol"] and e.get("price"):
            return float(e["price"])
    return None


def _entry(kind: str, text: str, amount: float | None, when: str, **extra) -> dict:
    return {"at": when, "kind": kind, "text": text, "amount": None if amount is None else _r2(amount), **extra}


def settle_expired(data_dir: str, acct: dict, snap_acct: dict | None, today: date, when: str) -> list[dict]:
    """Options past expiry: assign, call away or expire, using the last underlying price the bridge saw."""
    out: list[dict] = []
    keep: list[dict] = []
    stocks = [p for p in acct["positions"] if p.get("type") == "stock"]
    for p in acct["positions"]:
        if p.get("type") != "option" or p.get("expiration", "9999") >= today.isoformat():
            keep.append(p)
            continue
        px = _underlying(snap_acct, p)
        if px is None:
            keep.append(p)  # no price yet: settle on a later pass
            continue
        exp = p["expiration"]
        if p["side"] == "short" and p["optionType"] == "put":
            if px < p["strike"]:
                basis = _r2(p["strike"] - p["premium"])
                shares = {"id": _new_id(), "type": "stock", "symbol": p["symbol"], "qty": MULT * p["qty"], "avgCost": basis, "openedAt": exp}
                keep.append(shares)
                stocks.append(shares)
                acct["cash"] += p["premium"] * MULT * p["qty"]
                _closed_csp(data_dir, p, p["strike"] - px, exp, "assigned")
                out.append(_entry("assigned", f"{p['symbol']} ${p['strike']:g} put assigned at {px:.2f}: {MULT * p['qty']} shares at {basis:.2f}", -basis * MULT * p["qty"], when, symbol=p["symbol"]))
            else:
                acct["cash"] += (p["strike"] + p["premium"]) * MULT * p["qty"]
                _closed_csp(data_dir, p, 0.0, exp, "expired")
                out.append(_entry("expired", f"{p['symbol']} ${p['strike']:g} put expired worthless at {px:.2f}: +{p['premium'] * MULT * p['qty']:,.0f}", p["premium"] * MULT * p["qty"], when, symbol=p["symbol"]))
        elif p["side"] == "short" and p["optionType"] == "call":
            if px > p["strike"]:
                to_sell = MULT * p["qty"]
                for s in stocks:
                    if to_sell <= 0:
                        break
                    if s["symbol"] != p["symbol"] or s["qty"] <= 0:
                        continue
                    n = min(s["qty"], to_sell)
                    _closed_stock(data_dir, s, n, p["strike"], exp)
                    s["qty"] -= n
                    to_sell -= n
                    acct["cash"] += p["strike"] * n
                acct["cash"] += p["premium"] * MULT * p["qty"]
                _closed_call(data_dir, p, 0.0, exp, "assigned")
                out.append(_entry("called", f"{p['symbol']} called away at ${p['strike']:g} (price {px:.2f}): +{p['premium'] * MULT * p['qty']:,.0f} premium", p["strike"] * MULT * p["qty"], when, symbol=p["symbol"]))
            else:
                acct["cash"] += p["premium"] * MULT * p["qty"]
                _closed_call(data_dir, p, 0.0, exp, "expired")
                out.append(_entry("expired", f"{p['symbol']} ${p['strike']:g} call expired worthless at {px:.2f}: +{p['premium'] * MULT * p['qty']:,.0f}", p["premium"] * MULT * p["qty"], when, symbol=p["symbol"]))
        else:
            keep.append(p)  # a long option: not something the rules open; leave it to the Settings page
    acct["positions"] = [p for p in keep if not (p.get("type") == "stock" and p.get("qty", 0) <= 0)]
    return out


def _find_put(acct: dict, s: dict) -> dict | None:
    for p in acct["positions"]:
        if p.get("type") == "option" and p["side"] == "short" and p["optionType"] == "put" and p["symbol"] == s["symbol"] \
                and abs(float(p["strike"]) - float(s["strike"])) < 1e-6 and p["expiration"] == s["expiration"]:
            return p
    return None


def apply(data_dir: str, acct: dict, suggestions: list[dict], today: date, when: str, traded_today: set[str] | None = None) -> list[dict]:
    """Book each suggestion as a trade in the paper account. With hourly entry
    slots, a name gets at most one new put a day (the backtest added daily)."""
    out: list[dict] = []
    traded_today = traded_today if traded_today is not None else set()
    for s in suggestions:
        k = s["kind"]
        if k == "csp":
            if _find_put(acct, s) or s["symbol"] in traded_today:
                continue  # already on (the bridge's view lagged a pass), or sold one earlier today
            traded_today.add(s["symbol"])
            collateral = s["strike"] * MULT * s["qty"]
            if collateral > acct["cash"] + credits(acct):
                continue  # the premiums collected count as spendable, as at a broker; cash may dip below zero by at most that much
            acct["positions"].append({"id": _new_id(), "type": "option", "symbol": s["symbol"], "optionType": "put", "side": "short",
                                      "qty": s["qty"], "strike": s["strike"], "expiration": s["expiration"], "premium": s["price"], "openedAt": today.isoformat()})
            acct["cash"] -= collateral
            out.append(_entry("csp", f"Sold {s['qty']} × {s['symbol']} ${s['strike']:g} put ({s['expiration']}) at {s['price']:.2f}", s["price"] * MULT * s["qty"], when, symbol=s["symbol"], rule=s.get("rule")))
        elif k == "close":
            p = _find_put(acct, s)
            if not p:
                continue
            price = float(s["price"])
            realized = (p["premium"] - price) * MULT * p["qty"]
            acct["cash"] += p["strike"] * MULT * p["qty"] + realized
            _closed_csp(data_dir, p, price, today.isoformat(), "closed_profit" if realized >= 0 else "closed_loss")
            acct["positions"] = [x for x in acct["positions"] if x is not p]
            out.append(_entry("close", f"Bought back {p['qty']} × {p['symbol']} ${p['strike']:g} put at {price:.2f}: {'+' if realized >= 0 else '−'}{abs(realized):,.0f}", realized, when, symbol=p["symbol"], rule=s.get("rule")))
        elif k == "cc":
            if any(p.get("type") == "option" and p["side"] == "short" and p["optionType"] == "call" and p["symbol"] == s["symbol"] for p in acct["positions"]):
                continue
            exp = s.get("expiration") or (date.fromordinal(today.toordinal() + int(s.get("dte") or 14))).isoformat()
            acct["positions"].append({"id": _new_id(), "type": "option", "symbol": s["symbol"], "optionType": "call", "side": "short",
                                      "qty": s["qty"], "strike": s["strike"], "expiration": exp, "premium": s["price"], "openedAt": today.isoformat()})
            out.append(_entry("cc", f"Sold {s['qty']} × {s['symbol']} ${s['strike']:g} call ({exp}) at {s['price']:.2f}", s["price"] * MULT * s["qty"], when, symbol=s["symbol"], rule=s.get("rule")))
    return out


def run(ctx: dict, data_dir: str, entries: bool, today: date, vix: float | None, market_open: bool = True) -> dict:
    """One pass over the paper account. Returns the summary written to the log's meta.
    Nothing is booked outside market hours — not even on Run now — since a fill at
    3 a.m. is not a fill; the log's meta just notes the pass was skipped."""
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    acct = ensure_account(data_dir)
    log_path = os.path.join(data_dir, LOG_FILE)
    log = _read(log_path, None) or {"trades": []}
    if not market_open:
        meta = {**(log.get("meta") or {}), "skipped": f"market closed at {when}", "entries": False}
        _write(log_path, {"meta": meta, "trades": list(log.get("trades") or [])[-KEEP:]})
        return meta
    snap_acct = snapshot_account(data_dir)
    trades: list[dict] = list(log.get("trades") or [])

    trades += settle_expired(data_dir, acct, snap_acct, today, when)
    view = snap_acct or _synthetic(acct)
    ctx_paper = {**ctx, "snapshot": {"data": {PAPER_ID: view}}}
    cap = capacity(acct, snap_acct, vix)
    sug = suggest.build(ctx_paper, PAPER_ID, today=today, entries=entries, cap=cap)
    traded_today = {t.get("symbol") for t in trades if t.get("kind") == "csp" and str(t.get("at", ""))[:10] == today.isoformat()}
    trades += apply(data_dir, acct, [s for s in sug if s["kind"] in ("csp", "close", "cc")], today, when, traded_today)
    save_account(data_dir, acct)

    opts = [p for p in acct["positions"] if p.get("type") == "option"]
    meta = {"asOf": when, "accountId": PAPER_ID, "label": LABEL, "startingCash": CASH, "cash": _r2(acct["cash"]),
            "totalValue": _r2(float((view.get("summary") or {}).get("totalValue") or acct["cash"])),
            "puts": sum(1 for p in opts if p["optionType"] == "put"), "calls": sum(1 for p in opts if p["optionType"] == "call"),
            "shareLots": sum(1 for p in acct["positions"] if p.get("type") == "stock"),
            "priced": snap_acct is not None, "entries": entries, "trades": len(trades)}
    _write(log_path, {"meta": meta, "trades": trades[-KEEP:]})
    return meta


# ---- undo ----------------------------------------------------------------------------
def undo(data_dir: str, since: str, until: str | None = None) -> list[str]:
    """Reverse the paper trades booked between two UTC timestamps (ISO, prefix match
    is fine: "2026-10-02T08:5"). Sales and closes are unwound and dropped from the
    log; settlements (assigned / called / expired) are left alone, since the
    expiry really happened. Returns one line per trade for the terminal.

        docker compose exec trader python /opt/trader/paper.py undo 2026-10-02T08:50 2026-10-02T09:00
    """
    acct = ensure_account(data_dir)
    log_path = os.path.join(data_dir, LOG_FILE)
    log = _read(log_path, None) or {"trades": []}
    trades = list(log.get("trades") or [])
    hit = [t for t in trades if since <= str(t.get("at", "")) and (until is None or str(t.get("at", "")) <= until)]
    out: list[str] = []
    keep_ids: set[int] = set()
    for t in reversed(hit):  # newest first, so a close undone before the sale it followed
        kind, sym = t.get("kind"), t.get("symbol")
        text = t.get("text", "")
        if kind in ("csp", "cc"):
            ot = "put" if kind == "csp" else "call"
            rows = [p for p in acct["positions"] if p.get("type") == "option" and p["side"] == "short" and p["optionType"] == ot and p["symbol"] == sym]
            row = max(rows, key=lambda p: p.get("openedAt") or "") if rows else None
            if not row:
                out.append(f"skip  {text} (position no longer on the book)")
                keep_ids.add(id(t))
                continue
            acct["positions"] = [p for p in acct["positions"] if p is not row]
            if kind == "csp":
                acct["cash"] += row["strike"] * MULT * row["qty"]
            out.append(f"undid {text}")
        elif kind == "close":
            path = os.path.join(data_dir, MANUAL_DIR, "csp-closed.json")
            doc = _read(path, None) or {"closed": []}
            recs = [r for r in doc.get("closed", []) if r.get("symbol") == sym and r.get("accountId") == PAPER_ID and r.get("outcome") in ("closed_profit", "closed_loss")]
            rec = max(recs, key=lambda r: r.get("closedAt") or "") if recs else None
            if not rec:
                out.append(f"skip  {text} (closed record not found)")
                keep_ids.add(id(t))
                continue
            doc["closed"] = [r for r in doc["closed"] if r is not rec]
            _write(path, doc)
            acct["positions"].append({"id": rec["id"].split(":", 1)[1] if ":" in rec["id"] else _new_id(), "type": "option", "symbol": sym, "optionType": "put", "side": "short",
                                      "qty": rec["contracts"], "strike": rec["strike"], "expiration": rec["expiration"], "premium": rec["creditPerShare"], "openedAt": rec.get("openedAt")})
            acct["cash"] -= rec["collateral"] + rec["realizedPnl"]
            out.append(f"undid {text}")
        else:
            out.append(f"kept  {text} (a settlement, not undone)")
            keep_ids.add(id(t))
    hit_ids = {id(t) for t in hit} - keep_ids
    save_account(data_dir, acct)
    _write(log_path, {"meta": {**(log.get("meta") or {}), "cash": _r2(acct["cash"]), "undoneAt": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                      "trades": [t for t in trades if id(t) not in hit_ids]})
    return out or ["nothing in that window"]


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "undo":
        for line in undo(os.environ.get("APP_DATA_DIR", "/app/data"), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None):
            print(line)
    else:
        print("usage: paper.py undo <since-iso-utc> [until-iso-utc]")
