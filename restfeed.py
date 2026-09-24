#!/usr/bin/env python3
"""
The Overlap — REST candle feed
══════════════════════════════
Candles from the historical API instead of from websocket ticks.

Why this exists
───────────────
Every decision the Overlap makes is a comparison of raw OHLC values at
five-paise resolution: the two bodies must share 0.05, the four borrowed lines
come from one candle's high and low, the collapse compares a body top against a
line, the break compares a close against a line.

Candles built from ticks are only as good as the ticks that arrived. On
01-Sep the feed dropped five times in the forty minutes before the merge
candle formed, and nothing backfills a gap — a candle spanning a disconnect
has a truncated high and low. The lines drawn from that candle then governed
the whole session.

REST returns the candle the broker itself recorded, so it does not matter
whether our socket was up.

The split
─────────
    REST       candles -> the merge, the four lines, the collapse, the break
    WebSocket  LTP only -> the retest fill, the target, the safety stop

Everything decided on a candle close moves to REST. Everything decided on live
price stays on the socket, where it belongs.

The cost
────────
REST is pull, not push: nothing arrives unless we ask. Every emitted bar
records how long after its close it became available, and that is logged and
surfaced, so the latency is measured in production rather than assumed.

Balfund Trading Pvt Ltd | www.balfund.com
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, Dict, List, Optional

log = logging.getLogger("Overlap")


def aggregate_minutes(raw: List[dict], interval: int) -> List[dict]:
    """Roll 1-minute bars up into `interval`-second buckets.

    Bucket alignment is plain epoch arithmetic, the same as the tick engine
    uses: 09:15 IST is 13500 seconds into the UTC day, and 13500 divides
    exactly by both 180 and 300.
    """
    groups: Dict[int, dict] = {}
    for c in sorted(raw, key=lambda x: x["ts"]):
        ts = int(c["ts"])
        b = ts - (ts % interval)
        g = groups.get(b)
        if g is None:
            groups[b] = {"ts": b, "open": c["open"], "high": c["high"],
                         "low": c["low"], "close": c["close"], "members": 1}
        else:
            g["high"] = max(g["high"], c["high"])
            g["low"] = min(g["low"], c["low"])
            g["close"] = c["close"]
            g["members"] += 1
    return [groups[k] for k in sorted(groups)]


class RestCandleFeed:
    """Polls the 1-minute endpoint for each leg and emits completed bars.

    A rolled-up bar is emitted only when it is genuinely finished:

      * it holds a full `interval / 60` source minutes, or
      * the wall clock has passed its close by `grace` seconds

    The second rule matters on the outer strikes of the ladder. A deep option
    may not trade at all during a minute, and Dhan then returns no bar for
    that minute — so the group can never be complete by counting. A missing
    minute means nothing traded and the price did not move, so declaring the
    bar finished with what we have is correct rather than a compromise.
    """

    def __init__(self, legs: Dict[str, str], fetch_1m: Callable,
                 on_bar: Callable, interval: int = 180,
                 poll: float = 4.0, grace: float = 6.0,
                 on_latency: Optional[Callable] = None,
                 session_anchor: Optional[int] = None,
                 on_problem: Optional[Callable] = None):
        self.legs = dict(legs)              # {sec_id: label}
        self.fetch_1m = fetch_1m
        self.on_bar = on_bar                # on_bar(sec_id, bar_dict)
        self.on_latency = on_latency
        # Routed to the interface. A feed that fails only into a
        # log file fails invisibly.
        self.on_problem = on_problem
        self.interval = int(interval)
        self.period = max(1, self.interval // 60)
        self.poll = float(poll)
        self.grace = float(grace)

        # Nothing before today's reference candle may ever reach the strategy.
        # A poll made before the open otherwise returns the whole of the
        # previous session and every bar is emitted as though it were live.
        self.session_anchor = int(session_anchor) if session_anchor else 0

        self._last_ts: Dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self.latencies: List[float] = []
        self.polls = 0
        self.errors = 0
        self.dropped_forming = 0
        self.dropped_stale = 0
        self.late_bars = 0
        self.stalls = 0
        self.last_stall = 0.0
        self.stall_seconds = 60.0
        self._warned_at = 0.0
        self._suppressed = 0
        self._abandoned = 0
        self.empty_polls = 0
        self._warned_empty: set = set()

        # A socket timeout is not a guarantee — a call can sit for many
        # minutes on a dead keep-alive despite a read timeout. Each poll
        # therefore runs in a worker with a hard deadline, so one stuck
        # request cannot take the whole feed down with it.
        self.hard_deadline = 25.0
        self._pool = ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix="ovlpoll")

    # ─── lifecycle ─────────────────────────────────────────────────────────

    def set_legs(self, legs: Dict[str, str]):
        """Narrow or widen what is polled. Once the pair is locked only its
        two charts matter, so there is no reason to keep asking for the
        other eight."""
        with self._lock:
            self.legs = dict(legs)

    def prime(self, sec_id: str, upto_ts: int):
        """Mark everything at or before `upto_ts` as handled, so bars replayed
        during seeding are not emitted a second time."""
        self._last_ts[sec_id] = max(self._last_ts.get(sec_id, 0), int(upto_ts))

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info(f"  REST candle feed started — polling every {self.poll:.0f}s, "
                 f"{self.period}-minute rollup, grace {self.grace:.0f}s")

    def stop(self):
        self._stop.set()
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass

    def _problem(self, msg: str):
        log.error(msg)
        if self.on_problem:
            try:
                self.on_problem(msg)
            except Exception:
                pass

    # ─── internals ─────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                legs = list(self.legs.items())
            for sec_id, label in legs:
                if self._stop.is_set():
                    break
                t0 = time.time()
                try:
                    fut = self._pool.submit(self._poll_leg, sec_id, label)
                    fut.result(timeout=self.hard_deadline)
                except FutureTimeout:
                    self._abandoned += 1
                    self.errors += 1
                    self._problem(f"[{label}] poll exceeded "
                                  f"{self.hard_deadline:.0f}s and was "
                                  f"abandoned — carrying on rather than "
                                  f"blocking the feed.")
                except Exception as e:
                    self.errors += 1
                    self._problem(f"[{label}] REST poll error: "
                                  f"{type(e).__name__}: {e}")
                took = time.time() - t0
                if took > self.stall_seconds:
                    self.stalls += 1
                    self.last_stall = took
                    self._problem(f"[{label}] poll BLOCKED for {took:.0f}s — "
                                  f"the strategy was blind for that period "
                                  f"({self.stalls} stall(s) today)")
            self._stop.wait(self.poll)

    @staticmethod
    def drop_forming(raw: List[dict], now: float, minute: int = 60) -> List[dict]:
        """Remove the minute that is still being built.

        Dhan returns the CURRENT minute as soon as it starts and keeps
        updating its high, low and close. That is fine for a chart and fatal
        here: a three-minute bar is three one-minute bars, so a group can look
        complete while its last member is two seconds old, and we would draw
        the day's four lines from a candle that has not happened yet.
        """
        return [c for c in raw if int(c["ts"]) + minute <= now]

    def _poll_leg(self, sec_id: str, label: str):
        raw = self.fetch_1m(sec_id)
        self.polls += 1
        if not raw:
            self.empty_polls += 1
            # Say it once per leg. A silent empty response looks exactly like
            # a working feed with nothing to report.
            if sec_id not in self._warned_empty:
                self._warned_empty.add(sec_id)
                self._problem(f"[{label}] the history endpoint returned no "
                              f"bars. If this persists no candle will ever "
                              f"arrive for this chart.")
            return
        self._warned_empty.discard(sec_id)

        now_pre = time.time()
        before = len(raw)
        raw = self.drop_forming(raw, now_pre)
        self.dropped_forming += (before - len(raw))
        if not raw:
            return

        bars = aggregate_minutes(raw, self.interval)
        if not bars:
            return

        now = time.time()
        last = self._last_ts.get(sec_id, 0)

        for b in bars:
            if b["ts"] <= last:
                continue
            if self.session_anchor and b["ts"] < self.session_anchor:
                self.dropped_stale += 1
                continue

            closed_at = b["ts"] + self.interval
            complete = (b["members"] >= self.period
                        or now >= closed_at + self.grace)
            if not complete:
                continue

            lag = now - closed_at
            self.latencies.append(lag)
            if self.on_latency:
                try:
                    self.on_latency(sec_id, b["ts"], lag)
                except Exception:
                    pass

            if lag > 45:
                # One warning per backlogged bar buries everything else, so
                # rate-limit the message but keep counting.
                if now - self._warned_at > 60:
                    extra = (f" ({self._suppressed} similar suppressed)"
                             if self._suppressed else "")
                    log.warning(f"  [{label}] bar arrived {lag:.0f}s late — "
                                f"signals are running behind{extra}")
                    self._warned_at = now
                    self._suppressed = 0
                else:
                    self._suppressed += 1
                self.late_bars += 1

            self._last_ts[sec_id] = b["ts"]
            try:
                self.on_bar(sec_id, b)
            except Exception as e:
                log.error(f"  [{label}] bar handler error: {e}")

    # ─── health ────────────────────────────────────────────────────────────

    def health(self) -> dict:
        base = {"bars": len(self.latencies), "polls": self.polls,
                "errors": self.errors, "dropped_forming": self.dropped_forming,
                "dropped_stale": self.dropped_stale, "stalls": self.stalls,
                "late_bars": self.late_bars, "abandoned": self._abandoned,
                "empty_polls": self.empty_polls}
        if not self.latencies:
            base.update({"avg_lag": None, "max_lag": None,
                         "verdict": "no bars yet"})
            return base
        avg = sum(self.latencies) / len(self.latencies)
        mx = max(self.latencies)
        base.update({"avg_lag": avg, "max_lag": mx,
                     "verdict": ("comfortable" if mx < 10 else
                                 "tight" if mx < 30 else
                                 "TOO SLOW — signals are arriving late")})
        return base
