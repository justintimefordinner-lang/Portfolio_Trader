"""
suggest.py — turn what the bridge already knows into trade suggestions.

Pure: reads the dashboard's data folder (snapshot, quant scan, Morning Brief,
VIX), applies the same rules the dashboard's Quant pages apply, and returns a
list of suggestions. Nothing here talks to a broker or a phone.

The rules (the wheel study's combo 87 with 0.75-delta LEAPS):
  * new CSP: the scan's pick for every name that qualifies (no cap on how many,
    like the backtest), sized against this account — the smallest of free cash,
    room under the 10% per-name cap (15% stretch when adding) and room under
    buying power, in whole contracts; skipped when the name is already at its
    cap. Built only in the entry window (trader.py). When cash is short, the
    ranking below decides who gets it (see rank()).
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
# Who gets capital first (the user's picks, 2026-10-03). Every pick already pays
# the 4% target; the score says how good a trade it is:
#   liquidity — bid/ask spread as a share of the mid (0% best, 30%+ worst), plus open interest
#   cushion   — the delta it takes to reach 4% (0.10 best, the 0.35 cap worst)
#   vrp       — the Brief's implied ÷ realized volatility (0.9 worst, 1.3 best; unknown = middle)
# Names with a report inside the put's life queue after those without one.
# (Backtested 2026-10-04: queuing contracts over a third of the free cash behind
# smaller ones changed nothing at $800k and cost a little at $150k, so it's out.)
RANK = {
    "spreadZero": 30.0, "oiFull": 500, "deltaBest": 0.10, "deltaCap": 0.35,
    "vrpLo": 0.9, "vrpHi": 1.3,
    "w": {"liquidity": 0.40, "cushion": 0.35, "vrp": 0.25},
    "perRound": ((80, 3), (65, 2)),   # score at/above -> contracts per round (else 1)
}


def _read(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def load_snapshots(data_dir: str) -> dict | None:
    """Every account the dashboard shows: the base snapshot plus each bridge's
    subfolder (data/acct2/, data/manual/ …), merged the way the app merges them."""
    merged: dict = {"meta": {}, "accounts": [], "data": {}}
    found = False
    dirs = [data_dir]
    try:
        dirs += sorted(os.path.join(data_dir, d) for d in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, d)))
    except OSError:
        pass
    seen: set[str] = set()
    for d in dirs:
        snap = _read(os.path.join(d, "snapshot.json"))
        if not snap or not isinstance(snap.get("data"), dict):
            continue
        found = True
        if not merged["meta"]:
            merged["meta"] = snap.get("meta") or {}
        for a in snap.get("accounts") or []:
            if a.get("id") in seen:
                continue
            seen.add(a["id"])
            merged["accounts"].append(a)
        merged["data"].update(snap["data"])
    return merged if found else None


def account_label(acct: dict) -> str:
    """What the dashboard calls the account: its nickname, else type and masked number."""
    return str(acct.get("nickname") or f"{acct.get('type') or 'Account'} {acct.get('mask') or ''}".strip())


def load_context(data_dir: str) -> dict:
    """Everything the rules need, read fresh. Missing files are None."""
    return {
        "snapshot": load_snapshots(data_dir),
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


def capacity(acct: dict, vix: float | None, manual: bool = False) -> dict:
    """Free cash and buying power. A broker account's total is liquidation value, so
    cash is backed out of it; a manual account (hand-entered or imported) carries
    its own `cash`, already net of what secures its puts."""
    s, eq, opts = acct["summary"], acct["equities"], acct["options"]
    margin = vix_margin(vix)
    if manual:
        free = max(0.0, float(s.get("cash") or 0))
        return {
            "totalValue": s["totalValue"], "margin": margin,
            "buyingPower": s["totalValue"] * (1 + margin),
            "freeCash": free + margin * s["totalValue"],
            "putObligations": csp_collateral(opts) + spread_cash_requirement(opts),
        }
    options_net = sum((1 if o["side"] == "long" else -1) * o["mark"] * MULT * o["qty"] for o in opts)
    cash = s["totalValue"] - s["equityValue"] - s.get("cryptoValue", 0) - options_net
    money_market = sum(e["qty"] * e["price"] for e in eq if e["symbol"] in CASH_EQUIVALENTS)
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


def study_pick(row: dict) -> dict | None:
    """The put the BACKTEST's rule picks for a scan row. The dashboard's Quant
    settings can move the scan's `pick`; the trader stays on the study's rule
    (the user's call), which the bridge writes alongside as `study`."""
    p = row.get("study", row.get("pick"))
    return p if isinstance(p, dict) else None


def fit(pick: dict, sym: str, acct: dict, cap: dict, taken: float = 0.0, taken_sym: float = 0.0, stretch: bool = True) -> dict:
    """How many contracts the rules allow, plus the flags the dashboard shows.
    `taken` is collateral already handed out in this pass; `taken_sym` the part of
    it that went to this name. `stretch` allows the one-contract overshoot."""
    c = committed(sym, acct) + taken_sym
    per_cap = R["maxPerTicker"] * cap["buyingPower"]
    room_ticker = per_cap - c
    room_total = cap["buyingPower"] - committed_total(acct) - taken
    room = min(room_ticker, room_total, cap["freeCash"])
    contracts = int(room // pick["collateral"]) if room > 0 else 0
    if contracts < 1 and room_ticker > 0 and stretch:
        cap_hi = (R["maxPerTicker"] + R["tickerBand"]) * cap["buyingPower"] - c
        if cap_hi >= pick["collateral"] and min(room_total, cap["freeCash"]) >= pick["collateral"]:
            contracts = 1
    return {"contracts": contracts, "held": c > 0, "full": c >= per_cap, "cashShort": cap["freeCash"] < pick["collateral"], "committed": c, "perTickerCap": per_cap}


def _unit(x: float) -> float:
    return max(0.0, min(1.0, x))


def rank(pick: dict, vrp_ratio: float | None) -> dict:
    """0-100 score for a pick that already pays the target, with its parts (0-1)."""
    sp = pick.get("spreadPct")
    spread = _unit(1 - sp / RANK["spreadZero"]) if sp is not None else 0.5
    liquidity = 0.75 * spread + 0.25 * _unit((pick.get("oi") or 0) / RANK["oiFull"])
    cushion = _unit((RANK["deltaCap"] - abs(pick["delta"])) / (RANK["deltaCap"] - RANK["deltaBest"]))
    vrp = _unit((vrp_ratio - RANK["vrpLo"]) / (RANK["vrpHi"] - RANK["vrpLo"])) if vrp_ratio else 0.5
    w = RANK["w"]
    score = 100 * (w["liquidity"] * liquidity + w["cushion"] * cushion + w["vrp"] * vrp)
    per_round = next((n for lo, n in RANK["perRound"] if score >= lo), 1)
    return {"score": round(score, 1), "liquidity": round(liquidity, 2), "cushion": round(cushion, 2),
            "vrp": round(vrp, 2), "vrpRatio": vrp_ratio, "spreadPct": sp, "perRound": per_round}


def _rank_text(rk: dict, pick: dict) -> str:
    parts = [f"spread {rk['spreadPct']:.0f}% of mid" if rk["spreadPct"] is not None else "spread unknown",
             f"{abs(pick['delta']):.2f}Δ to reach the target",
             f"IV/RV {rk['vrpRatio']:.2f}" if rk["vrpRatio"] else "IV/RV unknown"]
    return f" Rank {round(rk['score'])} ({', '.join(parts)})."


# ---- the suggestions ---------------------------------------------------------
def _money(n: float) -> str:
    return f"${round(n):,}"


def _replacement(o: dict, picks: list, acct: dict, cap: dict) -> dict | None:
    """What to sell with the collateral a closing put frees: the same name's pick
    when it still pays, otherwise the best-ranked name with room. Sized to the
    freed collateral (a replacement, not an add)."""
    freed = o["strike"] * MULT * o["qty"]
    cap2 = {**cap, "freeCash": cap["freeCash"] + freed}
    acct2 = {**acct, "options": [x for x in acct["options"] if x is not o]}  # the closing put no longer counts
    ordered = [t for t in picks if t[1]["sym"] == o["symbol"]] + [t for t in picks if t[1]["sym"] != o["symbol"]]
    for _, row, p, rk in ordered:
        f = fit(p, row["sym"], acct2, cap2)
        if f["contracts"] < 1 or f["full"]:
            continue
        n = max(1, min(f["contracts"], int(freed // p["collateral"])))
        return {"symbol": row["sym"], "strike": p["strike"], "expiration": p["exp"], "dte": p["dte"], "qty": n,
                "price": p["mark"], "yield30": p["yield30"], "delta": p["delta"], "collateral": p["collateral"] * n,
                "score": rk["score"]}
    return None


def build(ctx: dict, account_id: str | None = None, today: date | None = None, entries: bool = True, cap: dict | None = None) -> list[dict]:
    """All suggestions, or with entries=False just the closes (what runs outside the entry window).
    `cap` overrides the capacity worked out from the snapshot (the paper account keeps its own)."""
    snap = ctx.get("snapshot")
    if not snap or not snap.get("data"):
        return []
    acct_id = account_id or next(iter(snap["data"]))
    acct = snap["data"].get(acct_id)
    if not acct:
        return []
    vix = ((ctx.get("vix") or {}).get("inputs") or {}).get("vix")
    cap = cap or capacity(acct, vix, manual=acct_id.startswith("manual-"))
    today = today or date.today()
    out: list[dict] = []

    # The scan's candidates under the STUDY's rule, in queue order: names without a
    # report inside the put's life first, then by rank() (yield, then symbol, break ties).
    scan = ctx.get("scan") or {}
    report = ctx.get("report") or {}
    scored = {r["sym"]: r for r in (report.get("screened") or report.get("board") or [])}
    picks = []
    for row in scan.get("rows") or []:
        p = study_pick(row)
        if not p:
            continue
        if _dte(p["exp"], today) < 20:
            continue  # a stale scan: the contract is no longer in the window
        er_days = row.get("erDays")
        rk = rank(p, (scored.get(row["sym"]) or {}).get("vrpRatio"))
        rk["earnings"] = er_days if er_days is not None and 0 <= er_days <= p["dte"] else None
        picks.append(((rk["earnings"] is not None, -rk["score"], -p["yield30"], row["sym"]), row, p, rk))
    picks.sort(key=lambda t: t[0])

    # 1. Close at 50% — paired with the replacement the rule would sell with the
    #    freed collateral: the same name if it still pays, else the best-ranked one.
    for o in acct["options"]:
        if o.get("kind") != "csp" or o["side"] != "short" or not o.get("entryPerShare"):
            continue
        captured = (o["entryPerShare"] - o["mark"]) / o["entryPerShare"]
        dte = _dte(o["expiration"], today)
        if captured >= R["closeAt"] and dte > 0:
            rep = _replacement(o, picks, acct, cap)
            out.append({
                "key": f"close|{o['symbol']}|{o['strike']}|{o['expiration']}",
                "kind": "close", "symbol": o["symbol"], "strike": o["strike"], "expiration": o["expiration"],
                "qty": o["qty"], "price": round(o["mark"], 2),
                "title": f"Close {o['qty']} × {o['symbol']} ${o['strike']:g} put ({o['expiration'][5:]})",
                "detail": (f"{round(captured * 100)}% of the {o['entryPerShare']:.2f} credit captured; buy back near {o['mark']:.2f} with {dte} days left. The study closes here every time."
                           + (f" Replace with: sell {rep['qty']} × {rep['symbol']} ${rep['strike']:g} put ({rep['expiration'][5:]}, {rep['dte']}d) at {rep['price']:.2f}, "
                              f"{rep['yield30']:.1f}% per 30 days, {rep['delta']:.2f}Δ{' — same name still pays' if rep['symbol'] == o['symbol'] else ''}." if rep
                              else " Nothing in the scan pays the target for the freed collateral right now.")),
                "amount": round(o["mark"] * MULT * o["qty"], 2),
                "rule": "close at 50%",
                "replacement": rep,
            })

    if not entries:
        for s in out:
            s["accountId"] = acct_id
        return out

    # 2. New CSPs from the scan: every name that qualifies, in queue order.
    # Capital goes in weighted rounds — each round a name takes 1 contract, 2 at
    # rank 65+, 3 at rank 80+ — until the cash or every
    # name's room is used. Plenty of cash: everyone fills to the cap as before;
    # short cash: the better trades get it first, still spread over several names.
    alloc: dict[str, int] = {}
    spent = 0.0
    progress = True
    while progress:
        progress = False
        for _, row, p, rk in picks:
            sym = row["sym"]
            for _ in range(rk["perRound"]):
                f = fit(p, sym, acct, {**cap, "freeCash": cap["freeCash"] - spent}, spent, alloc.get(sym, 0) * p["collateral"], stretch=sym not in alloc)
                if f["contracts"] < 1 or f["full"]:
                    break
                alloc[sym] = alloc.get(sym, 0) + 1
                spent += p["collateral"]
                progress = True
    for _, row, p, rk in picks:
        n = alloc.get(row["sym"], 0)
        if n < 1:
            continue
        f = fit(p, row["sym"], acct, cap)  # for the flags (held / adds)
        out.append({
            "key": f"csp|{row['sym']}|{p['strike']}|{p['exp']}",
            "kind": "csp", "symbol": row["sym"], "strike": p["strike"], "expiration": p["exp"],
            "qty": n, "price": p["mark"], "delta": p["delta"], "yield30": p["yield30"],
            "title": f"Sell {n} × {row['sym']} ${p['strike']:g} put ({p['exp'][5:]}, {p['dte']}d)",
            "detail": (f"{p['yield30']:.1f}% per 30 days at the {p['mark']:.2f} mid ({p['bid']:.2f}–{p['ask']:.2f}), {p['delta']:.2f}Δ, "
                       f"{p['belowSpotPct']:.1f}% below ${row['price']:.2f}. {_money(p['collateral'] * n)} collateral, {_money(p['mark'] * MULT * n)} credit."
                       + _rank_text(rk, p)
                       + (f" Earnings in {rk['earnings']}d — queued after names without a report." if rk["earnings"] is not None else "")
                       + (" Already held — this adds." if f["held"] else "")),
            "amount": round(p["mark"] * MULT * n, 2),
            "rule": "4% target",
            "rank": rk["score"],
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
