"""selftest.py — offline checks: rules, sizing, dedupe and the file the app reads.
No network: ntfy is replaced with a fake. Exit 0 = all passed."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok    " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    work = tempfile.mkdtemp(prefix="trader-selftest-")
    data = os.path.join(work, "data")
    os.makedirs(data)
    os.environ["APP_DATA_DIR"] = data
    os.environ["TRADER_STATE_DIR"] = work
    os.environ["NTFY_TOPIC"] = "selftest"
    os.environ["APP_URL"] = "http://pi:3000"
    import notify
    import suggest
    import trader

    far = (date.today() + timedelta(days=35)).isoformat()
    soon = (date.today() + timedelta(days=12)).isoformat()
    snap = {"data": {"ACC1": {
        "summary": {"totalValue": 1_000_000, "equityValue": 110_000, "cryptoValue": 0, "cash": 0},
        "equities": [
            {"symbol": "AAPL", "qty": 200, "avgCost": 180, "price": 200, "coveredCalls": [
                {"targetDte": 14, "dte": 14, "strike": 210, "delta": 0.3, "mark": 2.4, "premPct": 1.2, "annPct": 31, "oi": 100},
                {"targetDte": 21, "dte": 21, "strike": 215, "delta": 0.25, "mark": 2.5, "premPct": 1.25, "annPct": 22, "oi": 100},
                {"targetDte": 30, "dte": 30, "strike": 220, "delta": 0.2, "mark": 3.0, "premPct": 1.5, "annPct": 18, "oi": 100}]},
            {"symbol": "SWVXX", "qty": 70_000, "avgCost": 1, "price": 1},
        ],
        "options": [
            # winner: 60% captured
            {"id": "a", "kind": "csp", "symbol": "HOOD", "optionType": "put", "side": "short", "qty": 2, "strike": 90, "expiration": far, "entryPerShare": 2.0, "mark": 0.8, "delta": -0.1, "theta": 0, "iv": 0.5, "breakeven": 88},
            # not yet
            {"id": "b", "kind": "csp", "symbol": "SOFI", "optionType": "put", "side": "short", "qty": 10, "strike": 16, "expiration": far, "entryPerShare": 0.7, "mark": 0.5, "delta": -0.2, "theta": 0, "iv": 0.5, "breakeven": 15.3},
            # debit spread: must not count as collateral
            {"id": "c", "kind": "put-spread", "symbol": "SMH", "optionType": "put", "side": "short", "qty": 6, "strike": 580, "expiration": far, "entryPerShare": 26.3, "mark": 26.6, "delta": -0.3, "theta": 0, "iv": 0.3, "breakeven": 0},
            {"id": "d", "kind": "put-spread", "symbol": "SMH", "optionType": "put", "side": "long", "qty": 6, "strike": 610, "expiration": far, "entryPerShare": 39.05, "mark": 39.67, "delta": -0.4, "theta": 0, "iv": 0.3, "breakeven": 0},
        ],
        "valueHistory": []}}}
    json.dump(snap, open(os.path.join(data, "snapshot.json"), "w"))
    pick = lambda strike, dte, exp, mark, y, delta, coll: {"exp": exp, "dte": dte, "strike": strike, "bid": mark - 0.05, "ask": mark + 0.05, "mark": mark, "delta": delta, "yield30": y, "annPct": y * 12, "premium": mark * 100, "collateral": coll, "oi": 1000, "volume": 100, "spreadPct": 2.0, "iv": 0.5, "belowSpotPct": 8.0}
    scan = {"meta": {"asOf": "x", "qualifying": 3, "universe": 5, "params": {}}, "rows": [
        {"sym": "KLAC", "price": 200, "pick": pick(185, 35, far, 8.85, 4.8, 0.34, 18_500), "best": None, "reason": "ok", "erDate": None, "erDays": None, "erInWindow": False},
        {"sym": "MU", "price": 1000, "pick": pick(900, 35, far, 45.0, 5.0, 0.33, 90_000), "best": None, "reason": "ok", "erDate": soon, "erDays": 12, "erInWindow": True},
        {"sym": "FTNT", "price": 185, "pick": pick(170, 30, far, 7.0, 4.1, 0.30, 17_000), "best": None, "reason": "ok", "erDate": None, "erDays": None, "erInWindow": False},
        {"sym": "BIG", "price": 2000, "pick": pick(1900, 35, far, 80.0, 4.2, 0.3, 190_000), "best": None, "reason": "ok", "erDate": None, "erDays": None, "erInWindow": False},
    ]}
    json.dump(scan, open(os.path.join(data, "quant-scan.json"), "w"))
    json.dump({"board": [], "screened": [{"sym": "FTNT", "score": 90, "tier": "S"}, {"sym": "KLAC", "score": 70, "tier": "A"}]}, open(os.path.join(data, "am_report.json"), "w"))
    json.dump({"inputs": {"vix": 16.3}}, open(os.path.join(data, "vix.json"), "w"))

    print("capacity")
    acct = snap["data"]["ACC1"]
    cap = suggest.capacity(acct, 16.3)
    # cash = 1,000,000 − 110,000 − optionsNet(2×−80 ... ) ; collateral 18,000+16,000; debit spread holds nothing
    options_net = (-0.8 * 200) + (-0.5 * 1000) + (-26.6 * 600) + (39.67 * 600)
    expect = (1_000_000 - 110_000 - options_net) + 70_000 - (18_000 + 16_000)
    check("free cash backs the short book out once and holds nothing for a debit spread", abs(cap["freeCash"] - expect) < 1, f"{cap['freeCash']:.0f} vs {expect:.0f}")
    check("no margin allowance under VIX 20", cap["margin"] == 0)
    check("VIX 27 -> 25% allowance", suggest.vix_margin(27) == 0.25)

    print("suggestions")
    sug = suggest.build(suggest.load_context(data), None, today=date.today())
    kinds = {(s["kind"], s["symbol"]) for s in sug}
    check("HOOD put at 60% -> close", ("close", "HOOD") in kinds)
    check("SOFI put at 29% -> no close", ("close", "SOFI") not in kinds)
    check("MU skipped: earnings inside the put", ("csp", "MU") not in kinds)
    check("BIG skipped: one contract is over the per-name cap", ("csp", "BIG") not in kinds)
    csps = [s for s in sug if s["kind"] == "csp"]
    check("FTNT (Brief 90) ranks above KLAC (70) despite lower yield", [s["symbol"] for s in csps] == ["FTNT", "KLAC"], str([s["symbol"] for s in csps]))
    klac = next(s for s in csps if s["symbol"] == "KLAC")
    check("KLAC sized to 5 contracts under the $100k per-name cap", klac["qty"] == 5, str(klac["qty"]))
    cc = next((s for s in sug if s["kind"] == "cc"), None)
    # 215/21d pays 2.5/180/3 = 0.46%/wk, under the floor; 210/14d pays 0.67%/wk. 30d is outside 7–21.
    check("AAPL covered call: furthest strike paying 0.5%/wk, 7-21d", cc is not None and cc["strike"] == 210 and cc["qty"] == 2, json.dumps(cc)[:120] if cc else "none")
    check("no notes when inside the rules", not [s for s in sug if s["kind"] == "note"])

    print("pushes + the file")
    sent_bodies: list[dict] = []
    notify.push = lambda s, cfg=None, timeout=15.0: (sent_bodies.append(s) or True)  # type: ignore[assignment]
    r1 = trader.run_once(force=True, now=time.time())
    check("first pass pushes every new suggestion", r1["pushed"] == len(sug) and len(sent_bodies) == len(sug), str(r1))
    r2 = trader.run_once(force=True, now=time.time() + 60)
    check("second pass a minute later pushes nothing", r2["pushed"] == 0, str(r2))
    r3 = trader.run_once(force=True, now=time.time() + 25 * 3600)
    check("a day later, still-standing suggestions are re-sent", r3["pushed"] == len(sug), str(r3))
    doc = json.load(open(os.path.join(data, "trade-suggestions.json")))
    check("file lists them as new with pushedAt", all(s["status"] == "new" and s.get("pushedAt") for s in doc["suggestions"]))
    # the app marks one as done; the trader respects it and stops pushing it
    json.dump({klac["key"]: {"status": "done", "at": datetime.now().isoformat()}}, open(os.path.join(data, "trade-feedback.json"), "w"))
    sent_bodies.clear()
    trader.run_once(force=True, now=time.time() + 50 * 3600)
    doc = json.load(open(os.path.join(data, "trade-suggestions.json")))
    check("app's verdict wins and silences the push", next(s for s in doc["suggestions"] if s["key"] == klac["key"])["status"] == "done" and not any(b["key"] == klac["key"] for b in sent_bodies))
    # a suggestion that stops applying is marked expired, not deleted
    scan["rows"] = [r for r in scan["rows"] if r["sym"] != "FTNT"]
    json.dump(scan, open(os.path.join(data, "quant-scan.json"), "w"))
    trader.run_once(force=True, now=time.time() + 50 * 3600)
    doc = json.load(open(os.path.join(data, "trade-suggestions.json")))
    check("gone from the scan -> expired, kept in the log", next(s for s in doc["suggestions"] if s["symbol"] == "FTNT" and s["kind"] == "csp")["status"] == "expired")
    open(os.path.join(work, "paused"), "w").close()
    sent_bodies.clear()
    trader.run_once(force=True, now=time.time() + 80 * 3600)
    check("paused: nothing pushed", not sent_bodies)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        check("market_open false on a Sunday", not trader.market_open(datetime(2026, 10, 4, 15, 0, tzinfo=__import__("datetime").timezone.utc)))
        check("market_open true on a Thursday at 2pm ET", trader.market_open(datetime(2026, 10, 1, 18, 0, tzinfo=__import__("datetime").timezone.utc)))
    except Exception:  # noqa: BLE001 — no tz database here (a browser Python); CI has one
        print("  skip  market hours (no time-zone database)")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: " + "; ".join(FAILED))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
