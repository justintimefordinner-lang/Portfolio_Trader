"""selftest.py — offline checks: rules, sizing, dedupe and the file the app reads.
No network: ntfy is replaced with a fake. Exit 0 = all passed."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

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
            {"symbol": "AAPL", "qty": 200.0, "avgCost": 180, "price": 200, "coveredCalls": [
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
    # Two accounts, named the way the dashboard names them; the second is small.
    snap["accounts"] = [{"id": "ACC1", "nickname": "Trading", "mask": "1234", "type": "MARGIN", "isDefault": True},
                        {"id": "ACC2", "nickname": "Roth", "mask": "5678", "type": "IRA"}]
    snap["data"]["ACC2"] = {"summary": {"totalValue": 50_000, "equityValue": 0, "cryptoValue": 0, "cash": 50_000}, "equities": [], "valueHistory": [],
                            "options": [{"id": "r", "kind": "csp", "symbol": "HOOD", "optionType": "put", "side": "short", "qty": 1, "strike": 80, "expiration": far, "entryPerShare": 1.5, "mark": 0.4, "delta": -0.1, "theta": 0, "iv": 0.5, "breakeven": 78.5}]}
    json.dump(snap, open(os.path.join(data, "snapshot.json"), "w"))
    pick = lambda strike, dte, exp, mark, y, delta, coll: {"exp": exp, "dte": dte, "strike": strike, "bid": mark - 0.05, "ask": mark + 0.05, "mark": mark, "delta": delta, "yield30": y, "annPct": y * 12, "premium": mark * 100, "collateral": coll, "oi": 1000, "volume": 100, "spreadPct": 2.0, "iv": 0.5, "belowSpotPct": 8.0}
    scan = {"meta": {"asOf": "x", "qualifying": 3, "universe": 5, "params": {}}, "rows": [
        {"sym": "KLAC", "price": 200, "pick": pick(185, 35, far, 8.85, 4.8, 0.34, 18_500), "best": None, "reason": "ok", "erDate": None, "erDays": None, "erInWindow": False},
        {"sym": "MU", "price": 1000, "pick": pick(900, 35, far, 45.0, 5.0, 0.33, 90_000), "best": None, "reason": "ok", "erDate": soon, "erDays": 12, "erInWindow": True},
        {"sym": "FTNT", "price": 185, "pick": pick(170, 30, far, 7.0, 4.1, 0.30, 17_000), "best": None, "reason": "ok", "erDate": None, "erDays": None, "erInWindow": False},
        {"sym": "BIG", "price": 2000, "pick": pick(1900, 35, far, 80.0, 4.2, 0.3, 190_000), "best": None, "reason": "ok", "erDate": None, "erDays": None, "erInWindow": False},
    ] + [  # six more that qualify: the backtest took every name with room, so must we
        {"sym": f"X{i}", "price": 50, "pick": pick(45, 35, far, 2.0, 4.4, 0.30, 4_500), "best": None, "reason": "ok", "erDate": None, "erDays": None, "erInWindow": False}
        for i in range(6)
    ] + [  # the page's custom settings moved this pick; the trader must take the study's
        {"sym": "STU", "price": 50, "pick": pick(48, 35, far, 2.5, 5.2, 0.45, 4_800), "study": pick(44, 35, far, 1.8, 4.1, 0.28, 4_400), "best": None, "reason": "ok", "erDate": None, "erDays": None, "erInWindow": False}
    ]}
    json.dump(scan, open(os.path.join(data, "quant-scan.json"), "w"))
    json.dump({"board": [], "screened": [{"sym": "FTNT", "score": 50, "tier": "S", "vrpRatio": 1.3}, {"sym": "KLAC", "score": 90, "tier": "A", "vrpRatio": 1.0}]}, open(os.path.join(data, "am_report.json"), "w"))
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
    check("BIG skipped: one contract is over the per-name cap", ("csp", "BIG") not in kinds)
    csps = [s for s in sug if s["kind"] == "csp"]
    check("every qualifying name is suggested — no cap on the count", len(csps) == 10, str([s["symbol"] for s in csps]))
    mu = next((s for s in csps if s["symbol"] == "MU"), None)
    check("MU (earnings inside the put) is not skipped: queued last and says why", mu is not None and csps[-1]["symbol"] == "MU" and "Earnings in 12d" in mu["detail"], str([s["symbol"] for s in csps]))

    print("ranking")
    base = pick(170, 30, far, 7.0, 4.1, 0.30, 17_000)
    rk = suggest.rank(base, 1.3)
    check("rank: 2% spread, deep OI, 0.30Δ, IV/RV 1.3 -> 70, two contracts a round", rk["score"] == 70.0 and rk["perRound"] == 2, str(rk))
    check("rank: a 20% spread scores lower than a 2% one", suggest.rank({**base, "spreadPct": 20.0}, 1.3)["score"] < rk["score"])
    check("rank: reaching 4% at 0.15Δ beats reaching it at 0.30Δ", suggest.rank({**base, "delta": 0.15}, 1.3)["score"] > rk["score"])
    check("rank: unknown IV/RV counts as the middle", suggest.rank(base, None)["vrp"] == 0.5)
    stu = next((s for s in csps if s["symbol"] == "STU"), None)
    check("the trader takes the study's pick, not the page's custom one", stu is not None and stu["strike"] == 44, str(stu and (stu["strike"], stu["qty"])))
    hood_close = next(s for s in sug if s["kind"] == "close" and s["symbol"] == "HOOD")
    rep = hood_close.get("replacement")
    check("a close is paired with the best-ranked replacement, sized to the freed collateral", rep is not None and rep["symbol"] == "FTNT" and rep["qty"] == 1 and "Replace with: sell 1 × FTNT $170 put" in hood_close["detail"], str(rep))
    order = [s["symbol"] for s in csps]
    check("queue follows the rank, not the Brief score: FTNT (70) first, KLAC (0.34Δ, IV/RV 1.0) after the X names", order[0] == "FTNT" and order.index("KLAC") > order.index("X5"), str(order))
    ftnt = next(s for s in csps if s["symbol"] == "FTNT")
    check("the detail explains the rank", "Rank 70 (spread 2% of mid, 0.30Δ to reach the target, IV/RV 1.30)" in ftnt["detail"] and ftnt["rank"] == 70.0, ftnt["detail"])
    tight = {s["symbol"]: s["qty"] for s in suggest.build(suggest.load_context(data), None, today=date.today(), cap={**cap, "freeCash": 20_000}) if s["kind"] == "csp"}
    check("$20k: room for one put — the best-ranked name (FTNT) gets it", tight == {"FTNT": 1}, str(tight))
    rr = {s["symbol"]: s["qty"] for s in suggest.build(suggest.load_context(data), None, today=date.today(), cap={**cap, "freeCash": 60_000}) if s["kind"] == "csp"}
    check("$60k: FTNT (rank 70) takes two in its round, then one each down the queue", rr == {"FTNT": 2, "STU": 1, "X0": 1, "X1": 1, "X2": 1, "X3": 1}, str(rr))
    closes_only = suggest.build(suggest.load_context(data), None, today=date.today(), entries=False)
    check("outside the window only closes are built", {s["kind"] for s in closes_only} == {"close"}, str({s["kind"] for s in closes_only}))
    klac = next(s for s in csps if s["symbol"] == "KLAC")
    check("KLAC sized to 5 contracts under the $100k per-name cap", klac["qty"] == 5, str(klac["qty"]))
    cc = next((s for s in sug if s["kind"] == "cc"), None)
    # 215/21d pays 2.5/180/3 = 0.46%/wk, under the floor; 210/14d pays 0.67%/wk. 30d is outside 7–21.
    check("AAPL covered call: furthest strike paying 0.5%/wk, 7-21d", cc is not None and cc["strike"] == 210 and cc["qty"] == 2, json.dumps(cc)[:120] if cc else "none")
    leaps = [s for s in sug if s["key"].startswith("note|leaps|")]
    check("AAPL 200 shares, no long call: a note to buy 2 ~0.75Δ ~450-day LEAPS", [s["symbol"] for s in leaps] == ["AAPL"] and leaps[0]["title"].startswith("Buy 2 × AAPL ~0.75Δ LEAPS"), str([s["title"] for s in leaps]))
    check("no other notes when inside the rules", not [s for s in sug if s["kind"] == "note" and not s["key"].startswith("note|leaps|")])

    print("sizing settings (shared with the Quant pages)")
    sz0 = suggest.sizing(suggest.load_context(data), "ACC2")
    check("no settings file: the study's sizing", sz0 == {"maxPerTicker": 0.10, "tickerBand": 0.05, "vixMargin": True, "vixCash": False, "extraMargin": 0.0}, str(sz0))
    acct2 = snap["data"]["ACC2"]
    base2 = suggest.capacity(acct2, None)
    more2 = suggest.capacity(acct2, None, extra=20_000)
    check("extra margin adds to buying power and free cash alike", more2["buyingPower"] - base2["buyingPower"] == 20_000 and more2["freeCash"] - base2["freeCash"] == 20_000, f"{base2} -> {more2}")
    json.dump({"maxPerTicker": 0.2, "vixMargin": False, "vixCash": True, "extraMargin": {"ACC2": 25_000, "ACC1": "junk"}}, open(os.path.join(data, "quant-settings.json"), "w"))
    ctx_s = suggest.load_context(data)
    sz2 = suggest.sizing(ctx_s, "ACC2")
    check("settings file read: 20% per name, VIX margin off, cash reserve on, $25k extra for ACC2 only", sz2["maxPerTicker"] == 0.2 and not sz2["vixMargin"] and sz2["vixCash"] and sz2["extraMargin"] == 25_000 and suggest.sizing(ctx_s, "ACC1")["extraMargin"] == 0.0, str(sz2))
    check("VIX cash reserve: band midpoint (16.3 -> 22.5%)", suggest.vix_reserve(16.3) == 0.225)
    # ACC2: $50k account. 20% cap with $25k extra = $15k per name; reserve 22.5% of $50k held back.
    sug_s = suggest.build(ctx_s, "ACC2", today=date.today())
    ftnt2 = next((s for s in sug_s if s["kind"] == "csp" and s["symbol"] == "FTNT"), None)
    check("ACC2 with extra margin and a 20% cap now fits FTNT's $17k put (it couldn't at $5k a name)", ftnt2 is not None and ftnt2["qty"] == 1, str([(s["symbol"], s["qty"]) for s in sug_s if s["kind"] == "csp"]))
    os.remove(os.path.join(data, "quant-settings.json"))

    print("pushes + the file")
    sent_bodies: list[dict] = []
    notify.push = lambda s, cfg=None, timeout=15.0: (sent_bodies.append(s) or True)  # type: ignore[assignment]
    mkt = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)  # a Thursday, 14:00 ET: the paper book only trades in market hours
    sug2 = suggest.build(suggest.load_context(data), "ACC2", today=date.today())
    check("second account: its own close, and one-lot puts under its smaller cap", len([s for s in sug2 if s["kind"] == "csp"]) == 7 and ("close", "HOOD") in {(s["kind"], s["symbol"]) for s in sug2}, str([(s["kind"], s["symbol"], s.get("qty")) for s in sug2]))
    total = len(sug) + len(sug2)
    json.dump({"csp|KLAC|185|" + far: {"at": time.time(), "price": 8.85}}, open(trader.SENT_FILE, "w"))  # a receipt from before accounts were named
    r1 = trader.run_once(force=True, now=time.time(), entries=True, when=mkt)
    check("first pass pushes every new suggestion for every account (one old receipt migrated)", r1["pushed"] == total - 1 and r1["accounts"] == 2, f"{r1} vs {total}")
    check("every push names its account; keys carry the account", all(b.get("account") in ("Trading", "Roth") for b in sent_bodies) and all(b["key"].startswith(("ACC1|", "ACC2|")) for b in sent_bodies), str([(b.get("account"), b["key"]) for b in sent_bodies][:3]))
    r2 = trader.run_once(force=True, now=time.time() + 60, entries=True, when=mkt)
    check("second pass a minute later pushes nothing", r2["pushed"] == 0, str(r2))
    # 10:00 ET, before the window: closes only, and the morning's puts are NOT expired by their absence
    before = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    r2b = trader.run_once(force=True, now=time.time() + 120, when=before)
    doc = json.load(open(os.path.join(data, "trade-suggestions.json")))
    check("before the window: closes only, puts from the window still stand", r2b["entries"] is False and all(s["status"] == "new" for s in doc["suggestions"] if s["kind"] == "csp"), str(r2b))
    # 11:30 ET, the 11:00 slot, with a scan that is not fresh: wait, and ask the bridge for one
    trader.INBOX_DIR = os.path.join(work, "inbox")
    os.makedirs(trader.INBOX_DIR)
    inwin = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)
    r2c = trader.run_once(force=True, now=time.time() + 180, when=inwin)
    check("in a slot with a stale scan: waiting, scan requested", r2c.get("waiting") == "scan" and os.path.exists(os.path.join(trader.INBOX_DIR, "quant_scan")), str(r2c))
    scan["meta"]["asOf"] = "2026-10-01T15:05:00+00:00"
    json.dump(scan, open(os.path.join(data, "quant-scan.json"), "w"))
    r2d = trader.run_once(force=True, now=time.time() + 240, when=inwin)
    check("in a slot with a fresh scan: entries built", r2d["entries"] is True, str(r2d))
    r2e = trader.run_once(force=True, now=time.time() + 300, when=datetime(2026, 10, 1, 15, 40, tzinfo=timezone.utc))
    check("later in the same slot: closes only (a slot is served once)", r2e["entries"] is False and not r2e.get("waiting"), str(r2e))
    r2f = trader.run_once(force=True, now=time.time() + 360, when=datetime(2026, 10, 1, 16, 5, tzinfo=timezone.utc))
    check("the 12:00 slot wants a scan of its own: waiting again", r2f.get("waiting") == "scan", str(r2f))
    r3 = trader.run_once(force=True, now=time.time() + 25 * 3600, entries=True, when=mkt)
    check("a day later, still-standing suggestions are re-sent for every account", r3["pushed"] == total, f"{r3} vs {total}")
    open(os.path.join(data, "trader-run"), "w").close()
    sent_bodies.clear()
    check("Run now: marker consumed, full pass", trader.run_requested() and not os.path.exists(os.path.join(data, "trader-run")) and json.load(open(os.path.join(data, "trade-suggestions.json")))["meta"]["lastPass"] == "run now")
    doc = json.load(open(os.path.join(data, "trade-suggestions.json")))
    check("file lists them as new with pushedAt", all(s["status"] == "new" and s.get("pushedAt") for s in doc["suggestions"]))
    acc1_puts = sorted((s for s in doc["suggestions"] if s["kind"] == "csp" and s["key"].startswith("ACC1|")), key=lambda s: s["seq"])
    check("each row carries its build position; ACC1's puts by seq are the ranked queue (FTNT first, MU last)", acc1_puts and acc1_puts[0]["symbol"] == "FTNT" and acc1_puts[-1]["symbol"] == "MU", str([(s["symbol"], s.get("seq")) for s in acc1_puts]))
    # the app marks one as done; the trader respects it and stops pushing it
    json.dump({klac["key"]: {"status": "done", "at": datetime.now().isoformat()}}, open(os.path.join(data, "trade-feedback.json"), "w"))
    sent_bodies.clear()
    trader.run_once(force=True, now=time.time() + 50 * 3600, entries=True, when=mkt)
    doc = json.load(open(os.path.join(data, "trade-suggestions.json")))
    kkey = "ACC1|" + klac["key"]  # the verdict was written with the old key; it is migrated too
    check("app's verdict wins and silences the push", next(s for s in doc["suggestions"] if s["key"] == kkey)["status"] == "done" and not any(b["key"] == kkey for b in sent_bodies))
    # a suggestion that stops applying is marked expired, not deleted
    scan["rows"] = [r for r in scan["rows"] if r["sym"] != "FTNT"]
    json.dump(scan, open(os.path.join(data, "quant-scan.json"), "w"))
    trader.run_once(force=True, now=time.time() + 50 * 3600, entries=True, when=mkt)
    doc = json.load(open(os.path.join(data, "trade-suggestions.json")))
    check("gone from the scan -> expired, kept in the log", next(s for s in doc["suggestions"] if s["symbol"] == "FTNT" and s["kind"] == "csp")["status"] == "expired")
    open(os.path.join(work, "paused"), "w").close()
    sent_bodies.clear()
    trader.run_once(force=True, now=time.time() + 80 * 3600, entries=True, when=mkt)
    check("paused: nothing pushed", not sent_bodies)

    print("paper account")
    import paper
    paper.ENABLED = True
    pday = date(2026, 10, 1)
    paper.run(suggest.load_context(data), data, entries=True, today=pday, vix=16.3)
    mdoc = json.load(open(os.path.join(data, "manual_positions.json")))
    pacct = next(a for a in mdoc["accounts"] if a["id"] == paper.PAPER_ID)
    puts = [p for p in pacct["positions"] if p["type"] == "option" and p["optionType"] == "put"]
    # (the passes above already ran the paper book, so FTNT was sold before it left the scan)
    check("Auto Trader created with $400k and every qualifying put sold", pacct["label"] == "Auto Trader" and len(puts) == 9, f"{len(puts)} puts: {[p['symbol'] for p in puts]}")
    coll = sum(p["strike"] * 100 * p["qty"] for p in puts)
    check("cash dropped by the collateral (manual model: a put is collateral + P/L)", abs(pacct["cash"] - (400_000 - coll)) < 1, f"{pacct['cash']:.0f} vs {400_000 - coll:.0f}")
    paper.run(suggest.load_context(data), data, entries=True, today=pday, vix=16.3)
    pacct = next(a for a in json.load(open(os.path.join(data, "manual_positions.json")))["accounts"] if a["id"] == paper.PAPER_ID)
    check("a second entries pass the same day adds nothing (bridge view lagging)", len(pacct["positions"]) == 9, str(len(pacct["positions"])))
    # Expiry and a 50% close, driven by the bridge's priced view of the account.
    klac = next(p for p in pacct["positions"] if p["symbol"] == "KLAC")
    pacct["positions"] += [
        {"id": "exp1", "type": "option", "symbol": "HOOD", "optionType": "put", "side": "short", "qty": 1, "strike": 90, "expiration": "2026-09-25", "premium": 2.0, "openedAt": "2026-09-01"},
        {"id": "exp2", "type": "option", "symbol": "SOFI", "optionType": "put", "side": "short", "qty": 2, "strike": 16, "expiration": "2026-09-25", "premium": 0.7, "openedAt": "2026-09-01"},
    ]
    paper.save_account(data, pacct)
    cash0 = pacct["cash"]
    opt = lambda pid, sym, strike, exp, entry, mark, under, qty: {"id": f"{paper.PAPER_ID}:{pid}", "kind": "csp", "symbol": sym, "optionType": "put", "side": "short", "qty": qty, "strike": strike, "expiration": exp, "entryPerShare": entry, "mark": mark, "delta": -0.2, "theta": 0, "iv": 0.5, "breakeven": 0, "underlyingPrice": under}
    view = {"data": {paper.PAPER_ID: {"summary": {"totalValue": 401_000, "equityValue": 0, "cryptoValue": 0, "cash": cash0}, "equities": [], "valueHistory": [], "options": [
        opt("exp1", "HOOD", 90, "2026-09-25", 2.0, 5.0, 85.0, 1),
        opt("exp2", "SOFI", 16, "2026-09-25", 0.7, 0.0, 17.0, 2),
        opt(klac["id"], "KLAC", 185, klac["expiration"], 8.85, 4.0, 200.0, klac["qty"]),
    ]}}}
    os.makedirs(os.path.join(data, "manual"), exist_ok=True)
    json.dump(view, open(os.path.join(data, "manual", "snapshot.json"), "w"))
    pm = paper.run(suggest.load_context(data), data, entries=False, today=pday, vix=16.3)
    pacct = next(a for a in json.load(open(os.path.join(data, "manual_positions.json")))["accounts"] if a["id"] == paper.PAPER_ID)
    hood_shares = next((p for p in pacct["positions"] if p["type"] == "stock" and p["symbol"] == "HOOD"), None)
    check("ITM put assigned: 100 shares at strike − premium", hood_shares is not None and hood_shares["qty"] == 100 and hood_shares["avgCost"] == 88.0, str(hood_shares))
    check("OTM put expired, KLAC closed at 50%: both gone", not any(p.get("id") in ("exp1", "exp2", klac["id"]) for p in pacct["positions"]))
    expect_cash = cash0 + 2.0 * 100 + (16 + 0.7) * 100 * 2 + 185 * 100 * klac["qty"] + (8.85 - 4.0) * 100 * klac["qty"]
    check("cash: premium kept on assignment, collateral + premium on expiry, collateral + gain on the close", abs(pacct["cash"] - expect_cash) < 1, f"{pacct['cash']:.0f} vs {expect_cash:.0f}")
    closed = json.load(open(os.path.join(data, "manual", "csp-closed.json")))["closed"]
    check("three CSP round-trips booked to the paper account", len(closed) == 3 and all(r["accountId"] == paper.PAPER_ID for r in closed) and {r["outcome"] for r in closed} == {"assigned", "expired", "closed_profit"}, str([(r["symbol"], r["outcome"], r["realizedPnl"]) for r in closed]))
    plog = json.load(open(os.path.join(data, "trader-paper.json")))
    check("trade log written with a summary", plog["meta"]["label"] == "Auto Trader" and len(plog["trades"]) == 9 + 3 and pm["shareLots"] == 1 and pm["puts"] == 8, str(plog["meta"]))
    before = json.load(open(os.path.join(data, "manual_positions.json")))
    pm2 = paper.run(suggest.load_context(data), data, entries=True, today=pday, vix=16.3, market_open=False)
    check("market closed: the paper book is untouched, pass noted as skipped", pm2.get("skipped") and json.load(open(os.path.join(data, "manual_positions.json"))) == before, str(pm2.get("skipped")))
    # Undo everything booked in the last minute: the sales come off and the cash comes back; settlements stay.
    cash_before_undo = pacct["cash"]
    since = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(timespec="seconds")
    lines = paper.undo(data, since)
    pacct = next(a for a in json.load(open(os.path.join(data, "manual_positions.json")))["accounts"] if a["id"] == paper.PAPER_ID)
    puts_left = [p for p in pacct["positions"] if p["type"] == "option" and p["optionType"] == "put"]
    check("undo: the window's put sales are unwound, collateral returned, settlements kept", not puts_left and pacct["cash"] > cash_before_undo and any(l.startswith("kept") for l in lines) and sum(1 for l in lines if l.startswith("undid")) >= 8, f"{len(puts_left)} puts left; {lines[:3]}")

    print("notifications")
    check("ASCII header passes through", notify.header("Trading · Sell 2 x KLAC") == "Trading · Sell 2 x KLAC" or notify.header("Trading - Sell 2 x KLAC") == "Trading - Sell 2 x KLAC")
    check("a masked account label (bullets) is RFC 2047 encoded, not sent raw", notify.header("margin ••••9362 · Sell 1 × AMKR $46 put").startswith("=?UTF-8?B?") and notify.header("margin ••••9362").isascii())
    check("every header the push builds is ASCII", all(v.isascii() for v in {"Title": notify.header("margin ••••9362 · Close 2 × HOOD $90 put"), "Click": "http://pi:3000/trader"}.values()))

    print("clock (Eastern, no tz database needed)")
    utc = timezone.utc
    check("market_open false on a Sunday", not trader.market_open(datetime(2026, 10, 4, 15, 0, tzinfo=utc)))
    check("market_open true on a Thursday at 2pm ET", trader.market_open(datetime(2026, 10, 1, 18, 0, tzinfo=utc)))
    check("active: not at 10:30 ET (closes wait for the 11:00 run, like the backtest)", not trader.active(datetime(2026, 10, 1, 14, 30, tzinfo=utc)))
    check("active: 11:00 ET on", trader.active(datetime(2026, 10, 1, 15, 0, tzinfo=utc)) and trader.active(datetime(2026, 10, 1, 19, 45, tzinfo=utc)))
    check("active: not after the close", not trader.active(datetime(2026, 10, 1, 20, 5, tzinfo=utc)))
    check("an unforced pass at 10:30 ET is skipped", trader.run_once(when=datetime(2026, 10, 1, 14, 30, tzinfo=utc)).get("skipped", "").startswith("before 11:00"))
    slot = lambda *a: (trader.current_slot(datetime(*a, tzinfo=utc)) or (None,))[0]
    check("slot: 11:30 EDT is the 11:00 slot", slot(2026, 10, 1, 15, 30) == "2026-10-01T11")
    check("slot: 10:30 EDT is none", slot(2026, 10, 1, 14, 30) is None)
    check("slot: 15:30 EDT is the 15:00 slot (the last)", slot(2026, 10, 1, 19, 30) == "2026-10-01T15" and trader.last_slot_of_day("2026-10-01T15") and not trader.last_slot_of_day("2026-10-01T11"))
    check("slot: 15:55 EDT is none (50 minutes per slot)", slot(2026, 10, 1, 19, 55) is None)
    check("slot: 16:30 EDT is none", slot(2026, 10, 1, 20, 30) is None)
    check("slot: 11:30 EST in December", slot(2026, 12, 3, 16, 30) == "2026-12-03T11")
    check("slot: none on Saturday", slot(2026, 10, 3, 15, 30) is None)
    start = trader.current_slot(datetime(2026, 10, 1, 15, 30, tzinfo=utc))[1]
    check("scan 25 min before the slot start is fresh", trader.scan_fresh({"meta": {"asOf": "2026-10-01T14:35:00+00:00"}}, start))
    check("scan 40 min before the slot start is stale", not trader.scan_fresh({"meta": {"asOf": "2026-10-01T14:20:00+00:00"}}, start))
    check("no scan is stale", not trader.scan_fresh(None, start))
    check("manual account capacity: cash is free cash, total is buying power", suggest.capacity({"summary": {"totalValue": 120_000, "equityValue": 20_000, "cash": 60_000}, "equities": [], "options": []}, 16.3, manual=True)["freeCash"] == 60_000)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: " + "; ".join(FAILED))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
