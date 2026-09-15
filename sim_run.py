#!/usr/bin/env python3
"""
The Overlap — Offline Simulation
════════════════════════════════
Drives the engine through a scripted session with no network, no broker and
no GUI, and asserts that every rule in strategy note v1.2 behaves.

    python sim_run.py

Balfund Trading Pvt Ltd | www.balfund.com
"""

import logging
import sys
from datetime import time as dtime

logging.basicConfig(level=logging.CRITICAL)

from candles import Candle                    # noqa: E402
from config import OverlapConfig              # noqa: E402
from strategy import CondStatus, Leg, OverlapEngine, Phase  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def mk(o, h, l, c, bucket=0):
    cd = Candle(bucket, o)
    cd.open, cd.high, cd.low, cd.close = float(o), float(h), float(l), float(c)
    return cd


# ═══════════════════════════════════════════════════════════════════════════

def build_engine(**over):
    cfg = OverlapConfig()
    cfg.indices = ["NIFTY"]
    # The engine refuses to evaluate signals past the last entry time, so the
    # suite would quietly stop testing anything if it ran after 15:15 IST.
    # Push the session boundaries out; tests that care set them back.
    cfg.last_entry_time = dtime(23, 59, 0)
    cfg.square_off_time = dtime(23, 59, 30)
    cfg.timeframe_minutes = 3
    cfg.trade_mode = "paper"
    for k, v in over.items():
        setattr(cfg, k, v)
    eng = OverlapEngine(cfg, gui_callback=lambda e, d: None)
    eng.expiry = "2026-08-27"
    eng.atm_strike = 24650.0
    eng.ref_spot = 24637.0
    seg = "NSE_FNO"
    strikes = [24250, 24300, 24350, 24400, 24450, 24550, 24600, 24650, 24700, 24750]
    eng.ce_legs = [Leg(f"C{s}", float(s), "CE", f"NIFTY {s} CE", seg) for s in strikes]
    eng.pe_legs = [Leg(f"P{s}", float(s), "PE", f"NIFTY {s} PE", seg) for s in strikes]
    eng.legs = {l.sec_id: l for l in eng.ce_legs + eng.pe_legs}
    for l in eng.legs.values():
        eng.book.add(l.sec_id, l.label)
    eng.ladder_built = True
    return eng


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE OVERLAP TEST
# ═══════════════════════════════════════════════════════════════════════════

def test_overlap_rules():
    print("\n1. The overlap — three things must all be true")
    eng = build_engine()

    # (a) the worked-session pair from the note
    ce = mk(215.20, 215.60, 210.80, 211.40)
    pe = mk(209.80, 213.20, 208.90, 212.10)
    r = eng._pair_overlaps(ce, pe)
    check("worked-session pair matches", r is not None and r[0] == "CE",
          f"gap {r[1]:.2f}" if r else "no match")

    # (b) the perfect mirror
    ce = mk(218.60, 219.00, 211.00, 211.40)
    pe = mk(211.40, 219.00, 211.00, 218.60)
    check("perfect mirror matches", eng._pair_overlaps(ce, pe) is not None)

    # (c) exactly five paise
    ce = mk(121.00, 121.20, 119.40, 119.60)
    pe = mk(118.90, 119.80, 118.50, 119.65)
    check("exactly 5 paise matches", eng._pair_overlaps(ce, pe) is not None)

    # (d) four paise — under the floor
    ce = mk(121.00, 121.20, 119.40, 119.60)
    pe = mk(118.90, 119.80, 118.50, 119.64)
    check("4 paise rejected", eng._pair_overlaps(ce, pe) is None)

    # (e) far apart — closes satisfy the gap but ranges never touch
    ce = mk(33.00, 33.40, 30.90, 31.20)
    pe = mk(240.00, 249.00, 239.00, 248.60)
    check("far-apart pair rejected (no overlap)",
          eng._pair_overlaps(ce, pe) is None,
          "this is the case the overlap rule exists for")

    # (f) same colour
    ce = mk(210.00, 216.00, 209.00, 214.00)
    pe = mk(209.00, 215.00, 208.00, 213.00)
    check("two greens rejected", eng._pair_overlaps(ce, pe) is None)

    # (g) doji never pairs
    ce = mk(212.00, 214.00, 210.00, 212.00)
    pe = mk(209.00, 215.00, 208.00, 213.00)
    check("doji rejected", eng._pair_overlaps(ce, pe) is None)

    # (h) PE red, CE green — the mirror direction
    ce = mk(209.80, 213.20, 208.90, 212.10)
    pe = mk(215.20, 215.60, 210.80, 211.40)
    r = eng._pair_overlaps(ce, pe)
    check("PE-red direction matches", r is not None and r[0] == "PE")

    # ── bodies, not wicks (v1.6) ──
    # wicks cross, bodies stay apart -> no longer a merge
    ce = mk(120.00, 128.00, 119.00, 121.00)   # green body 120.00-121.00
    pe = mk(118.00, 122.00, 110.00, 111.00)   # red   body 111.00-118.00
    check("wicks crossing but bodies apart is rejected",
          eng._pair_overlaps(ce, pe) is None,
          "bodies 2.00 apart, wicks overlapped by 4.00")

    # bodies overlap by exactly the minimum
    ce = mk(100.00, 130.00, 90.00, 110.05)    # green body 100.00-110.05
    pe = mk(115.00, 140.00, 80.00, 110.00)    # red   body 110.00-115.00
    r = eng._pair_overlaps(ce, pe)
    check("bodies overlapping by exactly 5 paise accepted",
          r is not None and abs(r[1] - 0.05) < 1e-9,
          f"{r[1]:.4f}" if r else "no match")

    # four paise of body overlap -> under the floor
    ce = mk(100.00, 130.00, 90.00, 110.04)
    pe = mk(115.00, 140.00, 80.00, 110.00)
    check("4 paise of body overlap rejected", eng._pair_overlaps(ce, pe) is None)

    # the returned number is the body overlap, not the close gap
    ce = mk(72.90, 76.40, 71.50, 76.15)       # 31/08 call
    pe = mk(73.45, 75.90, 66.70, 69.05)       # 31/08 put
    r = eng._pair_overlaps(ce, pe)
    check("31/08 pair still merges", r is not None)
    if r:
        check("reported overlap is the body overlap 0.55",
              abs(r[1] - 0.55) < 0.001,
              f"got {r[1]:.2f}; the old close-gap number was 7.10")

    # the close-gap rule is implied, never needs testing separately
    for name, (co, ch, cl, cc), (po, ph, pl, pc) in [
        ("25/08", (15.60, 20.10, 15.50, 20.00), (23.80, 23.90, 19.05, 19.05)),
        ("26/08", (102.70, 109.15, 101.85, 108.70), (109.30, 110.50, 103.40, 103.75)),
        ("01/09", (34.00, 41.60, 33.95, 40.65), (43.40, 43.45, 34.40, 34.60)),
    ]:
        a_, b_ = mk(co, ch, cl, cc), mk(po, ph, pl, pc)
        r = eng._pair_overlaps(a_, b_)
        ok = r is not None
        if ok:
            red, green = (a_, b_) if a_.is_red else (b_, a_)
            ok = green.close - red.close >= r[1] - 1e-9
        check(f"{name} merges and the red close is below the green close", ok)

    # (i) overlap switched off lets the far pair through
    eng2 = build_engine(require_overlap=False)
    ce = mk(33.00, 33.40, 30.90, 31.20)
    pe = mk(240.00, 249.00, 239.00, 248.60)
    check("far pair passes when overlap is switched off",
          eng2._pair_overlaps(ce, pe) is not None)


# ═══════════════════════════════════════════════════════════════════════════
# 2. THE SIGNAL TESTS
# ═══════════════════════════════════════════════════════════════════════════

def test_signal_rules():
    print("\n2. The signal — collapse and break")
    eng = build_engine()

    # collapse: wholly below 208.90, body 81%
    c = mk(207.10, 207.40, 202.60, 203.20)
    ok, why = eng._is_collapse(c, 208.90)
    check("collapse accepted", ok, why)

    # wick crosses the line, body still below — now ACCEPTED (v1.3)
    c = mk(207.10, 209.00, 202.60, 203.20)
    ok, why = eng._is_collapse(c, 208.90)
    check("wick crossing the line is ignored", ok, why)

    # the 26/08 case: body below, wick touching the line exactly
    c = mk(101.00, 101.85, 96.00, 97.00)
    ok, why = eng._is_collapse(c, 101.85)
    check("body below, wick exactly on the line, accepted", ok, why)

    # open above the line -> body is not below it
    c = mk(209.50, 210.00, 202.60, 203.20)
    ok, why = eng._is_collapse(c, 208.90)
    check("open above the line rejected", not ok, why)

    # close above the line
    c = mk(207.00, 210.00, 206.00, 209.50)
    ok, _ = eng._is_collapse(c, 208.90)
    check("close above the line rejected", not ok)

    # body too small
    c = mk(206.00, 207.40, 200.00, 205.00)
    ok, why = eng._is_collapse(c, 208.90)
    check("thin body rejected", not ok, why)

    # green candle
    c = mk(203.00, 207.40, 202.60, 206.00)
    ok, _ = eng._is_collapse(c, 208.90)
    check("green candle rejected as collapse", not ok)

    # break: close above the line, no buffer
    c = mk(214.20, 219.00, 213.80, 218.60)
    ok, why = eng._is_breakout(c, 215.60)
    check("break accepted on close above the line", ok, why)

    # one paisa above the line still counts — the buffer is not part of the break
    c = mk(214.20, 216.00, 213.80, 215.61)
    ok, _ = eng._is_breakout(c, 215.60)
    check("one paisa above the line counts", ok,
          "buffer belongs to the retest only")

    # close exactly on the line
    c = mk(214.20, 216.00, 213.80, 215.60)
    ok, _ = eng._is_breakout(c, 215.60)
    check("close exactly on the line rejected", not ok)

    # red candle
    c = mk(219.00, 220.00, 216.00, 217.00)
    ok, _ = eng._is_breakout(c, 215.60)
    check("red candle rejected as break", not ok)


# ═══════════════════════════════════════════════════════════════════════════
# 3. FULL SESSION — trade one from the note
# ═══════════════════════════════════════════════════════════════════════════

def feed_bar(eng, bucket, prices):
    """prices: {sec_id: (o,h,l,c)}"""
    for sec_id, ohlc in prices.items():
        s = eng.book.get(sec_id)
        s.bars[bucket] = mk(*ohlc, bucket=bucket)
        s.last_ltp = ohlc[3]
    eng._on_bar_close(bucket)


def test_full_session():
    print("\n3. A full session — the worked example from the note")
    eng = build_engine(pair_mode="any")
    B = eng.cfg.interval_seconds
    t = 1_800_000_000 - (1_800_000_000 % B)

    flat = (200.0, 201.0, 199.0, 200.5)

    # ── bar 1: nothing overlapping ──
    feed_bar(eng, t, {
        "C24600": (240.0, 242.0, 238.0, 239.0),
        "P24700": (180.0, 182.0, 179.0, 181.5),
    })
    check("no frame before the overlap", eng.frame is None)

    # ── bar 2: the overlap ──
    t += B
    feed_bar(eng, t, {
        "C24600": (215.20, 215.60, 210.80, 211.40),
        "P24700": (209.80, 213.20, 208.90, 212.10),
    })
    fr = eng.frame
    ok = fr is not None
    check("overlap found", ok)
    if not ok:
        return
    check("pair is 24600 CE / 24700 PE",
          fr.ce.strike == 24600 and fr.pe.strike == 24700)
    check("CE chart borrows the PE candle",
          abs(fr.ce_borrowed_high - 213.20) < 0.001
          and abs(fr.ce_borrowed_low - 208.90) < 0.001)
    check("PE chart borrows the CE candle",
          abs(fr.pe_borrowed_high - 215.60) < 0.001
          and abs(fr.pe_borrowed_low - 210.80) < 0.001)

    # ── bar 3: CE collapses, PE breaks out ──
    t += B
    feed_bar(eng, t, {
        "C24600": (207.10, 207.40, 202.60, 203.20),
        "P24700": (214.20, 219.00, 213.80, 218.60),
    })
    sig = eng.signal
    check("signal armed", sig is not None)
    if sig is None:
        return
    check("we are buying the PE", sig.buy_side == "PE")
    check("collapse side is the CE", sig.collapse_side == "CE")
    check("breakout level is the PE's borrowed high 215.60",
          abs(sig.breakout_level - 215.60) < 0.001)
    check("band is 211.60 to 219.60",
          abs(sig.band_low - 211.60) < 0.001 and abs(sig.band_high - 219.60) < 0.001)
    check("phase is ARMED", eng.phase == Phase.ARMED)

    # ── ticks: runs away, then retests ──
    eng._on_trade_tick("P24700", 226.00)
    check("no entry above the band", eng.trade is None)

    eng._on_trade_tick("P24700", 217.40)
    tr = eng.trade
    check("entry taken inside the band", tr is not None)
    if tr is None:
        return
    check("entry at 217.40", abs(tr.entry_price - 217.40) < 0.001)
    check("target is 15% up (250.01)", abs(tr.target - 250.01) < 0.02,
          f"target {tr.target:.2f}")
    check("phase is IN TRADE", eng.phase == Phase.IN_TRADE)

    # ── target ──
    eng._on_trade_tick("P24700", 250.10)
    check("target closed the trade", eng.trade is None)
    check("one trade recorded", eng.trade_count == 1)
    if eng.closed_trades:
        c0 = eng.closed_trades[0]
        check("exit reason is TARGET", c0.exit_reason == "TARGET")
        check("P&L is positive", c0.pnl > 0, f"Rs.{c0.pnl:,.0f}")
    check("frame survives for the second trade", eng.frame is not None)
    check("back to watching", eng.phase == Phase.FRAMED)


# ═══════════════════════════════════════════════════════════════════════════
# 4. THE STRUCTURAL STOP
# ═══════════════════════════════════════════════════════════════════════════

def test_structural_stop():
    print("\n4. The stop on the other chart")
    eng = build_engine(pair_mode="any")
    B = eng.cfg.interval_seconds
    t = 1_800_000_000 - (1_800_000_000 % B)

    feed_bar(eng, t, {"C24600": (215.20, 215.60, 210.80, 211.40),
                      "P24700": (209.80, 213.20, 208.90, 212.10)})
    t += B
    feed_bar(eng, t, {"C24600": (207.10, 207.40, 202.60, 203.20),
                      "P24700": (214.20, 219.00, 213.80, 218.60)})
    eng._on_trade_tick("P24700", 217.40)
    check("in a trade", eng.trade is not None)

    # Two-sided: the CE must close above 213.20 AND the PE we hold must close
    # below its own borrowed low of 210.80, on the same candle.
    t += B
    eng.book.get("P24700").last_ltp = 205.00
    feed_bar(eng, t, {"C24600": (208.00, 215.00, 207.50, 214.00),
                      "P24700": (212.00, 213.00, 204.00, 205.00)})
    check("structural stop fired when both charts agreed", eng.trade is None)
    if eng.closed_trades:
        check("reason is STRUCTURAL STOP",
              eng.closed_trades[-1].exit_reason == "STRUCTURAL STOP")

    # and it must NOT fire while the CE stays below the line
    eng2 = build_engine(pair_mode="any")
    t2 = 1_800_000_000 - (1_800_000_000 % B)
    feed_bar(eng2, t2, {"C24600": (215.20, 215.60, 210.80, 211.40),
                        "P24700": (209.80, 213.20, 208.90, 212.10)})
    t2 += B
    feed_bar(eng2, t2, {"C24600": (207.10, 207.40, 202.60, 203.20),
                        "P24700": (214.20, 219.00, 213.80, 218.60)})
    eng2._on_trade_tick("P24700", 217.40)
    t2 += B
    feed_bar(eng2, t2, {"C24600": (204.00, 212.00, 203.00, 211.00),
                        "P24700": (218.00, 222.00, 217.00, 221.00)})
    check("stop holds while the CE stays below its line", eng2.trade is not None)

    # ── only the other chart moves: not enough under the two-sided rule ──
    eng3 = build_engine(pair_mode="any")
    t3 = 1_800_000_000 - (1_800_000_000 % B)
    feed_bar(eng3, t3, {"C24600": (215.20, 215.60, 210.80, 211.40),
                        "P24700": (209.80, 213.20, 208.90, 212.10)})
    t3 += B
    feed_bar(eng3, t3, {"C24600": (207.10, 207.40, 202.60, 203.20),
                        "P24700": (214.20, 219.00, 213.80, 218.60)})
    eng3._on_trade_tick("P24700", 217.40)
    check("in a trade", eng3.trade is not None)

    t3 += B   # CE above 213.20, but our PE stays above its low of 210.80
    feed_bar(eng3, t3, {"C24600": (208.00, 215.00, 207.50, 214.00),
                        "P24700": (216.00, 218.00, 212.00, 215.00)})
    check("other chart alone does NOT stop us out", eng3.trade is not None,
          "the leg we hold must also close below its borrowed low")

    t3 += B   # our PE falls below 210.80, but the CE drops back under 213.20
    feed_bar(eng3, t3, {"C24600": (206.00, 209.00, 205.00, 208.00),
                        "P24700": (212.00, 213.00, 204.00, 205.00)})
    check("our leg falling alone does NOT stop us out", eng3.trade is not None)

    t3 += B   # both on the same candle
    eng3.book.get("P24700").last_ltp = 203.00
    feed_bar(eng3, t3, {"C24600": (208.00, 216.00, 207.00, 214.50),
                        "P24700": (206.00, 207.00, 202.00, 203.00)})
    check("both on the same candle stops us out", eng3.trade is None)
    if eng3.closed_trades:
        check("reason is STRUCTURAL STOP",
              eng3.closed_trades[-1].exit_reason == "STRUCTURAL STOP")

    # ── the older one-sided behaviour is still available ──
    eng4 = build_engine(pair_mode="any", two_sided_stop=False)
    t4 = 1_800_000_000 - (1_800_000_000 % B)
    feed_bar(eng4, t4, {"C24600": (215.20, 215.60, 210.80, 211.40),
                        "P24700": (209.80, 213.20, 208.90, 212.10)})
    t4 += B
    feed_bar(eng4, t4, {"C24600": (207.10, 207.40, 202.60, 203.20),
                        "P24700": (214.20, 219.00, 213.80, 218.60)})
    eng4._on_trade_tick("P24700", 217.40)
    t4 += B
    eng4.book.get("P24700").last_ltp = 215.00
    feed_bar(eng4, t4, {"C24600": (208.00, 215.00, 207.50, 214.00),
                        "P24700": (216.00, 218.00, 212.00, 215.00)})
    check("with the two-sided stop off, the other chart alone still exits",
          eng4.trade is None)


# ═══════════════════════════════════════════════════════════════════════════
# 5. SIGNAL INVALIDATION AND LIMITS
# ═══════════════════════════════════════════════════════════════════════════

def test_invalidation_and_limits():
    print("\n5. Invalidation, the safety stop, and the daily limit")
    eng = build_engine(pair_mode="any")
    B = eng.cfg.interval_seconds
    t = 1_800_000_000 - (1_800_000_000 % B)

    feed_bar(eng, t, {"C24600": (215.20, 215.60, 210.80, 211.40),
                      "P24700": (209.80, 213.20, 208.90, 212.10)})
    t += B
    feed_bar(eng, t, {"C24600": (207.10, 207.40, 202.60, 203.20),
                      "P24700": (214.20, 219.00, 213.80, 218.60)})
    check("armed", eng.signal is not None)

    # CE recovers before we ever get filled
    t += B
    feed_bar(eng, t, {"C24600": (208.00, 215.00, 207.50, 214.00),
                      "P24700": (218.00, 220.00, 216.00, 219.00)})
    check("signal dropped when the collapsed leg recovered", eng.signal is None)
    check("no trade was taken", eng.trade is None)
    check("frame is retained", eng.frame is not None)

    # safety stop
    eng2 = build_engine(safety_stop_pct=25.0, pair_mode="any")
    t2 = 1_800_000_000 - (1_800_000_000 % B)
    feed_bar(eng2, t2, {"C24600": (215.20, 215.60, 210.80, 211.40),
                        "P24700": (209.80, 213.20, 208.90, 212.10)})
    t2 += B
    feed_bar(eng2, t2, {"C24600": (207.10, 207.40, 202.60, 203.20),
                        "P24700": (214.20, 219.00, 213.80, 218.60)})
    eng2._on_trade_tick("P24700", 217.40)
    eng2._on_trade_tick("P24700", 160.00)   # 26% down
    check("safety stop fired", eng2.trade is None)
    if eng2.closed_trades:
        check("reason is SAFETY STOP",
              eng2.closed_trades[-1].exit_reason == "SAFETY STOP")

    # daily limit
    eng3 = build_engine(max_trades_per_day=1, pair_mode="any")
    t3 = 1_800_000_000 - (1_800_000_000 % B)
    feed_bar(eng3, t3, {"C24600": (215.20, 215.60, 210.80, 211.40),
                        "P24700": (209.80, 213.20, 208.90, 212.10)})
    t3 += B
    feed_bar(eng3, t3, {"C24600": (207.10, 207.40, 202.60, 203.20),
                        "P24700": (214.20, 219.00, 213.80, 218.60)})
    eng3._on_trade_tick("P24700", 217.40)
    eng3._on_trade_tick("P24700", 260.00)
    check("first trade done", eng3.trade_count == 1)
    check("phase is DONE at the limit", eng3.phase == Phase.DONE)
    t3 += B
    feed_bar(eng3, t3, {"C24600": (200.00, 200.40, 195.00, 196.00),
                        "P24700": (260.00, 268.00, 259.00, 267.00)})
    check("no second signal past the limit", eng3.signal is None)


# ═══════════════════════════════════════════════════════════════════════════
# 6. TWO TRADES ON ONE PAIR, OPPOSITE DIRECTIONS
# ═══════════════════════════════════════════════════════════════════════════

def test_second_trade_flips():
    print("\n6. The second trade, on the same pair, the other way round")
    eng = build_engine(pair_mode="any")
    B = eng.cfg.interval_seconds
    t = 1_800_000_000 - (1_800_000_000 % B)

    feed_bar(eng, t, {"C24600": (215.20, 215.60, 210.80, 211.40),
                      "P24700": (209.80, 213.20, 208.90, 212.10)})
    t += B
    feed_bar(eng, t, {"C24600": (207.10, 207.40, 202.60, 203.20),
                      "P24700": (214.20, 219.00, 213.80, 218.60)})
    eng._on_trade_tick("P24700", 217.40)
    eng._on_trade_tick("P24700", 251.00)
    check("trade one closed on target", eng.trade_count == 1)

    # now the PE collapses below 210.80 and the CE closes above 213.20
    t += B
    feed_bar(eng, t, {"P24700": (209.00, 209.40, 204.00, 204.60),
                      "C24600": (211.00, 217.50, 210.50, 216.80)})
    sig = eng.signal
    check("second signal armed", sig is not None)
    if sig:
        check("this time we buy the CE", sig.buy_side == "CE")
        check("level is the CE's borrowed high 213.20",
              abs(sig.breakout_level - 213.20) < 0.001)
        eng._on_trade_tick("C24600", 214.60)
        check("second entry taken", eng.trade is not None)
        if eng.trade:
            check("entry at 214.60", abs(eng.trade.entry_price - 214.60) < 0.001)


# ═══════════════════════════════════════════════════════════════════════════
# 7. SAME-STRIKE PAIRING  (client change, 25/08)
# ═══════════════════════════════════════════════════════════════════════════

def test_same_strike_pairing():
    print("\n7. Same-strike pairing")

    eng = build_engine(pair_mode="same")
    n = len(eng.ce_legs)
    pairs = list(eng._candidate_pairs())
    check("same mode gives one pair per strike", len(pairs) == n, f"{len(pairs)} pairs")
    check("every pair is one strike",
          all(abs(c.strike - p.strike) < 0.01 for c, p in pairs))

    eng_any = build_engine(pair_mode="any")
    check("any mode gives all combinations",
          len(list(eng_any._candidate_pairs())) == n * n,
          f"{n} x {n}")

    # the 25/08 session paired 24100 CE with 24300 PE — must not happen now
    B = eng.cfg.interval_seconds
    t = 1_800_000_000 - (1_800_000_000 % B)
    eng.last_processed_bucket = t
    feed_bar(eng, t + B, {
        "C24550": (106.70, 119.20, 106.40, 113.80),   # green
        "P24750": (115.40, 115.50, 102.45, 107.20),   # red, overlapping
    })
    check("cross-strike pair no longer taken", eng.frame is None,
          "24100CE/24300PE was the 25/08 pairing")

    # a genuine same-strike overlap is taken
    eng2 = build_engine(pair_mode="same")
    t2 = 1_800_000_000 - (1_800_000_000 % B)
    eng2.last_processed_bucket = t2
    feed_bar(eng2, t2 + B, {
        "C24650": (118.40, 119.60, 116.20, 117.25),   # red
        "P24650": (117.25, 119.80, 116.00, 118.40),   # green
    })
    check("same-strike overlap taken", eng2.frame is not None)
    if eng2.frame:
        check("both legs are 24650",
              eng2.frame.ce.strike == 24650 and eng2.frame.pe.strike == 24650)

    # the same two candles would still pair in "any" mode — proving the
    # rejection above is the pairing rule and not something else
    eng3 = build_engine(pair_mode="any")
    t3 = 1_800_000_000 - (1_800_000_000 % B)
    eng3.last_processed_bucket = t3
    feed_bar(eng3, t3 + B, {
        "C24550": (106.70, 119.20, 106.40, 113.80),
        "P24750": (115.40, 115.50, 102.45, 107.20),
    })
    check("that same pair still matches in 'any' mode", eng3.frame is not None)


# ═══════════════════════════════════════════════════════════════════════════
# 8. THE REFERENCE CANDLE
# ═══════════════════════════════════════════════════════════════════════════

def test_reference_candle():
    print("\n8. The reference candle")

    eng = build_engine(ref_candle="after")
    ref = eng.ref_bucket()
    ready = eng._ladder_ready_epoch()
    from dhan_api import ist_bucket_label
    check("'after' uses the candle starting at the reference time",
          ist_bucket_label(ref) == "09:30", ist_bucket_label(ref))
    check("ladder locks one candle later",
          ist_bucket_label(ready) == "09:33", ist_bucket_label(ready))
    check("window label reads 09:30-09:33",
          eng._ref_window_label() == "09:30-09:33", eng._ref_window_label())

    old = build_engine(ref_candle="before")
    check("'before' still available for the old behaviour",
          ist_bucket_label(old.ref_bucket()) == "09:27",
          ist_bucket_label(old.ref_bucket()))

    # the reference candle itself must not be scanned for the overlap
    eng3 = build_engine(ref_candle="after")
    rb = eng3.ref_bucket()
    feed_bar(eng3, rb, {"C24650": (118.40, 119.60, 116.20, 117.25),
                        "P24650": (117.25, 119.80, 116.00, 118.40)})
    check("reference candle not scanned for the overlap", eng3.frame is None)
    feed_bar(eng3, rb + eng3.cfg.interval_seconds,
             {"C24650": (118.40, 119.60, 116.20, 117.25),
              "P24650": (117.25, 119.80, 116.00, 118.40)})
    check("the next candle is scanned", eng3.frame is not None)


# ═══════════════════════════════════════════════════════════════════════════
# 9. REGRESSIONS FROM THE 25/08 LOGS
# ═══════════════════════════════════════════════════════════════════════════

def test_log_regressions():
    print("\n9. Regressions from the 25/08 session")

    B = 180

    # (a) three trades were taken on a two-trade limit after a restart
    eng = build_engine(max_trades_per_day=2)
    eng.trade_count = 2
    t = 1_800_000_000 - (1_800_000_000 % B)
    eng.last_processed_bucket = t
    feed_bar(eng, t + B, {"C24650": (118.40, 119.60, 116.20, 117.25),
                          "P24650": (117.25, 119.80, 116.00, 118.40)})
    feed_bar(eng, t + 2 * B, {"C24650": (110.00, 110.40, 104.00, 105.00),
                              "P24650": (118.00, 124.00, 117.50, 123.00)})
    check("no arming once the limit is already reached", eng.signal is None,
          "this is what let 25/08 take a third trade")

    # (b) the log kept reporting findings after the day was over
    logged = []
    eng2 = build_engine(max_trades_per_day=2)
    eng2.gui = lambda e, d: logged.append(d.get("msg", "")) if e == "log" else None
    eng2.trade_count = 2
    t2 = 1_800_000_000 - (1_800_000_000 % B)
    eng2.last_processed_bucket = t2
    feed_bar(eng2, t2 + B, {"C24650": (118.40, 119.60, 116.20, 117.25),
                            "P24650": (117.25, 119.80, 116.00, 118.40)})
    logged.clear()
    for i in range(2, 8):
        feed_bar(eng2, t2 + i * B, {"C24650": (110.00, 110.40, 104.00, 105.00),
                                    "P24650": (118.00, 124.00, 117.50, 123.00)})
    noise = [m for m in logged if "COLLAPSE" in m or "BREAK" in m]
    check("no COLLAPSE/BREAK noise after the day is done", not noise,
          f"{len(noise)} lines would have been logged")

    # (c) state must survive a restart
    eng3 = build_engine()
    t3 = 1_800_000_000 - (1_800_000_000 % B)
    eng3.last_processed_bucket = t3
    feed_bar(eng3, t3 + B, {"C24650": (118.40, 119.60, 116.20, 117.25),
                            "P24650": (117.25, 119.80, 116.00, 118.40)})
    feed_bar(eng3, t3 + 2 * B, {"C24650": (110.00, 110.40, 104.00, 105.00),
                                "P24650": (118.00, 124.00, 117.50, 123.00)})
    eng3._on_trade_tick("P24650", 120.00)
    if eng3.trade:
        eng3._on_trade_tick("P24650", 200.00)
    check("state written as soon as the trade closed",
          eng3.cfg.state_file.exists())
    try:
        eng3.cfg.state_file.unlink()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# 9b. THE ORDERING RULE — collapse first, or together
# ═══════════════════════════════════════════════════════════════════════════

def test_ordering_rule():
    print("\n9b. The order — the collapse comes first, or they land together")
    B = 180

    def frame_then(eng, t, seq):
        eng.last_processed_bucket = t
        feed_bar(eng, t + B, {"C24450": (102.70, 109.15, 101.85, 108.70),
                              "P24450": (109.30, 110.50, 103.40, 103.75)})
        for i, bars in enumerate(seq, start=2):
            feed_bar(eng, t + i * B, bars)
        return eng

    # borrowed: CE low 103.40 / high 110.50 ; PE low 101.85 / high 109.15
    PE_BREAKS  = {"P24450": (104.00, 112.00, 103.50, 111.00),
                  "C24450": (108.00, 109.00, 106.00, 107.00)}
    CE_QUIET   = {"C24450": (107.00, 108.00, 105.00, 106.50),
                  "P24450": (111.00, 113.00, 110.50, 112.00)}
    CE_FALLS   = {"C24450": (103.00, 104.00, 99.00, 100.00),
                  "P24450": (112.00, 119.00, 111.50, 118.60)}
    CE_FALLS_ONLY = {"C24450": (103.00, 104.00, 99.00, 100.00),
                     "P24450": (104.00, 106.00, 103.00, 105.00)}

    # (a) break BEFORE the collapse — the 26/08 shape — must not arm
    eng = build_engine(pair_mode="same")
    t = 1_800_000_000 - (1_800_000_000 % B)
    frame_then(eng, t, [PE_BREAKS, CE_QUIET, CE_FALLS])
    check("break before the collapse does not arm", eng.signal is None,
          "this is the 26/08 shape")

    # (b) both on the same candle — arms
    eng = build_engine(pair_mode="same")
    t = 1_800_000_000 - (1_800_000_000 % B)
    frame_then(eng, t, [CE_FALLS])
    check("collapse and break together arms", eng.signal is not None)
    if eng.signal:
        check("we buy the put", eng.signal.buy_side == "PE")

    # (c) collapse first, break on a later candle — arms
    eng = build_engine(pair_mode="same")
    t = 1_800_000_000 - (1_800_000_000 % B)
    frame_then(eng, t, [CE_FALLS_ONLY, PE_BREAKS])
    check("collapse first, break later, arms", eng.signal is not None)
    if eng.signal:
        check("still buying the put", eng.signal.buy_side == "PE")
        check("band anchored to the put's borrowed high 109.15",
              abs(eng.signal.breakout_level - 109.15) < 0.01)

    # (d) collapse cancelled if that leg recovers before the break arrives
    eng = build_engine(pair_mode="same")
    t = 1_800_000_000 - (1_800_000_000 % B)
    CE_RECOVERS = {"C24450": (100.00, 112.00, 99.50, 111.50),
                   "P24450": (105.00, 106.00, 103.00, 104.00)}
    frame_then(eng, t, [CE_FALLS_ONLY, CE_RECOVERS, PE_BREAKS])
    check("a recovered collapse does not arm a later break",
          eng.signal is None)


# ═══════════════════════════════════════════════════════════════════════════
# 9c. THE RELAXED ENTRY AFTER A STOP
# ═══════════════════════════════════════════════════════════════════════════

def test_relaxed_entry():
    print("\n9c. After a stop, the entry is relaxed")
    B = 180

    def framed(**kw):
        eng = build_engine(pair_mode="same", **kw)
        t = 1_800_000_000 - (1_800_000_000 % B)
        eng.last_processed_bucket = t
        feed_bar(eng, t + B, {"C24450": (102.70, 109.15, 101.85, 108.70),
                              "P24450": (109.30, 110.50, 103.40, 103.75)})
        return eng, t

    PE_BREAKS = {"P24450": (104.00, 112.00, 103.50, 111.00),
                 "C24450": (108.00, 109.00, 106.00, 107.00)}

    check("a fresh engine starts in full mode",
          build_engine().entry_mode == "full")

    # ── a stop switches to relaxed ──
    eng, t = framed()
    eng.trade = None
    feed_bar(eng, t + 2 * B, {"C24450": (103.00, 104.00, 99.00, 100.00),
                              "P24450": (112.00, 119.00, 111.50, 118.60)})
    eng._on_trade_tick("P24450", 112.00)
    check("entered", eng.trade is not None)
    if eng.trade:
        eng._exit_trade(100.00, "SAFETY STOP")
        check("a safety stop relaxes the next entry",
              eng.entry_mode == "relaxed")

    # ── a target does not ──
    eng2, t2 = framed()
    feed_bar(eng2, t2 + 2 * B, {"C24450": (103.00, 104.00, 99.00, 100.00),
                                "P24450": (112.00, 119.00, 111.50, 118.60)})
    eng2._on_trade_tick("P24450", 112.00)
    if eng2.trade:
        eng2._exit_trade(200.00, "TARGET")
        check("a target keeps the full setup", eng2.entry_mode == "full")

    # ── relaxed arms on a break alone ──
    eng3, t3 = framed()
    eng3.entry_mode = "relaxed"
    feed_bar(eng3, t3 + 2 * B, PE_BREAKS)
    check("relaxed mode arms on a break with no collapse",
          eng3.signal is not None)
    if eng3.signal:
        check("buying the leg that broke", eng3.signal.buy_side == "PE")

    # ── the guard blocks it when the other chart is above its line ──
    eng4, t4 = framed(guard_relaxed_entry=True)
    eng4.entry_mode = "relaxed"
    feed_bar(eng4, t4 + 2 * B, {"P24450": (104.00, 112.00, 103.50, 111.00),
                                "C24450": (108.00, 113.00, 107.00, 112.00)})
    check("guard blocks the relaxed entry when the other leg is above its line",
          eng4.signal is None, "the stop would fire next candle")

    eng5, t5 = framed(guard_relaxed_entry=False)
    eng5.entry_mode = "relaxed"
    feed_bar(eng5, t5 + 2 * B, {"P24450": (104.00, 112.00, 103.50, 111.00),
                                "C24450": (108.00, 113.00, 107.00, 112.00)})
    check("with the guard off it arms anyway", eng5.signal is not None)

    # ── the mode survives a restart ──
    eng6, _ = framed()
    eng6.entry_mode = "relaxed"
    eng6.trade_count = 1
    eng6.save_state()
    eng7 = build_engine(pair_mode="same")
    eng7.load_state()
    check("relaxed mode survives a restart", eng7.entry_mode == "relaxed")
    check("and so does the trade count", eng7.trade_count == 1)
    try:
        eng6.cfg.state_file.unlink()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# 9d. DEFECTS FROM THE 01/09 SESSION
# ═══════════════════════════════════════════════════════════════════════════

def test_session_defects():
    print("\n9d. Defects found in the 01/09 log")
    import threading
    from datetime import time as dtime
    import dhan_api as dapi

    # ── the login must be cancellable ──
    ev = threading.Event()
    ev.set()
    tm = dapi.DhanTokenManager(ev)
    check("a cancelled login reports itself cancelled", tm.cancelled)
    check("a cancelled login returns nothing instead of retrying",
          tm.generate(max_retries=3) is None)
    try:
        tm.ensure_token()
        ok = False
    except RuntimeError:
        ok = True
    check("a cancelled login raises rather than hanging", ok)

    # the wait must return early when cancelled, not sleep it out
    import time as _t
    t0 = _t.time()
    tm._sleep(5)
    check("a cancelled wait returns at once", _t.time() - t0 < 0.5,
          f"{_t.time()-t0:.2f}s instead of 5s")

    # and an uncancelled manager still waits
    tm2 = dapi.DhanTokenManager()
    t0 = _t.time()
    tm2._sleep(0.2)
    check("an active wait still waits", _t.time() - t0 >= 0.15)

    # ── the feed is dropped at square-off ──
    eng = build_engine()
    eng.cfg.square_off_time = dtime(0, 0, 1)   # already past
    check("feed is open to begin with", not eng.feed_stop.is_set())
    eng._check_eod()
    check("feed is closed at square-off", eng.feed_stop.is_set(),
          "the 01/09 session reconnected until 17:20")
    before = eng.feed_stop.is_set()
    eng._check_eod()
    check("square-off is not announced twice", before and eng.feed_stop.is_set())

    # ── the arming message describes the two-sided stop ──
    said = []
    eng2 = build_engine(pair_mode="same")
    eng2.gui = lambda e, d: said.append(d.get("msg", "")) if e == "log" else None
    B = eng2.cfg.interval_seconds
    t = 1_800_000_000 - (1_800_000_000 % B)
    eng2.last_processed_bucket = t
    feed_bar(eng2, t + B, {"C24450": (102.70, 109.15, 101.85, 108.70),
                           "P24450": (109.30, 110.50, 103.40, 103.75)})
    said.clear()
    feed_bar(eng2, t + 2 * B, {"C24450": (103.00, 104.00, 99.00, 100.00),
                               "P24450": (112.00, 119.00, 111.50, 118.60)})
    arm_lines = [m for m in said if "Stop will" in m]
    check("arming names both stop conditions",
          bool(arm_lines) and "BOTH" in arm_lines[0],
          arm_lines[0].strip() if arm_lines else "no stop line logged")


# ═══════════════════════════════════════════════════════════════════════════
# 9e. THE REST CANDLE FEED
# ═══════════════════════════════════════════════════════════════════════════

def test_rest_feed():
    print("\n9e. Candles from the broker's own bars")
    import time as _t
    from restfeed import RestCandleFeed, aggregate_minutes

    B = 180
    base = 1_800_000_000 - (1_800_000_000 % B)

    def m(ts, o, h, l, c):
        return {"ts": ts, "open": o, "high": h, "low": l, "close": c}

    # ── the payload the API actually returns must work end to end ──
    # On 09-Sep every poll raised KeyError('ts') because the API returns rows
    # keyed "timestamp" while the feed works in "ts". The fakes below used the
    # feed's shape, so the tests passed while the live run delivered nothing.
    import dhan_api as dapi
    eng0 = build_engine(pair_mode="same")
    real_rows = [{"timestamp": base + i * 60, "open": 10.0 + i, "high": 14.0 + i,
                  "low": 9.0 + i, "close": 12.0 + i} for i in range(3)]
    _real = dapi.fetch_intraday
    dapi.fetch_intraday = lambda *a, **k: list(real_rows)
    try:
        rows = eng0._fetch_1m("C24450")
    finally:
        dapi.fetch_intraday = _real
    check("the real API payload is translated for the feed",
          bool(rows) and all("ts" in r for r in rows),
          "api returns 'timestamp', the feed reads 'ts'")

    emitted0 = []
    feed0 = RestCandleFeed(legs={"C24450": "call"},
                           fetch_1m=lambda sec: list(rows),
                           on_bar=lambda sid, b: emitted0.append(b),
                           interval=B, grace=6.0)
    with_patched_time(feed0, base + B + 30,
                      lambda: feed0._poll_leg("C24450", "call"))
    check("and a bar is produced from it", len(emitted0) == 1,
          "this is the check that would have caught 09-Sep")

    # a broken payload must be reported, not swallowed
    problems = []
    feed_bad = RestCandleFeed(legs={"Q": "q"},
                              fetch_1m=lambda sec: [{"timestamp": base,
                                                     "open": 1, "high": 2,
                                                     "low": 1, "close": 1}],
                              on_bar=lambda sid, b: None,
                              interval=B, poll=0.01,
                              on_problem=problems.append)
    feed_bad._stop.set()
    try:
        feed_bad._poll_leg("Q", "q")
    except Exception as e:
        feed_bad._problem(f"[q] REST poll error: {type(e).__name__}: {e}")
    check("a payload the feed cannot read is reported",
          any("KeyError" in p for p in problems),
          problems[0] if problems else "nothing reported")

    # an empty response is announced once, not silently ignored
    problems2 = []
    feed_empty = RestCandleFeed(legs={"E": "e"}, fetch_1m=lambda sec: [],
                                on_bar=lambda sid, b: None, interval=B,
                                on_problem=problems2.append)
    feed_empty._poll_leg("E", "e")
    feed_empty._poll_leg("E", "e")
    check("an empty history response is reported", len(problems2) == 1,
          "once per leg, not on every poll")
    check("and counted", feed_empty.empty_polls == 2)

    # ── rollup ──
    mins = [m(base, 10, 14, 9, 12), m(base+60, 12, 18, 11, 17),
            m(base+120, 17, 19, 13, 15)]
    bars = aggregate_minutes(mins, B)
    check("three minutes roll into one bar", len(bars) == 1)
    b0 = bars[0]
    check("rolled bar takes first open and last close",
          b0["open"] == 10 and b0["close"] == 15)
    check("rolled bar spans the full high and low",
          b0["high"] == 19 and b0["low"] == 9)
    check("member count is kept", b0["members"] == 3)

    # ── the forming minute must never reach the strategy ──
    now = base + 150          # the base+120 minute is still building
    kept = RestCandleFeed.drop_forming(mins, now)
    check("the minute still forming is dropped", len(kept) == 2,
          "Dhan serves the current minute as it builds")
    check("finished minutes survive", kept[-1]["ts"] == base + 60)
    check("a minute is kept the instant it closes",
          len(RestCandleFeed.drop_forming(mins, base + 180)) == 3)

    # ── a bar is only emitted once it is complete ──
    emitted = []
    src = {"rows": []}
    feed = RestCandleFeed(legs={"X": "test"},
                          fetch_1m=lambda sec: list(src["rows"]),
                          on_bar=lambda sid, b: emitted.append(b),
                          interval=B, poll=0.01, grace=6.0)
    feed._poll_leg("X", "test")
    check("nothing emitted with no data", not emitted)

    # two of three minutes present, and the bar has only just closed
    src["rows"] = [m(base, 10, 14, 9, 12), m(base+60, 12, 18, 11, 17)]
    with_patched_time(feed, base + 185, lambda: feed._poll_leg("X", "test"))
    check("an incomplete bar is held back", not emitted,
          "2 of 3 minutes, still inside the grace window")

    # the third minute arrives -> complete by count
    src["rows"].append(m(base+120, 17, 19, 13, 15))
    with_patched_time(feed, base + 185, lambda: feed._poll_leg("X", "test"))
    check("a full bar is emitted", len(emitted) == 1)
    if emitted:
        check("and it is the rolled-up bar",
              emitted[0]["high"] == 19 and emitted[0]["low"] == 9)

    # ── grace: a leg that did not trade every minute still completes ──
    emitted2 = []
    src2 = {"rows": [m(base, 10, 14, 9, 12), m(base+60, 12, 18, 11, 17)]}
    feed2 = RestCandleFeed(legs={"Y": "thin"},
                           fetch_1m=lambda sec: list(src2["rows"]),
                           on_bar=lambda sid, b: emitted2.append(b),
                           interval=B, poll=0.01, grace=6.0)
    with_patched_time(feed2, base + B + 7, lambda: feed2._poll_leg("Y", "thin"))
    check("grace completes a bar on a leg that did not trade every minute",
          len(emitted2) == 1,
          "an outer strike may simply not trade for a minute")

    # ── the session anchor keeps yesterday out ──
    emitted3 = []
    yday = base - 86400
    src3 = {"rows": [m(yday, 1, 2, 1, 2), m(yday+60, 2, 3, 1, 2),
                     m(yday+120, 2, 3, 1, 2)]}
    feed3 = RestCandleFeed(legs={"Z": "z"},
                           fetch_1m=lambda sec: list(src3["rows"]),
                           on_bar=lambda sid, b: emitted3.append(b),
                           interval=B, grace=6.0, session_anchor=base)
    with_patched_time(feed3, base + 300, lambda: feed3._poll_leg("Z", "z"))
    check("bars from before today's reference are refused", not emitted3,
          "a poll before the open otherwise replays the whole previous session")
    check("and they are counted", feed3.dropped_stale > 0)

    # ── a bar is never emitted twice ──
    before = len(emitted)
    with_patched_time(feed, base + 300, lambda: feed._poll_leg("X", "test"))
    check("a bar already emitted is not repeated", len(emitted) == before)

    # ── health verdicts ──
    feed.latencies = [1.0, 2.0, 3.0]
    check("a fast feed reads comfortable",
          feed.health()["verdict"] == "comfortable")
    feed.latencies = [1.0, 20.0]
    check("a slow feed reads tight", feed.health()["verdict"] == "tight")
    feed.latencies = [1.0, 90.0]
    check("a very slow feed says signals are late",
          "TOO SLOW" in feed.health()["verdict"])

    # ── the engine installs a REST bar over any tick-built one ──
    eng = build_engine(pair_mode="same")
    eng._on_rest_bar("C24450", {"ts": base, "open": 102.70, "high": 109.15,
                                "low": 101.85, "close": 108.70})
    got = eng.book.get("C24450").get(base)
    check("a REST bar lands in the book", got is not None)
    if got:
        check("with the broker's own values",
              got.high == 109.15 and got.low == 101.85)
    check("and the bucket is marked as delivered",
          "C24450" in eng._rest_bars.get(base, set()))

    # ── we wait for the candle to form on every chart ──
    check("before the merge we wait for the whole ladder",
          eng._expected_legs() == len(eng.legs))

    # only 1 of 10 charts has reported: nothing may be evaluated yet
    eng.last_processed_bucket = base - B
    eng._process_rest_buckets(base + B + 10, B)
    check("a partial ladder is not evaluated", eng.frame is None,
          "1 of 10 charts had reported")
    check("and the bucket is not consumed",
          eng.last_processed_bucket == base - B)

    # the rest of the ladder reports -> now it runs
    for leg in list(eng.legs.values()):
        if leg.sec_id in ("C24450", "P24450"):
            continue
        eng._on_rest_bar(leg.sec_id, {"ts": base, "open": 50.0, "high": 51.0,
                                      "low": 49.0, "close": 50.5})
    eng._on_rest_bar("P24450", {"ts": base, "open": 109.30, "high": 110.50,
                                "low": 103.40, "close": 103.75})
    eng._process_rest_buckets(base + B + 10, B)
    check("a complete ladder is evaluated", eng.frame is not None)
    if eng.frame:
        check("the merge is the 24450 pair", eng.frame.ce.strike == 24450)
        check("after the merge only the pair is awaited",
              eng._expected_legs() == 2)

    # ── a chart that never reports abandons the bucket, it is not part-run ──
    eng2 = build_engine(pair_mode="same")
    eng2.last_processed_bucket = base - B
    eng2._on_rest_bar("C24450", {"ts": base, "open": 102.70, "high": 109.15,
                                 "low": 101.85, "close": 108.70})
    eng2._process_rest_buckets(base + B + 10, B)
    check("still waiting inside the window", eng2.last_processed_bucket == base - B)
    said2 = []
    eng2.gui = lambda e, d: said2.append(d.get("msg", "")) if e == "log" else None
    eng2._process_rest_buckets(base + (3 * B) + 30, B)
    check("a candle that never completed is abandoned",
          eng2.last_processed_bucket >= base)
    check("nothing was evaluated on it", eng2.frame is None,
          "a partial candle must never produce a merge")
    check("and the log says so",
          any("abandoned" in m for m in said2),
          said2[0].strip() if said2 else "nothing logged")


def with_patched_time(obj, fake_now, fn):
    """Run fn with restfeed's clock pinned, so bar completeness is testable."""
    import restfeed
    real = restfeed.time.time
    restfeed.time.time = lambda: fake_now
    try:
        return fn()
    finally:
        restfeed.time.time = real


# ═══════════════════════════════════════════════════════════════════════════
# 9f. THE REFERENCE CANDLE ON A LATE START
# ═══════════════════════════════════════════════════════════════════════════

def test_late_start_reference():
    print("\n9f. A late start still uses the reference candle")
    import time as _t
    import dhan_api as dapi

    eng = build_engine()
    bucket = eng.ref_bucket()
    iv = eng.cfg.interval_seconds

    real = dapi.fetch_intraday
    calls = []

    def fake(sec, seg, instr="OPTIDX", interval="1", days=3):
        calls.append((sec, seg, instr))
        # three finished 1-minute bars inside the reference candle
        return [{"timestamp": bucket, "open": 23500.0, "high": 23520.0,
                 "low": 23495.0, "close": 23510.0},
                {"timestamp": bucket + 60, "open": 23510.0, "high": 23530.0,
                 "low": 23505.0, "close": 23522.0},
                {"timestamp": bucket + 120, "open": 23522.0, "high": 23535.0,
                 "low": 23518.0, "close": 23528.40}]

    dapi.fetch_intraday = fake
    try:
        close = eng._reference_spot_from_history(bucket)
    finally:
        dapi.fetch_intraday = real

    check("the reference close comes from the index history",
          close is not None and abs(close - 23528.40) < 0.01,
          f"got {close}")
    check("and it is read from the index segment, not the option one",
          bool(calls) and calls[0][1] == "IDX_I" and calls[0][2] == "INDEX",
          str(calls[0]) if calls else "no call made")

    # a candle that has not finished must not be used.
    # The minute currently in progress can never have closed, whenever the
    # suite happens to run.
    now_i = int(_t.time())
    cur_min = now_i - (now_i % 60)
    cur_bucket = cur_min - (cur_min % iv)

    def fake_forming(sec, seg, instr="OPTIDX", interval="1", days=3):
        return [{"timestamp": cur_min, "open": 1.0, "high": 2.0,
                 "low": 1.0, "close": 1.5}]

    dapi.fetch_intraday = fake_forming
    try:
        val = eng._reference_spot_from_history(cur_bucket)
    finally:
        dapi.fetch_intraday = real
    check("a still-forming reference candle is refused", val is None,
          "its last minute has not closed yet")

    # nothing returned -> no value, so the caller can fall back
    dapi.fetch_intraday = lambda *a, **k: []
    try:
        check("an empty history returns nothing",
              eng._reference_spot_from_history(bucket) is None)
    finally:
        dapi.fetch_intraday = real

    # a failing call must not take the engine down
    def boom(*a, **k):
        raise RuntimeError("network down")
    dapi.fetch_intraday = boom
    try:
        check("a failed lookup is survivable",
              eng._reference_spot_from_history(bucket) is None)
    finally:
        dapi.fetch_intraday = real

    # the tick-built candle still wins when we were running at the time
    eng2 = build_engine()
    s = eng2.book.add(eng2.cfg.index_security_id, "IDX")
    s.apply_bar(bucket, 23400.0, 23450.0, 23390.0, 23444.0)
    dapi.fetch_intraday = fake
    try:
        check("our own candle is preferred when we have one",
              abs(eng2._reference_spot() - 23444.0) < 0.01)
    finally:
        dapi.fetch_intraday = real


# ═══════════════════════════════════════════════════════════════════════════
# 9g. CATCHING UP AFTER A LATE START
# ═══════════════════════════════════════════════════════════════════════════

def test_late_start_catchup():
    print("\n9g. A late start replays the candles it missed")
    import time as _t
    B = 180
    now = int(_t.time())
    ref = (now - 3600) - ((now - 3600) % B)      # reference an hour ago

    def rig(**kw):
        eng = build_engine(pair_mode="same", **kw)
        eng.ref_bucket = lambda: ref
        eng.ladder_built = True
        return eng

    def put(eng, bucket, ce, pe):
        eng._on_rest_bar("C24450", {"ts": bucket, "open": ce[0], "high": ce[1],
                                    "low": ce[2], "close": ce[3]})
        eng._on_rest_bar("P24450", {"ts": bucket, "open": pe[0], "high": pe[1],
                                    "low": pe[2], "close": pe[3]})
        for leg in eng.legs.values():
            if leg.sec_id in ("C24450", "P24450"):
                continue
            eng._on_rest_bar(leg.sec_id, {"ts": bucket, "open": 50.0,
                                          "high": 51.0, "low": 49.0,
                                          "close": 50.5})

    # a merge sits in the history, 40 minutes before we started
    eng = rig()
    merge_b = ref + (20 * B)
    put(eng, merge_b, (102.70, 109.15, 101.85, 108.70),
                      (109.30, 110.50, 103.40, 103.75))
    live = ref + (60 * B)

    check("catch-up does not run on the first pass",
          eng._catch_up(live + B, B, live) is False,
          "it announces itself, then waits for the charts")
    eng._catchup_started = live - 60          # charts have had their moment
    done = eng._catch_up(live + B, B, live)
    check("catch-up completes on the second pass", done is True)
    check("the merge in the history was found", eng.frame is not None)
    if eng.frame:
        check("the pair is the 24450 strike", eng.frame.ce.strike == 24450)
        check("the four lines come from the historical candle",
              abs(eng.frame.ce_borrowed_high - 110.50) < 0.01
              and abs(eng.frame.pe_borrowed_high - 109.15) < 0.01)
    check("no trade was taken from the replay", eng.trade is None)
    check("and no signal was armed from it", eng.signal is None,
          "an hour-old band would be nowhere near current price")
    check("live processing starts from the current candle",
          eng.last_processed_bucket == live)

    # the reference candle itself is never scanned, even in replay
    eng2 = rig()
    put(eng2, ref, (102.70, 109.15, 101.85, 108.70),
                   (109.30, 110.50, 103.40, 103.75))
    eng2._catch_up(live + B, B, live)
    eng2._catchup_started = live - 60
    eng2._catch_up(live + B, B, live)
    check("a merge on the reference candle is not taken", eng2.frame is None)

    # nothing in the history -> we just carry on
    eng3 = rig()
    put(eng3, ref + B, (10, 11, 9, 10.5), (20, 21, 19, 20.5))   # both green
    eng3._catch_up(live + B, B, live)
    eng3._catchup_started = live - 60
    eng3._catch_up(live + B, B, live)
    check("an empty history is not a problem", eng3.frame is None)
    check("and catch-up still finishes", eng3._catchup_done)

    # switched off -> no replay at all
    eng4 = rig(replay_history_on_late_start=False)
    put(eng4, merge_b, (102.70, 109.15, 101.85, 108.70),
                       (109.30, 110.50, 103.40, 103.75))
    check("with replay off it is skipped", eng4._catch_up(live + B, B, live))
    check("and no merge is taken from history", eng4.frame is None)

    # started on time -> nothing behind us
    eng5 = rig()
    check("an on-time start needs no catch-up",
          eng5._catch_up(ref + (2 * B), B, ref + B))
    check("and it is marked done", eng5._catchup_done)

    # a nonsense span is refused rather than looped over
    eng6 = rig()
    said6 = []
    eng6.gui = lambda e, d: said6.append(d.get("msg", "")) if e == "log" else None
    check("an impossible span is skipped",
          eng6._catch_up(ref + (5000 * B), B, ref + (4000 * B)))
    check("with a warning that the clock or reference is wrong",
          any("longer than a trading session" in m for m in said6),
          said6[0].strip()[:60] if said6 else "nothing logged")

    # the duplicate startup fetch is gone when the feed is running
    calls = []
    import dhan_api as dapi
    real = dapi.fetch_intraday
    dapi.fetch_intraday = lambda *a, **k: calls.append(a) or []
    try:
        rig()._seed_history()
        check("no duplicate history fetch when candles come from REST",
              not calls, f"{len(calls)} redundant calls")
    finally:
        dapi.fetch_intraday = real


# ═══════════════════════════════════════════════════════════════════════════
# 9h. LIVE MODE
# ═══════════════════════════════════════════════════════════════════════════

def test_live_mode():
    print("\n9h. Live mode")
    import threading as _th
    import dhan_api as dapi
    from config import OverlapConfig
    B = 180

    def armed(**kw):
        """An engine sitting in an armed signal, ready to enter."""
        eng = build_engine(pair_mode="same", **kw)
        t = 1_800_000_000 - (1_800_000_000 % B)
        eng.last_processed_bucket = t
        feed_bar(eng, t + B, {"C24450": (102.70, 109.15, 101.85, 108.70),
                              "P24450": (109.30, 110.50, 103.40, 103.75)})
        feed_bar(eng, t + 2 * B, {"C24450": (103.00, 104.00, 99.00, 100.00),
                                  "P24450": (112.00, 119.00, 111.50, 118.60)})
        return eng

    def tick(eng, sec, px):
        """A tick as the websocket delivers one: the book is updated first."""
        s = eng.book.get(sec)
        if s is not None:
            s.last_ltp = px
        eng._on_trade_tick(sec, px)

    # ── the build starts in paper and live is never inherited ──
    check("a fresh config is paper", OverlapConfig().paper_mode is True)
    check("live is never restored from a settings file",
          OverlapConfig.from_dict({"trade_mode": "live"}).trade_mode == "paper")

    # ── no live order is sent in paper mode ──
    sent = []
    real_lim, real_mkt = dapi.place_order_limit_ioc, dapi.place_order_market
    dapi.place_order_limit_ioc = lambda *a, **k: sent.append(("BUY",) + a) or {
        "filled": True, "price": 112.0, "order_id": "X"}
    dapi.place_order_market = lambda *a, **k: sent.append(("SELL",) + a) or {
        "filled": True, "price": 200.0, "order_id": "X"}
    try:
        e = armed()
        tick(e, "P24450", 112.00)
        check("paper mode sends no order", not sent, f"{len(sent)} orders sent")
        check("but the trade is recorded", e.trade is not None)
    finally:
        dapi.place_order_limit_ioc, dapi.place_order_market = real_lim, real_mkt

    # ── the entry race: two ticks must produce one order ──
    orders = []
    started = _th.Event()

    def slow_buy(*a, **k):
        orders.append(a)
        started.set()
        _t.sleep(0.4)                     # the broker taking its time
        return {"filled": True, "price": 112.0, "order_id": "O1"}

    dapi.place_order_limit_ioc = slow_buy
    dapi.get_available_funds = lambda: 10_000_000.0
    dapi.position_qty = lambda sid: 0
    try:
        e = armed(trade_mode="live")
        th = _th.Thread(target=lambda: tick(e, "P24450", 112.00))
        th.start()
        started.wait(2)
        tick(e, "P24450", 112.10)   # a second tick mid-order
        th.join(3)
        check("two ticks during one order send a single BUY",
              len(orders) == 1, f"{len(orders)} orders")
        check("and one trade is opened", e.trade is not None)
    finally:
        dapi.place_order_limit_ioc = real_lim

    # ── slippage refuses an entry that has run away ──
    tried = []
    dapi.place_order_limit_ioc = lambda *a, **k: tried.append(a) or {
        "filled": True, "price": 1.0, "order_id": "S"}
    try:
        e = armed(trade_mode="live", max_entry_slippage_pct=1.0)
        # the retest fired at 112 but the market has already moved to 130
        e.book.get("P24450").last_ltp = 130.00
        e._on_trade_tick("P24450", 112.00)
        check("an entry that has slipped too far is refused", not tried)
        check("and no trade is opened", e.trade is None)
    finally:
        dapi.place_order_limit_ioc = real_lim

    # ── not enough money, no order ──
    tried2 = []
    dapi.place_order_limit_ioc = lambda *a, **k: tried2.append(a) or {
        "filled": True, "price": 112.0, "order_id": "F"}
    dapi.get_available_funds = lambda: 100.0
    try:
        e = armed(trade_mode="live")
        tick(e, "P24450", 112.00)
        check("an entry is refused when funds are short", not tried2)
    finally:
        dapi.place_order_limit_ioc = real_lim
        dapi.get_available_funds = lambda: 10_000_000.0

    # ── a failed exit must NOT clear the trade ──
    e = armed(trade_mode="live", exit_retry_attempts=2)
    dapi.place_order_limit_ioc = lambda *a, **k: {"filled": True, "price": 112.0,
                                                  "order_id": "O2"}
    dapi.place_order_market = lambda *a, **k: {"filled": False, "price": 0.0,
                                               "order_id": ""}
    dapi.position_qty = lambda sid: 65
    dapi.fill_from_tradebook = lambda oid: 0.0
    failures = []
    e.gui = lambda ev, d: failures.append(d) if ev == "exit_failed" else None
    try:
        tick(e, "P24450", 112.00)
        check("in a live trade", e.trade is not None)
        e._exit_trade(105.0, "TARGET")
        check("a failed exit leaves the trade OPEN", e.trade is not None,
              "believing we are flat while holding is the worst state")
        check("and it is not counted as closed", e.trade_count == 0)
        check("and the failure is announced", bool(failures))
    finally:
        dapi.place_order_limit_ioc, dapi.place_order_market = real_lim, real_mkt

    # ── an unconfirmed sell that actually went through is recognised ──
    e = armed(trade_mode="live", exit_retry_attempts=2)
    dapi.place_order_limit_ioc = lambda *a, **k: {"filled": True, "price": 112.0,
                                                  "order_id": "O3"}
    dapi.place_order_market = lambda *a, **k: {"filled": False, "price": 0.0,
                                               "order_id": "O4"}
    dapi.position_qty = lambda sid: 0            # broker says we are flat
    dapi.fill_from_tradebook = lambda oid: 121.50
    try:
        tick(e, "P24450", 112.00)
        e._exit_trade(105.0, "TARGET")
        check("an unconfirmed sell that did go through is booked",
              e.trade is None and e.trade_count == 1)
        if e.closed_trades:
            check("at the price from the trade book",
                  abs(e.closed_trades[-1].exit_price - 121.50) < 0.01)
    finally:
        dapi.place_order_limit_ioc, dapi.place_order_market = real_lim, real_mkt

    # ── a position closed outside the app is reconciled ──
    e = armed(trade_mode="live")
    dapi.place_order_limit_ioc = lambda *a, **k: {"filled": True, "price": 112.0,
                                                  "order_id": "O5"}
    dapi.position_qty = lambda sid: 0
    dapi.fill_from_tradebook = lambda oid: 118.25
    try:
        tick(e, "P24450", 112.00)
        check("holding a live position", e.trade is not None)
        e.trade.entry_time = "00:00:01"          # old enough to be judged
        e._last_position_sync = 0
        e._sync_positions(_t.time())
        check("a position closed elsewhere is booked",
              e.trade is None and e.trade_count == 1)
        if e.closed_trades:
            check("with the right reason",
                  e.closed_trades[-1].exit_reason == "CLOSED EXTERNALLY")
    finally:
        dapi.place_order_limit_ioc = real_lim

    # ── a fresh fill is given time before being judged missing ──
    e = armed(trade_mode="live")
    dapi.place_order_limit_ioc = lambda *a, **k: {"filled": True, "price": 112.0,
                                                  "order_id": "O6"}
    try:
        tick(e, "P24450", 112.00)
        e._last_position_sync = 0
        e._sync_positions(_t.time())
        check("a just-opened trade is not judged missing", e.trade is not None,
              "the broker needs a moment to register a fill")
    finally:
        dapi.place_order_limit_ioc = real_lim

    # ── a failing entry must not be retried on every tick ──
    attempts = []
    dapi.place_order_limit_ioc = lambda *a, **k: attempts.append(a) or {
        "filled": False, "price": 0.0, "order_id": ""}
    dapi.position_qty = lambda sid: 0
    try:
        e = armed(trade_mode="live", max_entry_attempts=3,
                  entry_retry_seconds=0.0)
        for px in (112.00, 112.10, 112.20, 112.30, 112.40, 112.50):
            tick(e, "P24450", px)
        check("a failing entry stops after the attempt limit",
              len(attempts) == 3, f"{len(attempts)} attempts from 6 ticks")
        check("and the signal is dropped rather than left hammering",
              e.signal is None)
        check("no trade was opened", e.trade is None)
    finally:
        dapi.place_order_limit_ioc = real_lim

    # ── and attempts are spaced out ──
    attempts2 = []
    dapi.place_order_limit_ioc = lambda *a, **k: attempts2.append(a) or {
        "filled": False, "price": 0.0, "order_id": ""}
    try:
        e = armed(trade_mode="live", max_entry_attempts=5,
                  entry_retry_seconds=30.0)
        for px in (112.00, 112.10, 112.20, 112.30):
            tick(e, "P24450", px)
        check("attempts inside the cooldown are ignored",
              len(attempts2) == 1, f"{len(attempts2)} attempts in one burst")
    finally:
        dapi.place_order_limit_ioc = real_lim

    # ── DH-905: a permanent refusal stops everything at once ──
    body905 = ('{"errorType":"Input_Exception","errorCode":"DH-905",'
               '"errorMessage":"Invalid IP"}')
    check("DH-905 is recognised as permanent",
          dapi.classify_order_error(body905) is not None)
    check("an ordinary rejection is not",
          dapi.classify_order_error('{"errorMessage":"insufficient funds"}') is None)

    tries905 = []

    def refuse(*a, **k):
        tries905.append(a)
        return {"filled": False, "price": 0.0, "order_id": "", "fatal": True,
                "code": "DH-905", "advice": "IP not whitelisted"}

    dapi.place_order_limit_ioc = refuse
    halts = []
    try:
        e = armed(trade_mode="live", max_entry_attempts=3,
                  entry_retry_seconds=0.0)
        e.gui = lambda ev, d: halts.append(d) if ev == "halted" else None
        for px in (112.00, 112.10, 112.20, 112.30, 112.40):
            tick(e, "P24450", px)
        check("a permanent refusal stops after ONE attempt",
              len(tries905) == 1,
              f"{len(tries905)} attempts — 09-Sep sent 60 orders in 62s")
        check("trading is halted", e.trading_halted is True)
        check("the reason names the code", "DH-905" in e.halt_reason)
        check("the signal is dropped", e.signal is None)
        check("and the halt is announced", bool(halts))
    finally:
        dapi.place_order_limit_ioc = real_lim

    # a halted engine never tries again, even on a fresh signal
    dapi.place_order_limit_ioc = refuse
    try:
        before = len(tries905)
        e2 = armed(trade_mode="live")
        e2.trading_halted = True
        for px in (112.00, 112.50):
            tick(e2, "P24450", px)
        check("a halted engine sends nothing further",
              len(tries905) == before)
    finally:
        dapi.place_order_limit_ioc = real_lim

    # a fatal refusal while holding says the position is still open
    dapi.place_order_limit_ioc = lambda *a, **k: {"filled": True, "price": 112.0,
                                                  "order_id": "OK"}
    dapi.place_order_market = lambda *a, **k: {
        "filled": False, "price": 0.0, "order_id": "", "fatal": True,
        "code": "DH-905", "advice": "IP not whitelisted"}
    dapi.position_qty = lambda sid: 65
    said905 = []
    try:
        e3 = armed(trade_mode="live", exit_retry_attempts=4)
        e3.gui = lambda ev, d: said905.append(d.get("msg", "")) if ev == "log" else None
        tick(e3, "P24450", 112.00)
        check("in a live trade", e3.trade is not None)
        e3._exit_trade(105.0, "TARGET")
        check("a fatal refusal on exit keeps the trade open",
              e3.trade is not None)
        check("and says the position is still open",
              any("POSITION IS STILL OPEN" in m for m in said905))
        check("and halts", e3.trading_halted is True)
    finally:
        dapi.place_order_limit_ioc, dapi.place_order_market = real_lim, real_mkt

    # ── STOP must reach the order path ──
    # On 09-Sep an entry sequence begun at 12:57:18 was still sending orders
    # when STOP was pressed at 12:57:20.
    sent_after = []
    dapi.place_order_limit_ioc = lambda *a, **k: sent_after.append(a) or {
        "filled": False, "price": 0.0, "order_id": ""}
    try:
        e = armed(trade_mode="live")
        e.stop_event.set()
        for px in (112.00, 112.20, 112.40):
            tick(e, "P24450", px)
        check("a stopped engine sends no order on any tick",
              not sent_after, f"{len(sent_after)} orders after STOP")
        check("and opens no trade", e.trade is None)
    finally:
        dapi.place_order_limit_ioc = real_lim

    # the order layer refuses mid-sequence too
    dapi.set_order_abort_check(lambda: True)
    try:
        r = dapi.place_order_limit_ioc("1", "NSE_FNO", "BUY", 65, 100.0)
        check("the order layer refuses a BUY once stopped",
              r.get("aborted") is True, str(r))
        r2 = dapi.place_order_market("1", "NSE_FNO", "BUY", 65)
        check("and refuses a market BUY", r2.get("aborted") is True)
    finally:
        dapi.set_order_abort_check(None)

    # ...but an exit must still be allowed through
    placed = []
    dapi.set_order_abort_check(lambda: True)
    import requests as _rq
    real_post = _rq.post
    _rq.post = lambda *a, **k: placed.append(a) or (_ for _ in ()).throw(
        RuntimeError("no network in tests"))
    try:
        dapi.place_order_market("1", "NSE_FNO", "SELL", 65)
        check("a SELL is still attempted after a stop", bool(placed),
              "leaving a position open would be worse")
    finally:
        _rq.post = real_post
        dapi.set_order_abort_check(None)

    # stopping with a live position says so
    saidstop = []
    dapi.place_order_limit_ioc = lambda *a, **k: {"filled": True, "price": 112.0,
                                                  "order_id": "OK"}
    try:
        e = armed(trade_mode="live")
        tick(e, "P24450", 112.00)
        e.gui = lambda ev, d: saidstop.append(d.get("msg", "")) if ev == "log" else None
        e.stop()
        check("stopping with a live position warns about it",
              any("LIVE POSITION IS STILL OPEN" in m for m in saidstop))
    finally:
        dapi.place_order_limit_ioc = real_lim

    # ── a rejection reaches the interface, not just the file ──
    seen = []
    dapi.set_order_notifier(lambda m, lvl="error": seen.append(m))
    dapi._notify("[REJECTED] Insufficient funds (order 123)", "warning")
    check("an order rejection is surfaced", bool(seen),
          seen[0] if seen else "nothing surfaced")
    dapi.set_order_notifier(None)

    # ── paper mode never calls the broker for positions ──
    calls = []
    dapi.position_qty = lambda sid: calls.append(sid) or 0
    e = armed()
    tick(e, "P24450", 112.00)
    e._last_position_sync = 0
    e._sync_positions(_t.time())
    check("paper mode never asks the broker about positions", not calls)


import time as _t


# ═══════════════════════════════════════════════════════════════════════════
# 9i. RUNNING SEVERAL INDICES
# ═══════════════════════════════════════════════════════════════════════════

def test_multi_index():
    print("\n9i. Two or three indices at once")
    from config import OverlapConfig, state_file_for
    from supervisor import OverlapSupervisor

    # ── the selection itself ──
    c = OverlapConfig()
    check("one index by default", c.indices == ["NIFTY"] and c.index == "NIFTY")
    c.indices = ["NIFTY", "SENSEX"]
    check("two are accepted", c.validate() == [])
    c.indices = ["NIFTY", "BANKNIFTY", "SENSEX"]
    check("all three are accepted", c.validate() == [])
    c.indices = ["NIFTY", "NIFTY"]
    check("the same one twice is refused", bool(c.validate()))
    c.indices = ["NIFTY", "GOLD"]
    check("an unknown index is refused", bool(c.validate()))

    # ── each engine gets its own single-index settings ──
    c = OverlapConfig()
    c.indices = ["NIFTY", "BANKNIFTY", "SENSEX"]
    c.target_pct = 12.0
    c.trade_mode = "live"
    subs = [c.for_index(n) for n in c.indices]
    check("each copy carries exactly one index",
          all(len(s.indices) == 1 for s in subs))
    check("and the right one",
          [s.index for s in subs] == ["NIFTY", "BANKNIFTY", "SENSEX"])
    check("settings are carried across", all(s.target_pct == 12.0 for s in subs))
    check("the trade mode is carried across too",
          all(s.trade_mode == "live" for s in subs),
          "otherwise a live session would silently run on paper")
    check("strike spacing follows the index",
          [s.strike_gap for s in subs] == [50, 100, 100])

    # ── daily state must not collide ──
    files = [s.state_file.name for s in subs]
    check("each index keeps its own daily state", len(set(files)) == 3,
          ", ".join(files))
    check("and they are named for the index",
          all(s.index in s.state_file.name for s in subs))

    # ── the supervisor builds one engine per index ──
    sup = OverlapSupervisor(c, gui_callback=lambda e, d: None)
    check("the supervisor knows it is multi", sup.multi is True)
    check("a single index is not multi",
          OverlapSupervisor(OverlapConfig(), gui_callback=None).multi is False)

    # ── polling load is recognised, not ignored ──
    said = []
    c2 = OverlapConfig()
    c2.indices = ["NIFTY", "BANKNIFTY", "SENSEX"]
    c2.strikes_each_side = 2
    c2.rest_poll = 4.0
    sup2 = OverlapSupervisor(c2, gui_callback=lambda e, d: said.append(d.get("msg", "")))
    advised = sup2._advise_poll_rate()
    check("three ladders cannot be polled every 4s", advised > 4.0,
          f"advised {advised}s for 30 charts through one 4/sec gate")
    check("and the reason is stated", any("rate limit" in m for m in said))

    c3 = OverlapConfig()
    c3.indices = ["NIFTY"]
    c3.rest_poll = 4.0
    sup3 = OverlapSupervisor(c3, gui_callback=lambda e, d: None)
    check("one ladder keeps the 4s poll", sup3._advise_poll_rate() == 4.0)

    # ── a permanent refusal on one index halts them all ──
    from strategy import OverlapEngine
    sup4 = OverlapSupervisor(c, gui_callback=lambda e, d: None)
    for n in c.indices:
        sup4.engines[n] = OverlapEngine(c.for_index(n), gui_callback=lambda e, d: None)
    sup4.engines["NIFTY"].trading_halted = True
    sup4.engines["NIFTY"].halt_reason = "DH-905: IP not whitelisted"
    sup4._check_shared_halt()
    check("a refusal on one index halts the others",
          all(e.trading_halted for e in sup4.engines.values()),
          "the refusal is about the account, not the index")
    check("and they carry the same reason",
          sup4.engines["SENSEX"].halt_reason.startswith("DH-905"))

    # ── engines stay independent otherwise ──
    a_, b_ = sup4.engines["NIFTY"], sup4.engines["SENSEX"]
    a_.trade_count = 2
    check("one index reaching its limit does not affect another",
          b_.trade_count == 0)
    check("and they hold separate frames", a_.frame is None and b_.frame is None)
    check("engines are tagged for the log when several run",
          OverlapSupervisor(c, gui_callback=None).multi is True)


# ═══════════════════════════════════════════════════════════════════════════
# 10. CONFIG
# ═══════════════════════════════════════════════════════════════════════════

def test_config():
    print("\n10. Settings and validation")
    c = OverlapConfig()
    check("defaults match the note",
          c.min_overlap == 0.05 and c.retest_buffer == 4.0
          and c.target_pct == 15.0 and c.max_trades_per_day == 2
          and c.min_body_pct == 50.0)
    check("3-minute interval is 180s", OverlapConfig(timeframe_minutes=3).interval_seconds == 180)
    check("5-minute interval is 300s", OverlapConfig(timeframe_minutes=5).interval_seconds == 300)
    check("no validation errors by default", c.validate() == [])

    bad = OverlapConfig(timeframe_minutes=7)
    check("bad timeframe caught", len(bad.validate()) > 0)

    rt = OverlapConfig.from_dict(OverlapConfig().to_dict())
    check("round-trips through JSON", rt.to_dict() == OverlapConfig().to_dict())

    # bucket alignment for the 09:15 open
    for iv in (180, 300):
        check(f"09:15 aligns on a {iv // 60}-minute grid", 13500 % iv == 0)


# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 72)
    print("  THE OVERLAP — offline simulation against strategy note v1.2")
    print("=" * 72)

    test_config()
    test_overlap_rules()
    test_signal_rules()
    test_full_session()
    test_structural_stop()
    test_invalidation_and_limits()
    test_second_trade_flips()
    test_same_strike_pairing()
    test_reference_candle()
    test_log_regressions()
    test_ordering_rule()
    test_relaxed_entry()
    test_session_defects()
    test_rest_feed()
    test_late_start_reference()
    test_late_start_catchup()
    test_live_mode()
    test_multi_index()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 72)
    if passed == total:
        print(f"  SIMULATION PASSED — {passed}/{total} checks")
        print("=" * 72)
        return 0
    print(f"  SIMULATION FAILED — {passed}/{total} checks")
    for n, ok, d in _results:
        if not ok:
            print(f"    FAIL: {n}  {d}")
    print("=" * 72)
    return 1


if __name__ == "__main__":
    sys.exit(main())
