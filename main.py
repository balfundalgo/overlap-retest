#!/usr/bin/env python3
"""
The Overlap — Entry Point
═════════════════════════
    python main.py           launch the application
    python main.py --test    run internal checks and exit
    python main.py --console run headless, no GUI (credentials from .env)

Balfund Trading Pvt Ltd | www.balfund.com
"""

import logging
import os
import sys
from datetime import datetime

from config import (APP_NAME, APP_VERSION, LOG_DIR, PAPER_ONLY_BUILD,
                    STRATEGY_NOTE_VERSION)


def _setup_logging():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(str(LOG_DIR / f"ovl_activity_{ts}.log"),
                                      encoding="utf-8"),
                  logging.StreamHandler()],
    )


# ═══════════════════════════════════════════════════════════════════════════
# SELF TEST
# ═══════════════════════════════════════════════════════════════════════════

def run_tests() -> int:
    print("=" * 72)
    print(f"  {APP_NAME} v{APP_VERSION} — internal checks")
    print(f"  Strategy note v{STRATEGY_NOTE_VERSION}")
    print("=" * 72)

    failures = []

    def ck(name, ok, detail=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # ── imports ──
    try:
        import customtkinter  # noqa: F401
        ck("customtkinter imports", True)
    except Exception as e:
        ck("customtkinter imports", False, str(e))
    for mod in ("requests", "pyotp", "websocket", "dotenv"):
        try:
            __import__(mod)
            ck(f"{mod} imports", True)
        except Exception as e:
            ck(f"{mod} imports", False, str(e))

    # ── project modules ──
    try:
        from config import INDEX_CONFIG, OverlapConfig
        import dhan_api
        from candles import Candle, CandleBook, CandleSeries
        from strategy import OverlapEngine
        ck("project modules import", True)
    except Exception as e:
        ck("project modules import", False, str(e))
        print("\n  ABORTED — cannot continue without the project modules.")
        return 1

    # ── config ──
    cfg = OverlapConfig()
    ck("default settings validate", cfg.validate() == [])
    ck("a fresh config starts in paper mode",
       cfg.trade_mode == "paper" and cfg.paper_mode is True)
    live = OverlapConfig(trade_mode="live")
    ck("live mode is available" if not PAPER_ONLY_BUILD
       else "the build lock forces paper",
       (live.is_live is True) if not PAPER_ONLY_BUILD
       else (live.paper_mode is True))
    ck("live is never restored from a settings file",
       OverlapConfig.from_dict({"trade_mode": "live"}).trade_mode == "paper")
    ck("three indices configured",
       set(INDEX_CONFIG) == {"NIFTY", "BANKNIFTY", "SENSEX"},
       ", ".join(INDEX_CONFIG))
    ck("strike gaps are right",
       INDEX_CONFIG["NIFTY"]["strike_gap"] == 50
       and INDEX_CONFIG["BANKNIFTY"]["strike_gap"] == 100
       and INDEX_CONFIG["SENSEX"]["strike_gap"] == 100)

    # ── every control in the panel must reach a real setting ──
    # A renamed field once left "Minimum gap" writing to nothing, so the
    # value in the panel was silently ignored.
    try:
        import re as _re
        from dataclasses import fields as _fields
        _app = open(__file__.replace("main.py", "app.py"), encoding="utf-8").read()
        _seg = _app[_app.index("def _collect_config"):_app.index("def _save_settings")]
        _valid = {f.name for f in _fields(OverlapConfig())}
        _stray = sorted(set(_re.findall(r"\bc\.([a-z_]+)\s*=", _seg)) - _valid)
        ck("every settings control writes to a real field", not _stray,
           f"orphaned: {', '.join(_stray)}" if _stray else "")
    except Exception as _e:
        ck("settings wiring check ran", False, str(_e))

    # ── strike rounding, per the note ──
    r = OverlapEngine.round_to_strike
    ck("24,637 rounds to 24,650", r(24637, 50) == 24650)
    ck("24,623 rounds to 24,600", r(24623, 50) == 24600)
    ck("24,660 rounds to 24,650", r(24660, 50) == 24650)
    ck("BANKNIFTY 52,340 rounds to 52,300", r(52340, 100) == 52300)

    # ── candle mechanics ──
    c = Candle(0, 100.0)
    c.update(104.0); c.update(98.0); c.update(101.0)
    ck("candle tracks OHLC",
       c.open == 100.0 and c.high == 104.0 and c.low == 98.0 and c.close == 101.0)
    ck("candle colour", c.is_green and not c.is_red)

    a = Candle(0, 210.0); a.high, a.low, a.close = 215.6, 210.8, 211.4; a.open = 215.2
    b = Candle(0, 209.8); b.high, b.low, b.close = 213.2, 208.9, 212.1; b.open = 209.8
    ck("overlapping ranges detected", a.overlaps(b))
    d = Candle(0, 31.0); d.high, d.low, d.close, d.open = 33.4, 30.9, 31.2, 33.0
    ck("distant ranges not overlapping", not a.overlaps(d))

    body = Candle(0, 207.1); body.open, body.high, body.low, body.close = 207.1, 207.4, 202.6, 203.2
    ck("body percentage", abs(body.body_pct - 81.25) < 0.5, f"{body.body_pct:.1f}%")

    # ── bucket alignment ──
    book = CandleBook(180)
    ck("3-minute buckets align to the 09:15 open", 13500 % 180 == 0)
    ck("5-minute buckets align to the 09:15 open", 13500 % 300 == 0)
    ck("bucket maths", book.bucket_for(13500 + 200) == 13500 + 180)

    # ── series finalises a bucket even with no follow-on tick ──
    s = CandleSeries("X", "test", 180)
    s.on_tick(100.0, 13500)
    s.on_tick(105.0, 13600)
    fin = s.finalise(13500)
    ck("finalise closes the live bucket", fin is not None and fin.high == 105.0)

    # ── engine constructs ──
    try:
        eng = OverlapEngine(cfg, gui_callback=lambda e, d: None)
        ck("engine constructs", True)
        ck("seven conditions defined", len(eng.conditions) == 7,
           ", ".join(eng.conditions))
    except Exception as e:
        ck("engine constructs", False, str(e))

    print("=" * 72)
    if failures:
        print(f"  {len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        print("=" * 72)
        return 1
    print("  ALL CHECKS PASSED")
    print("=" * 72)
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# CONSOLE MODE
# ═══════════════════════════════════════════════════════════════════════════

def run_console() -> int:
    from dotenv import load_dotenv
    from config import ENV_FILE, OverlapConfig, SETTINGS_FILE
    import dhan_api as api
    from strategy import OverlapEngine
    import json

    _setup_logging()
    load_dotenv(str(ENV_FILE), override=True)

    cid = os.getenv("DHAN_CLIENT_ID", "")
    pin = os.getenv("DHAN_PIN", "")
    totp = os.getenv("DHAN_TOTP_SECRET", "")
    if not (cid and pin and totp):
        print("Set DHAN_CLIENT_ID, DHAN_PIN and DHAN_TOTP_SECRET in .env first.")
        return 1

    cfg = OverlapConfig()
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                cfg = OverlapConfig.from_dict(json.load(f))
        except Exception:
            pass

    api.set_credentials(cid, pin, totp, os.getenv("DHAN_ACCESS_TOKEN", ""))
    api.init_credentials()

    def on_event(event, data):
        if event == "log":
            print(f"{data.get('time', '')}  {data.get('msg', '')}")

    engine = OverlapEngine(cfg, gui_callback=on_event)
    try:
        engine.run()
    except KeyboardInterrupt:
        engine.stop()
    return 0


# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    if "--test" in args or "-t" in args:
        return run_tests()
    if "--console" in args or "-c" in args:
        return run_console()

    _setup_logging()
    try:
        from app import main as gui_main
    except Exception as e:
        print(f"Could not start the interface: {e}")
        print("Run 'pip install -r requirements.txt' and try again.")
        return 1
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
