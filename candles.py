#!/usr/bin/env python3
"""
The Overlap — Candle Engine
═══════════════════════════
Builds N-minute candles from ticks for every instrument in the ladder.

The Overlap scan has to compare ten charts against each other on the SAME
bar, so bar closes must be deterministic rather than tick-driven. Each
instrument keeps its bars in a dict keyed by bucket; the strategy loop
asks for a specific bucket once the wall clock has passed its end.

Bucket alignment note: the NSE/BSE session opens at 09:15 IST, which is
13500 seconds into the UTC day. 13500 is an exact multiple of both 180 and
300, so plain epoch bucketing lands on 09:15, 09:18, 09:21 ... for the
3-minute setting and 09:15, 09:20, 09:25 ... for the 5-minute one.

Balfund Trading Pvt Ltd | www.balfund.com
"""

import threading
from typing import Dict, List, Optional

RED = "red"
GREEN = "green"
DOJI = "doji"


class Candle:
    __slots__ = ("bucket", "open", "high", "low", "close", "ticks")

    def __init__(self, bucket: int, price: float):
        self.bucket = int(bucket)
        self.open = float(price)
        self.high = float(price)
        self.low = float(price)
        self.close = float(price)
        self.ticks = 1

    def update(self, price: float):
        p = float(price)
        if p > self.high:
            self.high = p
        if p < self.low:
            self.low = p
        self.close = p
        self.ticks += 1

    # ─── Shape helpers ─────────────────────────────────────────────────────

    @property
    def colour(self) -> str:
        if self.close > self.open:
            return GREEN
        if self.close < self.open:
            return RED
        return DOJI

    @property
    def is_red(self) -> bool:
        return self.close < self.open

    @property
    def is_green(self) -> bool:
        return self.close > self.open

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_pct(self) -> float:
        """Body as a percentage of the total candle height.

        A candle with no range at all (a single-tick bar) is treated as
        100 percent body — it has no wick to be indecisive with.
        """
        r = self.range
        if r <= 0:
            return 100.0
        return (self.body / r) * 100.0

    @property
    def body_top(self) -> float:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        return min(self.open, self.close)

    def overlaps(self, other: "Candle") -> bool:
        """True when the two candles' full ranges cover common ground."""
        return (self.low <= other.high) and (other.low <= self.high)

    def body_overlap(self, other: "Candle") -> float:
        """How much of the two BODIES cover the same stretch of price.

        Wicks are ignored — only the open-to-close block counts. A positive
        number is the depth of the overlap; zero or negative means the bodies
        do not meet.
        """
        return (min(self.body_top, other.body_top)
                - max(self.body_bottom, other.body_bottom))

    def to_dict(self) -> dict:
        return {"bucket": self.bucket, "open": round(self.open, 2),
                "high": round(self.high, 2), "low": round(self.low, 2),
                "close": round(self.close, 2), "ticks": self.ticks,
                "colour": self.colour}

    def __repr__(self):
        return (f"<Candle {self.bucket} O{self.open:.2f} H{self.high:.2f} "
                f"L{self.low:.2f} C{self.close:.2f} {self.colour}>")


class CandleSeries:
    """One instrument's chart."""

    MAX_BARS = 600

    def __init__(self, sec_id: str, label: str, interval: int):
        self.sec_id = str(sec_id)
        self.label = label
        self.interval = int(interval)
        self.bars: Dict[int, Candle] = {}
        self.current: Optional[Candle] = None
        self.last_ltp: Optional[float] = None
        self.last_ltt: int = 0
        self.tick_count = 0
        self._lock = threading.Lock()

    # ─── Ingest ────────────────────────────────────────────────────────────

    def on_tick(self, ltp: float, ltt: int):
        bucket = int(ltt) - (int(ltt) % self.interval)
        with self._lock:
            self.last_ltp = float(ltp)
            self.last_ltt = int(ltt)
            self.tick_count += 1

            if self.current is None:
                self.current = Candle(bucket, ltp)
                return

            if bucket == self.current.bucket:
                self.current.update(ltp)
                return

            if bucket > self.current.bucket:
                self.bars[self.current.bucket] = self.current
                self._trim()
                self.current = Candle(bucket, ltp)

    def apply_bar(self, bucket: int, o: float, h: float, l: float,
                  c: float) -> "Candle":
        """Install a completed bar, overwriting anything already there.

        Used by the REST feed. The broker's own candle is authoritative — if
        a tick-built bar exists for the same bucket it was our reconstruction
        of the same thing, and this one is the original.
        """
        with self._lock:
            cd = Candle(int(bucket), float(o))
            cd.open, cd.high, cd.low, cd.close = (float(o), float(h),
                                                  float(l), float(c))
            self.bars[int(bucket)] = cd
            self._trim()
            if self.current is not None and self.current.bucket <= int(bucket):
                self.current = None
            return cd

    def seed_from_minutes(self, minute_candles: List[dict]):
        """Fold 1-minute history into this series' timeframe.

        Used when the app is started after 09:30 so the ladder still has
        a chart to work with.
        """
        with self._lock:
            for m in minute_candles:
                ts = int(m.get("timestamp", 0) or 0)
                if ts <= 0:
                    continue
                bucket = ts - (ts % self.interval)
                c = self.bars.get(bucket)
                if c is None:
                    c = Candle(bucket, m["open"])
                    c.high = float(m["high"])
                    c.low = float(m["low"])
                    c.close = float(m["close"])
                    self.bars[bucket] = c
                else:
                    c.high = max(c.high, float(m["high"]))
                    c.low = min(c.low, float(m["low"]))
                    c.close = float(m["close"])
                    c.ticks += 1
            self._trim()

    def _trim(self):
        if len(self.bars) > self.MAX_BARS:
            for b in sorted(self.bars.keys())[:-self.MAX_BARS]:
                self.bars.pop(b, None)

    # ─── Query ─────────────────────────────────────────────────────────────

    def finalise(self, bucket: int) -> Optional[Candle]:
        """Return the completed candle for `bucket`.

        If the live candle is still sitting on that bucket (because no tick
        has yet arrived for the next one), close it out where it stands.
        """
        bucket = int(bucket)
        with self._lock:
            c = self.bars.get(bucket)
            if c is not None:
                return c
            if self.current is not None and self.current.bucket == bucket:
                self.bars[bucket] = self.current
                self.current = None
                self._trim()
                return self.bars[bucket]
        return None

    def get(self, bucket: int) -> Optional[Candle]:
        with self._lock:
            return self.bars.get(int(bucket))

    def last_closed(self) -> Optional[Candle]:
        with self._lock:
            if not self.bars:
                return None
            return self.bars[max(self.bars.keys())]

    def recent(self, n: int = 20) -> List[Candle]:
        with self._lock:
            keys = sorted(self.bars.keys())[-n:]
            return [self.bars[k] for k in keys]

    @property
    def ltp(self) -> Optional[float]:
        return self.last_ltp


class CandleBook:
    """All ten charts, plus the index."""

    def __init__(self, interval: int):
        self.interval = int(interval)
        self.series: Dict[str, CandleSeries] = {}
        self._lock = threading.Lock()

    def add(self, sec_id: str, label: str) -> CandleSeries:
        with self._lock:
            s = self.series.get(str(sec_id))
            if s is None:
                s = CandleSeries(sec_id, label, self.interval)
                self.series[str(sec_id)] = s
            return s

    def get(self, sec_id: str) -> Optional[CandleSeries]:
        return self.series.get(str(sec_id))

    def on_tick(self, sec_id: str, ltp: float, ltt: int):
        s = self.series.get(str(sec_id))
        if s is not None:
            s.on_tick(ltp, ltt)

    def clear(self):
        with self._lock:
            self.series.clear()

    def bucket_for(self, epoch: int) -> int:
        return int(epoch) - (int(epoch) % self.interval)

    def finalise_all(self, bucket: int) -> Dict[str, Candle]:
        """Close out `bucket` on every chart and return what we have."""
        out = {}
        for sec_id, s in list(self.series.items()):
            c = s.finalise(bucket)
            if c is not None:
                out[sec_id] = c
        return out
