#!/usr/bin/env python3
"""
The Overlap — Strategy Engine
═════════════════════════════
Implements Strategy Note 03 v1.2 exactly.

    09:30      Ten charts locked — 5 strikes, CE and PE at each
    Overlap    One CE and one PE candle, opposite colours, ranges literally
               overlapping, red close at least 5 paise below green close
    Frame      Each chart borrows the other's high and low from that candle
    Signal     One chart's red candle lies wholly below its borrowed low with
               a body of at least half the candle, while the other closes a
               green candle above its borrowed high
    Entry      Buy the breakout leg when price returns into a band of four
               points either side of that same borrowed high
    Exit       15% target on our own premium, or the collapsed chart closing
               a candle back above its borrowed high, or 15:15

Balfund Trading Pvt Ltd | www.balfund.com
"""

import csv
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from enum import Enum
from typing import Dict, List, Optional, Tuple

import websocket

from candles import Candle, CandleBook
from restfeed import RestCandleFeed
from config import (INDEX_CONFIG, LOG_DIR, PAPER_ONLY_BUILD, STATE_FILE,
                    OverlapConfig)
import dhan_api as api

log = logging.getLogger("Overlap")


# ═══════════════════════════════════════════════════════════════════════════
# ENUMS AND STATE
# ═══════════════════════════════════════════════════════════════════════════

class Phase(Enum):
    IDLE = "IDLE"
    WAITING_REF = "WAITING FOR 09:30"
    SCANNING = "SCANNING FOR OVERLAP"
    FRAMED = "FRAME DRAWN"
    ARMED = "ARMED — WAITING RETEST"
    IN_TRADE = "IN TRADE"
    DONE = "DONE FOR THE DAY"
    STOPPED = "STOPPED"


class CondStatus(Enum):
    IDLE = "idle"          # not relevant yet
    WATCHING = "watching"  # actively being tested
    MET = "met"            # satisfied
    FAILED = "failed"      # tested and lost


@dataclass
class Leg:
    """One option instrument in the ladder."""
    sec_id: str
    strike: float
    side: str          # "CE" or "PE"
    label: str
    segment: str

    @property
    def key(self) -> str:
        return f"{int(self.strike)}{self.side}"


@dataclass
class Frame:
    """The four lines, drawn once from the overlapping candle."""
    bucket: int
    ce: Leg
    pe: Leg
    ce_candle: dict
    pe_candle: dict
    # what each chart borrows FROM the other
    ce_borrowed_high: float   # = pe candle high, drawn on the CE chart
    ce_borrowed_low: float    # = pe candle low
    pe_borrowed_high: float   # = ce candle high, drawn on the PE chart
    pe_borrowed_low: float    # = ce candle low

    def borrowed_high(self, side: str) -> float:
        return self.ce_borrowed_high if side == "CE" else self.pe_borrowed_high

    def borrowed_low(self, side: str) -> float:
        return self.ce_borrowed_low if side == "CE" else self.pe_borrowed_low

    def leg(self, side: str) -> Leg:
        return self.ce if side == "CE" else self.pe

    def other(self, side: str) -> str:
        return "PE" if side == "CE" else "CE"


@dataclass
class Signal:
    """An armed setup waiting for its retest."""
    bucket: int
    buy_side: str            # side we will buy — the leg that broke out
    collapse_side: str       # side that fell out of the bottom
    breakout_level: float    # the borrowed high the breakout closed above
    band_low: float
    band_high: float
    collapse_bucket: int
    breakout_bucket: int
    armed_at: str
    attempts: int = 0
    last_attempt: float = 0.0


@dataclass
class Trade:
    trade_no: int
    side: str
    leg_label: str
    sec_id: str
    qty: int
    entry_price: float
    entry_time: str
    target: float
    safety_stop: float
    breakout_level: float
    collapse_side: str
    order_id: str = ""
    exit_price: float = 0.0
    exit_time: str = ""
    exit_reason: str = ""
    pnl: float = 0.0
    is_open: bool = True


@dataclass
class Condition:
    key: str
    title: str
    subtitle: str
    status: CondStatus = CondStatus.IDLE
    detail: str = "—"
    value: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "subtitle": self.subtitle,
                "status": self.status.value, "detail": self.detail,
                "value": self.value}


# ═══════════════════════════════════════════════════════════════════════════
# TRADE LOG
# ═══════════════════════════════════════════════════════════════════════════

class TradeLogger:
    FIELDS = ["date", "trade_no", "index", "leg", "side", "qty",
              "entry_time", "entry_price", "exit_time", "exit_price",
              "reason", "points", "pnl", "breakout_level", "collapse_side",
              "mode"]

    def __init__(self):
        self.path = LOG_DIR / f"ovl_trades_{datetime.now().strftime('%Y%m%d')}.csv"
        self._lock = threading.Lock()
        if not self.path.exists():
            try:
                with open(self.path, "w", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()
            except Exception as e:
                log.error(f"Trade log init failed: {e}")

    def write(self, row: dict):
        with self._lock:
            try:
                with open(self.path, "a", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=self.FIELDS).writerow(
                        {k: row.get(k, "") for k in self.FIELDS})
            except Exception as e:
                log.error(f"Trade log write failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# THE ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class OverlapEngine:

    BAR_GRACE_SECONDS = 2

    def __init__(self, config: Optional[OverlapConfig] = None, gui_callback=None):
        self.cfg = config or OverlapConfig()
        if PAPER_ONLY_BUILD:
            self.cfg.trade_mode = "paper"
        self.gui = gui_callback

        self.book = CandleBook(self.cfg.interval_seconds)
        self.trade_logger = TradeLogger()

        # ladder
        self.expiry: Optional[str] = None
        self.atm_strike: Optional[float] = None
        self.ref_spot: Optional[float] = None
        self.legs: Dict[str, Leg] = {}          # sec_id -> Leg
        self.ce_legs: List[Leg] = []
        self.pe_legs: List[Leg] = []
        self.ladder_built = False

        # frame / signal / trade
        self.frame: Optional[Frame] = None
        self.merge_label: str = ""
        self.merge_gap: float = 0.0
        self.merge_red_side: str = ""
        self.signal: Optional[Signal] = None
        self.trade: Optional[Trade] = None
        self.closed_trades: List[Trade] = []
        self.trade_count = 0
        # "full" = collapse and break required. "relaxed" = break only, set
        # after a stop and reset after a target. Survives a restart.
        self.entry_mode: str = "full"

        # pending half-signals, for the signal window
        self._pending_collapse: Dict[str, int] = {}   # side -> bucket
        self._pending_breakout: Dict[str, int] = {}   # side -> bucket
        self._break_since: Dict[str, int] = {}        # side -> first bucket of the run
        self._order_warned: Dict[str, bool] = {}      # so the notice is logged once
        self._guard_warned: Dict[str, bool] = {}      # same, for the relaxed guard

        # runtime
        self.phase = Phase.IDLE
        self.spot_ltp: Optional[float] = None
        self.last_processed_bucket: int = 0
        self.packet_count = 0
        self.ws = None
        self.ws_connected = threading.Event()
        self.stop_event = threading.Event()
        # Dropped after square-off. The 01/09 session kept reconnecting until
        # 17:20 with nothing left to listen to.
        self.feed_stop = threading.Event()

        # REST candle feed (used when candle_source == "rest")
        self.rest: Optional[RestCandleFeed] = None
        self._rest_bars: Dict[int, set] = {}   # bucket -> sec_ids delivered
        self.last_lag: float = 0.0
        self._first_bar_seen = False
        self._catchup_done = False
        self._catchup_started = 0.0
        # Set for the whole duration of an order round trip. In live mode the
        # broker takes seconds to answer, and without this two ticks arriving
        # in that window would both send a BUY.
        self._order_in_progress = False
        self._last_position_sync = 0.0
        self._orphan_warned = False
        # Set when the broker refuses orders for a reason only a person
        # can fix. Nothing further is attempted this session.
        self.trading_halted = False
        self.halt_reason = ""
        self._lock = threading.RLock()
        self.status_msg = "Idle"

        # Set when more than one index is running, so log lines can be told
        # apart. A single-index session reads exactly as it did before.
        self.tag_logs = False
        self.conditions: Dict[str, Condition] = self._make_conditions()

    # ─── Conditions ────────────────────────────────────────────────────────

    def _make_conditions(self) -> Dict[str, Condition]:
        c = [
            Condition("ladder", "The ladder of ten",
                      "Index read at 09:30 · five strikes · CE and PE at each"),
            Condition("overlap", "The overlap",
                      "The strike's call and put — bodies covering the same price, wicks ignored"),
            Condition("frame", "The four lines",
                      "Each chart borrows the other's high and low"),
            Condition("collapse", "The collapse",
                      "One leg dies: red candle, whole candle including wicks below its borrowed low, body at least half the candle"),
            Condition("breakout", "The break",
                      "The other leg wins: green candle whose close is above its borrowed high. Close only — the candle need not clear it"),
            Condition("retest", "The retest",
                      "Price back inside the band around that same high"),
            Condition("exit", "The exit watch",
                      "Target on our premium · stop needs both charts to agree"),
        ]
        return {x.key: x for x in c}

    def _cond(self, key: str, status: CondStatus = None,
              detail: str = None, value: str = None):
        c = self.conditions.get(key)
        if not c:
            return
        if status is not None:
            c.status = status
        if detail is not None:
            c.detail = detail
        if value is not None:
            c.value = value

    def _reset_conditions_from(self, *keys):
        for k in keys:
            self._cond(k, CondStatus.IDLE, "—", "")

    # ─── Emit ──────────────────────────────────────────────────────────────

    def _emit(self, event: str, data: dict = None):
        if self.gui:
            try:
                payload = dict(data or {})
                payload.setdefault("index", self.cfg.index)
                self.gui(event, payload)
            except Exception as e:
                log.error(f"GUI callback error: {e}")

    def _say(self, msg: str, level: str = "info"):
        # The file log carries the index too, so a three-index session can be
        # read back apart afterwards.
        getattr(log, level if level in ("info", "warning", "error") else "info")(
            f"[{self.cfg.index}] {msg}" if self.tag_logs else msg)
        self._emit("log", {"msg": msg, "level": level,
                           "time": api.now_ist().strftime("%H:%M:%S")})

    def _set_phase(self, phase: Phase, msg: str = ""):
        self.phase = phase
        if msg:
            self.status_msg = msg
        self._emit("phase", {"phase": phase.value, "msg": self.status_msg})

    # ═══════════════════════════════════════════════════════════════════════
    # LADDER
    # ═══════════════════════════════════════════════════════════════════════

    def _ref_epoch_today(self) -> int:
        n = api.now_ist()
        ref = n.replace(hour=self.cfg.ref_time.hour,
                        minute=self.cfg.ref_time.minute,
                        second=0, microsecond=0)
        return int(ref.timestamp())

    def ref_bucket(self) -> int:
        """The bucket of the reference candle itself.

        "after"  — the candle that STARTS at the reference time, so on a
                   3-minute chart with ref 09:30 that is 09:30-09:33 and the
                   ladder is locked at 09:33.
        "before" — the candle that ENDS at the reference time (09:27-09:30).
        """
        ref_ep = self._ref_epoch_today()
        if self.cfg.ref_candle == "before":
            return ref_ep - self.cfg.interval_seconds
        return ref_ep

    def _ladder_ready_epoch(self) -> int:
        """When the reference candle has finished and the ladder can be built."""
        return self.ref_bucket() + self.cfg.interval_seconds

    def _reference_spot(self) -> Optional[float]:
        """The index close of the reference candle.

        Tried in order:
          1. the candle we built from ticks, if we were running at the time
          2. the index's own 1-minute history from the broker
          3. the live index price, as a last resort

        Step 2 matters on a late start. Without it the strike is chosen from
        whatever the index happens to be when the app comes up, which is not
        the number the strategy is defined on. On 09-Sep the app started at
        09:34 and picked the ladder from the 09:34:43 price rather than the
        09:33 close.
        """
        bucket = self.ref_bucket()

        idx_series = self.book.get(self.cfg.index_security_id)
        if idx_series is not None:
            c = idx_series.finalise(bucket)
            if c is not None:
                self._say(f"Reference candle {self._ref_window_label()}: "
                          f"close {c.close:.2f}")
                return c.close

        close = self._reference_spot_from_history(bucket)
        if close is not None:
            self._say(f"Reference candle {self._ref_window_label()}: "
                      f"close {close:.2f} (read from the index's own history — "
                      f"we were not running when it formed)")
            return close

        if idx_series is not None and idx_series.ltp:
            self._say("The index history did not return the reference candle "
                      "— falling back to the live index price. The ladder may "
                      "not match what the strategy would have chosen at "
                      f"{self.cfg.ref_time.strftime('%H:%M')}.", "warning")
            return idx_series.ltp
        return self.spot_ltp

    def _reference_spot_from_history(self, bucket: int) -> Optional[float]:
        """Close of the reference candle, from the index's 1-minute history."""
        try:
            seg = INDEX_CONFIG[self.cfg.index]["index_segment"]
            mins = api.fetch_intraday(self.cfg.index_security_id, seg,
                                      "INDEX", "1", days=1)
            if not mins:
                return None
            iv = self.cfg.interval_seconds
            members = [m for m in mins
                       if bucket <= int(m.get("timestamp", 0)) < bucket + iv]
            if not members:
                return None
            members.sort(key=lambda m: int(m["timestamp"]))
            # The candle must be finished — its last minute has to have closed.
            if int(members[-1]["timestamp"]) + 60 > int(time.time()):
                return None
            return float(members[-1]["close"])
        except Exception as e:
            log.warning(f"  Index history lookup failed: {e}")
            return None

    def _ref_window_label(self) -> str:
        b0 = self.ref_bucket()
        return (f"{api.ist_bucket_label(b0)}-"
                f"{api.ist_bucket_label(b0 + self.cfg.interval_seconds)}")

    @staticmethod
    def round_to_strike(price: float, gap: int) -> float:
        return float(round(price / gap) * gap)

    def build_ladder(self) -> bool:
        """Read the index, choose five strikes, open ten charts."""
        with self._lock:
            if self.ladder_built:
                return True

        self._cond("ladder", CondStatus.WATCHING, "Reading the index...")
        self._emit("conditions", self.conditions_payload())

        spot = self._reference_spot()
        if not spot:
            self._say("No index price available — cannot build the ladder yet.", "warning")
            self._cond("ladder", CondStatus.WATCHING, "Waiting for an index price")
            return False

        gap = self.cfg.strike_gap
        atm = self.round_to_strike(spot, gap)
        self.ref_spot = spot
        self.atm_strike = atm

        oc = api.fetch_option_chain(self.cfg.index, self.expiry)
        if not oc:
            self._say("Option chain unavailable — retrying shortly.", "warning")
            return False

        n = self.cfg.strikes_each_side
        wanted = [atm + (i * gap) for i in range(-n, n + 1)]
        seg = self.cfg.option_segment

        ce_legs, pe_legs = [], []
        missing = []
        for sk in wanted:
            entry = None
            for k, v in oc["oc"].items():
                try:
                    if abs(float(k) - sk) < 0.001:
                        entry = v
                        break
                except Exception:
                    continue
            if entry is None:
                missing.append(sk)
                continue
            for side, key in (("CE", "ce"), ("PE", "pe")):
                od = entry.get(key)
                if not od or not od.get("security_id"):
                    missing.append(f"{int(sk)}{side}")
                    continue
                leg = Leg(sec_id=str(od["security_id"]), strike=sk, side=side,
                          label=f"{self.cfg.index} {int(sk)} {side}", segment=seg)
                (ce_legs if side == "CE" else pe_legs).append(leg)

        if len(ce_legs) < 2 or len(pe_legs) < 2:
            self._say(f"Only found {len(ce_legs)} CE and {len(pe_legs)} PE strikes — "
                      f"cannot build a usable ladder.", "error")
            self._cond("ladder", CondStatus.FAILED, "Not enough strikes in the chain")
            return False

        if missing:
            self._say(f"Strikes not found in the chain: {missing}", "warning")

        ce_legs.sort(key=lambda l: l.strike)
        pe_legs.sort(key=lambda l: l.strike)

        with self._lock:
            self.ce_legs = ce_legs
            self.pe_legs = pe_legs
            self.legs = {l.sec_id: l for l in (ce_legs + pe_legs)}
            for l in ce_legs + pe_legs:
                self.book.add(l.sec_id, l.label)
            self.ladder_built = True

        self._say(f"Ladder locked. Index {spot:.2f} rounds to {int(atm)}. "
                  f"{len(ce_legs)} calls and {len(pe_legs)} puts on {self.expiry}.")
        self._cond("ladder", CondStatus.MET,
                   f"Index {spot:.2f} → ATM {int(atm)} · expiry {self.expiry}",
                   f"{len(ce_legs) + len(pe_legs)} charts")
        self._cond("overlap", CondStatus.WATCHING, "Comparing every call against every put")

        self._subscribe_ladder()
        self._seed_history()
        self._start_rest_feed()
        self._emit("ladder", self.ladder_payload())
        self._set_phase(Phase.SCANNING, "Scanning for the overlap")
        return True

    def _subscribe_ladder(self):
        if not (self.ws_connected.is_set() and self.ws):
            return
        instruments = [{"ExchangeSegment": l.segment, "SecurityId": l.sec_id}
                       for l in self.legs.values()]
        try:
            self.ws.send(api.build_subscribe_message(instruments))
            self._say(f"Subscribed to {len(instruments)} option instruments.")
        except Exception as e:
            self._say(f"Subscribe failed: {e}", "error")

    def _seed_history(self):
        """If we started late, backfill each chart from 1-minute history.

        Skipped when the REST feed is running — it fetches the same bars
        moments later, and doing both is twenty wasted calls at startup.
        """
        if self.cfg.candle_source == "rest":
            return
        ref_ep = self.ref_bucket()
        if int(time.time()) < ref_ep + self.cfg.interval_seconds * 2:
            return
        self._say("Started after the reference time — backfilling charts from history.")
        for leg in list(self.legs.values()):
            try:
                mins = api.fetch_intraday(leg.sec_id, leg.segment, "OPTIDX", "1", days=1)
                mins = [m for m in mins if m["timestamp"] >= ref_ep]
                if mins:
                    s = self.book.get(leg.sec_id)
                    if s:
                        s.seed_from_minutes(mins)
            except Exception as e:
                log.warning(f"  Backfill failed for {leg.label}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # REST CANDLE FEED
    # ═══════════════════════════════════════════════════════════════════════

    def _fetch_1m(self, sec_id: str) -> List[dict]:
        """One-minute bars for a leg, in the shape the feed expects.

        api.fetch_intraday returns rows keyed "timestamp"; the feed works in
        "ts". Translating here, at the one boundary between them, is what
        stops that mismatch from becoming a silent failure — on 09-Sep every
        poll raised KeyError and not a single bar reached the strategy.
        """
        leg = self.legs.get(str(sec_id))
        seg = leg.segment if leg else self.cfg.option_segment
        rows = api.fetch_intraday(sec_id, seg, "OPTIDX", "1", days=1)
        out = []
        for r in rows or []:
            ts = r.get("ts", r.get("timestamp"))
            if not ts:
                continue
            out.append({"ts": int(ts), "open": float(r["open"]),
                        "high": float(r["high"]), "low": float(r["low"]),
                        "close": float(r["close"])})
        return out

    def _on_rest_bar(self, sec_id: str, bar: dict):
        """A completed bar arrived from the broker. It is authoritative."""
        series = self.book.get(sec_id)
        if series is None:
            return
        series.apply_bar(bar["ts"], bar["open"], bar["high"],
                         bar["low"], bar["close"])
        self._rest_bars.setdefault(int(bar["ts"]), set()).add(str(sec_id))
        if not self._first_bar_seen:
            self._first_bar_seen = True
            self._say(f"First broker bar received — {series.label} "
                      f"{api.ist_bucket_label(bar['ts'])} "
                      f"O{bar['open']:.2f} H{bar['high']:.2f} "
                      f"L{bar['low']:.2f} C{bar['close']:.2f}")

    def _on_rest_latency(self, sec_id: str, ts: int, lag: float):
        self.last_lag = lag

    def _start_rest_feed(self):
        if self.cfg.candle_source != "rest" or self.rest is not None:
            return
        legs = {l.sec_id: l.label for l in self.legs.values()}
        self.rest = RestCandleFeed(
            legs=legs, fetch_1m=self._fetch_1m, on_bar=self._on_rest_bar,
            interval=self.cfg.interval_seconds,
            poll=self.cfg.rest_poll, grace=self.cfg.rest_grace,
            on_latency=self._on_rest_latency,
            session_anchor=self.ref_bucket(),
            on_problem=lambda m: self._say(f"  {m}", "error"))
        self.rest.start()
        self._say("Candles are coming from the broker's own bars. The feed is "
                  "used for live price only — the retest, the target and the "
                  "safety stop.")

    def _narrow_rest_to_pair(self):
        """Once the pair is locked the other eight charts decide nothing."""
        if not (self.rest and self.cfg.rest_narrow_after_merge and self.frame):
            return
        fr = self.frame
        self.rest.set_legs({fr.ce.sec_id: fr.ce.label,
                            fr.pe.sec_id: fr.pe.label})
        self._say(f"  Now polling only {fr.ce.label} and {fr.pe.label} — the "
                  f"other eight charts decide nothing from here.")

    def _expected_legs(self) -> int:
        if self.frame is not None and self.cfg.rest_narrow_after_merge:
            return 2
        return max(1, len(self.legs))

    # ═══════════════════════════════════════════════════════════════════════
    # THE OVERLAP SCAN
    # ═══════════════════════════════════════════════════════════════════════

    def _pair_overlaps(self, ce: Candle, pe: Candle) -> Optional[Tuple[str, float]]:
        """Test one CE/PE pair. Returns (red_side, overlap) when they merge.

        Two things must be true:
          1. opposite colours (a doji is neither, so it never pairs)
          2. the two BODIES cover at least `min_overlap` of common ground

        Wicks take no part. Note that (2) also guarantees the red candle's
        close sits at least `min_overlap` below the green candle's close —
        the green body's top IS its close and the red body's bottom IS its
        close — so no separate close-gap test is needed.
        """
        if ce.is_red and pe.is_green:
            red, green, red_side = ce, pe, "CE"
        elif pe.is_red and ce.is_green:
            red, green, red_side = pe, ce, "PE"
        else:
            return None

        # Prices are quoted in paise, so round to two decimals — otherwise
        # 110.05 - 110.00 comes out as 0.049999... and an exact-minimum
        # overlap is rejected by floating point rather than by the rule.
        overlap = round(red.body_overlap(green), 2)
        if self.cfg.require_overlap and overlap < self.cfg.min_overlap:
            return None

        return red_side, overlap

    def _candidate_pairs(self):
        """Which call/put combinations are eligible.

        In "same" mode a strike only ever pairs with its own opposite leg —
        24100 CE with 24100 PE. That is the mode the strategy is designed
        around: a call and a put at the SAME strike only trade at the same
        premium when the index is sitting on that strike, so the overlap is
        telling us the index has arrived there. Cross-strike pairs can show
        the same premium for no reason other than being different distances
        from the money.
        """
        if self.cfg.pair_mode == "same":
            by_strike = {pe.strike: pe for pe in self.pe_legs}
            for ce in self.ce_legs:
                pe = by_strike.get(ce.strike)
                if pe is not None:
                    yield ce, pe
        else:
            for ce in self.ce_legs:
                for pe in self.pe_legs:
                    yield ce, pe

    def _scan_overlap(self, bars: Dict[str, Candle], bucket: int) -> bool:
        """Look for the day's pair. Scan order is strike ascending, and the
        first match wins — as specified."""
        best_near = None  # for the live 'how close are we' readout

        for ce_leg, pe_leg in self._candidate_pairs():
            ce_c = bars.get(ce_leg.sec_id)
            pe_c = bars.get(pe_leg.sec_id)
            if ce_c is None or pe_c is None:
                continue

            hit = self._pair_overlaps(ce_c, pe_c)
            if hit:
                red_side, gap = hit
                self._lock_frame(ce_leg, pe_leg, ce_c, pe_c, bucket, red_side, gap)
                return True

            # track the nearest miss so the GUI has something to show
            d = ce_c.body_overlap(pe_c)
            if best_near is None or d > best_near[0]:
                best_near = (d, ce_leg, pe_leg)

        if best_near:
            d, cl, pl = best_near
            same = abs(cl.strike - pl.strike) < 0.01
            who = f"{int(cl.strike)}" if same else f"{int(cl.strike)}CE / {int(pl.strike)}PE"
            shown = (f"bodies overlap {d:.2f}" if d > 0
                     else f"bodies {abs(d):.2f} apart")
            self._cond("overlap", CondStatus.WATCHING,
                       f"Closest so far: {who} — {shown} "
                       f"(need {self.cfg.min_overlap:.2f})",
                       f"{api.ist_bucket_label(bucket)}")
        else:
            mode = ("same-strike pairs" if self.cfg.pair_mode == "same"
                    else "every call against every put")
            self._cond("overlap", CondStatus.WATCHING,
                       f"Nothing overlapping yet · checking {mode}",
                       f"{api.ist_bucket_label(bucket)}")
        return False

    def _lock_frame(self, ce_leg: Leg, pe_leg: Leg, ce_c: Candle, pe_c: Candle,
                    bucket: int, red_side: str, gap: float):
        frame = Frame(
            bucket=bucket, ce=ce_leg, pe=pe_leg,
            ce_candle=ce_c.to_dict(), pe_candle=pe_c.to_dict(),
            # the CE chart borrows the PE candle's high and low
            ce_borrowed_high=pe_c.high, ce_borrowed_low=pe_c.low,
            # the PE chart borrows the CE candle's high and low
            pe_borrowed_high=ce_c.high, pe_borrowed_low=ce_c.low,
        )
        with self._lock:
            self.frame = frame

        t = api.ist_bucket_label(bucket)
        self._say(f"OVERLAP at {t} — {ce_leg.label} and {pe_leg.label}. "
                  f"{red_side} is red; bodies overlap by {gap:.2f}.")
        self._say(f"  {ce_leg.label}: O{ce_c.open:.2f} H{ce_c.high:.2f} "
                  f"L{ce_c.low:.2f} C{ce_c.close:.2f}")
        self._say(f"  {pe_leg.label}: O{pe_c.open:.2f} H{pe_c.high:.2f} "
                  f"L{pe_c.low:.2f} C{pe_c.close:.2f}")

        self._cond("overlap", CondStatus.MET,
                   f"{ce_leg.label} and {pe_leg.label} at {t} · "
                   f"bodies overlap {gap:.2f}",
                   f"{red_side} red")
        self._cond("frame", CondStatus.MET,
                   f"CE chart borrows {frame.ce_borrowed_low:.2f} / "
                   f"{frame.ce_borrowed_high:.2f} · PE chart borrows "
                   f"{frame.pe_borrowed_low:.2f} / {frame.pe_borrowed_high:.2f}",
                   "4 lines")
        self._cond("collapse", CondStatus.WATCHING,
                   "Watching both charts for a red candle below its borrowed low")
        self._cond("breakout", CondStatus.WATCHING,
                   "Watching both charts for a green close above its borrowed high")

        with self._lock:
            self.merge_label = t
            self.merge_gap = gap
            self.merge_red_side = red_side

        payload = self.frame_payload()
        payload.update({"gap": gap, "red_side": red_side,
                        "strike": int(ce_leg.strike)
                        if abs(ce_leg.strike - pe_leg.strike) < 0.01 else None})
        self._narrow_rest_to_pair()
        self._emit("merged", payload)
        self._emit("frame", payload)
        self._set_phase(Phase.FRAMED, "Frame drawn — watching for the signal")

    # ═══════════════════════════════════════════════════════════════════════
    # THE SIGNAL
    # ═══════════════════════════════════════════════════════════════════════

    def _is_collapse(self, c: Candle, borrowed_low: float) -> Tuple[bool, str]:
        """Red candle whose BODY sits below the borrowed low.

        Open and close must both be under the line; the wick may reach up
        through it and the candle still counts. Body must be at least
        min_body_pct of the candle's own height.
        """
        if not c.is_red:
            return False, "not red"
        body_top = max(c.open, c.close)
        if body_top >= borrowed_low:
            return False, f"body top {body_top:.2f} not below {borrowed_low:.2f}"
        if c.body_pct < self.cfg.min_body_pct:
            return False, f"body {c.body_pct:.0f}% under {self.cfg.min_body_pct:.0f}%"
        wick = " (wick crossed, ignored)" if c.high > borrowed_low else ""
        return True, f"body below {borrowed_low:.2f}, body {c.body_pct:.0f}%{wick}"

    def _is_breakout(self, c: Candle, borrowed_high: float) -> Tuple[bool, str]:
        """Green candle closing above the borrowed high.

        Measured against the line itself — the retest buffer plays no part
        in the break. See strategy note v1.2, pages 6 and 7.
        """
        if not c.is_green:
            return False, "not green"
        if c.close <= borrowed_high:
            return False, f"close {c.close:.2f} not above {borrowed_high:.2f}"
        return True, f"closed {c.close:.2f} above {borrowed_high:.2f}"

    def _check_signal(self, bars: Dict[str, Candle], bucket: int):
        fr = self.frame
        if fr is None:
            return

        # Once the day's trades are done there is nothing left to arm, so we
        # stop testing rather than filling the log with findings we cannot act
        # on. The 25/08 session logged 35 such lines after it had finished.
        if self.trade_count >= self.cfg.max_trades_per_day:
            return
        if self._past(self.cfg.last_entry_time):
            return

        # Each card reports ONE test across BOTH legs. Mixing the two tests
        # into one line, or showing only one leg per card, is what made the
        # cards unreadable in the field.
        col_notes, brk_notes = [], []
        for side in ("CE", "PE"):
            leg = fr.leg(side)
            short = f"{int(leg.strike)} {side}"
            c = bars.get(leg.sec_id)
            if c is None:
                col_notes.append(f"{short}: no candle")
                brk_notes.append(f"{short}: no candle")
                continue

            ok_c, why_c = self._is_collapse(c, fr.borrowed_low(side))
            ok_b, why_b = self._is_breakout(c, fr.borrowed_high(side))

            if ok_c:
                self._pending_collapse[side] = bucket
                self._say(f"  COLLAPSE on {leg.label} — {why_c}")
            if ok_b:
                # remember when this run of breaks STARTED, so a break that
                # has been true for many candles is not mistaken for a fresh one
                if side not in self._pending_breakout:
                    self._break_since[side] = bucket
                self._pending_breakout[side] = bucket
                age = (bucket - self._break_since.get(side, bucket)) // self.cfg.interval_seconds
                self._say(f"  BREAK on {leg.label} — {why_b}"
                          + (f" (above the line for {age} candles)" if age else ""))

            if not ok_b:
                self._pending_breakout.pop(side, None)
                self._break_since.pop(side, None)
                self._order_warned.pop(side, None)

            # A collapse stands until that leg closes back above its borrowed
            # high — the same test that ends a live trade. No arbitrary timer.
            if side in self._pending_collapse and not ok_c:
                if c.close > fr.borrowed_high(side):
                    self._say(f"  Collapse on {leg.label} cancelled — closed "
                              f"{c.close:.2f}, back above "
                              f"{fr.borrowed_high(side):.2f}")
                    self._pending_collapse.pop(side, None)

            col_notes.append(f"{short}: {'YES — ' if ok_c else ''}{why_c}")
            brk_notes.append(f"{short}: {'YES — ' if ok_b else ''}{why_b}")

        notes = {"collapse": "     ·     ".join(col_notes),
                 "breakout": "     ·     ".join(brk_notes)}

        # A trade already running takes priority — no new arming.
        if self.trade is not None or self.signal is not None:
            return
        if self.trade_count >= self.cfg.max_trades_per_day:
            return

        # ── After a stop: a break alone is enough ──────────────────────────
        if self.entry_mode == "relaxed":
            for breakout_side in ("CE", "PE"):
                bb = self._pending_breakout.get(breakout_side)
                if bb is None or bb != bucket:
                    continue
                other = fr.other(breakout_side)
                if self.cfg.guard_relaxed_entry:
                    oc = bars.get(fr.leg(other).sec_id)
                    if oc is not None and oc.close > fr.borrowed_high(other):
                        if not self._guard_warned.get(breakout_side):
                            self._guard_warned[breakout_side] = True
                            self._say(f"  Relaxed entry held back — "
                                      f"{fr.leg(other).label} closed {oc.close:.2f}, "
                                      f"above its own borrowed high "
                                      f"{fr.borrowed_high(other):.2f}. The stop "
                                      f"would fire on the next candle.")
                        continue
                self._guard_warned.pop(breakout_side, None)
                self._arm(breakout_side, other, bb, bb, bucket, relaxed=True)
                return
            return

        # ── The order matters ──────────────────────────────────────────────
        # The collapse comes first, or the two land on the same candle. A break
        # that happened BEFORE the collapse does not count — by the time the
        # collapse arrives, price has usually left the entry band behind.
        for collapse_side in ("CE", "PE"):
            breakout_side = fr.other(collapse_side)
            cb = self._pending_collapse.get(collapse_side)
            bb = self._pending_breakout.get(breakout_side)
            if cb is None or bb is None:
                continue

            # A leg stays "broken out" for as long as it keeps closing above
            # its line, so compare against when that run STARTED — otherwise a
            # break from before the collapse looks simultaneous with it.
            bstart = self._break_since.get(breakout_side, bb)

            if bstart < cb:
                if bucket == bb and not self._order_warned.get(breakout_side):
                    self._order_warned[breakout_side] = True
                    self._say(f"  Not arming — {fr.leg(breakout_side).label} has "
                              f"been above its line since "
                              f"{api.ist_bucket_label(bstart)}, before the collapse "
                              f"on {fr.leg(collapse_side).label} at "
                              f"{api.ist_bucket_label(cb)}. The break must come "
                              f"with the collapse or after it.")
                continue

            # the break has to still be true on this candle
            if bb != bucket:
                continue

            age = (bstart - cb) // self.cfg.interval_seconds
            self._arm(breakout_side, collapse_side, cb, bstart, bucket, age)
            return

        # nothing armed — reflect whichever leg actually did what
        if self._pending_collapse:
            side = sorted(self._pending_collapse,
                          key=lambda k: self._pending_collapse[k])[-1]
            self._cond("collapse", CondStatus.MET,
                       f"{fr.leg(side).label} fell below "
                       f"{fr.borrowed_low(side):.2f}",
                       api.ist_bucket_label(self._pending_collapse[side]))
        else:
            self._cond("collapse", CondStatus.WATCHING,
                       notes.get("collapse", "") or "Watching both charts")

        if self._pending_breakout:
            side = sorted(self._pending_breakout,
                          key=lambda k: self._pending_breakout[k])[-1]
            self._cond("breakout", CondStatus.MET,
                       f"{fr.leg(side).label} closed above "
                       f"{fr.borrowed_high(side):.2f}",
                       api.ist_bucket_label(self._pending_breakout[side]))
        else:
            self._cond("breakout", CondStatus.WATCHING,
                       notes.get("breakout", "") or "Watching both charts")

    def _arm(self, buy_side: str, collapse_side: str,
             collapse_bucket: int, breakout_bucket: int, bucket: int,
             age_bars: int = 0, relaxed: bool = False):
        fr = self.frame
        level = fr.borrowed_high(buy_side)
        buf = self.cfg.retest_buffer
        sig = Signal(
            bucket=bucket, buy_side=buy_side, collapse_side=collapse_side,
            breakout_level=level,
            band_low=level - buf, band_high=level + buf,
            collapse_bucket=collapse_bucket, breakout_bucket=breakout_bucket,
            armed_at=api.now_ist().strftime("%H:%M:%S"),
        )
        with self._lock:
            self.signal = sig

        buy_leg = fr.leg(buy_side)
        col_leg = fr.leg(collapse_side)
        if relaxed:
            when = "relaxed entry after a stop — no collapse required"
        elif age_bars == 0:
            when = "collapse and break on the same candle"
        else:
            when = f"break {age_bars} candle(s) after the collapse"
        self._say(f"ARMED — buying {buy_leg.label} on a return into "
                  f"{sig.band_low:.2f}–{sig.band_high:.2f} "
                  f"(line {level:.2f}, buffer {buf:.2f}) — {when}")
        if self.cfg.two_sided_stop:
            self._say(f"  Stop will need BOTH — {col_leg.label} closing above "
                      f"{fr.borrowed_high(collapse_side):.2f} AND "
                      f"{buy_leg.label} closing below "
                      f"{fr.borrowed_low(buy_side):.2f}")
        else:
            self._say(f"  Stop will watch {col_leg.label} closing above "
                      f"{fr.borrowed_high(collapse_side):.2f}")

        if relaxed:
            self._cond("collapse", CondStatus.IDLE,
                       "Not required — relaxed entry after a stop")
        else:
            self._cond("collapse", CondStatus.MET,
                       f"{col_leg.label} fell below "
                       f"{fr.borrowed_low(collapse_side):.2f}",
                       api.ist_bucket_label(collapse_bucket))
        self._cond("breakout", CondStatus.MET,
                   f"{buy_leg.label} closed above {level:.2f}",
                   api.ist_bucket_label(breakout_bucket))
        self._cond("retest", CondStatus.WATCHING,
                   f"Waiting for {buy_leg.label} to trade back into "
                   f"{sig.band_low:.2f}–{sig.band_high:.2f}")

        self._emit("signal", self.signal_payload())
        self._set_phase(Phase.ARMED, f"Armed on {buy_leg.label}")

    def _drop_signal(self, why: str):
        if self.signal is None:
            return
        leg = self.frame.leg(self.signal.buy_side)
        self._say(f"Signal dropped on {leg.label} — {why}")
        with self._lock:
            self.signal = None
            self._pending_collapse.clear()
            self._pending_breakout.clear()
            self._break_since.clear()
            self._order_warned.clear()
        self._cond("retest", CondStatus.FAILED, why)
        self._cond("collapse", CondStatus.WATCHING, "Watching again")
        self._cond("breakout", CondStatus.WATCHING, "Watching again")
        self._emit("signal", {})
        self._set_phase(Phase.FRAMED, "Watching for the signal again")

    # ═══════════════════════════════════════════════════════════════════════
    # BAR CLOSE
    # ═══════════════════════════════════════════════════════════════════════

    def _on_bar_close(self, bucket: int, merge_only: bool = False):
        bars = self.book.finalise_all(bucket)
        if not bars:
            return

        if merge_only:
            # Replaying a candle that has already happened. It may tell us
            # where the pair is; it may not tell us to trade.
            if self.frame is None and bucket > self.ref_bucket():
                self._scan_overlap(bars, bucket)
            return

        self._emit("bars", {"bucket": bucket,
                            "label": api.ist_bucket_label(bucket),
                            "candles": {sid: c.to_dict() for sid, c in bars.items()}})

        if self.frame is None:
            # The reference candle built the ladder; scanning starts after it.
            if self.ladder_built and bucket > self.ref_bucket():
                self._scan_overlap(bars, bucket)
            return

        # Frame exists — check the stop first, it outranks everything.
        if self.trade is not None:
            self._check_structural_stop(bars, bucket)
            return

        # Armed but not filled: does the collapsed leg's recovery kill it?
        if self.signal is not None:
            fr = self.frame
            sig = self.signal
            col_leg = fr.leg(sig.collapse_side)
            c = bars.get(col_leg.sec_id)
            if c is not None and c.close > fr.borrowed_high(sig.collapse_side):
                self._drop_signal(
                    f"{col_leg.label} closed {c.close:.2f}, back above "
                    f"{fr.borrowed_high(sig.collapse_side):.2f}")
                return

            # Heartbeat. Without this the log falls completely silent while
            # armed, which reads as a hung application — it is why the 26/08
            # log appears to stop dead at 10:09.
            buy_leg = fr.leg(sig.buy_side)
            bc = bars.get(buy_leg.sec_id)
            if bc is not None:
                if sig.band_low <= bc.close <= sig.band_high:
                    where = "inside the band"
                elif bc.close > sig.band_high:
                    where = f"{bc.close - sig.band_high:.2f} above the band"
                else:
                    where = f"{sig.band_low - bc.close:.2f} below the band"
                self._say(f"  Still armed on {buy_leg.label} — {bc.close:.2f}, "
                          f"{where} ({sig.band_low:.2f}-{sig.band_high:.2f}), "
                          f"waiting since {sig.armed_at}")
            return

        self._check_signal(bars, bucket)

    def _check_structural_stop(self, bars: Dict[str, Candle], bucket: int):
        """The structural stop, read on BOTH charts.

        Both must be true on the same closed candle:
          - the other leg closes ABOVE its borrowed high, and
          - the leg we hold closes BELOW its own borrowed low.

        One alone is not enough. Set two_sided_stop False to go back to the
        older behaviour, where the other chart ended the trade by itself.
        """
        tr = self.trade
        fr = self.frame
        if tr is None or fr is None:
            return

        other_side = tr.collapse_side
        ours_side = tr.side
        other_leg, our_leg = fr.leg(other_side), fr.leg(ours_side)
        other_line = fr.borrowed_high(other_side)   # they must close above this
        our_line = fr.borrowed_low(ours_side)       # we must close below this

        oc = bars.get(other_leg.sec_id)
        mc = bars.get(our_leg.sec_id)

        other_hit = oc is not None and oc.close > other_line
        ours_hit = mc is not None and mc.close < our_line

        if not self.cfg.two_sided_stop:
            ours_hit = True   # one-sided: the other chart decides alone

        if other_hit and ours_hit:
            parts = [f"{other_leg.label} closed {oc.close:.2f} above "
                     f"{other_line:.2f}"]
            if self.cfg.two_sided_stop and mc is not None:
                parts.append(f"{our_leg.label} closed {mc.close:.2f} below "
                             f"{our_line:.2f}")
            self._say("STRUCTURAL STOP — " + " and ".join(parts))
            price = self._ltp_of(tr.sec_id) or tr.entry_price
            self._exit_trade(price, "STRUCTURAL STOP")
            return

        # Not stopped — show where each side stands.
        bits = []
        if oc is not None:
            bits.append(f"{int(other_leg.strike)} {other_side} "
                        f"{oc.close:.2f} vs {other_line:.2f} "
                        f"{'ABOVE' if other_hit else 'below'}")
        if self.cfg.two_sided_stop and mc is not None:
            below = mc.close < our_line
            bits.append(f"{int(our_leg.strike)} {ours_side} "
                        f"{mc.close:.2f} vs {our_line:.2f} "
                        f"{'BELOW' if below else 'above'}")
        if bits:
            self._cond("exit", CondStatus.WATCHING, "     ·     ".join(bits),
                       "1 of 2" if other_hit else "")
            if other_hit and self.cfg.two_sided_stop:
                self._say(f"  Stop half-met — {other_leg.label} closed "
                          f"{oc.close:.2f} above {other_line:.2f}, but "
                          f"{our_leg.label} has not closed below "
                          f"{our_line:.2f}. Holding.")

    # ═══════════════════════════════════════════════════════════════════════
    # TICKS — RETEST AND EXITS
    # ═══════════════════════════════════════════════════════════════════════

    def _ltp_of(self, sec_id: str) -> Optional[float]:
        s = self.book.get(sec_id)
        return s.ltp if s else None

    def _on_trade_tick(self, sec_id: str, ltp: float):
        # Once stopped, no tick may start anything. A tick already in flight
        # when STOP was pressed used to be enough to begin a whole entry
        # sequence — four orders — after the person had asked us to stop.
        if self.stop_event.is_set():
            return

        # ── Retest, watched on live price ──
        sig = self.signal
        if sig is not None and self.trade is None and self.frame is not None:
            leg = self.frame.leg(sig.buy_side)
            if sec_id == leg.sec_id:
                if sig.band_low <= ltp <= sig.band_high:
                    self._enter(leg, ltp, sig)
                else:
                    dist = (ltp - sig.band_high) if ltp > sig.band_high else (sig.band_low - ltp)
                    self._cond("retest", CondStatus.WATCHING,
                               f"{leg.label} at {ltp:.2f} · band "
                               f"{sig.band_low:.2f}–{sig.band_high:.2f}",
                               f"{dist:.2f} away")
            return

        # ── Target and safety stop on the option we hold ──
        tr = self.trade
        if tr is not None and sec_id == tr.sec_id:
            if ltp >= tr.target:
                self._say(f"TARGET — {tr.leg_label} reached {ltp:.2f} "
                          f"(target {tr.target:.2f})")
                self._exit_trade(ltp, "TARGET")
                return
            if self.cfg.safety_stop_enabled and ltp <= tr.safety_stop:
                self._say(f"SAFETY STOP — {tr.leg_label} fell to {ltp:.2f} "
                          f"(stop {tr.safety_stop:.2f})", "warning")
                self._exit_trade(ltp, "SAFETY STOP")
                return
            pnl = (ltp - tr.entry_price) * tr.qty
            self._cond("exit", CondStatus.WATCHING,
                       f"{tr.leg_label} at {ltp:.2f} · target {tr.target:.2f}",
                       f"{'+' if pnl >= 0 else ''}{pnl:,.0f}")
            self._emit("trade_tick", {"ltp": ltp, "pnl": pnl,
                                      "entry": tr.entry_price,
                                      "target": tr.target})

    # ═══════════════════════════════════════════════════════════════════════
    # ORDERS
    # ═══════════════════════════════════════════════════════════════════════

    def _enter(self, leg: Leg, price: float, sig: Signal):
        with self._lock:
            if self.trade is not None or self._order_in_progress:
                return
            if self.trade_count >= self.cfg.max_trades_per_day:
                self._drop_signal("daily trade limit reached")
                return
            if self._past(self.cfg.last_entry_time):
                self._drop_signal("past the last entry time")
                return
            # An entry that keeps failing must not be retried on every tick.
            # Each attempt is several orders, and the exchange should not be
            # asked the same question twenty times in fifteen seconds.
            if self.trading_halted or self.stop_event.is_set():
                return
            now = time.time()
            if sig.attempts >= self.cfg.max_entry_attempts:
                self._drop_signal(
                    f"{self.cfg.max_entry_attempts} entry attempts all failed. "
                    f"Not trying again on this signal — fix the cause before "
                    f"restarting")
                return
            if sig.attempts and (now - sig.last_attempt) < self.cfg.entry_retry_seconds:
                return
            sig.attempts += 1
            sig.last_attempt = now

            # Held until the order resolves, so a second tick cannot send a
            # second BUY while the broker is still thinking about the first.
            self._order_in_progress = True

        try:
            self._place_entry(leg, price, sig)
        finally:
            with self._lock:
                self._order_in_progress = False

    def _place_entry(self, leg: Leg, price: float, sig: Signal):
        if self.stop_event.is_set():
            return
        qty = self.cfg.quantity
        self._say(f"RETEST HIT — {leg.label} traded {price:.2f} inside "
                  f"{sig.band_low:.2f}–{sig.band_high:.2f}. Buying {qty}.")

        if self.cfg.paper_mode:
            fill = {"filled": True, "price": price, "order_id": "PAPER"}
            self._say(f"  [PAPER] BUY {qty} {leg.label} @ {price:.2f}")
        else:
            if not self._live_entry_allowed(leg, price, qty):
                return
            self._say(f"  [LIVE] Sending BUY {qty} {leg.label}...")
            fill = api.place_order_limit_ioc(leg.sec_id, leg.segment, "BUY", qty,
                                             price, self.cfg.order_price_buffer)

        if fill.get("aborted"):
            self._say(f"  Entry abandoned — stopped before the order was sent.",
                      "warning")
            return

        if fill.get("fatal"):
            self._halt_trading(fill.get("code", ""), fill.get("advice", ""))
            return

        if not fill.get("filled"):
            left = self.cfg.max_entry_attempts - sig.attempts
            self._say(f"  Entry not filled on {leg.label} "
                      f"(attempt {sig.attempts} of "
                      f"{self.cfg.max_entry_attempts}"
                      f"{f', {left} left' if left > 0 else ', none left'}).",
                      "warning")
            if self.cfg.is_live:
                self._check_orphan_position(leg, qty)
            return

        entry = float(fill["price"])
        tr = Trade(
            trade_no=self.trade_count + 1,
            side=sig.buy_side, leg_label=leg.label, sec_id=leg.sec_id, qty=qty,
            entry_price=entry, entry_time=api.now_ist().strftime("%H:%M:%S"),
            target=round(entry * (1 + self.cfg.target_pct / 100.0), 2),
            safety_stop=round(entry * (1 - self.cfg.safety_stop_pct / 100.0), 2),
            breakout_level=sig.breakout_level, collapse_side=sig.collapse_side,
            order_id=str(fill.get("order_id", "")),
        )
        with self._lock:
            self.trade = tr
            self.signal = None

        col_leg = self.frame.leg(tr.collapse_side)
        self._say(f"IN TRADE #{tr.trade_no} — {leg.label} at {entry:.2f}, "
                  f"target {tr.target:.2f}")
        if self.cfg.two_sided_stop:
            self._say(f"  Stop needs BOTH — {col_leg.label} closing above "
                      f"{self.frame.borrowed_high(tr.collapse_side):.2f} AND "
                      f"{leg.label} closing below "
                      f"{self.frame.borrowed_low(tr.side):.2f}")
        else:
            self._say(f"  Stop watches {col_leg.label} closing above "
                      f"{self.frame.borrowed_high(tr.collapse_side):.2f}")
        if self.cfg.safety_stop_enabled:
            self._say(f"  Safety stop at {tr.safety_stop:.2f}")

        self._cond("retest", CondStatus.MET,
                   f"Bought {leg.label} at {entry:.2f}", f"trade #{tr.trade_no}")
        self._cond("exit", CondStatus.WATCHING,
                   f"Target {tr.target:.2f} · stop needs {col_leg.label} above "
                   f"{self.frame.borrowed_high(tr.collapse_side):.2f} and "
                   f"{leg.label} below {self.frame.borrowed_low(tr.side):.2f}"
                   if self.cfg.two_sided_stop else
                   f"Target {tr.target:.2f} · stop on {col_leg.label}")
        self._emit("trade", self.trade_payload())
        self._set_phase(Phase.IN_TRADE, f"In trade on {leg.label}")

    def _live_entry_allowed(self, leg: Leg, price: float, qty: int) -> bool:
        """Checks that only matter with real money behind the order."""
        # Slippage. The retest fired at a price; if the market has since run
        # away, the trade we are about to take is not the one we decided on.
        ltp = self._ltp_of(leg.sec_id)
        if ltp and price > 0:
            slip = abs(ltp - price) / price * 100.0
            if slip > self.cfg.max_entry_slippage_pct:
                self._say(f"  Entry abandoned — {leg.label} has moved from "
                          f"{price:.2f} to {ltp:.2f} ({slip:.1f}%), past the "
                          f"{self.cfg.max_entry_slippage_pct:.1f}% limit.",
                          "warning")
                return False

        # Funds. A rejected order is recoverable; a rejected order we did not
        # expect is how a session ends in confusion.
        funds = api.get_available_funds()
        if funds is not None:
            need = (ltp or price) * qty
            if funds < need:
                self._say(f"  Entry abandoned — Rs.{funds:,.0f} available but "
                          f"Rs.{need:,.0f} needed for {qty} of {leg.label}.",
                          "error")
                return False
        return True

    def _check_orphan_position(self, leg: Leg, qty: int):
        """Did an order we believe failed actually fill?

        An unfilled entry that left a position at the broker is the worst
        outcome of all — we would hold an option nobody is watching.
        """
        try:
            held = api.position_qty(leg.sec_id)
        except Exception:
            return
        if held:
            self._say(f"  WARNING — the broker shows {held} of {leg.label} "
                      f"despite the order not confirming. Adopting it so it "
                      f"is watched and squared off.", "error")
            self._adopt_position(leg, held)

    def _adopt_position(self, leg: Leg, held: int):
        """Take ownership of a position the broker says we hold."""
        sig = self.signal
        fr = self.frame
        if fr is None:
            return
        side = leg.side
        entry = self._ltp_of(leg.sec_id) or 0.0
        if entry <= 0:
            return
        tr = Trade(
            trade_no=self.trade_count + 1, side=side, leg_label=leg.label,
            sec_id=leg.sec_id, qty=abs(int(held)), entry_price=entry,
            entry_time=api.now_ist().strftime("%H:%M:%S"),
            target=round(entry * (1 + self.cfg.target_pct / 100.0), 2),
            safety_stop=round(entry * (1 - self.cfg.safety_stop_pct / 100.0), 2),
            breakout_level=sig.breakout_level if sig else 0.0,
            collapse_side=(sig.collapse_side if sig else fr.other(side)),
            order_id="ADOPTED")
        with self._lock:
            self.trade = tr
            self.signal = None
        self._emit("trade", self.trade_payload())
        self._set_phase(Phase.IN_TRADE, f"Adopted position in {leg.label}")

    # ═══════════════════════════════════════════════════════════════════════
    # POSITION RECONCILIATION
    # ═══════════════════════════════════════════════════════════════════════

    def _sync_positions(self, now: float):
        """Ask the broker what we actually hold, and believe the answer.

        A position can disappear without us: squared off by hand, closed by
        the broker's own risk system, or a stop we never placed. Carrying on
        as though the trade were open would mean a phantom exit later and a
        P&L that never happened.
        """
        if not self.cfg.is_live or self.cfg.paper_mode:
            return
        tr = self.trade
        if tr is None or not tr.is_open or tr.order_id == "PAPER":
            return
        if now - self._last_position_sync < self.cfg.position_sync_seconds:
            return
        self._last_position_sync = now

        # Give the broker a moment to register a fresh fill before judging it.
        try:
            entered = datetime.strptime(tr.entry_time, "%H:%M:%S").time()
            n = api.now_ist()
            age = (n.hour * 3600 + n.minute * 60 + n.second) - (
                entered.hour * 3600 + entered.minute * 60 + entered.second)
            if age < 30:
                return
        except Exception:
            pass

        try:
            held = api.position_qty(tr.sec_id)
        except Exception as e:
            log.warning(f"  Position sync failed: {e}")
            return

        if held:
            return                      # still there, nothing to do

        exit_price = api.fill_from_tradebook(tr.order_id) or \
            self._ltp_of(tr.sec_id) or tr.entry_price
        self._say(f"The broker no longer shows a position in {tr.leg_label}. "
                  f"It was closed outside this app — booking it at "
                  f"{exit_price:.2f}.", "warning")
        self._finalise_trade(tr, exit_price, "CLOSED EXTERNALLY")

    def _exit_trade(self, price: float, reason: str):
        # The trade is NOT cleared here. In live mode an order can fail, and
        # clearing first would leave us believing we are flat while still
        # holding the option. It is cleared only once the exit is confirmed.
        with self._lock:
            tr = self.trade
            if tr is None or not tr.is_open or self._order_in_progress:
                return
            self._order_in_progress = True

        try:
            leg = self.frame.leg(tr.side)
            if self.cfg.paper_mode:
                fill = {"filled": True, "price": price, "order_id": "PAPER"}
                self._say(f"  [PAPER] SELL {tr.qty} {tr.leg_label} @ {price:.2f}")
            else:
                fill = self._sell_until_flat(leg, tr, reason)
                if fill is None:
                    return          # still holding; the alarm has been raised
        finally:
            with self._lock:
                self._order_in_progress = False

        exit_price = float(fill.get("price") or price)
        self._finalise_trade(tr, exit_price, reason)

    def _sell_until_flat(self, leg: Leg, tr: Trade, reason: str):
        """Sell, and keep trying. A failed exit is not an exit.

        If every attempt fails the trade stays open and the failure is stated
        plainly rather than being buried — the position is real, and someone
        has to deal with it.
        """
        last = None
        for attempt in range(1, self.cfg.exit_retry_attempts + 1):
            self._say(f"  [LIVE] SELL {tr.qty} {tr.leg_label} "
                      f"(attempt {attempt}/{self.cfg.exit_retry_attempts})")
            last = api.place_order_market(leg.sec_id, leg.segment, "SELL", tr.qty)
            if last.get("filled"):
                return last
            if last.get("fatal"):
                # Retrying will not help, and the position is real.
                self._halt_trading(last.get("code", ""), last.get("advice", ""),
                                   holding=tr)
                break

            # The order may have gone through even though we did not get a
            # confirmation. Ask what we actually hold before trying again.
            try:
                if api.position_qty(leg.sec_id) == 0:
                    px = api.fill_from_tradebook(last.get("order_id", "")) or \
                        self._ltp_of(leg.sec_id) or tr.entry_price
                    self._say(f"  The position is already closed at the broker "
                              f"— booking it at {px:.2f}.")
                    return {"filled": True, "price": px,
                            "order_id": last.get("order_id", "")}
            except Exception:
                pass
            time.sleep(1.0)

        self._say(f"EXIT FAILED — {tr.qty} of {tr.leg_label} could not be sold "
                  f"after {self.cfg.exit_retry_attempts} attempts ({reason}). "
                  f"THE POSITION IS STILL OPEN. Square it off in the broker "
                  f"terminal now.", "error")
        self._emit("exit_failed", {"leg": tr.leg_label, "qty": tr.qty,
                                   "reason": reason})
        return None

    def _halt_trading(self, code: str, advice: str, holding: Optional[Trade] = None):
        """Stop trying to trade. The broker has refused us for a reason that
        will not change until somebody changes it."""
        if self.trading_halted:
            return
        with self._lock:
            self.trading_halted = True
            self.halt_reason = f"{code}: {advice}"

        self._say(f"TRADING HALTED — the broker refused the order ({code}).",
                  "error")
        self._say(f"  {advice}", "error")
        self._say("  No further orders will be sent this session. Fix the "
                  "cause, then restart the app.", "error")

        if holding is not None:
            self._say(f"  A POSITION IS STILL OPEN — {holding.qty} of "
                      f"{holding.leg_label}. It cannot be closed through the "
                      f"API while this is refused. Square it off in the broker "
                      f"terminal now.", "error")

        if self.signal is not None:
            self._drop_signal(f"trading halted ({code})")
        self._emit("halted", {"code": code, "advice": advice,
                              "holding": holding.leg_label if holding else ""})
        self._set_phase(Phase.STOPPED, f"Halted — {code}")

    def _finalise_trade(self, tr: Trade, exit_price: float, reason: str):
        """Book a closed trade. Everything after the position is actually gone."""
        with self._lock:
            if self.trade is not tr and self.trade is not None:
                return
            self.trade = None

        tr.exit_price = exit_price
        tr.exit_time = api.now_ist().strftime("%H:%M:%S")
        tr.exit_reason = reason
        tr.pnl = (exit_price - tr.entry_price) * tr.qty
        tr.is_open = False

        with self._lock:
            self.closed_trades.append(tr)
            self.trade_count += 1
            self._pending_collapse.clear()
            self._pending_breakout.clear()
            self._break_since.clear()
            self._order_warned.clear()
        # Persist immediately. If the app is killed and restarted the same day
        # the count has to survive, or the daily limit means nothing.
        self.save_state()

        stopped = reason in ("STRUCTURAL STOP", "SAFETY STOP")
        with self._lock:
            self.entry_mode = ("relaxed" if (stopped and self.cfg.relaxed_after_stop)
                               else "full")

        pts = exit_price - tr.entry_price
        self._say(f"CLOSED #{tr.trade_no} {tr.leg_label} — {reason}. "
                  f"{tr.entry_price:.2f} → {exit_price:.2f} "
                  f"({pts:+.2f} pts, Rs.{tr.pnl:+,.0f})",
                  "info" if tr.pnl >= 0 else "warning")

        self.trade_logger.write({
            "date": api.now_ist().strftime("%Y-%m-%d"), "trade_no": tr.trade_no,
            "index": self.cfg.index, "leg": tr.leg_label, "side": tr.side,
            "qty": tr.qty, "entry_time": tr.entry_time,
            "entry_price": f"{tr.entry_price:.2f}", "exit_time": tr.exit_time,
            "exit_price": f"{exit_price:.2f}", "reason": reason,
            "points": f"{pts:.2f}", "pnl": f"{tr.pnl:.2f}",
            "breakout_level": f"{tr.breakout_level:.2f}",
            "collapse_side": tr.collapse_side,
            "mode": "PAPER" if self.cfg.paper_mode else "LIVE",
        })

        self._cond("exit", CondStatus.MET if tr.pnl >= 0 else CondStatus.FAILED,
                   f"{reason} · {tr.entry_price:.2f} → {exit_price:.2f}",
                   f"Rs.{tr.pnl:+,.0f}")
        self._emit("trade_closed", self.summary_payload())

        if self.trade_count >= self.cfg.max_trades_per_day:
            self._say(f"Two trades completed — done for the day.")
            self._set_phase(Phase.DONE, "Done for the day")
            self._cond("retest", CondStatus.IDLE, "Daily limit reached")
        else:
            self._reset_conditions_from("retest")
            if self.entry_mode == "relaxed":
                self._say("That was a stop, so the next entry is the relaxed one — "
                          "a break above a borrowed high and a retest. No collapse "
                          "is required.")
                self._cond("collapse", CondStatus.IDLE,
                           "Not required after a stop")
                self._cond("breakout", CondStatus.WATCHING,
                           "Either chart closing above its borrowed high")
            else:
                self._say("That was a target, so the next entry is the full "
                          "two-sided setup again.")
                self._cond("collapse", CondStatus.WATCHING, "Watching again")
                self._cond("breakout", CondStatus.WATCHING, "Watching again")
            self._set_phase(Phase.FRAMED, "Watching for the signal again")

    # ═══════════════════════════════════════════════════════════════════════
    # SESSION CONTROL
    # ═══════════════════════════════════════════════════════════════════════

    def _past(self, t: dtime) -> bool:
        n = api.now_ist().time()
        return n >= t

    def _check_eod(self):
        if not self._past(self.cfg.square_off_time):
            return
        if self.trade is not None:
            price = self._ltp_of(self.trade.sec_id) or self.trade.entry_price
            self._say("Square-off time — closing at market.", "warning")
            self._exit_trade(price, "EOD SQUARE-OFF")
            if self.trade is not None and self.cfg.is_live:
                self._say("The square-off did not complete. The feed will be "
                          "left running and the position is STILL OPEN — "
                          "close it in the broker terminal.", "error")
                return
        if self.signal is not None:
            self._drop_signal("square-off time reached")
        if self.phase not in (Phase.DONE, Phase.STOPPED):
            self._set_phase(Phase.DONE, "Session closed")

        if not self.feed_stop.is_set():
            self._say("Square-off time — closing the market feed. "
                      "Nothing further will be traded today.")
            self.feed_stop.set()
            if self.rest:
                try:
                    self.rest.stop()
                except Exception:
                    pass
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════════════════════════
    # WEBSOCKET
    # ═══════════════════════════════════════════════════════════════════════

    def _on_ws_open(self, ws):
        self.ws_connected.set()
        instruments = [{"ExchangeSegment": INDEX_CONFIG[self.cfg.index]["index_segment"],
                        "SecurityId": self.cfg.index_security_id}]
        instruments += [{"ExchangeSegment": l.segment, "SecurityId": l.sec_id}
                        for l in self.legs.values()]
        try:
            ws.send(api.build_subscribe_message(instruments))
            self._say(f"WebSocket connected — {len(instruments)} instruments.")
        except Exception as e:
            self._say(f"Subscribe on open failed: {e}", "error")
        self._emit("ws", {"connected": True, "instruments": len(instruments)})

    def _on_ws_message(self, ws, msg):
        if isinstance(msg, str):
            return
        hdr = api.parse_header(bytes(msg))
        if not hdr or hdr["resp_code"] != 2:
            return
        t = api.parse_ticker(hdr["payload"])
        if not t:
            return
        sec_id = hdr["security_id"]
        ltp = t["ltp"]
        ltt = api.normalize_epoch(t["ltt"])
        self.packet_count += 1

        if sec_id == self.cfg.index_security_id:
            self.spot_ltp = ltp

        self.book.on_tick(sec_id, ltp, ltt)

        if sec_id in self.legs:
            try:
                self._on_trade_tick(sec_id, ltp)
            except Exception as e:
                log.error(f"Tick handling error: {e}")

    def _on_ws_error(self, ws, error):
        log.error(f"WS error: {error}")

    def _on_ws_close(self, ws, code, msg):
        self.ws_connected.clear()
        if not (self.feed_stop.is_set() or self.stop_event.is_set()):
            self._say(f"WebSocket closed ({code}).", "warning")
        self._emit("ws", {"connected": False})

    def _run_ws(self):
        while not self.stop_event.is_set() and not self.feed_stop.is_set():
            try:
                self.ws = websocket.WebSocketApp(
                    api.get_ws_url(),
                    on_open=self._on_ws_open, on_message=self._on_ws_message,
                    on_error=self._on_ws_error, on_close=self._on_ws_close)
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.error(f"WS exception: {e}")
            if not self.stop_event.is_set() and not self.feed_stop.is_set():
                time.sleep(2)

    # ═══════════════════════════════════════════════════════════════════════
    # PAYLOADS FOR THE GUI
    # ═══════════════════════════════════════════════════════════════════════

    def conditions_payload(self) -> dict:
        return {"conditions": [c.to_dict() for c in self.conditions.values()]}

    def ladder_payload(self) -> dict:
        rows = []
        for i, ce in enumerate(self.ce_legs):
            pe = self.pe_legs[i] if i < len(self.pe_legs) else None
            ce_s = self.book.get(ce.sec_id)
            pe_s = self.book.get(pe.sec_id) if pe else None
            rows.append({
                "strike": int(ce.strike),
                "is_atm": self.atm_strike is not None and abs(ce.strike - self.atm_strike) < 0.01,
                "ce_label": ce.label, "ce_ltp": ce_s.ltp if ce_s else None,
                "pe_label": pe.label if pe else "", "pe_ltp": pe_s.ltp if pe_s else None,
            })
        return {"rows": rows, "atm": self.atm_strike, "ref_spot": self.ref_spot,
                "expiry": self.expiry, "index": self.cfg.index}

    def frame_payload(self) -> dict:
        fr = self.frame
        if not fr:
            return {}
        return {
            "bucket_label": api.ist_bucket_label(fr.bucket),
            "ce_label": fr.ce.label, "pe_label": fr.pe.label,
            "ce_candle": fr.ce_candle, "pe_candle": fr.pe_candle,
            "ce_borrowed_high": fr.ce_borrowed_high,
            "ce_borrowed_low": fr.ce_borrowed_low,
            "pe_borrowed_high": fr.pe_borrowed_high,
            "pe_borrowed_low": fr.pe_borrowed_low,
        }

    def signal_payload(self) -> dict:
        s = self.signal
        if not s or not self.frame:
            return {}
        return {"buy_leg": self.frame.leg(s.buy_side).label,
                "collapse_leg": self.frame.leg(s.collapse_side).label,
                "level": s.breakout_level, "band_low": s.band_low,
                "band_high": s.band_high, "armed_at": s.armed_at}

    def trade_payload(self) -> dict:
        tr = self.trade
        if not tr:
            return {}
        return {"trade_no": tr.trade_no, "leg": tr.leg_label, "qty": tr.qty,
                "entry": tr.entry_price, "target": tr.target,
                "safety_stop": tr.safety_stop, "entry_time": tr.entry_time,
                "collapse_leg": self.frame.leg(tr.collapse_side).label
                if self.frame else ""}

    def summary_payload(self) -> dict:
        total = sum(t.pnl for t in self.closed_trades)
        fr = self.frame
        pair_txt = "—"
        pair_ce = pair_pe = None
        if fr:
            pair_ce, pair_pe = int(fr.ce.strike), int(fr.pe.strike)
            pair_txt = f"{pair_ce} / {pair_pe}"
        buy_strike = None
        if self.trade and fr:
            buy_strike = int(fr.leg(self.trade.side).strike)
        elif self.signal and fr:
            buy_strike = int(fr.leg(self.signal.buy_side).strike)
        return {
            "index": self.cfg.index,
            "phase": self.phase.value,
            "merged_at": self.merge_label,
            "merge_overlap": self.merge_gap,
            "merge_red_side": self.merge_red_side,
            "ref_window": self._ref_window_label(),
            "candle_source": self.cfg.candle_source,
            "rest_health": self.rest_health(),
            "pair_mode": self.cfg.pair_mode,
            "entry_mode": self.entry_mode,
            "pair": pair_txt,
            "pair_ce": pair_ce,
            "pair_pe": pair_pe,
            "buy_strike": buy_strike,
            "status": self.status_msg,
            "trades_done": self.trade_count,
            "trades_max": self.cfg.max_trades_per_day,
            "total_pnl": total,
            "packets": self.packet_count,
            "spot": self.spot_ltp,
            "paper": self.cfg.paper_mode,
            "trade_mode": self.cfg.trade_mode,
            "halted": self.trading_halted,
            "halt_reason": self.halt_reason,
            "closed": [
                {"no": t.trade_no, "leg": t.leg_label, "entry": t.entry_price,
                 "exit": t.exit_price, "reason": t.exit_reason, "pnl": t.pnl,
                 "entry_time": t.entry_time, "exit_time": t.exit_time}
                for t in self.closed_trades
            ],
        }

    # ═══════════════════════════════════════════════════════════════════════
    # STATE PERSISTENCE
    # ═══════════════════════════════════════════════════════════════════════

    def save_state(self):
        try:
            with open(self.cfg.state_file, "w", encoding="utf-8") as f:
                fr = self.frame
                json.dump({
                    "date": api.now_ist().strftime("%Y-%m-%d"),
                    "trade_count": self.trade_count,
                    "entry_mode": self.entry_mode,
                    "frame": ({"bucket": fr.bucket,
                               "ce_strike": fr.ce.strike, "pe_strike": fr.pe.strike,
                               "ce_high": fr.ce_candle["high"], "ce_low": fr.ce_candle["low"],
                               "pe_high": fr.pe_candle["high"], "pe_low": fr.pe_candle["low"]}
                              if fr else None),
                    "closed": [
                        {"no": t.trade_no, "leg": t.leg_label,
                         "entry": t.entry_price, "exit": t.exit_price,
                         "reason": t.exit_reason, "pnl": t.pnl,
                         "entry_time": t.entry_time, "exit_time": t.exit_time}
                        for t in self.closed_trades],
                }, f, indent=2)
        except Exception as e:
            log.error(f"State save failed: {e}")

    def load_state(self):
        try:
            if not self.cfg.state_file.exists():
                return
            with open(self.cfg.state_file, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") != api.now_ist().strftime("%Y-%m-%d"):
                return
            self.trade_count = int(d.get("trade_count", 0))
            self.entry_mode = str(d.get("entry_mode", "full"))
            if self.entry_mode == "relaxed":
                self._say("The last trade ended on a stop, so the next entry is "
                          "the relaxed one — a break and a retest, no collapse.")
            if self.trade_count:
                self._say(f"Resuming — {self.trade_count} trade(s) already taken today.")
                if self.trade_count >= self.cfg.max_trades_per_day:
                    self._say("The daily limit was already reached in an earlier "
                              "session. No further entries will be taken.", "warning")
            pf = d.get("frame")
            if pf:
                self._say(f"An earlier session had paired {int(pf['ce_strike'])}CE "
                          f"with {int(pf['pe_strike'])}PE at "
                          f"{api.ist_bucket_label(int(pf['bucket']))}.")
        except Exception as e:
            log.error(f"State load failed: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # RUN
    # ═══════════════════════════════════════════════════════════════════════

    def initialize(self) -> bool:
        errs = self.cfg.validate()
        if errs:
            for e in errs:
                self._say(f"Settings error: {e}", "error")
            return False

        api.set_order_notifier(lambda m, lvl="error": self._say(f"  {m}", lvl))
        api.set_order_abort_check(self.stop_event.is_set)
        if self.cfg.is_live:
            self._say("═══ LIVE MODE — orders will reach the exchange ═══",
                      "warning")
            funds = api.get_available_funds()
            if funds is not None:
                self._say(f"Available funds Rs.{funds:,.0f}")
            else:
                self._say("Could not read available funds. Trading will "
                          "continue, but the pre-entry funds check cannot "
                          "run.", "warning")
        self._say(f"{self.cfg.index} · {self.cfg.timeframe_minutes}-minute candles · "
                  f"{'PAPER' if self.cfg.paper_mode else 'LIVE'} mode")
        self.load_state()

        try:
            api.update_lot_sizes()
        except Exception as e:
            log.warning(f"Lot size refresh failed, using defaults: {e}")
        self._say(f"Lot size {self.cfg.lot_size} · {self.cfg.lots_per_entry} lot(s) "
                  f"= {self.cfg.quantity} qty")

        exp = api.get_target_expiry(self.cfg.index, self.cfg.expiry_offset)
        if not exp:
            self._say("Could not determine an expiry.", "error")
            return False
        self.expiry = exp
        self._say(f"Expiry {exp}")

        self.book.add(self.cfg.index_security_id, f"{self.cfg.index} INDEX")
        win = self._ref_window_label()
        self._say(f"Reference candle is {win} — the ladder locks at "
                  f"{api.ist_bucket_label(self._ladder_ready_epoch())}")
        self._cond("ladder", CondStatus.WATCHING, f"Waiting for the {win} candle")
        self._set_phase(Phase.WAITING_REF, f"Waiting for the {win} candle")
        return True

    def run(self):
        if not self.initialize():
            self._set_phase(Phase.STOPPED, "Initialisation failed")
            return

        threading.Thread(target=self._run_ws, daemon=True).start()
        self._say("Connecting to the market feed...")
        self.ws_connected.wait(timeout=20)
        if not self.ws_connected.is_set():
            self._say("WebSocket did not connect.", "error")
            self._set_phase(Phase.STOPPED, "Feed unavailable")
            return

        interval = self.cfg.interval_seconds
        ladder_retry = 0

        while not self.stop_event.is_set():
            try:
                now_ep = int(time.time())

                # ── Build the ladder once the reference time has passed ──
                if not self.ladder_built and now_ep >= self._ladder_ready_epoch():
                    if now_ep - ladder_retry >= 10:
                        ladder_retry = now_ep
                        self.build_ladder()

                # ── Bar closes ──
                if self.cfg.candle_source == "rest":
                    self._process_rest_buckets(now_ep, interval)
                else:
                    latest_done = self.book.bucket_for(
                        now_ep - self.BAR_GRACE_SECONDS) - interval
                    if self.last_processed_bucket == 0:
                        self.last_processed_bucket = latest_done - interval
                    while self.last_processed_bucket < latest_done:
                        self.last_processed_bucket += interval
                        if self.ladder_built:
                            self._on_bar_close(self.last_processed_bucket)

                self._sync_positions(time.time())
                self._check_eod()

                self._emit("tick", {**self.summary_payload(),
                                    **self.conditions_payload(),
                                    "ladder": self.ladder_payload()})
                time.sleep(1)
            except Exception as e:
                log.error(f"Monitor loop error: {e}", exc_info=True)
                time.sleep(1)

    def _process_rest_buckets(self, now_ep: int, interval: int):
        """Process a bucket once the candle has formed on every chart.

        We wait for the candle, exactly as the SENSEX build does. The feed
        itself already guarantees a bar is only emitted when it is genuinely
        finished — a full set of source minutes, or the grace period elapsed —
        so waiting here simply means waiting for those bars to land. Nothing
        is ever evaluated on a half-formed candle.

        The only backstop is a hard one: if a chart has still said nothing a
        whole candle later, that bucket is abandoned rather than processed
        with part of the picture, and the log says so. Without it a single
        silent instrument would stall the strategy for the rest of the day.
        """
        if not self.ladder_built:
            return

        latest_closed = self.book.bucket_for(now_ep) - interval

        if not self._catchup_done:
            if not self._catch_up(now_ep, interval, latest_closed):
                return

        if self.last_processed_bucket == 0:
            self.last_processed_bucket = latest_closed - interval

        while self.last_processed_bucket < latest_closed:
            nxt = self.last_processed_bucket + interval
            have = len(self._rest_bars.get(nxt, ()))
            want = self._expected_legs()

            if have < want:
                # A full extra candle past this one's close, and still not
                # everything. Abandon the bucket — do not act on part of it.
                if now_ep < nxt + (2 * interval) + self.cfg.rest_grace:
                    return
                self._say(f"  {api.ist_bucket_label(nxt)} abandoned — only "
                          f"{have} of {want} charts ever reported that candle. "
                          f"Nothing was evaluated on it.", "warning")
                self.last_processed_bucket = nxt
                self._rest_bars.pop(nxt, None)
                continue

            self.last_processed_bucket = nxt
            self._on_bar_close(nxt)
            self._rest_bars.pop(nxt, None)

    def _catch_up(self, now_ep: int, interval: int, latest_closed: int) -> bool:
        """Replay the candles that happened before we started.

        Only the merge is replayed. A signal armed an hour ago would have an
        entry band nowhere near the current price, so arming it now would
        either sit unfillable all day or fill badly. History tells us where
        the pair is; the live candle decides whether to trade.

        Returns True once catch-up is finished and normal processing may run.
        """
        first_live = self.ref_bucket() + interval

        # Started on time — there is nothing behind us.
        if (not self.cfg.replay_history_on_late_start
                or self.cfg.candle_source != "rest"
                or latest_closed <= first_live):
            self._catchup_done = True
            return True

        # A trading session is under 400 minutes. Anything longer than that
        # means the reference time or the clock is wrong, and replaying it
        # would loop for hours over candles that cannot exist.
        span = (latest_closed - first_live) // interval + 1
        max_span = int((6.75 * 3600) // interval)
        if span > max_span:
            self._say(f"Skipping the replay — {span} candles between the "
                      f"reference time and now is longer than a trading "
                      f"session. Check the reference time and the system "
                      f"clock. Watching live from here.", "warning")
            self._catchup_done = True
            return True

        # Give every chart a chance to deliver its history. The first poll of
        # a leg returns the whole session at once, so one full cycle is
        # enough; the deadline is only there in case a chart never answers.
        if not self._catchup_started:
            self._catchup_started = now_ep
            n = (latest_closed - first_live) // interval + 1
            self._say(f"Started late — {n} candle(s) have already happened. "
                      f"Fetching them to look for the merge.")
            return False

        delivered = {sid for b in self._rest_bars.values() for sid in b}
        waited = now_ep - self._catchup_started
        if len(delivered) < len(self.legs) and waited < 30:
            return False

        replayed = 0
        b = first_live
        while b <= latest_closed:
            if self.frame is None:
                self._on_bar_close(b, merge_only=True)
                replayed += 1
            self._rest_bars.pop(b, None)
            b += interval

        if self.frame is not None:
            self._say(f"Found the merge in the replayed candles. The four "
                      f"lines are drawn and trading starts from the live "
                      f"candle — nothing that happened while we were off is "
                      f"acted on.")
        else:
            self._say(f"Replayed {replayed} candle(s); no merge in any of "
                      f"them. Watching live from here.")

        self.last_processed_bucket = latest_closed
        self._catchup_done = True
        return True

    def rest_health(self) -> dict:
        return self.rest.health() if self.rest else {}

    def stop(self):
        self._say("Stopping. No further orders will be sent.")
        self.stop_event.set()
        tr = self.trade
        if tr is not None and tr.is_open and self.cfg.is_live:
            self._say(f"  A LIVE POSITION IS STILL OPEN — {tr.qty} of "
                      f"{tr.leg_label}. Stopping the app does not close it. "
                      f"Square it off in the broker terminal, or restart and "
                      f"let the app manage it.", "error")
        if self.rest:
            try:
                self.rest.stop()
            except Exception:
                pass
        self.save_state()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self._set_phase(Phase.STOPPED, "Stopped")
