#!/usr/bin/env python3
"""
The Overlap — Desktop Application
═════════════════════════════════
Live condition cards, the ladder of ten, and an activity log.

Balfund Trading Pvt Ltd | www.balfund.com
"""

import json
import logging
import os
import queue
import sys
import threading
from datetime import datetime, time as dtime
from pathlib import Path

import customtkinter as ctk
from dotenv import load_dotenv, set_key

from config import (APP_NAME, APP_VERSION, ENV_FILE, FONTS, INDEX_CONFIG,
                    LOG_DIR, PALETTE as P, PAPER_ONLY_BUILD, SETTINGS_FILE,
                    STRATEGY_NOTE_VERSION, OverlapConfig)
import dhan_api as api
from strategy import OverlapEngine
from supervisor import OverlapSupervisor

# ─── Logging ──────────────────────────────────────────────────────────────
_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(str(LOG_DIR / f"ovl_activity_{_ts}.log"),
                                  encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("Overlap")

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

STATUS_COLOURS = {
    "idle":     (P["pending_soft"], P["pending"], "○"),
    "watching": (P["gold_soft"],    P["gold"],    "◐"),
    "met":      (P["green_soft"],   P["green"],   "●"),
    "failed":   (P["red_soft"],     P["red"],     "✕"),
}

CARD_ACCENTS = {
    "ladder":   P["teal"],
    "overlap":  P["violet"],
    "frame":    P["accent"],
    "collapse": P["red"],
    "breakout": P["green"],
    "retest":   P["navy"],
    "exit":     P["gold"],
}


# ═══════════════════════════════════════════════════════════════════════════
# CONDITION CARD
# ═══════════════════════════════════════════════════════════════════════════

class ConditionCard(ctk.CTkFrame):
    def __init__(self, master, key, index, title, subtitle):
        super().__init__(master, fg_color=P["card"], corner_radius=10,
                         border_width=2, border_color=P["border"])
        self.key = key
        self.accent = CARD_ACCENTS.get(key, P["accent"])
        self._status = "idle"

        self.grid_columnconfigure(1, weight=1)

        self.badge = ctk.CTkLabel(self, text=str(index), width=30, height=30,
                                  corner_radius=15, fg_color=P["pending_soft"],
                                  text_color=P["pending"], font=FONTS["mid"])
        self.badge.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=12)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=1, sticky="ew", pady=(11, 0), padx=(0, 10))
        head.grid_columnconfigure(0, weight=1)

        self.lbl_title = ctk.CTkLabel(head, text=title, font=FONTS["head"],
                                      text_color=P["text"], anchor="w")
        self.lbl_title.grid(row=0, column=0, sticky="w")

        self.lbl_value = ctk.CTkLabel(head, text="", font=FONTS["mid"],
                                      text_color=P["dim"], anchor="e")
        self.lbl_value.grid(row=0, column=1, sticky="e")

        self.lbl_sub = ctk.CTkLabel(self, text=subtitle, font=FONTS["tiny"],
                                    text_color=P["dim"], anchor="w",
                                    justify="left", wraplength=560)
        self.lbl_sub.grid(row=1, column=1, sticky="ew", padx=(0, 10))

        self.lbl_detail = ctk.CTkLabel(self, text="—", font=FONTS["small"],
                                       text_color=P["text"], anchor="w",
                                       justify="left", wraplength=620)
        self.lbl_detail.grid(row=2, column=0, columnspan=2, sticky="ew",
                             padx=12, pady=(6, 11))

    def update_state(self, status, detail, value):
        bg, fg, glyph = STATUS_COLOURS.get(status, STATUS_COLOURS["idle"])
        if status == "met":
            fg = self.accent
            bg = P["card_alt"]
        self.configure(border_color=fg if status != "idle" else P["border"],
                       border_width=2 if status in ("met", "watching") else 1)
        self.badge.configure(fg_color=bg, text_color=fg, text=glyph)
        self.lbl_title.configure(text_color=fg if status == "met" else P["text"])
        self.lbl_detail.configure(text=detail or "—",
                                  text_color=P["text"] if status != "idle" else P["dim"])
        self.lbl_value.configure(text=value or "", text_color=fg)
        self._status = status


# ═══════════════════════════════════════════════════════════════════════════
# APPLICATION
# ═══════════════════════════════════════════════════════════════════════════

class OverlapApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        load_dotenv(str(ENV_FILE), override=True)

        self.title(f"{APP_NAME} v{APP_VERSION} — Balfund Trading")
        self.geometry("1560x960")
        self.minsize(1320, 840)
        self.configure(fg_color=P["bg"])

        self.engine = None
        self.engine_thread = None
        self.is_running = False
        # Each start gets its own number. A worker that dies only resets the
        # UI if it is still the current one — otherwise a failed login can
        # re-enable START while a later engine is running.
        self._worker_seq = 0
        self._auth_stop = threading.Event()
        self.events = queue.Queue()
        self.cards = {}
        self.ladder_rows = {}
        self.vars = {}

        self._build_ui()
        self._load_settings()
        self._load_env()
        self._pump()

    # ═══════════════════════════════════════════════════════════════════════
    # LAYOUT
    # ═══════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        self._build_topbar()

        body = ctk.CTkFrame(self, fg_color=P["bg"])
        body.pack(fill="both", expand=True, padx=10, pady=(6, 8))

        # NOTE: no pack_propagate(False) here — on a CTkScrollableFrame it
        # collapses the inner canvas and the panel renders empty.
        self.left = ctk.CTkScrollableFrame(body, fg_color=P["card"], width=300,
                                           corner_radius=10, border_width=1,
                                           border_color=P["border"])
        self.left.pack(side="left", fill="y", padx=(0, 6))

        centre = ctk.CTkFrame(body, fg_color=P["bg"])
        centre.pack(side="left", fill="both", expand=True, padx=6)

        self.right = ctk.CTkFrame(body, fg_color=P["card"], width=340,
                                  corner_radius=10, border_width=1,
                                  border_color=P["border"])
        self.right.pack(side="left", fill="y", padx=(6, 0))
        self.right.pack_propagate(False)

        self._build_settings(self.left)
        self._build_centre(centre)
        self._build_right(self.right)

    # ─── Top bar ───────────────────────────────────────────────────────────

    def _build_topbar(self):
        top = ctk.CTkFrame(self, fg_color=P["navy"], height=62, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", padx=18)
        ctk.CTkLabel(left, text="◆  THE OVERLAP", font=("Segoe UI", 20, "bold"),
                     text_color="#ffffff").pack(anchor="w", pady=(9, 0))
        ctk.CTkLabel(left, text=f"Strategy Note 03 · v{STRATEGY_NOTE_VERSION}"
                                f"   ·   Balfund Trading Pvt Ltd",
                     font=FONTS["tiny"], text_color="#9db8d4").pack(anchor="w")

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right", padx=18)

        self.lbl_clock = ctk.CTkLabel(right, text="--:--:--",
                                      font=("Segoe UI", 17, "bold"),
                                      text_color="#ffffff")
        self.lbl_clock.pack(side="right", padx=(14, 0))

        self.lbl_mode = ctk.CTkLabel(right, text="PAPER", font=FONTS["small"],
                                     fg_color="#2f7d4f", text_color="#ffffff",
                                     corner_radius=6, width=64, height=24)
        self.lbl_mode.pack(side="right", padx=8)

        self.lbl_ws = ctk.CTkLabel(right, text="FEED  ○", font=FONTS["small"],
                                   text_color="#9db8d4")
        self.lbl_ws.pack(side="right", padx=8)

        self.lbl_bars = ctk.CTkLabel(right, text="BARS  —", font=FONTS["small"],
                                     text_color="#9db8d4")
        self.lbl_bars.pack(side="right", padx=8)

        self.lbl_phase = ctk.CTkLabel(right, text="● IDLE", font=FONTS["mid"],
                                      text_color="#f0c674")
        self.lbl_phase.pack(side="right", padx=10)

    # ─── Settings panel ────────────────────────────────────────────────────

    def _section(self, parent, text):
        ctk.CTkLabel(parent, text=text.upper(), font=FONTS["tiny"],
                     text_color=P["accent"], anchor="w").pack(
            fill="x", padx=14, pady=(14, 4))
        ctk.CTkFrame(parent, height=1, fg_color=P["border"]).pack(
            fill="x", padx=14, pady=(0, 6))

    def _field(self, parent, label, key, default, width=110):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(row, text=label, font=FONTS["body"], text_color=P["text"],
                     anchor="w").pack(side="left")
        var = ctk.StringVar(value=str(default))
        ctk.CTkEntry(row, textvariable=var, width=width, height=26,
                     font=FONTS["body"], border_color=P["border"]).pack(side="right")
        self.vars[key] = var
        return var

    def _option(self, parent, label, key, values, default, width=110):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(row, text=label, font=FONTS["body"], text_color=P["text"],
                     anchor="w").pack(side="left")
        var = ctk.StringVar(value=str(default))
        ctk.CTkOptionMenu(row, variable=var, values=values, width=width, height=26,
                          font=FONTS["body"], fg_color=P["accent_soft"],
                          text_color=P["text"], button_color=P["accent"],
                          dropdown_font=FONTS["body"]).pack(side="right")
        self.vars[key] = var
        return var

    def _switch(self, parent, label, key, default=True):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(row, text=label, font=FONTS["body"], text_color=P["text"],
                     anchor="w").pack(side="left")
        var = ctk.BooleanVar(value=default)
        ctk.CTkSwitch(row, text="", variable=var, width=42,
                      progress_color=P["accent"]).pack(side="right")
        self.vars[key] = var
        return var

    def _build_settings(self, parent):
        ctk.CTkLabel(parent, text="Settings", font=FONTS["title"],
                     text_color=P["text"], anchor="w").pack(
            fill="x", padx=14, pady=(14, 0))

        self._section(parent, "Credentials")
        self._field(parent, "Client ID", "client_id", "", 150)
        self._field(parent, "PIN", "pin", "", 150)
        self._field(parent, "TOTP secret", "totp", "", 150)

        self._section(parent, "Candles")
        self._option(parent, "Source", "candle_source",
                     ["Broker bars (REST)", "Live ticks"],
                     "Broker bars (REST)", 150)
        self._field(parent, "Poll every (s)", "rest_poll", "4.0", 70)
        self._field(parent, "Grace (s)", "rest_grace", "6.0", 70)
        self._switch(parent, "Replay on late start",
                     "replay_history_on_late_start", True)

        self._section(parent, "The ladder")
        self._index_checks(parent)
        self._option(parent, "Timeframe", "timeframe_minutes", ["3", "5"], "3")
        self._field(parent, "Reference time", "ref_time", "09:30:00")
        self._option(parent, "Reference candle", "ref_candle",
                     ["Starting at that time", "Ending at that time"],
                     "Starting at that time", 150)
        self._field(parent, "Strikes each side", "strikes_each_side", "2", 70)
        self._option(parent, "Expiry", "expiry_offset",
                     ["Current", "Next"], "Current", 100)

        self._section(parent, "The overlap")
        self._option(parent, "Pair", "pair_mode",
                     ["Same strike", "Any strike"], "Same strike", 120)
        self._field(parent, "Min body overlap", "min_overlap", "0.05", 80)
        self._switch(parent, "Bodies must overlap", "require_overlap", True)

        self._section(parent, "The signal")
        self._field(parent, "Minimum body %", "min_body_pct", "50", 80)
        self._field(parent, "Signal window (bars)", "signal_window_bars", "0", 70)

        self._section(parent, "After a stop")
        self._switch(parent, "Relaxed entry", "relaxed_after_stop", True)
        self._switch(parent, "Guard relaxed entry", "guard_relaxed_entry", True)

        self._section(parent, "The entry")
        self._field(parent, "Retest buffer (pts)", "retest_buffer", "4.0", 80)

        self._section(parent, "The exit")
        self._switch(parent, "Two-sided stop", "two_sided_stop", True)
        self._field(parent, "Target %", "target_pct", "15", 80)
        self._switch(parent, "Safety stop", "safety_stop_enabled", True)
        self._field(parent, "Safety stop %", "safety_stop_pct", "25", 80)

        self._section(parent, "Execution")
        self._option(parent, "Mode", "trade_mode", ["paper", "live"], "paper", 100)
        self._field(parent, "Max slippage %", "max_entry_slippage_pct", "3.0", 70)
        self._field(parent, "Position sync (s)", "position_sync_seconds", "15", 70)
        self._field(parent, "Exit retries", "exit_retry_attempts", "5", 70)
        self._field(parent, "Entry attempts", "max_entry_attempts", "3", 70)
        self._field(parent, "Entry retry gap (s)", "entry_retry_seconds", "10", 70)

        self._section(parent, "Trade control")
        self._field(parent, "Lots per entry", "lots_per_entry", "1", 70)
        self._field(parent, "Max trades / day", "max_trades_per_day", "2", 70)
        self._field(parent, "Last entry", "last_entry_time", "15:15:00")
        self._field(parent, "Square off", "square_off_time", "15:15:00")

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(18, 6))
        self.btn_start = ctk.CTkButton(btns, text="START", height=40,
                                       font=FONTS["head"], fg_color=P["green"],
                                       hover_color="#276b43", command=self.on_start)
        self.btn_start.pack(fill="x", pady=(0, 6))
        self.btn_stop = ctk.CTkButton(btns, text="STOP", height=34,
                                      font=FONTS["mid"], fg_color=P["red"],
                                      hover_color="#96302f", state="disabled",
                                      command=self.on_stop)
        self.btn_stop.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(btns, text="Save settings", height=28, font=FONTS["body"],
                      fg_color=P["card_alt"], text_color=P["text"],
                      border_width=1, border_color=P["border"],
                      hover_color=P["accent_soft"],
                      command=self._save_settings).pack(fill="x")

        ctk.CTkLabel(parent,
                     text=("This build is paper trading only.\n"
                           "No order reaches the exchange."
                           if PAPER_ONLY_BUILD else
                           "Paper by default. Live must be chosen and\n"
                           "confirmed each session — it is never restored\n"
                           "from saved settings."),
                     font=FONTS["tiny"],
                     text_color=P["green"] if PAPER_ONLY_BUILD else P["dim"],
                     justify="left").pack(fill="x", padx=14, pady=(10, 16))

    # ─── Centre ────────────────────────────────────────────────────────────

    def _index_checks(self, parent):
        """Any one, any two, or all three. Each runs independently."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(row, text="Indices", font=FONTS["body"],
                     text_color=P["text"], anchor="w").pack(anchor="w")
        self.index_vars = {}
        for name in ("NIFTY", "BANKNIFTY", "SENSEX"):
            v = ctk.BooleanVar(value=(name == "NIFTY"))
            ctk.CTkCheckBox(row, text=name, variable=v, font=FONTS["small"],
                            checkbox_width=18, checkbox_height=18,
                            fg_color=P["accent"],
                            command=self._on_index_toggle).pack(anchor="w",
                                                                pady=1)
            self.index_vars[name] = v
        self.lbl_index_note = ctk.CTkLabel(row, text="", font=FONTS["tiny"],
                                           text_color=P["dim"], anchor="w",
                                           justify="left", wraplength=250)
        self.lbl_index_note.pack(anchor="w", pady=(2, 0))

    def _selected_indices(self):
        chosen = [n for n, v in self.index_vars.items() if v.get()]
        return chosen or ["NIFTY"]

    def _on_index_toggle(self):
        n = len(self._selected_indices())
        if n <= 1:
            self.lbl_index_note.configure(text="")
            return
        charts = n * 2 * (2 * self._int_var("strikes_each_side", 2) + 1)
        self.lbl_index_note.configure(
            text=f"{n} indices · {charts} charts polled. Each keeps its own "
                 f"pair and its own daily limit.")

    def _int_var(self, key, default):
        try:
            return int(float(self.vars[key].get()))
        except Exception:
            return default

    def _build_centre(self, parent):
        strip = ctk.CTkFrame(parent, fg_color=P["card"], height=78,
                             corner_radius=10, border_width=1,
                             border_color=P["border"])
        strip.pack(fill="x", pady=(0, 8))
        strip.pack_propagate(False)

        self.stats = {}
        for key, label in (("spot", "INDEX"), ("atm", "ATM"),
                           ("pair", "THE PAIR"), ("merged", "MERGED AT"),
                           ("mode", "NEXT ENTRY"), ("trades", "TRADES"),
                           ("pnl", "P&L")):
            cell = ctk.CTkFrame(strip, fg_color="transparent")
            cell.pack(side="left", expand=True, fill="both", pady=12)
            ctk.CTkLabel(cell, text=label, font=FONTS["tiny"],
                         text_color=P["dim"]).pack()
            v = ctk.CTkLabel(cell, text="—", font=("Segoe UI", 17, "bold"),
                             text_color=P["text"])
            v.pack()
            self.stats[key] = v

        # ── index tabs, shown only when more than one is running ──
        self.tab_bar = ctk.CTkFrame(parent, fg_color="transparent", height=32)
        self.tab_buttons = {}
        self.active_index = None
        self._per_index = {}       # index -> last payload, so tabs can switch

        # ── the merge banner — dormant until the charts overlap ──
        self.merge_bar = ctk.CTkFrame(parent, fg_color=P["card"], height=92,
                                      corner_radius=10, border_width=1,
                                      border_color=P["border"])
        self.merge_bar.pack(fill="x", pady=(0, 8))
        self.merge_bar.pack_propagate(False)

        self.merge_left = ctk.CTkFrame(self.merge_bar, fg_color="transparent", width=210)
        self.merge_left.pack(side="left", fill="y", padx=(16, 8), pady=10)
        ctk.CTkLabel(self.merge_left, text="THE CHARTS MERGED", font=FONTS["tiny"],
                     text_color=P["dim"], anchor="w").pack(anchor="w")
        self.merge_time = ctk.CTkLabel(self.merge_left, text="not yet",
                                       font=("Segoe UI", 26, "bold"),
                                       text_color=P["pending"], anchor="w")
        self.merge_time.pack(anchor="w")
        self.merge_sub = ctk.CTkLabel(self.merge_left, text="watching every candle",
                                      font=FONTS["tiny"], text_color=P["dim"],
                                      anchor="w")
        self.merge_sub.pack(anchor="w")

        self.merge_right = ctk.CTkFrame(self.merge_bar, fg_color="transparent")
        self.merge_right.pack(side="left", fill="both", expand=True,
                              padx=(8, 16), pady=10)
        self.merge_detail = ctk.CTkLabel(
            self.merge_right,
            text="When one call and one put print overlapping candles, the moment "
                 "is stamped here\nalong with the four lines taken from it.",
            font=FONTS["small"], text_color=P["dim"], anchor="w", justify="left")
        self.merge_detail.pack(anchor="w", fill="both", expand=True)

        wrap = ctk.CTkScrollableFrame(parent, fg_color=P["bg"])
        wrap.pack(fill="both", expand=True)

        ctk.CTkLabel(wrap, text="THE CONDITIONS", font=FONTS["tiny"],
                     text_color=P["accent"], anchor="w").pack(fill="x", pady=(0, 6))

        defs = [
            ("ladder", "The ladder of ten",
             "Index read at 09:30 · five strikes · CE and PE at each"),
            ("overlap", "The overlap",
             "The strike's call and put — bodies covering the same price, wicks ignored"),
            ("frame", "The four lines",
             "Each chart borrows the other's high and low"),
            ("collapse", "The collapse",
             "One leg dies: red candle, whole candle including wicks below its borrowed low, body at least half the candle"),
            ("breakout", "The break",
             "The other leg wins: green candle whose close is above its borrowed high. Close only — the candle need not clear it"),
            ("retest", "The retest",
             "Price back inside the band around that same high"),
            ("exit", "The exit watch",
             "Target on our premium · stop needs both charts to agree"),
        ]
        for i, (k, t, s) in enumerate(defs, 1):
            c = ConditionCard(wrap, k, i, t, s)
            c.pack(fill="x", pady=4)
            self.cards[k] = c

        ctk.CTkLabel(wrap, text="THE LADDER", font=FONTS["tiny"],
                     text_color=P["accent"], anchor="w").pack(fill="x", pady=(14, 6))

        self.ladder_box = ctk.CTkFrame(wrap, fg_color=P["card"], corner_radius=10,
                                       border_width=1, border_color=P["border"])
        self.ladder_box.pack(fill="x")
        self._ladder_header()

    def _ladder_header(self):
        h = ctk.CTkFrame(self.ladder_box, fg_color="transparent")
        h.pack(fill="x", padx=12, pady=(10, 2))
        for txt, w, anc in (("CALL", 150, "w"), ("LTP", 80, "e"),
                            ("STRIKE", 90, "center"),
                            ("LTP", 80, "w"), ("PUT", 150, "e")):
            ctk.CTkLabel(h, text=txt, font=FONTS["tiny"], text_color=P["dim"],
                         width=w, anchor=anc).pack(
                side="left", expand=True, fill="x")
        ctk.CTkFrame(self.ladder_box, height=1, fg_color=P["border"]).pack(
            fill="x", padx=12, pady=(2, 0))
        self.ladder_body = ctk.CTkFrame(self.ladder_box, fg_color="transparent")
        self.ladder_body.pack(fill="x", padx=12, pady=(0, 10))

    # ─── Right panel ───────────────────────────────────────────────────────

    def _build_right(self, parent):
        ctk.CTkLabel(parent, text="Activity log", font=FONTS["title"],
                     text_color=P["text"], anchor="w").pack(
            fill="x", padx=14, pady=(14, 2))
        ctk.CTkLabel(parent, text="Everything the strategy does, as it does it.",
                     font=FONTS["tiny"], text_color=P["dim"], anchor="w").pack(
            fill="x", padx=14, pady=(0, 8))

        self.log_box = ctk.CTkTextbox(parent, fg_color="#0f1c2b",
                                      text_color="#d6e2ee", font=FONTS["mono_sm"],
                                      corner_radius=8, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.log_box.configure(state="disabled")

        for tag, col in (("info", "#d6e2ee"), ("warning", "#f0c674"),
                         ("error", "#f28b82"), ("good", "#7ee2a8"),
                         ("dim", "#7d8fa3")):
            try:
                self.log_box.tag_config(tag, foreground=col)
            except Exception:
                pass

        ctk.CTkLabel(parent, text="CLOSED TRADES", font=FONTS["tiny"],
                     text_color=P["accent"], anchor="w").pack(
            fill="x", padx=14, pady=(4, 4))
        self.trades_box = ctk.CTkFrame(parent, fg_color=P["card_alt"],
                                       corner_radius=8, height=150)
        self.trades_box.pack(fill="x", padx=12, pady=(0, 8))
        self.trades_box.pack_propagate(False)
        self.lbl_no_trades = ctk.CTkLabel(self.trades_box, text="No trades yet",
                                          font=FONTS["small"], text_color=P["dim"])
        self.lbl_no_trades.pack(pady=20)

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(row, text="Open log folder", height=28, font=FONTS["small"],
                      fg_color=P["card_alt"], text_color=P["text"], border_width=1,
                      border_color=P["border"], hover_color=P["accent_soft"],
                      command=self._open_logs).pack(side="left", expand=True,
                                                    fill="x", padx=(0, 4))
        ctk.CTkButton(row, text="Clear", width=64, height=28, font=FONTS["small"],
                      fg_color=P["card_alt"], text_color=P["text"], border_width=1,
                      border_color=P["border"], hover_color=P["accent_soft"],
                      command=self._clear_log).pack(side="left")

    # ═══════════════════════════════════════════════════════════════════════
    # SETTINGS PERSISTENCE
    # ═══════════════════════════════════════════════════════════════════════

    def _collect_config(self) -> OverlapConfig:
        def _f(k, d):
            try:
                return float(self.vars[k].get())
            except Exception:
                return d

        def _i(k, d):
            try:
                return int(float(self.vars[k].get()))
            except Exception:
                return d

        def _t(k, d):
            try:
                parts = [int(x) for x in self.vars[k].get().strip().split(":")]
                while len(parts) < 3:
                    parts.append(0)
                return dtime(*parts[:3])
            except Exception:
                return d

        c = OverlapConfig()
        c.indices = self._selected_indices()
        c.candle_source = ("ticks" if self.vars["candle_source"].get().startswith("Live")
                           else "rest")
        c.rest_poll = _f("rest_poll", 4.0)
        c.rest_grace = _f("rest_grace", 6.0)
        c.replay_history_on_late_start = bool(
            self.vars["replay_history_on_late_start"].get())
        c.timeframe_minutes = _i("timeframe_minutes", 3)
        c.ref_time = _t("ref_time", dtime(9, 30))
        c.strikes_each_side = _i("strikes_each_side", 2)
        c.expiry_offset = 1 if self.vars["expiry_offset"].get() == "Next" else 0
        c.ref_candle = ("before"
                        if self.vars["ref_candle"].get().startswith("Ending")
                        else "after")
        c.pair_mode = "any" if self.vars["pair_mode"].get() == "Any strike" else "same"
        c.min_overlap = _f("min_overlap", 0.05)
        c.require_overlap = bool(self.vars["require_overlap"].get())
        c.min_body_pct = _f("min_body_pct", 50.0)
        c.signal_window_bars = _i("signal_window_bars", 0)
        c.relaxed_after_stop = bool(self.vars["relaxed_after_stop"].get())
        c.guard_relaxed_entry = bool(self.vars["guard_relaxed_entry"].get())
        c.retest_buffer = _f("retest_buffer", 4.0)
        c.target_pct = _f("target_pct", 15.0)
        c.two_sided_stop = bool(self.vars["two_sided_stop"].get())
        c.safety_stop_enabled = bool(self.vars["safety_stop_enabled"].get())
        c.safety_stop_pct = _f("safety_stop_pct", 25.0)
        c.trade_mode = self.vars["trade_mode"].get()
        c.max_entry_slippage_pct = _f("max_entry_slippage_pct", 3.0)
        c.position_sync_seconds = _i("position_sync_seconds", 15)
        c.exit_retry_attempts = _i("exit_retry_attempts", 5)
        c.max_entry_attempts = _i("max_entry_attempts", 3)
        c.entry_retry_seconds = _f("entry_retry_seconds", 10.0)
        c.lots_per_entry = _i("lots_per_entry", 1)
        c.max_trades_per_day = _i("max_trades_per_day", 2)
        c.last_entry_time = _t("last_entry_time", dtime(15, 15))
        c.square_off_time = _t("square_off_time", dtime(15, 15))
        return c

    def _save_settings(self):
        try:
            cfg = self._collect_config()
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg.to_dict(), f, indent=2)
            self._log("Settings saved.", "good")
        except Exception as e:
            self._log(f"Could not save settings: {e}", "error")

    def _load_settings(self):
        if not SETTINGS_FILE.exists():
            return
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            cfg = OverlapConfig.from_dict(d)
            m = cfg.to_dict()
            m["expiry_offset"] = "Next" if cfg.expiry_offset == 1 else "Current"
            m["candle_source"] = ("Live ticks" if cfg.candle_source == "ticks"
                                  else "Broker bars (REST)")
            m["trade_mode"] = "paper"   # live is chosen per session, never restored
            for nm, v in self.index_vars.items():
                v.set(nm in cfg.indices)
            self._on_index_toggle()
            m["ref_candle"] = ("Ending at that time" if cfg.ref_candle == "before"
                               else "Starting at that time")
            m["pair_mode"] = ("Any strike" if cfg.pair_mode == "any"
                              else "Same strike")
            for k, var in self.vars.items():
                if k in ("client_id", "pin", "totp"):
                    continue
                if k in m:
                    if isinstance(var, ctk.BooleanVar):
                        var.set(bool(m[k]))
                    else:
                        var.set(str(m[k]))
        except Exception as e:
            log.warning(f"Settings load failed: {e}")

    def _load_env(self):
        for k, env in (("client_id", "DHAN_CLIENT_ID"), ("pin", "DHAN_PIN"),
                       ("totp", "DHAN_TOTP_SECRET")):
            v = os.getenv(env, "")
            if v:
                self.vars[k].set(v)

    def _save_env(self):
        try:
            if not ENV_FILE.exists():
                ENV_FILE.write_text("", encoding="utf-8")
            for k, env in (("client_id", "DHAN_CLIENT_ID"), ("pin", "DHAN_PIN"),
                           ("totp", "DHAN_TOTP_SECRET")):
                set_key(str(ENV_FILE), env, self.vars[k].get().strip())
        except Exception as e:
            log.warning(f"Could not persist credentials: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # RUN CONTROL
    # ═══════════════════════════════════════════════════════════════════════

    def on_start(self):
        if self.is_running:
            return
        if self.engine_thread is not None and self.engine_thread.is_alive():
            self._log("The previous session is still shutting down — "
                      "give it a moment and press START again.", "warning")
            return
        cid = self.vars["client_id"].get().strip()
        pin = self.vars["pin"].get().strip()
        totp = self.vars["totp"].get().strip()
        if not (cid and pin and totp):
            self._log("Enter the client ID, PIN and TOTP secret first.", "error")
            return

        cfg = self._collect_config()
        errs = cfg.validate()
        if errs:
            for e in errs:
                self._log(f"Settings: {e}", "error")
            return

        if cfg.is_live and not self._confirm_live(cfg):
            return

        self._save_env()
        self._save_settings()

        self.is_running = True
        self._auth_stop.clear()
        self._worker_seq += 1
        seq = self._worker_seq
        self.btn_start.configure(state="disabled", text="RUNNING")
        self.btn_stop.configure(state="normal")
        names = ", ".join(cfg.indices)
        self._log(f"Starting — {names}, {cfg.timeframe_minutes}-minute candles.",
                  "good")
        self._per_index = {}
        self._build_tabs(list(cfg.indices))

        def worker():
            try:
                api.set_credentials(cid, pin, totp, os.getenv("DHAN_ACCESS_TOKEN", ""))
                api.init_credentials(
                    lambda m: self.events.put(
                        ("log", {"msg": m, "level": "info",
                                 "time": datetime.now().strftime("%H:%M:%S")})),
                    stop_event=self._auth_stop)
                if self._auth_stop.is_set() or seq != self._worker_seq:
                    return
                engine = OverlapSupervisor(cfg, gui_callback=self._on_engine_event)
                self.engine = engine
                engine.run()
            except Exception as e:
                self.events.put(("log", {"msg": f"Engine stopped: {e}",
                                         "level": "error",
                                         "time": datetime.now().strftime("%H:%M:%S")}))
            finally:
                self.events.put(("finished", {"seq": seq}))

        self.engine_thread = threading.Thread(target=worker, daemon=True)
        self.engine_thread.start()

    def _confirm_live(self, cfg) -> bool:
        """Live mode has to be typed out. A dropdown is too easy to nudge."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Live trading")
        dlg.geometry("470x300")
        dlg.configure(fg_color=P["card"])
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="LIVE TRADING", font=("Segoe UI", 20, "bold"),
                     text_color=P["red"]).pack(pady=(20, 4))
        ctk.CTkLabel(dlg, text="Orders will be sent to the exchange with real money.",
                     font=FONTS["body"], text_color=P["text"]).pack()

        box = ctk.CTkFrame(dlg, fg_color=P["red_soft"], corner_radius=8)
        box.pack(fill="x", padx=24, pady=14)
        ctk.CTkLabel(box, justify="left", font=FONTS["small"],
                     text_color=P["text"],
                     text=(f"Indices      {', '.join(cfg.indices)}\n"
                           f"Quantity     {cfg.quantity}  "
                           f"({cfg.lots_per_entry} lot x {cfg.lot_size})\n"
                           f"Max trades   {cfg.max_trades_per_day} per index"
                           f"{f' ({cfg.max_trades_per_day * len(cfg.indices)} total)' if len(cfg.indices) > 1 else ''}\n"
                           f"Target       {cfg.target_pct:.0f}%   "
                           f"Safety stop  "
                           f"{cfg.safety_stop_pct:.0f}%"
                           f"{'' if cfg.safety_stop_enabled else '  (OFF)'}")
                     ).pack(anchor="w", padx=14, pady=12)

        ctk.CTkLabel(dlg, text="Type  LIVE  to confirm", font=FONTS["body"],
                     text_color=P["dim"]).pack()
        entry = ctk.CTkEntry(dlg, width=140, height=30, justify="center",
                             font=FONTS["head"])
        entry.pack(pady=6)
        entry.focus()

        out = {"ok": False}

        def go():
            if entry.get().strip().upper() == "LIVE":
                out["ok"] = True
                dlg.destroy()
            else:
                entry.configure(border_color=P["red"])

        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(pady=8)
        ctk.CTkButton(row, text="Cancel", width=120, fg_color=P["card_alt"],
                      text_color=P["text"], border_width=1,
                      border_color=P["border"], hover_color=P["accent_soft"],
                      command=dlg.destroy).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Trade live", width=120, fg_color=P["red"],
                      hover_color="#96302f", command=go).pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: go())

        self.wait_window(dlg)
        if not out["ok"]:
            self._log("Live trading cancelled — nothing was started.", "warning")
        return out["ok"]

    def on_stop(self):
        # Cancel a login that has not finished yet — otherwise the worker sits
        # in the TOTP retry loop and a later START runs alongside it.
        self._auth_stop.set()
        if self.engine:
            try:
                self.engine.stop()
            except Exception as e:
                log.error(f"Stop error: {e}")
        self.is_running = False
        self.btn_start.configure(state="normal", text="START")
        self.btn_stop.configure(state="disabled")
        self._log("Stopped.", "warning")

    def _on_engine_event(self, event, data):
        self.events.put((event, data))

    # ═══════════════════════════════════════════════════════════════════════
    # EVENT PUMP
    # ═══════════════════════════════════════════════════════════════════════

    def _pump(self):
        self.lbl_clock.configure(text=datetime.now().strftime("%H:%M:%S"))
        try:
            while True:
                event, data = self.events.get_nowait()
                self._handle(event, data)
        except queue.Empty:
            pass
        except Exception as e:
            log.error(f"Pump error: {e}")
        self.after(250, self._pump)

    def _handle(self, event, data):
        if event == "log":
            lvl = data.get("level", "info")
            tag = {"error": "error", "warning": "warning"}.get(lvl, "info")
            msg = data.get("msg", "")
            ix = data.get("index", "")
            if ix and len(self.tab_buttons) > 1:
                msg = f"[{ix}] {msg}"
            if any(msg.startswith(p) for p in
                   ("OVERLAP", "ARMED", "IN TRADE", "TARGET", "RETEST HIT")):
                tag = "good"
            self._log(msg, tag, data.get("time"))

        elif event == "phase":
            self._set_phase(data.get("phase", ""), data.get("msg", ""))

        elif event == "ws":
            ok = data.get("connected")
            self.lbl_ws.configure(text="FEED  ●" if ok else "FEED  ○",
                                  text_color="#7ee2a8" if ok else "#9db8d4")

        elif event in ("tick", "conditions"):
            ix = data.get("index") or self.active_index
            if event == "tick":
                self._per_index[ix] = data
                self._update_totals()
            # Only the index being looked at repaints the cards.
            if ix and self.active_index and ix != self.active_index:
                return
            for c in data.get("conditions", []):
                card = self.cards.get(c["key"])
                if card:
                    card.update_state(c["status"], c["detail"], c["value"])
            if event == "tick":
                self._update_stats(data)
                self._update_ladder(data.get("ladder", {}))

        elif event == "halted":
            self._log(f"TRADING HALTED ({data.get('code','')}) — "
                      f"{data.get('advice','')}", "error")
            if data.get("holding"):
                self._log(f"A POSITION IS STILL OPEN in {data['holding']}. "
                          f"Square it off in the broker terminal.", "error")
            try:
                self.lbl_phase.configure(
                    text=f"● HALTED — {data.get('code','')}",
                    text_color="#f28b82")
            except Exception:
                pass

        elif event == "exit_failed":
            self._log(f"POSITION STILL OPEN — {data.get('qty')} of "
                      f"{data.get('leg')} could not be sold. Square it off in "
                      f"the broker terminal.", "error")
            try:
                self.lbl_phase.configure(text="● EXIT FAILED — POSITION OPEN",
                                         text_color="#f28b82")
            except Exception:
                pass

        elif event == "merged":
            ix = data.get("index") or self.active_index
            self._per_index[f"merge::{ix}"] = data
            if not self.active_index or ix == self.active_index:
                self._show_merge(data)

        elif event == "ladder":
            self._update_ladder(data)

        elif event == "trade_closed":
            self._update_stats(data)
            self._update_trades(data.get("closed", []))

        elif event == "finished":
            if data.get("seq") not in (None, self._worker_seq):
                return   # an older worker ending; a newer one is live
            self.is_running = False
            self.engine = None
            self.btn_start.configure(state="normal", text="START")
            self.btn_stop.configure(state="disabled")

    def _show_merge(self, d):
        """Light up the banner the moment the two charts overlap."""
        if not d:
            return
        self.merge_bar.configure(border_color=P["violet"], border_width=2,
                                 fg_color=P["violet_soft"])
        self.merge_time.configure(text=d.get("bucket_label", "—"),
                                  text_color=P["violet"])

        strike = d.get("strike")
        gap = d.get("gap", 0.0)
        red = d.get("red_side", "")
        self.merge_sub.configure(
            text=(f"{strike} CE and PE   ·   {red} red   ·   gap {gap:.2f}"
                  if strike else
                  f"{d.get('ce_label','')} / {d.get('pe_label','')}   ·   gap {gap:.2f}"),
            text_color=P["violet"])

        cc, pc = d.get("ce_candle", {}), d.get("pe_candle", {})
        self.merge_detail.configure(
            text=(
                f"{d.get('ce_label','CE')}   "
                f"O {cc.get('open','—')}   H {cc.get('high','—')}   "
                f"L {cc.get('low','—')}   C {cc.get('close','—')}\n"
                f"{d.get('pe_label','PE')}   "
                f"O {pc.get('open','—')}   H {pc.get('high','—')}   "
                f"L {pc.get('low','—')}   C {pc.get('close','—')}\n"
                f"CE chart borrows  {d.get('ce_borrowed_low',0):.2f}  /  "
                f"{d.get('ce_borrowed_high',0):.2f}        "
                f"PE chart borrows  {d.get('pe_borrowed_low',0):.2f}  /  "
                f"{d.get('pe_borrowed_high',0):.2f}"),
            text_color=P["text"], font=FONTS["mono_sm"])

    def _build_tabs(self, names):
        """One button per running index. A single index shows nothing at all,
        so a normal session looks exactly as it did before."""
        for w in self.tab_bar.winfo_children():
            w.destroy()
        self.tab_buttons = {}
        if len(names) < 2:
            self.tab_bar.pack_forget()
            self.active_index = names[0] if names else None
            return
        self.tab_bar.pack(fill="x", pady=(0, 6), before=self.merge_bar)
        for n in names:
            b = ctk.CTkButton(self.tab_bar, text=n, width=110, height=28,
                              font=FONTS["small"],
                              fg_color=P["card"], text_color=P["text"],
                              border_width=1, border_color=P["border"],
                              hover_color=P["accent_soft"],
                              command=lambda x=n: self._show_index(x))
            b.pack(side="left", padx=(0, 6))
            self.tab_buttons[n] = b
        self.active_index = names[0]
        self._paint_tabs()

    def _paint_tabs(self):
        for n, b in self.tab_buttons.items():
            on = (n == self.active_index)
            b.configure(fg_color=P["accent"] if on else P["card"],
                        text_color="#ffffff" if on else P["text"],
                        border_color=P["accent"] if on else P["border"])

    def _show_index(self, name):
        self.active_index = name
        self._paint_tabs()
        d = self._per_index.get(name)
        if d:
            for c in d.get("conditions", []):
                card = self.cards.get(c["key"])
                if card:
                    card.update_state(c["status"], c["detail"], c["value"])
            self._update_stats(d)
            self._update_ladder(d.get("ladder", {}))
        m = self._per_index.get(f"merge::{name}")
        if m:
            self._show_merge(m)
        else:
            self._reset_merge_bar()

    def _reset_merge_bar(self):
        self.merge_bar.configure(border_color=P["border"], border_width=1,
                                 fg_color=P["card"])
        self.merge_time.configure(text="not yet", text_color=P["pending"])
        self.merge_sub.configure(text="watching every candle",
                                 text_color=P["dim"])
        self.merge_detail.configure(
            text="When one call and one put print overlapping candles, the "
                 "moment is stamped here\nalong with the four lines taken "
                 "from it.",
            text_color=P["dim"], font=FONTS["small"])

    def _set_phase(self, phase, msg):
        colour = {
            "IDLE": "#f0c674", "WAITING FOR 09:30": "#f0c674",
            "SCANNING FOR OVERLAP": "#9db8d4", "FRAME DRAWN": "#8fd6ff",
            "ARMED — WAITING RETEST": "#f0c674", "IN TRADE": "#7ee2a8",
            "DONE FOR THE DAY": "#9db8d4", "STOPPED": "#f28b82",
        }.get(phase, "#ffffff")
        self.lbl_phase.configure(text=f"● {phase}", text_color=colour)

    def _update_totals(self):
        """The P&L cell shows every index added together; the rest of the
        strip belongs to whichever tab is open."""
        if len(self.tab_buttons) < 2:
            return
        tot = 0.0
        done = maxn = 0
        for k, d in self._per_index.items():
            if str(k).startswith("merge::"):
                continue
            tot += d.get("total_pnl", 0.0) or 0.0
            done += d.get("trades_done", 0) or 0
            maxn += d.get("trades_max", 0) or 0
        self.stats["pnl"].configure(
            text=f"{tot:+,.0f}" if tot else "0",
            text_color=P["green"] if tot > 0 else (P["red"] if tot < 0 else P["text"]))
        self.stats["trades"].configure(text=f"{done} / {maxn}")

    def _update_stats(self, d):
        spot = d.get("spot")
        self.stats["spot"].configure(text=f"{spot:,.2f}" if spot else "—")
        pair = d.get("pair", "—")
        self.stats["pair"].configure(
            text=pair, text_color=P["violet"] if pair != "—" else P["text"])
        em = d.get("entry_mode", "full")
        self.stats["mode"].configure(
            text="RELAXED" if em == "relaxed" else "FULL",
            text_color=P["gold"] if em == "relaxed" else P["text"])
        h = d.get("rest_health") or {}
        if d.get("candle_source") == "rest":
            if h.get("avg_lag") is None:
                self.lbl_bars.configure(text="BARS  waiting", text_color="#9db8d4")
            else:
                v = h.get("verdict", "")
                col = ("#7ee2a8" if v == "comfortable" else
                       "#f0c674" if v == "tight" else "#f28b82")
                self.lbl_bars.configure(
                    text=f"BARS  {h['avg_lag']:.1f}s avg / {h['max_lag']:.1f}s max",
                    text_color=col)
        else:
            self.lbl_bars.configure(text="BARS  ticks", text_color="#9db8d4")
        merged = d.get("merged_at", "")
        self.stats["merged"].configure(
            text=merged or "—",
            text_color=P["violet"] if merged else P["text"])
        self._pair_ce = d.get("pair_ce")
        self._pair_pe = d.get("pair_pe")
        self._buy_strike = d.get("buy_strike")
        if len(self.tab_buttons) < 2:
            self.stats["trades"].configure(
                text=f"{d.get('trades_done', 0)} / {d.get('trades_max', 2)}")
            pnl = d.get("total_pnl", 0.0)
            self.stats["pnl"].configure(
                text=f"{pnl:+,.0f}" if pnl else "0",
                text_color=P["green"] if pnl > 0 else (P["red"] if pnl < 0
                                                       else P["text"]))
        live = not d.get("paper", True)
        self.lbl_mode.configure(text="● LIVE" if live else "PAPER",
                                fg_color=P["red"] if live else P["green"],
                                font=FONTS["mid"] if live else FONTS["small"])

    def _update_ladder(self, d):
        rows = d.get("rows", [])
        if not rows:
            return
        atm = d.get("atm")
        if atm:
            self.stats["atm"].configure(text=f"{int(atm):,}")

        if len(self.ladder_rows) != len(rows):
            for w in self.ladder_body.winfo_children():
                w.destroy()
            self.ladder_rows = {}
            for r in rows:
                fr = ctk.CTkFrame(self.ladder_body,
                                  fg_color=P["accent_soft"] if r["is_atm"] else "transparent",
                                  corner_radius=6, height=26)
                fr.pack(fill="x", pady=1)
                cells = {}
                for name, w, anc, col in (
                        ("ce_label", 150, "w", P["red"]),
                        ("ce_ltp", 80, "e", P["text"]),
                        ("strike", 90, "center", P["navy"]),
                        ("pe_ltp", 80, "w", P["text"]),
                        ("pe_label", 150, "e", P["green"])):
                    lb = ctk.CTkLabel(fr, text="—", font=FONTS["small"],
                                      text_color=col, width=w, anchor=anc)
                    lb.pack(side="left", expand=True, fill="x", padx=2, pady=3)
                    cells[name] = lb
                self.ladder_rows[r["strike"]] = cells

        pce = getattr(self, "_pair_ce", None)
        ppe = getattr(self, "_pair_pe", None)
        buy = getattr(self, "_buy_strike", None)

        for r in rows:
            cells = self.ladder_rows.get(r["strike"])
            if not cells:
                continue
            sk = r["strike"]
            in_pair_ce = (pce is not None and sk == pce)
            in_pair_pe = (ppe is not None and sk == ppe)
            cells["ce_label"].configure(
                text=("▸ " if in_pair_ce else "") + r["ce_label"].split()[-2] + " CE",
                text_color=P["violet"] if in_pair_ce else P["red"],
                font=FONTS["mid"] if in_pair_ce else FONTS["small"])
            cells["pe_label"].configure(
                text_color=P["violet"] if in_pair_pe else P["green"],
                font=FONTS["mid"] if in_pair_pe else FONTS["small"])
            cells["pe_label"].configure(
                text=(("◂ " if in_pair_pe else "") + r["pe_label"].split()[-2] + " PE")
                if r["pe_label"] else "—")
            cells["strike"].configure(
                text=f"{r['strike']:,}" + ("  ★" if r["is_atm"] else ""))
            for k in ("ce_ltp", "pe_ltp"):
                v = r.get(k)
                cells[k].configure(text=f"{v:.2f}" if v else "—")

    def _update_trades(self, closed):
        for w in self.trades_box.winfo_children():
            w.destroy()
        if not closed:
            ctk.CTkLabel(self.trades_box, text="No trades yet", font=FONTS["small"],
                         text_color=P["dim"]).pack(pady=20)
            return
        for t in closed[-4:]:
            fr = ctk.CTkFrame(self.trades_box, fg_color=P["card"], corner_radius=6)
            fr.pack(fill="x", padx=6, pady=3)
            top = ctk.CTkFrame(fr, fg_color="transparent")
            top.pack(fill="x", padx=8, pady=(5, 0))
            ctk.CTkLabel(top, text=f"#{t['no']}  {t['leg']}", font=FONTS["small"],
                         text_color=P["text"], anchor="w").pack(side="left")
            pnl = t.get("pnl", 0)
            ctk.CTkLabel(top, text=f"Rs.{pnl:+,.0f}", font=FONTS["mid"],
                         text_color=P["green"] if pnl >= 0 else P["red"],
                         anchor="e").pack(side="right")
            ctk.CTkLabel(fr,
                         text=f"{t['entry']:.2f} → {t['exit']:.2f}   ·   {t['reason']}",
                         font=FONTS["tiny"], text_color=P["dim"], anchor="w").pack(
                fill="x", padx=8, pady=(0, 5))

    # ═══════════════════════════════════════════════════════════════════════
    # LOG
    # ═══════════════════════════════════════════════════════════════════════

    def _log(self, msg, tag="info", tstamp=None):
        t = tstamp or datetime.now().strftime("%H:%M:%S")
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{t}  ", "dim")
            self.log_box.insert("end", f"{msg}\n", tag)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _open_logs(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(LOG_DIR))
            elif sys.platform == "darwin":
                os.system(f'open "{LOG_DIR}"')
            else:
                os.system(f'xdg-open "{LOG_DIR}"')
        except Exception as e:
            self._log(f"Could not open the log folder: {e}", "error")


def main():
    app = OverlapApp()
    app._log(f"{APP_NAME} v{APP_VERSION} — strategy note v{STRATEGY_NOTE_VERSION}", "good")
    app._log("Enter your Dhan credentials, check the settings, then press START.", "dim")
    app.mainloop()


if __name__ == "__main__":
    main()
