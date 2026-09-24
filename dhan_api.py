#!/usr/bin/env python3
"""
The Overlap — Dhan API Layer
════════════════════════════
Token management, REST endpoints, order placement, WS binary parsing.

Connection logic follows the RA17 project. Nothing in this file knows
anything about the Overlap strategy.

Balfund Trading Pvt Ltd | www.balfund.com
"""

import io
import csv as csvmod
import json
import logging
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pyotp
import requests
from dotenv import load_dotenv, set_key

from config import ENV_FILE, INDEX_CONFIG

log = logging.getLogger("Overlap")

load_dotenv(str(ENV_FILE), override=True)

IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

DHAN_CLIENT_ID = ""
DHAN_PIN = ""
DHAN_TOTP_SECRET = ""
DHAN_ACCESS_TOKEN = ""

HEADERS: Dict[str, str] = {}
WS_URL = ""


# ═══════════════════════════════════════════════════════════════════════════
# RATE GATES
# ═══════════════════════════════════════════════════════════════════════════

class RateGate:
    def __init__(self, max_per_sec: float):
        self.min_gap = 1.0 / max_per_sec
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            gap = now - self._last
            if gap < self.min_gap:
                time.sleep(self.min_gap - gap)
            self._last = time.time()


# Order failures must reach the person watching, not only the log file. The
# strategy points this at its own logger so a rejection is visible on screen.
_ORDER_NOTIFY = None


# Errors that will not fix themselves. Retrying these is pointless, and doing
# it at tick speed sends dozens of requests for something only a person can
# put right. DH-905 cost 60 rejected orders in 62 seconds on 09-Sep.
FATAL_ORDER_CODES = {
    "DH-905": ("This machine's IP address is not whitelisted for order "
               "placement on the Dhan account. Add it under the DhanHQ API "
               "section of the account profile."),
    "DH-901": ("The access token is not valid for trading. Re-authenticate, "
               "and check that API trading is enabled on the account."),
    "DH-902": "The account is not permitted to trade this segment.",
}


def classify_order_error(body: str) -> Optional[Tuple[str, str]]:
    """(code, what to do) when a response is a permanent refusal."""
    if not body:
        return None
    for code, advice in FATAL_ORDER_CODES.items():
        if code in body:
            return code, advice
    return None


# Asked before every order attempt. When it returns True the caller has been
# stopped, and nothing further may be sent — an entry sequence is four orders,
# so a STOP part way through must not let the rest of them out.
_ORDER_ABORT = None


def set_order_abort_check(fn):
    global _ORDER_ABORT
    _ORDER_ABORT = fn


def _aborted() -> bool:
    if _ORDER_ABORT is None:
        return False
    try:
        return bool(_ORDER_ABORT())
    except Exception:
        return False


def set_order_notifier(fn):
    global _ORDER_NOTIFY
    _ORDER_NOTIFY = fn


def _notify(msg: str, level: str = "error"):
    log.error(msg) if level == "error" else log.warning(msg)
    if _ORDER_NOTIFY:
        try:
            _ORDER_NOTIFY(msg, level)
        except Exception:
            pass


DATA_GATE = RateGate(4.0)
ORDER_GATE = RateGate(8.0)
OC_GATE = RateGate(0.30)


# ═══════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    return datetime.now(IST)


def normalize_epoch(ts) -> int:
    """Dhan sometimes sends IST-shifted epochs. Fold them back to true UTC epoch."""
    ts = int(ts)
    now_ts = int(time.time())
    if int(4.5 * 3600) <= (ts - now_ts) <= int(6.5 * 3600):
        ts -= 19800
    return ts


def epoch_to_ist(ts, fmt="%H:%M:%S") -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(normalize_epoch(int(ts)), tz=IST).strftime(fmt)


def ist_bucket_label(bucket: int, fmt="%H:%M") -> str:
    return datetime.fromtimestamp(int(bucket), tz=IST).strftime(fmt)


# ═══════════════════════════════════════════════════════════════════════════
# TOKEN MANAGER
# ═══════════════════════════════════════════════════════════════════════════

class DhanTokenManager:
    """Token acquisition.

    Every wait is interruptible. Without this the TOTP retry loop sleeps for
    up to 30 seconds with no way out, so pressing STOP cannot reach it and a
    second START spawns a rival login that fights the first for the same
    rate-limited endpoint.
    """

    def __init__(self, stop_event: Optional[threading.Event] = None):
        self.stop_event = stop_event or threading.Event()

    def _sleep(self, seconds: float) -> bool:
        """Wait, returning False if we were asked to stop."""
        return not self.stop_event.wait(seconds)

    @property
    def cancelled(self) -> bool:
        return self.stop_event.is_set()

    def verify(self, token: str) -> bool:
        if not token:
            return False
        try:
            h = {"access-token": token, "client-id": DHAN_CLIENT_ID}
            return requests.get(f"{BASE_URL}/profile", headers=h, timeout=10).status_code == 200
        except Exception:
            return False

    def renew(self, token: str) -> Optional[str]:
        try:
            h = {"access-token": token, "dhanClientId": DHAN_CLIENT_ID,
                 "Content-Type": "application/json"}
            r = requests.get(f"{BASE_URL}/RenewToken", headers=h, timeout=15)
            d = r.json()
            if "accessToken" in d:
                log.info("Token renewed")
                return d["accessToken"]
            log.warning(f"Renew failed: {d}")
        except Exception as e:
            log.warning(f"Renew error: {e}")
        return None

    def generate(self, max_retries=3) -> Optional[str]:
        url = "https://auth.dhan.co/app/generateAccessToken"
        for attempt in range(max_retries):
            if self.cancelled:
                log.info("  Login cancelled.")
                return None
            rem = 30 - (int(time.time()) % 30)
            if attempt > 0 or rem < 10:
                log.info(f"  Waiting {rem + 1}s for TOTP window...")
                if not self._sleep(rem + 1):
                    log.info("  Login cancelled while waiting.")
                    return None
            totp = pyotp.TOTP(DHAN_TOTP_SECRET).now()
            log.info(f"  TOTP attempt {attempt + 1}")
            try:
                params = {"dhanClientId": DHAN_CLIENT_ID, "pin": DHAN_PIN, "totp": totp}
                r = requests.post(url, params=params, timeout=15)
                d = r.json()
                if "accessToken" in d:
                    log.info("Token generated")
                    return d["accessToken"]
                log.warning(f"  Generate attempt {attempt + 1} failed: "
                            f"{d.get('errorMessage', d)}")
            except Exception as e:
                log.warning(f"  Generate error: {e}")
        return None

    def ensure_token(self) -> str:
        if self.cancelled:
            raise RuntimeError("Login cancelled")
        if DHAN_ACCESS_TOKEN:
            log.info("Verifying existing token...")
            if self.verify(DHAN_ACCESS_TOKEN):
                return DHAN_ACCESS_TOKEN
            log.info("Token invalid, trying renew...")
            t = self.renew(DHAN_ACCESS_TOKEN)
            if t:
                self._save(t)
                return t
            log.info("Renew failed, generating via TOTP...")
        else:
            log.info("No existing token, generating via TOTP...")
        t = self.generate()
        if not t:
            raise RuntimeError("Could not obtain Dhan access token")
        self._save(t)
        return t

    def _save(self, token: str):
        try:
            set_key(str(ENV_FILE), "DHAN_ACCESS_TOKEN", token)
        except Exception:
            pass


def set_credentials(cid: str, pin: str, totp: str, token: str = ""):
    global DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, DHAN_ACCESS_TOKEN
    DHAN_CLIENT_ID = (cid or "").strip()
    DHAN_PIN = (pin or "").strip()
    DHAN_TOTP_SECRET = (totp or "").strip()
    DHAN_ACCESS_TOKEN = (token or "").strip()


def init_credentials(status_cb=None, stop_event: Optional[threading.Event] = None):
    global HEADERS, WS_URL, DHAN_ACCESS_TOKEN
    log.info("Authenticating with Dhan...")
    if status_cb:
        status_cb("Authenticating with Dhan...")
    token = DhanTokenManager(stop_event).ensure_token()
    DHAN_ACCESS_TOKEN = token
    HEADERS.update({"Content-Type": "application/json",
                    "access-token": token,
                    "client-id": DHAN_CLIENT_ID})
    WS_URL = (f"wss://api-feed.dhan.co?version=2"
              f"&token={token}&clientId={DHAN_CLIENT_ID}&authType=2")
    log.info("Credentials initialised. WebSocket URL ready.")


def get_ws_url() -> str:
    return WS_URL


# ═══════════════════════════════════════════════════════════════════════════
# REST
# ═══════════════════════════════════════════════════════════════════════════

def _hdrs() -> Dict[str, str]:
    return {"Content-Type": "application/json",
            "access-token": HEADERS.get("access-token", ""),
            "client-id": HEADERS.get("client-id", "")}


def api_post(endpoint: str, payload: dict, retries: int = 2):
    if "optionchain" in endpoint:
        OC_GATE.wait()
    else:
        DATA_GATE.wait()
    for att in range(retries + 1):
        try:
            r = requests.post(f"{BASE_URL}{endpoint}", headers=_hdrs(),
                              json=payload, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 ** (att + 1))
                continue
            log.error(f"  API {endpoint} -> HTTP {r.status_code}: {r.text[:200]}")
            if att < retries:
                time.sleep(1.5)
        except Exception as e:
            log.error(f"  API {endpoint} error: {e}")
            if att < retries:
                time.sleep(1.5)
    return None


def fetch_lot_sizes() -> Dict[str, int]:
    log.info("  Fetching lot sizes from scrip master...")
    try:
        r = requests.get(MASTER_URL, stream=True, timeout=120)
        r.raise_for_status()
        raw = r.content.decode("utf-8", errors="ignore")
        if raw.startswith("\ufeff"):
            raw = raw[1:]
        lots: Dict[str, int] = {}
        for row in csvmod.DictReader(io.StringIO(raw)):
            if row.get("SEM_INSTRUMENT_NAME", "").strip().upper() != "OPTIDX":
                continue
            try:
                lot_i = int(float(row.get("SEM_LOT_UNITS", "0").strip()))
            except Exception:
                continue
            if lot_i <= 0:
                continue
            ts = row.get("SEM_TRADING_SYMBOL", "").upper()
            for nm in ("BANKNIFTY", "FINNIFTY", "NIFTY", "SENSEX"):
                if ts.startswith(nm):
                    lots.setdefault(nm, lot_i)
                    break
        log.info(f"  Lot sizes: {lots}")
        return lots
    except Exception as e:
        log.error(f"  Scrip master error: {e}")
        return {}


def update_lot_sizes():
    lots = fetch_lot_sizes()
    for idx, cfg in INDEX_CONFIG.items():
        if idx in lots and lots[idx] > 0:
            old = cfg["lot_size"]
            cfg["lot_size"] = lots[idx]
            if old != lots[idx]:
                log.info(f"  {idx} lot size: {old} -> {lots[idx]}")


def fetch_expiry_list(index_name: str) -> List[str]:
    cfg = INDEX_CONFIG[index_name]
    payload = {"UnderlyingScrip": int(cfg["security_id"]), "UnderlyingSeg": "IDX_I"}
    resp = api_post("/optionchain/expirylist", payload)
    if resp and resp.get("status") == "success":
        return resp.get("data", [])
    log.error(f"  Expiry list failed for {index_name}: {resp}")
    return []


def get_target_expiry(index_name: str, offset: int = 0) -> Optional[str]:
    expiries = fetch_expiry_list(index_name)
    if not expiries:
        return None
    today = now_ist().date()
    future = sorted(
        (datetime.strptime(e, "%Y-%m-%d").date(), e)
        for e in expiries
        if datetime.strptime(e, "%Y-%m-%d").date() >= today
    )
    if not future:
        return None
    if len(future) > offset:
        return future[offset][1]
    return future[-1][1]


def fetch_option_chain(index_name: str, expiry: str) -> Optional[dict]:
    cfg = INDEX_CONFIG[index_name]
    payload = {"UnderlyingScrip": int(cfg["security_id"]),
               "UnderlyingSeg": "IDX_I", "Expiry": expiry}
    resp = api_post("/optionchain", payload)
    if resp and resp.get("status") == "success":
        return {"spot_price": float(resp["data"]["last_price"]),
                "oc": resp["data"]["oc"]}
    log.error(f"  Option chain failed for {index_name} {expiry}")
    return None


def fetch_intraday(security_id: str, segment: str, instrument: str = "OPTIDX",
                   interval: str = "1", days: int = 3) -> List[dict]:
    """One-minute history, used to seed candles when the app starts mid-session."""
    to_d = now_ist().strftime("%Y-%m-%d")
    fr_d = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
    payload = {"securityId": str(security_id), "exchangeSegment": segment,
               "instrument": instrument, "interval": interval,
               "fromDate": fr_d, "toDate": to_d}
    resp = api_post("/charts/intraday", payload)
    if not resp or "open" not in resp:
        return []
    out = []
    n = len(resp["open"])
    stamps = resp.get("timestamp", [0] * n)
    for i in range(n):
        try:
            out.append({
                "timestamp": normalize_epoch(int(stamps[i])) if stamps[i] else 0,
                "open": float(resp["open"][i]),
                "high": float(resp["high"][i]),
                "low": float(resp["low"][i]),
                "close": float(resp["close"][i]),
            })
        except Exception:
            continue
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ORDERS
# ═══════════════════════════════════════════════════════════════════════════

def _position_delta(security_id: str, baseline: Optional[int]) -> int:
    """How much the position has grown since we started trying."""
    if baseline is None:
        return 0
    try:
        return position_qty(security_id) - baseline
    except Exception:
        return 0


def _already_filled(security_id, baseline, qty, oids, side) -> Optional[dict]:
    """Did an earlier attempt fill without us seeing the confirmation?

    A poll that times out is not proof the order failed. On 23-Sep a limit
    order filled after we had given up on it, the market fallback filled as
    well, and the account ended up with twice the intended quantity. Asking
    the broker before sending anything further is the only way to be sure.
    """
    if baseline is None:
        return None
    got = _position_delta(security_id, baseline)
    if side == "SELL":
        got = -got
    if got < qty:
        return None
    px = 0.0
    for oid in reversed(oids):
        px = fill_from_tradebook(oid)
        if px > 0:
            break
    _notify(f"[ORDER] An earlier attempt had already filled {got} — not "
            f"sending another. Treating it as done.", "warning")
    return {"filled": True, "price": px, "order_id": oids[-1] if oids else "",
            "already": True}


def place_order_market(security_id, segment, side, qty, allow_when_stopped=False,
                       baseline: Optional[int] = None) -> dict:
    """A single market order. One request, one fill, nothing to reconcile.

    The limit+IOC ladder this replaced sent up to four orders per entry and
    took 36-51 seconds. On 23-Sep two of those four filled and the account
    carried twice the intended size; on 22-Sep all three limits missed and the
    market fallback paid 4.4% slippage anyway — straight through the very cap
    the limits existed to enforce. The price protection now sits where it can
    actually work: the slippage check made before this is called.
    """
    # An exit is allowed through after a stop — leaving a position open
    # because someone pressed STOP would be worse than the order itself.
    if _aborted() and not (allow_when_stopped or side == "SELL"):
        _notify("[ORDER] Stopped — market order not sent.", "warning")
        return {"filled": False, "price": 0.0, "order_id": "", "aborted": True}

    # If a caller retries us, make sure an earlier attempt has not already
    # filled. One order per entry is the point; this is the belt to that brace.
    if baseline is not None:
        done = _already_filled(security_id, baseline, qty, [], side)
        if done:
            return done

    ORDER_GATE.wait()
    pl = {"dhanClientId": str(DHAN_CLIENT_ID), "transactionType": side,
          "exchangeSegment": segment, "productType": "INTRADAY",
          "orderType": "MARKET", "validity": "DAY",
          "securityId": str(security_id), "quantity": int(qty),
          "disclosedQuantity": 0, "price": 0, "triggerPrice": 0,
          "afterMarketOrder": False}
    try:
        r = requests.post(f"{BASE_URL}/orders", headers=_hdrs(), json=pl, timeout=15)
        if r.status_code == 200:
            d = r.json()
            oid = str(d.get("orderId", ""))
            avg = float(d.get("averageTradedPrice", 0) or 0)
            if avg <= 0:
                avg = fill_from_tradebook(oid)
            if avg > 0:
                log.info(f"  [MKT FILLED] {side} qty={qty} @ Rs.{avg:.2f}")
                return {"filled": True, "price": avg, "order_id": oid}
            for _ in range(8):
                time.sleep(0.5)
                ORDER_GATE.wait()
                try:
                    pr = requests.get(f"{BASE_URL}/orders/{oid}",
                                      headers=_hdrs(), timeout=10)
                    if pr.status_code == 200:
                        pd = pr.json()
                        pp = float(pd.get("averageTradedPrice", 0) or 0)
                        if pp <= 0:
                            pp = fill_from_tradebook(oid)
                        if pp > 0:
                            log.info(f"  [MKT FILLED] {side} qty={qty} @ Rs.{pp:.2f}")
                            return {"filled": True, "price": pp, "order_id": oid}
                except Exception:
                    pass
            # Do not report this as a failure until the broker has been asked.
            # Assuming it failed is what let a second order go out on 23-Sep.
            try:
                held = position_qty(security_id)
                got = (held - baseline) if baseline is not None else held
                if side == "SELL":
                    got = -got
                if got >= qty:
                    px = fill_from_tradebook(oid) or 0.0
                    _notify(f"[MARKET ORDER] {side} {qty} did fill after all "
                            f"({got} held). Taking it as done.", "warning")
                    return {"filled": True, "price": px, "order_id": oid,
                            "already": True}
            except Exception:
                pass
            _notify(f"[MARKET ORDER] {side} {qty} placed as {oid} but no fill "
                    f"was confirmed. Check the broker terminal.", "warning")
            return {"filled": False, "price": 0.0, "order_id": oid}
        fatal = classify_order_error(r.text or "")
        if fatal:
            code, advice = fatal
            _notify(f"[ORDER REFUSED — {code}] {advice}")
            return {"filled": False, "price": 0.0, "order_id": "",
                    "fatal": True, "code": code, "advice": advice}
        _notify(f"[MARKET ORDER] HTTP {r.status_code}: {r.text[:250]}")
    except Exception as e:
        _notify(f"[MARKET ORDER] {type(e).__name__}: {e}")
    return {"filled": False, "price": 0.0, "order_id": ""}


# ═══════════════════════════════════════════════════════════════════════════
# LIVE ACCOUNT — POSITIONS, FILLS, FUNDS
# ═══════════════════════════════════════════════════════════════════════════

def api_get(endpoint: str, retries: int = 1):
    DATA_GATE.wait()
    for att in range(retries + 1):
        try:
            r = requests.get(f"{BASE_URL}{endpoint}", headers=_hdrs(), timeout=12)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 ** (att + 1))
                continue
            log.error(f"  GET {endpoint} -> HTTP {r.status_code}: {r.text[:160]}")
        except Exception as e:
            log.error(f"  GET {endpoint} error: {e}")
        if att < retries:
            time.sleep(1.0)
    return None


def fill_from_tradebook(order_id: str) -> float:
    """Average traded price for an order, from the trade book.

    The order endpoint reports an average that is sometimes zero or stale on a
    partial fill, so the trade book is the more reliable source: it lists the
    individual fills and we weight them by quantity.
    """
    if not order_id:
        return 0.0
    try:
        tr = api_get(f"/trades/{order_id}", retries=0)
        if not tr:
            return 0.0
        rows = tr if isinstance(tr, list) else [tr]
        qty = val = 0.0
        for t in rows:
            if not isinstance(t, dict):
                continue
            q = float(t.get("tradedQuantity", 0) or 0)
            p = float(t.get("tradedPrice", 0) or 0)
            qty += q
            val += q * p
        return (val / qty) if qty > 0 else 0.0
    except Exception as e:
        log.warning(f"  Trade book lookup failed for {order_id}: {e}")
        return 0.0


def fill_from_recent_trades(security_id: str, side: str = "BUY") -> float:
    """Quantity-weighted price we actually paid for an instrument today.

    Used when a position has to be adopted because no single order confirmed.
    The day's trade book is the only honest source — the last traded price is
    what the market was doing, not what we were filled at.
    """
    try:
        rows = api_get("/trades", retries=0)
        if not rows:
            return 0.0
        rows = rows if isinstance(rows, list) else rows.get("data", []) or []
        qty = val = 0.0
        for t in rows:
            if not isinstance(t, dict):
                continue
            if str(t.get("securityId", "")) != str(security_id):
                continue
            if str(t.get("transactionType", "")).upper() != side.upper():
                continue
            q = float(t.get("tradedQuantity", 0) or 0)
            p = float(t.get("tradedPrice", 0) or 0)
            qty += q
            val += q * p
        return (val / qty) if qty > 0 else 0.0
    except Exception as e:
        log.warning(f"  Trade book lookup failed for {security_id}: {e}")
        return 0.0


def get_positions() -> List[dict]:
    """Open positions at the broker. Only those with a non-zero net quantity."""
    try:
        resp = api_get("/positions", retries=1)
        if not resp:
            return []
        rows = resp if isinstance(resp, list) else resp.get("data", [])
        out = []
        for p in rows or []:
            if not isinstance(p, dict):
                continue
            try:
                if int(float(p.get("netQty", 0) or 0)) != 0:
                    out.append(p)
            except Exception:
                continue
        return out
    except Exception as e:
        log.warning(f"  Position fetch failed: {e}")
        return []


def position_qty(security_id: str) -> int:
    """Net quantity we hold in one instrument, per the broker."""
    for p in get_positions():
        if str(p.get("securityId", "")) == str(security_id):
            try:
                return int(float(p.get("netQty", 0) or 0))
            except Exception:
                return 0
    return 0


def get_available_funds() -> Optional[float]:
    """Cash available to trade. None when the call fails — which is not the
    same as zero, and must never be treated as such."""
    try:
        resp = api_get("/fundlimit", retries=1)
        if not resp or not isinstance(resp, dict):
            return None
        for k in ("availabelBalance", "availableBalance", "withdrawableBalance"):
            if k in resp:
                return float(resp[k] or 0)
        return None
    except Exception as e:
        log.warning(f"  Fund limit fetch failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET BINARY PARSERS
# ═══════════════════════════════════════════════════════════════════════════

EXCH_SEG_MAP = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CUR",
                4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CUR", 8: "BSE_FNO"}


def parse_header(msg: bytes) -> Optional[dict]:
    if len(msg) < 8:
        return None
    return {"resp_code": msg[0],
            "seg": EXCH_SEG_MAP.get(msg[3], str(msg[3])),
            "security_id": str(struct.unpack_from("<I", msg, 4)[0]),
            "payload": msg[8:]}


def parse_ticker(payload: bytes) -> Optional[dict]:
    if len(payload) < 8:
        return None
    return {"ltp": float(struct.unpack_from("<f", payload, 0)[0]),
            "ltt": int(struct.unpack_from("<I", payload, 4)[0])}


def build_subscribe_message(instruments: List[dict]) -> str:
    """instruments: [{"ExchangeSegment": ..., "SecurityId": ...}, ...]"""
    return json.dumps({"RequestCode": 15,
                       "InstrumentCount": len(instruments),
                       "InstrumentList": instruments})
