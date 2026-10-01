"""
suggest.py — turn what the bridge already knows into trade suggestions.

Pure: reads the dashboard's data folder (snapshot, quant scan, Morning Brief,
VIX), applies the same rules the dashboard's Quant pages apply, and returns a
list of suggestions. Nothing here talks to a broker or a phone.

The rules (the wheel study's combo 87 with 0.75-delta LEAPS):
  * new CSP: the scan's pick for every name that qualifies (no cap on how many,
    like the backtest), sized against this account — the smallest of free cash,
    room under the 10% per-name cap (15% stretch when adding) and room under
    buying power, in whole contracts; skipped when it spans earnings or the
    name is already at its cap. Built only in the entry window (trader.py).
  * close a short put once 50% of its credit is captured
  * on 100+ shares with no call: a 7–21 day call at or above cost, the furthest
    strike still paying 0.5% of basis a week (from the bridge's ladder)
  * notes: a name over its cap, collateral beyond cash and the VIX allowance
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

MULT = 100
CASH_EQUIVALENTS = {"SWVXX", "SNVXX", "SNSXX", "SNOXX", "SWGXX", "SNAXX", "SGUXX", "SNRXX", "SUTXX", "SWPXX"}
R = {
    "closeAt": 0.5, "maxPerTicker": 0.10, "tickerBand": 0.05,
    "callMinDte": 7, "callMaxDte": 21, "callWeeklyMin": 0.005,
}


def _read(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def load_context(data_dir: str) -> dict:
    """Everything the rules need, read fresh. Missing files are None."""
    return {
        "snapshot": _read(os.path.join(data_dir, "snapshot.json")),
        "scan": _read(os.path.join(data_dir, "quant-scan.json")),
        "report": _read(os.path.join(data_dir, "am_report.json")),
        "vix": _read(os.path.join(data_dir, "vix.json")),
    }


# ---- capital, the way the dashboard counts it ---------------------------------
def _dte(exp: str, today: date | None = None) -> int:
    today = today or date.today()
    try:
        return (date.fromisoformat(exp[:10]) - today).days
    except ValueError:
        return 0


def _spread_pairs(options: list[dict]):
    groups: dict[str, list[dict]] = {}
    for o in options:
        if o.get("kind") in ("put-spread", "call-spread"):
            groups.setdefault(f"{o['symbol']}|{o['optionType']}|{o['expiration']}", []).append(o)
    for legs in groups.values():
        shorts = [o for o in legs if o["side"] == "short"]
        longs = [o for o in legs if o["side"] == "long"]
        for s in shorts:
            match = next((l for l in longs if l["strike"] != s["strike"]), None)
            if not match:
                continue
            longs.remove(match)
            yield s, match


def spread_risk(options: list[dict]) -> float:
    """Capital at risk: width − credit for a credit spread, the debit for a debit spread."""
    total = 0.0
    for s, l in _spread_pairs(options):
        qty = min(s["qty"], l["qty"])
        net = s["entryPerShare"] - l["entryPerShare"]
        width = abs(s["strike"] - l["strike"])
        total += (max(width - net, 0) if net >= 0 else -net) * MULT * qty
    return total


def spread_cash_requirement(options: list[dict]) -> float:
    """What the broker holds: full width for a credit spread, nothing for a debit."""
    total = 0.0
    for s, l in _spread_pairs(options):
        if s["entryPerShare"] - l["entryPerShare"] < 0:
            continue
        total += abs(s["strike"] - l["strike"]) * MULT * min(s["qty"], l["qty"])
    return total


def csp_collateral(options: list[dict]) -> float:
    return sum(o["strike"] * MULT * o["qty"] for o in options if o.get("kind") == "csp" and o["side"] == "short")


def vix_margin(vix: float | None) -> float:
    if vix is None or vix < 20:
        return 0.0
    return min(0.35, 0.05 * int(vix // 5))


def capacity(acct: dict, vix: float | None) -> dict:
    s, eq, opts = acct["summary"], acct["equities"], acct["options"]
    options_net = sum((1 if o["side"] == "long" else -1) * o["mark"] * MULT * o["qty"] for o in opts)
    cash = s["totalValue"] - s["equityValue"] - s.get("cryptoValue", 0) - options_net
    money_market = sum(e["qty"] * e["price"] for e in eq if e["symbol"] in CASH_EQUIVALENTS)
    margin = vix_margin(vix)
    free = max(0.0, cash + money_market - csp_collateral(opts) - spread_cash_requirement(opts))
    return {
        "totalValue": s["totalValue"], "margin": margin,
        "buyingPower": s["totalValue"] * (1 + margin),
        "freeCash": free + margin * s["totalValue"],
        "putObligations": csp_collateral(opts) + spread_cash_requirement(opts),
    }


def committed(sym: str, acct: dict) -> float:
    opts = [o for o in acct["options"] if o["symbol"] == sym]
    longs = sum(o["mark"] * MULT * o["qty"] for o in opts if o["side"] == "long" and o.get("kind") in ("leap-call", "leap-put-hedge", "other"))
    shares = sum(e["qty"] * e["price"] for e in acct["equities"] if e["symbol"] == sym)
    return csp_collateral(opts) + spread_risk(opts) + longs + shares


def committed_total(acct: dict) -> float:
    syms = {o["symbol"] for o in acct["options"]} | {e["symbol"] for e in acct["equities"]}
    return sum(committed(s, acct) for s in syms)


def fit(pick: dict, sym: str, acct: dict, cap: dict) -> dict:
    """How many contracts the rules allow, plus the flags the dashboard shows."""
    c = committed(sym, acct)
    per_cap = R["maxPerTicker"] * cap["buyingPower"]
    room_ticker = per_cap - c
    room_total = cap["buyingPower"] - committed_total(acct)
    room = min(room_ticker, room_total, cap["freeCash"])
    contracts = int(room // pick["collateral"]) if room > 0 else 0
    if contracts < 1 and room_ticker > 0:
        cap_hi = (R["maxPerTicker"] + R["tickerBand"]) * cap["buyingPower"] - c
        if cap_hi >= pick["collateral"] and min(room_total, cap["freeCash"]) >= pick["collateral"]:
            contracts = 1
    return {"contracts": contracts, "held": c > 0, "full": c >= per_cap, "cashShort": cap["freeCash"] < pick["collateral"], "committed": c, "perTickerCap": per_cap}


# ---- the suggestions ---------------------------------------------------------
def _money(n: float) -> str:
    return f"${round(n):,}"


def build(ctx: dict, account_id: str | None = None, today: date | None = None, entries: bool = True) -> list[dict]:
    """All suggestions, or with entries=False just the closes (what runs outside the entry window)."""
    snap = ctx.get("snapshot")
    if not snap or not snap.get("data"):
        return []
    acct_id = account_id or next(iter(snap["data"]))
    acct = snap["data"].get(acct_id)
    if not acct:
        return []
    vix = ((ctx.get("vix") or {}).get("inputs") or {}).get("vix")
    cap = capacity(acct, vix)
    today = today or date.today()
    out: list[dict] = []

    # 1. Close at 50%.
    for o in acct["options"]:
        if o.get("kind") != "csp" or o["side"] != "short" or not o.get("entryPerShare"):
            continue
        captured = (o["entryPerShare"] - o["mark"]) / o["entryPerShare"]
        dte = _dte(o["expiration"], today)
        if captured >= R["closeAt"] and dte > 0:
            out.append({
                "key": f"close|{o['symbol']}|{o['strike']}|{o['expiration']}",
                "kind": "close", "symbol": o["symbol"], "strike": o["strike"], "expiration": o["expiration"],
                "qty": o["qty"], "price": round(o["mark"], 2),
                "title": f"Close {o['qty']} × {o['symbol']} ${o['strike']:g} put ({o['expiration'][5:]})",
                "detail": f"{round(captured * 100)}% of the {o['entryPerShare']:.2f} credit captured; buy back near {o['mark']:.2f} with {dte} days left. The study closes here every time.",
                "amount": round(o["mark"] * MULT * o["qty"], 2),
                "rule": "close at 50%",
            })

    if not entries:
        for s in out:
            s["accountId"] = acct_id
        return out

    # 2. New CSPs from the scan: every name that qualifies, in Brief-score then yield order.
    scan = ctx.get("scan") or {}
    report = ctx.get("report") or {}
    scored = {r["sym"]: r for r in (report.get("screened") or report.get("board") or [])}
    picks = []
    for row in scan.get("rows") or []:
        p = row.get("pick")
        if not p or row.get("erInWindow"):
            continue
        if _dte(p["exp"], today) < 20:
            continue  # a stale scan: the contract is no longer in the window
        f = fit(p, row["sym"], acct, cap)
        if f["contracts"] < 1 or f["full"]:
            continue
        score = (scored.get(row["sym"]) or {}).get("score")
        picks.append((-(score if score is not None else -1), -p["yield30"], row, p, f, score))
    picks.sort(key=lambda t: (t[0], t[1]))
    for _, _, row, p, f, score in picks:
        n = f["contracts"]
        out.append({
            "key": f"csp|{row['sym']}|{p['strike']}|{p['exp']}",
            "kind": "csp", "symbol": row["sym"], "strike": p["strike"], "expiration": p["exp"],
            "qty": n, "price": p["mark"], "delta": p["delta"], "yield30": p["yield30"],
            "title": f"Sell {n} × {row['sym']} ${p['strike']:g} put ({p['exp'][5:]}, {p['dte']}d)",
            "detail": (f"{p['yield30']:.1f}% per 30 days at the {p['mark']:.2f} mid ({p['bid']:.2f}–{p['ask']:.2f}), {p['delta']:.2f}Δ, "
                       f"{p['belowSpotPct']:.1f}% below ${row['price']:.2f}. {_money(p['collateral'] * n)} collateral, {_money(p['mark'] * MULT * n)} credit."
                       + (f" Brief score {round(score)}." if score is not None else "")
                       + (" Already held — this adds." if f["held"] else "")),
            "amount": round(p["mark"] * MULT * n, 2),
            "rule": "4% target",
        })

    # 3. Covered calls on 100+ shares with no call on.
    called = {o["symbol"] for o in acct["options"] if o["side"] == "short" and o["optionType"] == "call"}
    by_sym: dict[str, list[dict]] = {}
    for e in acct["equities"]:
        by_sym.setdefault(e["symbol"], []).append(e)
    for sym, lots in by_sym.items():
        shares = sum(e["qty"] for e in lots)
        if shares < 100 or sym in called or sym in CASH_EQUIVALENTS:
            continue
        basis = sum(e["qty"] * e["avgCost"] for e in lots) / shares
        ladder = [c for c in (lots[0].get("coveredCalls") or []) if R["callMinDte"] <= c["dte"] <= R["callMaxDte"] and c["strike"] >= basis]
        ok = [c for c in ladder if (c["mark"] / basis) / (c["dte"] / 7) >= R["callWeeklyMin"]]
        if not ok:
            continue
        top = max(c["strike"] for c in ok)
        pick = max((c for c in ok if c["strike"] == top), key=lambda c: (c["mark"] / basis) / (c["dte"] / 7))
        n = shares // 100
        out.append({
            "key": f"cc|{sym}|{pick['strike']}|{pick['dte']}",
            "kind": "cc", "symbol": sym, "strike": pick["strike"], "qty": n, "price": pick["mark"], "dte": pick["dte"],
            "title": f"Sell {n} × {sym} ${pick['strike']:g} call, {pick['dte']}d",
            "detail": f"Furthest strike at or above your ${basis:.2f} basis paying {(pick['mark'] / basis) / (pick['dte'] / 7) * 100:.2f}% of basis a week ({pick['mark']:.2f} × 100 × {n}).",
            "amount": round(pick["mark"] * MULT * n, 2),
            "rule": "covered call",
        })

    # 4. Notes: outside the rules.
    if cap["freeCash"] < 0:
        out.append({"key": "note|cash", "kind": "note", "symbol": "—",
                    "title": f"Collateral exceeds cash by {_money(-cap['freeCash'])}",
                    "detail": f"{_money(cap['putObligations'])} pledged; margin allowance {round(cap['margin'] * 100)}% at VIX {vix if vix is not None else '?'}.",
                    "amount": round(-cap["freeCash"], 2), "rule": "cash-secured"})
    cap_hi = (R["maxPerTicker"] + R["tickerBand"]) * cap["buyingPower"]
    for sym in sorted({o["symbol"] for o in acct["options"]} | {e["symbol"] for e in acct["equities"]}):
        c = committed(sym, acct)
        if c > cap_hi:
            out.append({"key": f"note|cap|{sym}", "kind": "note", "symbol": sym,
                        "title": f"{sym} is {_money(c - R['maxPerTicker'] * cap['buyingPower'])} over its cap",
                        "detail": f"{_money(c)} in {sym} against a {_money(R['maxPerTicker'] * cap['buyingPower'])} 10% cap ({_money(cap_hi)} stretch).",
                        "amount": round(c - R["maxPerTicker"] * cap["buyingPower"], 2), "rule": "10% per name"})

    for s in out:
        s["accountId"] = acct_id
    return out
