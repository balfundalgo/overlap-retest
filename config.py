#!/usr/bin/env python3
"""
The Overlap — Configuration
═══════════════════════════
Strategy Note 03 · v1.2 · Overlap Retest Option Buyer

Every tunable in the strategy note lives here and nowhere else.

Balfund Trading Pvt Ltd | www.balfund.com
"""

import sys
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from typing import Dict, List

# ═══════════════════════════════════════════════════════════════════════════
# BUILD FLAG
# ═══════════════════════════════════════════════════════════════════════════
# When True, no order can reach the exchange whatever the settings say. This
# is the build-level lock; `trade_mode` below is the per-session choice.
PAPER_ONLY_BUILD = False

APP_NAME = "The Overlap"
APP_VERSION = "3.1"
STRATEGY_NOTE_VERSION = "1.6"

# ═══════════════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════════════

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent if "__file__" in globals() else Path.cwd()

ENV_FILE = BASE_DIR / ".env"
SETTINGS_FILE = BASE_DIR / "ovl_settings.json"
STATE_FILE = BASE_DIR / "ovl_daily_state.json"          # legacy single-index


def state_file_for(index: str):
    """Each index keeps its own daily state. Sharing one file would mean the
    second index to start wiped the first one's trade count."""
    return BASE_DIR / f"ovl_daily_state_{index.upper()}.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
# INDEX CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════
# strike_gap is the rounding step used to find the at-the-money strike.
# lot_size is a fallback — the live value is pulled from the scrip master.

INDEX_CONFIG: Dict[str, dict] = {
    "NIFTY": {
        "security_id": "13",
        "index_segment": "IDX_I",
        "option_segment": "NSE_FNO",
        "strike_gap": 50,
        "lot_size": 75,
    },
    "BANKNIFTY": {
        "security_id": "25",
        "index_segment": "IDX_I",
        "option_segment": "NSE_FNO",
        "strike_gap": 100,
        "lot_size": 35,
    },
    "SENSEX": {
        "security_id": "51",
        "index_segment": "IDX_I",
        "option_segment": "BSE_FNO",
        "strike_gap": 100,
        "lot_size": 20,
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OverlapConfig:
    """
    Defaults are the values specified in strategy note v1.2, page 11.
    Anything the note calls configurable is configurable here.
    """

    # ─── The ladder ────────────────────────────────────────────────────────
    # One or more indices. Each runs its own ladder, its own pair, its own
    # trades and its own daily limit — they never share anything but the
    # account. `index` below is the one this config belongs to; a multi-index
    # session hands each engine a copy carrying a single name.
    indices: List[str] = field(default_factory=lambda: ["NIFTY"])
    timeframe_minutes: int = 3          # 3 or 5
    ref_time: dtime = dtime(9, 30, 0)   # ladder freeze
    strikes_each_side: int = 2          # 2 either side of ATM -> 5 strikes
    expiry_offset: int = 0              # 0 = nearest expiry, 1 = next

    # ─── Where candles come from ───────────────────────────────────────────
    # "rest"  — the broker's own 1-minute bars, rolled up. Independent of
    #           whether our websocket was connected.
    # "ticks" — built from the live tick stream, the original behaviour.
    # The websocket is used for live price either way: the retest fill, the
    # target and the safety stop are all judged on traded price.
    candle_source: str = "rest"
    rest_poll: float = 4.0              # seconds between polls
    rest_grace: float = 6.0             # declare a bar closed this long after
    rest_narrow_after_merge: bool = True  # poll only the pair once it is locked
    # On a late start, replay the candles that already happened to find the
    # merge and draw the four lines. Only the merge is replayed — a signal
    # armed an hour ago would have a band nowhere near the current price, so
    # trading decisions always start from the live candle.
    replay_history_on_late_start: bool = True

    # ─── The reference candle ──────────────────────────────────────────────
    # "after"  = the candle that STARTS at ref_time (09:30-09:33 on a 3-min chart)
    # "before" = the candle that ENDS at ref_time   (09:27-09:30)
    ref_candle: str = "after"

    # ─── The overlap ───────────────────────────────────────────────────────
    # "same" = a strike pairs only with its own put/call (24100 CE with 24100 PE)
    # "any"  = any call may pair with any put in the ladder
    pair_mode: str = "same"
    # The two BODIES must cover at least this much common ground. Wicks are
    # ignored. This also guarantees the red close sits at least min_overlap
    # below the green close, so no separate close-gap test is needed.
    min_overlap: float = 0.05
    require_overlap: bool = True        # set False to skip the body-overlap test

    # ─── The signal ────────────────────────────────────────────────────────
    min_body_pct: float = 50.0          # collapsing candle body as % of its range
    # The collapse is what we wait for. Once it happens the other chart is
    # checked, and a break counts whether it came before, with, or after —
    # so long as that leg is still above its line when the pair is made.
    # Set True to require the collapse first, the older behaviour.
    require_collapse_first: bool = False
    # Only used when breaks before the collapse are allowed. A break that has
    # been running for many candles has usually carried price well past the
    # entry band, so the retest never fills. 0 = no limit.
    max_break_age_bars: int = 0
    signal_window_bars: int = 0         # retained for compatibility; unused

    # ─── After a stop ──────────────────────────────────────────────────────
    # A trade that stopped out (either stop) relaxes the NEXT entry: the
    # collapse is no longer required, only a break and a retest. A trade that
    # reached its target does not — the next entry is the full setup again.
    relaxed_after_stop: bool = True
    # Do not take the relaxed entry while the opposite chart is already above
    # its own borrowed high, since the stop would fire on the next candle.
    guard_relaxed_entry: bool = True

    # ─── The entry ─────────────────────────────────────────────────────────
    # The buffer is a tolerance around the borrowed high for the RETURN only.
    # The breakout itself is judged on the borrowed high alone — see note p.6/7.
    retest_buffer: float = 4.0          # points of premium either side

    # ─── The exit ──────────────────────────────────────────────────────────
    # The structural stop needs BOTH charts to agree on the same closed candle:
    # the other leg closing above its borrowed high, AND the leg we hold closing
    # below its own borrowed low. Set False for the older one-sided behaviour,
    # where the other chart alone ended the trade.
    two_sided_stop: bool = True
    target_pct: float = 15.0            # above entry premium
    safety_stop_enabled: bool = True
    safety_stop_pct: float = 25.0       # below entry premium

    # ─── Trade control ─────────────────────────────────────────────────────
    lots_per_entry: int = 1
    max_trades_per_day: int = 2
    # Once a call has been bought, no second call that day; the same for puts.
    # So with two trades allowed, a day can hold at most one of each side.
    one_trade_per_side: bool = True
    last_entry_time: dtime = dtime(15, 15, 0)
    square_off_time: dtime = dtime(15, 15, 0)

    # ─── Execution ─────────────────────────────────────────────────────────
    # "paper" simulates every fill at the traded price. "live" sends real
    # orders to the exchange. Nothing else about the strategy changes.
    trade_mode: str = "paper"

    # ─── Live safeguards ───────────────────────────────────────────────────
    # How often to ask the broker what we actually hold. Catches a manual
    # square-off, a broker auto-square-off, or a fill we never saw.
    position_sync_seconds: int = 15
    # An exit that fails leaves us holding a position while believing we are
    # flat, which is the worst state to be in. Keep trying.
    exit_retry_attempts: int = 5
    # Refuse an entry if the premium has moved further than this from the
    # price that triggered the retest.
    max_entry_slippage_pct: float = 3.0
    # How many times an entry may be attempted for one armed signal before
    # giving up on it. Every tick inside the band would otherwise trigger a
    # fresh attempt, and each attempt is several orders — on 09-Sep five
    # attempts went out in fifteen seconds.
    max_entry_attempts: int = 3
    # Minimum gap between those attempts.
    entry_retry_seconds: float = 10.0

    @property
    def paper_mode(self) -> bool:
        return PAPER_ONLY_BUILD or self.trade_mode != "live"

    @property
    def is_live(self) -> bool:
        return not self.paper_mode

    # ─── Derived ───────────────────────────────────────────────────────────
    @property
    def index(self) -> str:
        """The index this config is for. With several selected this is the
        first; each engine is given its own single-index copy."""
        return self.indices[0] if self.indices else "NIFTY"

    def for_index(self, name: str) -> "OverlapConfig":
        """A copy of these settings carrying exactly one index."""
        c = OverlapConfig.from_dict(self.to_dict())
        c.indices = [name]
        c.trade_mode = self.trade_mode      # never restored by from_dict
        return c

    @property
    def state_file(self):
        return state_file_for(self.index)

    @property
    def interval_seconds(self) -> int:
        return int(self.timeframe_minutes) * 60

    @property
    def strike_gap(self) -> int:
        return INDEX_CONFIG[self.index]["strike_gap"]

    @property
    def option_segment(self) -> str:
        return INDEX_CONFIG[self.index]["option_segment"]

    @property
    def index_security_id(self) -> str:
        return INDEX_CONFIG[self.index]["security_id"]

    @property
    def lot_size(self) -> int:
        return INDEX_CONFIG[self.index]["lot_size"]

    @property
    def quantity(self) -> int:
        return int(self.lots_per_entry) * int(self.lot_size)

    def validate(self):
        errs = []
        if not self.indices:
            errs.append("Choose at least one index")
        for ix in self.indices:
            if ix not in INDEX_CONFIG:
                errs.append(f"Unknown index: {ix}")
        if len(set(self.indices)) != len(self.indices):
            errs.append("The same index is selected more than once")
        if self.timeframe_minutes not in (3, 5):
            errs.append("Timeframe must be 3 or 5 minutes")
        if self.strikes_each_side < 1:
            errs.append("Strikes each side must be at least 1")
        if self.candle_source not in ("rest", "ticks"):
            errs.append("Candle source must be 'rest' or 'ticks'")
        if self.rest_poll <= 0:
            errs.append("REST poll interval must be positive")
        if self.rest_grace < 0:
            errs.append("REST grace cannot be negative")
        if self.ref_candle not in ("after", "before"):
            errs.append("Reference candle must be 'after' or 'before'")
        if self.pair_mode not in ("same", "any"):
            errs.append("Pair mode must be 'same' or 'any'")
        if self.min_overlap < 0:
            errs.append("Minimum overlap cannot be negative")
        if not (0 <= self.min_body_pct <= 100):
            errs.append("Minimum body must be between 0 and 100 percent")
        if self.signal_window_bars < 0:
            errs.append("Signal window cannot be negative")
        if self.retest_buffer < 0:
            errs.append("Retest buffer cannot be negative")
        if self.target_pct <= 0:
            errs.append("Target must be greater than zero")
        if self.trade_mode not in ("paper", "live"):
            errs.append("Trade mode must be 'paper' or 'live'")
        if self.position_sync_seconds < 5:
            errs.append("Position sync interval must be at least 5 seconds")
        if self.exit_retry_attempts < 1:
            errs.append("Exit retries must be at least 1")
        if self.max_break_age_bars < 0:
            errs.append("Max break age cannot be negative")
        if self.max_entry_attempts < 1:
            errs.append("Entry attempts must be at least 1")
        if self.entry_retry_seconds < 0:
            errs.append("Entry retry gap cannot be negative")
        if not (0 < self.max_entry_slippage_pct <= 100):
            errs.append("Max entry slippage must be between 0 and 100 percent")
        if self.safety_stop_enabled and not (0 < self.safety_stop_pct < 100):
            errs.append("Safety stop must be between 0 and 100 percent")
        if self.lots_per_entry < 1:
            errs.append("Lots per entry must be at least 1")
        if self.max_trades_per_day < 1:
            errs.append("Max trades per day must be at least 1")
        if self.ref_time >= self.last_entry_time:
            errs.append("Reference time must be before the last entry time")
        return errs

    # ─── Persistence ───────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "indices": list(self.indices),
            "timeframe_minutes": self.timeframe_minutes,
            "ref_time": self.ref_time.strftime("%H:%M:%S"),
            "strikes_each_side": self.strikes_each_side,
            "expiry_offset": self.expiry_offset,
            "candle_source": self.candle_source,
            "rest_poll": self.rest_poll,
            "rest_grace": self.rest_grace,
            "rest_narrow_after_merge": self.rest_narrow_after_merge,
            "replay_history_on_late_start": self.replay_history_on_late_start,
            "ref_candle": self.ref_candle,
            "pair_mode": self.pair_mode,
            "min_overlap": self.min_overlap,
            "require_overlap": self.require_overlap,
            "min_body_pct": self.min_body_pct,
            "require_collapse_first": self.require_collapse_first,
            "max_break_age_bars": self.max_break_age_bars,
            "signal_window_bars": self.signal_window_bars,
            "relaxed_after_stop": self.relaxed_after_stop,
            "guard_relaxed_entry": self.guard_relaxed_entry,
            "retest_buffer": self.retest_buffer,
            "target_pct": self.target_pct,
            "two_sided_stop": self.two_sided_stop,
            "safety_stop_enabled": self.safety_stop_enabled,
            "safety_stop_pct": self.safety_stop_pct,
            "lots_per_entry": self.lots_per_entry,
            "max_trades_per_day": self.max_trades_per_day,
            "one_trade_per_side": self.one_trade_per_side,
            "last_entry_time": self.last_entry_time.strftime("%H:%M:%S"),
            "square_off_time": self.square_off_time.strftime("%H:%M:%S"),
            "trade_mode": self.trade_mode,
            "position_sync_seconds": self.position_sync_seconds,
            "exit_retry_attempts": self.exit_retry_attempts,
            "max_entry_slippage_pct": self.max_entry_slippage_pct,
            "max_entry_attempts": self.max_entry_attempts,
            "entry_retry_seconds": self.entry_retry_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OverlapConfig":
        def _t(v, default):
            try:
                parts = [int(x) for x in str(v).split(":")]
                while len(parts) < 3:
                    parts.append(0)
                return dtime(parts[0], parts[1], parts[2])
            except Exception:
                return default

        c = cls()
        ix = d.get("indices")
        if not ix:
            one = d.get("index")            # settings files from before this
            ix = [one] if one else None
        c.indices = list(ix) if ix else c.indices
        c.timeframe_minutes = int(d.get("timeframe_minutes", c.timeframe_minutes))
        c.ref_time = _t(d.get("ref_time"), c.ref_time)
        c.strikes_each_side = int(d.get("strikes_each_side", c.strikes_each_side))
        c.expiry_offset = int(d.get("expiry_offset", c.expiry_offset))
        c.candle_source = str(d.get("candle_source", c.candle_source))
        c.rest_poll = float(d.get("rest_poll", c.rest_poll))
        c.rest_grace = float(d.get("rest_grace", c.rest_grace))
        c.rest_narrow_after_merge = bool(
            d.get("rest_narrow_after_merge", c.rest_narrow_after_merge))
        c.replay_history_on_late_start = bool(
            d.get("replay_history_on_late_start", c.replay_history_on_late_start))
        c.ref_candle = str(d.get("ref_candle", c.ref_candle))
        c.pair_mode = str(d.get("pair_mode", c.pair_mode))
        # "min_gap" is the old key; read it so existing settings files still load
        c.min_overlap = float(d.get("min_overlap", d.get("min_gap", c.min_overlap)))
        c.require_overlap = bool(d.get("require_overlap", c.require_overlap))
        c.min_body_pct = float(d.get("min_body_pct", c.min_body_pct))
        c.require_collapse_first = bool(d.get("require_collapse_first",
                                              c.require_collapse_first))
        c.max_break_age_bars = int(d.get("max_break_age_bars",
                                         c.max_break_age_bars))
        c.signal_window_bars = int(d.get("signal_window_bars", c.signal_window_bars))
        c.relaxed_after_stop = bool(d.get("relaxed_after_stop", c.relaxed_after_stop))
        c.guard_relaxed_entry = bool(d.get("guard_relaxed_entry", c.guard_relaxed_entry))
        c.retest_buffer = float(d.get("retest_buffer", c.retest_buffer))
        c.target_pct = float(d.get("target_pct", c.target_pct))
        c.two_sided_stop = bool(d.get("two_sided_stop", c.two_sided_stop))
        c.safety_stop_enabled = bool(d.get("safety_stop_enabled", c.safety_stop_enabled))
        c.safety_stop_pct = float(d.get("safety_stop_pct", c.safety_stop_pct))
        c.lots_per_entry = int(d.get("lots_per_entry", c.lots_per_entry))
        c.max_trades_per_day = int(d.get("max_trades_per_day", c.max_trades_per_day))
        c.one_trade_per_side = bool(d.get("one_trade_per_side", c.one_trade_per_side))
        c.last_entry_time = _t(d.get("last_entry_time"), c.last_entry_time)
        c.square_off_time = _t(d.get("square_off_time"), c.square_off_time)
        # Live is never restored from a settings file. It has to be chosen,
        # and confirmed, every session.
        c.trade_mode = "paper"
        c.position_sync_seconds = int(d.get("position_sync_seconds",
                                            c.position_sync_seconds))
        c.exit_retry_attempts = int(d.get("exit_retry_attempts",
                                          c.exit_retry_attempts))
        c.max_entry_slippage_pct = float(d.get("max_entry_slippage_pct",
                                               c.max_entry_slippage_pct))
        c.max_entry_attempts = int(d.get("max_entry_attempts",
                                         c.max_entry_attempts))
        c.entry_retry_seconds = float(d.get("entry_retry_seconds",
                                            c.entry_retry_seconds))
        return c


# ═══════════════════════════════════════════════════════════════════════════
# GUI PALETTE
# ═══════════════════════════════════════════════════════════════════════════
# Taken from the strategy note so the app and the document look related.

PALETTE = {
    "bg":          "#eef1f5",
    "card":        "#ffffff",
    "card_alt":    "#f7f9fb",
    "navy":        "#1b3a5c",
    "accent":      "#2b6cb0",
    "accent_soft": "#e3edf7",
    "text":        "#1b2a38",
    "dim":         "#6b7783",
    "border":      "#d8dee4",

    "green":       "#2f7d4f",
    "green_soft":  "#e4f2e9",
    "red":         "#b03a3a",
    "red_soft":    "#f8e7e7",
    "gold":        "#b5820f",
    "gold_soft":   "#fdf3dd",
    "violet":      "#6b46c1",
    "violet_soft": "#ede8f8",
    "teal":        "#0e7490",
    "teal_soft":   "#e0f2f7",

    "pending":     "#9aa5ae",
    "pending_soft":"#f1f3f5",
}

FONTS = {
    "title":   ("Segoe UI", 19, "bold"),
    "head":    ("Segoe UI", 14, "bold"),
    "mid":     ("Segoe UI", 12, "bold"),
    "body":    ("Segoe UI", 11),
    "small":   ("Segoe UI", 10),
    "tiny":    ("Segoe UI", 9),
    "mono":    ("Consolas", 10),
    "mono_sm": ("Consolas", 9),
    "big":     ("Segoe UI", 24, "bold"),
}
