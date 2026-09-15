#!/usr/bin/env python3
"""
The Overlap — REST candle freshness probe
═════════════════════════════════════════
How long after a 1-minute candle closes does Dhan actually return it?

This is the measurement that decides whether the strategy can be driven from
the broker's own bars. Run it during market hours, on the machine that will
run the strategy, because the answer depends on that connection.

    python freshness.py
    python freshness.py --minutes 30 --every 3

Reading the result:

    < 10s   comfortable. Signals arrive well within the candle they belong to.
    10-30s  workable but tight. Entries will be late into the retest band.
    > 30s   REST cannot drive the strategy at this latency. Switch the candle
            source back to live ticks in the settings.

Balfund Trading Pvt Ltd | www.balfund.com
"""

import argparse
import logging
import os
import time
from datetime import datetime

from dotenv import load_dotenv

from config import ENV_FILE, INDEX_CONFIG
import dhan_api as api
from restfeed import RestCandleFeed

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=10, help="how long to watch")
    ap.add_argument("--every", type=int, default=3, help="poll interval, seconds")
    ap.add_argument("--index", default="NIFTY", choices=list(INDEX_CONFIG))
    ap.add_argument("--sec-id", default="",
                    help="probe this security id directly, skipping the "
                         "expiry and option-chain lookups")
    ap.add_argument("--side", choices=["CE", "PE"], default="CE")
    args = ap.parse_args()

    load_dotenv(str(ENV_FILE), override=True)
    cid = os.getenv("DHAN_CLIENT_ID", "")
    pin = os.getenv("DHAN_PIN", "")
    totp = os.getenv("DHAN_TOTP_SECRET", "")
    if not (cid and pin and totp):
        raise SystemExit("Fill DHAN_CLIENT_ID, DHAN_PIN and DHAN_TOTP_SECRET "
                         "in .env first.")

    def timed(label, fn, *a, **kw):
        t0 = time.time()
        out = fn(*a, **kw)
        dt = time.time() - t0
        flag = "" if dt < 3 else ("   <- slow" if dt < 15 else "   <- VERY SLOW")
        print(f"  {label:<30} {dt:5.1f}s{flag}")
        return out

    print("\nSetup timings — anything above a second or two is worth noting:")
    api.set_credentials(cid, pin, totp, os.getenv("DHAN_ACCESS_TOKEN", ""))
    timed("login", api.init_credentials)

    cfg = INDEX_CONFIG[args.index]
    seg = cfg["option_segment"]

    if args.sec_id:
        sec = args.sec_id
        print(f"  probing security id {sec} directly")
    else:
        expiry = timed("expiry list", api.get_target_expiry, args.index, 0)
        if not expiry:
            raise SystemExit("Could not read an expiry.")
        oc = timed("option chain", api.fetch_option_chain, args.index, expiry)
        if not oc:
            raise SystemExit("Option chain unavailable.")
        spot = oc["spot_price"]
        gap = cfg["strike_gap"]
        atm = round(spot / gap) * gap
        sec = None
        for sk, sd in oc["oc"].items():
            try:
                if abs(float(sk) - atm) > 0.01:
                    continue
            except ValueError:
                continue
            od = sd.get("ce" if args.side == "CE" else "pe")
            if od:
                sec = str(od["security_id"])
        if not sec:
            raise SystemExit(f"{args.side} not listed at {int(atm)}.")
        print(f"  index {spot:.2f} -> ATM {int(atm)} {args.side}, secId {sec}")

    t0 = time.time()
    probe = api.fetch_intraday(sec, seg, "OPTIDX", "1", days=1)
    dt = time.time() - t0
    n_all = len(probe or [])
    now_s = time.time()
    closed = RestCandleFeed.drop_forming(
        [{**c, "ts": int(c["timestamp"])} for c in (probe or [])], now_s)
    print(f"  intraday fetch (the one that matters) {dt:5.1f}s   "
          f"{n_all} bars, {n_all - len(closed)} still forming")
    if closed:
        newest = max(c["ts"] for c in closed)
        print(f"  newest CLOSED bar is {api.epoch_to_ist(newest, '%H:%M')}, "
              f"{now_s - (newest + 60):.0f}s old")
    if not probe:
        raise SystemExit("\nThe endpoint returned nothing. Check market hours "
                         "and the security id.")

    print(f"\nWatching secId {sec} for {args.minutes} min, "
          f"polling every {args.every}s")
    print("Waiting for the first new candle...\n")
    print(f"{'bar closed':>12} {'first seen':>12} {'delay':>9}   verdict")
    print("-" * 56)

    seen, delays, forming = set(), [], 0
    deadline = time.time() + args.minutes * 60
    warm = True

    while time.time() < deadline:
        try:
            raw = api.fetch_intraday(sec, seg, "OPTIDX", "1", days=1)
        except Exception as ex:
            print(f"  fetch error: {ex}")
            time.sleep(args.every)
            continue

        now_s = time.time()
        raw_n = len(raw or [])
        rows = RestCandleFeed.drop_forming(
            [{**c, "ts": int(c["timestamp"])} for c in (raw or [])], now_s)
        forming += (raw_n - len(rows))

        if rows:
            ts = max(c["ts"] for c in rows)
            if warm:
                seen.add(ts)      # ignore whatever was already there
                warm = False
            elif ts not in seen:
                seen.add(ts)
                closed_at = ts + 60
                delay = time.time() - closed_at
                delays.append(delay)
                verdict = ("comfortable" if delay < 10 else
                           "tight" if delay < 30 else "TOO SLOW")
                print(f"{api.epoch_to_ist(closed_at):>12} "
                      f"{datetime.now(api.IST).strftime('%H:%M:%S'):>12} "
                      f"{delay:>8.1f}s   {verdict}")
        time.sleep(args.every)

    print("-" * 56)
    if not delays:
        print("No new candles appeared. Was the market open?")
        return

    lo, hi = min(delays), max(delays)
    avg = sum(delays) / len(delays)
    print(f"{len(delays)} candles observed:  min {lo:.1f}s   "
          f"avg {avg:.1f}s   max {hi:.1f}s")
    print("(delay = candle close to it appearing in the API, counting only")
    print(" bars that had actually finished)")
    print(f"{forming} still-forming bars were seen and discarded — Dhan serves")
    print("the current minute as it builds, so it must never reach the strategy.")

    if lo < 0:
        print("\nNEGATIVE DELAY: a bar appeared before it closed, which is not")
        print("possible. The timestamp convention is not what we assume —")
        print("stop and investigate rather than trusting these numbers.")
        return

    print()
    if hi < 10:
        print("REST can drive the strategy. Leave the candle source on "
              "'Broker bars'.")
    elif hi < 30:
        print("REST is workable but tight. Entries will be late into the "
              "retest band\non fast-moving candles.")
    else:
        print("REST cannot drive entries at this latency. Set the candle "
              "source to\n'Live ticks' in the settings.")


if __name__ == "__main__":
    main()
