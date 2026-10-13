#!/usr/bin/env python3
"""
The Overlap — Supervisor
════════════════════════
Runs one engine per selected index.

Each index is a separate strategy in every sense that matters: its own
ladder of ten, its own pair, its own four lines, its own signals, its own
trades and its own daily limit. Nothing is shared but the broker account.

So rather than teaching the engine to hold three of everything — and risking
every rule, guard and fix built into it — the supervisor simply runs three
engines. Each one is the same object that has been running NIFTY alone, given
a copy of the settings carrying a single index.

What is shared, and therefore what the supervisor has to think about:

  * the account          — funds, positions, the daily trade limits are per
                           index but the money is not
  * the REST rate gate   — every engine's polling queues through one gate, so
                           three ladders is three times the traffic
  * the order path       — a permanent refusal on one index is a refusal on
                           all of them, so a halt stops everything

Balfund Trading Pvt Ltd | www.balfund.com
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import dhan_api as api
from config import INDEX_CONFIG, OverlapConfig
from strategy import OverlapEngine, Phase

log = logging.getLogger("Overlap")


class OverlapSupervisor:

    def __init__(self, config: OverlapConfig, gui_callback: Optional[Callable] = None):
        self.cfg = config
        self.gui = gui_callback
        self.engines: Dict[str, OverlapEngine] = {}
        self.threads: Dict[str, threading.Thread] = {}
        self.stop_event = threading.Event()
        self._halt_shared = False

    # ─── helpers ───────────────────────────────────────────────────────────

    @property
    def indices(self) -> List[str]:
        return list(self.cfg.indices)

    @property
    def multi(self) -> bool:
        return len(self.indices) > 1

    def _say(self, msg: str, level: str = "info"):
        getattr(log, level if level in ("info", "warning", "error") else "info")(msg)
        if self.gui:
            try:
                self.gui("log", {"msg": msg, "level": level, "index": "",
                                 "time": api.now_ist().strftime("%H:%M:%S")})
            except Exception:
                pass

    # ─── polling load ──────────────────────────────────────────────────────

    def _advise_poll_rate(self) -> float:
        """Three ladders is thirty instruments through one 4-per-second gate.

        The gate serialises, so asking for a 4-second cycle when a cycle
        physically takes 7.5 seconds does not make it faster — it just means
        the feed never rests. Better to ask for something achievable and say
        so, than to pretend.
        """
        poll = float(self.cfg.rest_poll)
        if self.cfg.candle_source != "rest":
            return poll

        per_index = 2 * (2 * self.cfg.strikes_each_side + 1)
        total = per_index * len(self.indices)
        gate_per_sec = 4.0
        cycle = total / gate_per_sec

        if cycle <= poll:
            return poll

        advised = round(cycle + 1.0, 1)
        self._say(f"{len(self.indices)} indices means {total} charts polled "
                  f"through one rate limit — a full cycle takes about "
                  f"{cycle:.0f}s, so the {poll:.0f}s poll cannot be met. "
                  f"Using {advised:.0f}s instead. Bars will still be complete; "
                  f"they will simply be noticed a few seconds later.",
                  "warning")
        return advised

    # ─── lifecycle ─────────────────────────────────────────────────────────

    def run(self):
        errs = self.cfg.validate()
        if errs:
            for e in errs:
                self._say(f"Settings error: {e}", "error")
            return

        names = ", ".join(self.indices)
        if self.multi:
            self._say(f"Running {len(self.indices)} indices — {names}. Each "
                      f"keeps its own ladder, its own pair and its own daily "
                      f"limit; only the account is shared.")
        poll = self._advise_poll_rate()

        for name in self.indices:
            sub = self.cfg.for_index(name)
            sub.rest_poll = poll
            eng = OverlapEngine(sub, gui_callback=self._on_engine_event)
            eng.tag_logs = self.multi
            self.engines[name] = eng

        # Started one at a time. Each does its own login-free setup, expiry
        # lookup and option chain, and launching three at once would put all
        # of that through the rate limit simultaneously.
        for name, eng in self.engines.items():
            if self.stop_event.is_set():
                break
            t = threading.Thread(target=self._run_one, args=(name, eng),
                                 daemon=True, name=f"ovl-{name}")
            self.threads[name] = t
            t.start()
            time.sleep(1.5)

        # Stay alive while any engine is, and watch for a shared halt.
        while not self.stop_event.is_set():
            if not any(t.is_alive() for t in self.threads.values()):
                break
            self._check_shared_halt()
            time.sleep(1.0)

    def _run_one(self, name: str, eng: OverlapEngine):
        try:
            eng.run()
        except Exception as e:
            log.error(f"[{name}] engine stopped: {e}", exc_info=True)
            self._say(f"[{name}] stopped: {e}", "error")

    def _check_shared_halt(self):
        """A permanent order refusal is about the account, not the index.

        If one engine is halted because the IP is not whitelisted, the others
        will be refused for exactly the same reason. Stopping them all at once
        is both truthful and quieter than three copies of the same discovery.
        """
        if self._halt_shared:
            return
        halted = [n for n, e in self.engines.items() if e.trading_halted]
        if not halted:
            return
        self._halt_shared = True
        reason = self.engines[halted[0]].halt_reason
        others = [n for n in self.engines if n not in halted]
        if others:
            self._say(f"{halted[0]} was refused by the broker ({reason}). That "
                      f"is an account-level refusal, so {', '.join(others)} "
                      f"would be refused too — halting them as well.", "error")
            for n in others:
                e = self.engines[n]
                e.trading_halted = True
                e.halt_reason = reason

    def stop(self):
        self.stop_event.set()
        for name, eng in self.engines.items():
            try:
                eng.stop()
            except Exception as e:
                log.error(f"[{name}] stop error: {e}")

    # ─── events ────────────────────────────────────────────────────────────

    def _on_engine_event(self, event: str, data: dict):
        if self.gui:
            try:
                self.gui(event, data)
            except Exception as e:
                log.error(f"GUI callback error: {e}")

    # ─── aggregate view ────────────────────────────────────────────────────

    def summary(self) -> dict:
        per = {}
        total_pnl = 0.0
        trades = 0
        for name, eng in self.engines.items():
            s = eng.summary_payload()
            per[name] = s
            total_pnl += s.get("total_pnl", 0.0) or 0.0
            trades += s.get("trades_done", 0) or 0
        return {"indices": self.indices, "per_index": per,
                "total_pnl": total_pnl, "trades_done": trades,
                "multi": self.multi}
