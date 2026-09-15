# The Overlap

**Overlap Retest Option Buyer** — Strategy Note 03, v1.2
Balfund Trading Pvt Ltd · www.balfund.com

> This build is **paper trading only**. `PAPER_ONLY_BUILD = True` in
> `config.py` forces every order path to simulate. No order reaches the
> exchange.

---

## The strategy in short

The index is read from the 09:30–09:33 candle and ten option charts are
opened around it — five strikes, the call and the put at each. From the next
candle onward we look for a strike whose **call and put candles literally
overlap** in price, in opposite colours, with the red candle's close at least
five paise below the green candle's close.

That pair is fixed for the day. Each chart then borrows the other's high
and low from the overlapping candle — four lines that never move.

When one chart prints a red candle lying **wholly** below its borrowed low
(wicks included, body at least half the candle) while the other closes a
green candle above its borrowed high, the setup is armed. We buy the leg
that broke out — but only when price returns into a band of four points
either side of that same high.

Out at fifteen per cent, or when the collapsed chart closes a candle back
above its borrowed high, or at 15:15.

Two trades a day, both from the same pair and the same four lines.

---

## Running it

```bash
pip install -r requirements.txt
python main.py
```

Enter the Dhan client ID, PIN and TOTP secret in the left panel, check the
settings, and press **START**. Credentials are written to a local `.env`
which is git-ignored.

Other modes:

```bash
python main.py --test      # internal checks, no network
python sim_run.py          # full scripted session against the note
python main.py --console   # headless, credentials from .env
```

Both suites run in CI before the EXE is built, so a broken build never
produces an artifact.

---

## The interface

**Seven condition cards**, in the order the strategy tests them. Each one
is grey while it is not yet relevant, amber while it is actively being
tested with a live readout of how close it is, green once satisfied, and
red when it has been tested and lost.

| # | Card | What it watches |
|---|------|-----------------|
| 1 | The ladder of ten | Index read at 09:30, five strikes, CE and PE at each |
| 2 | The overlap | Every call against every put, on each closed candle |
| 3 | The four lines | Levels cross-plotted from the overlapping candle |
| 4 | The collapse | Both legs tested: red, whole candle below its borrowed low, body 50%+ |
| 5 | The break | Both legs tested: green, close above its borrowed high |
| 6 | The retest | Live price returning into the band |
| 7 | The exit watch | Target on our premium, stop on the other chart |

The **ladder table** shows all ten instruments with live prices. Once the
pair is found, those two legs are marked in violet with arrows pointing
inward, so it is obvious at a glance which two charts are in play.

The **activity log** carries every decision as it is made, including the
misses — the candle that failed the body test, the pair that overlapped
but missed the gap by two paise.

---

## Files

| File | What it holds |
|------|---------------|
| `config.py` | Every tunable in the note, in one place. Palette and fonts. |
| `dhan_api.py` | Token manager, REST, orders, WS binary parsing. Knows nothing about the strategy. |
| `candles.py` | Tick aggregation into N-minute bars, with deterministic bar closes. |
| `strategy.py` | The state machine — ladder, overlap scan, frame, signal, retest, exits. |
| `app.py` | The interface. |
| `main.py` | Entry point, self-test, console mode. |
| `sim_run.py` | Scripted session asserting the note's rules. |

Written to `logs/`:

- `ovl_activity_YYYYMMDD_HHMMSS.log` — everything
- `ovl_trades_YYYYMMDD.csv` — one row per closed trade

---

## Settings

| Setting | Default | Notes |
|---|---|---|
| Index | NIFTY | NIFTY, BANKNIFTY or SENSEX, one at a time |
| Timeframe | 3 min | 3 or 5 |
| Reference time | 09:30 | Ladder freeze |
| Strikes each side | 2 | Gives five strikes, ten charts |
| Minimum gap | 0.05 | Red close below green close |
| Ranges must overlap | on | Turning this off is not advised — see below |
| Minimum body | 50% | Of the collapsing candle's own range |
| Signal window | 0 bars | 0 = collapse and break on the same candle |
| Max break age | 0 (off) | See "the stale break" below |
| Retest buffer | 4.0 pts | **Applies to the return only, never to the break** |
| Pair | Same strike | Same strike, or any call against any put |
| Reference candle | Starting at that time | 09:30–09:33, or the older 09:27–09:30 |
| Expiry | Current | Current or next |
| Target | 15% | Above the price paid |
| Safety stop | on, 25% | Below the price paid |
| Lots per entry | 1 | |
| Max trades per day | 2 | |
| Last entry / square off | 15:15 | |

## Changes in v1.1 (after the 25/08 paper session)

**Same-strike pairing, now the default.** A call pairs only with the put at
its own strike. This is the mode the strategy is designed around: a call and
a put at the *same* strike only trade at the same premium when the index is
sitting on that strike, so the overlap is telling us something. Cross-strike
pairs can show equal premiums for no reason beyond being different distances
from the money — on 25/08 the app paired 24100 CE with 24300 PE, which is
that failure. `Any strike` remains available as a setting.

**The reference candle is 09:30–09:33.** The ladder is now built from the
candle that *starts* at the reference time and locks when it closes at 09:33.
The overlap scan begins on the next candle; the reference candle itself is
not scanned, since the ladder does not exist until it has closed.

**The merge is highlighted.** A banner above the condition cards stamps the
moment the two charts overlapped, with both candles' OHLC and all four
borrowed lines. The top strip carries a `MERGED AT` figure alongside the pair.

**The daily limit now survives a restart.** State was only written on a clean
stop, so a kill and relaunch reset the count — on 25/08 that produced three
trades against a limit of two, numbered #1, #1, #2. State is now written the
moment a trade closes, and the frame is written with it.

**No more logging after the day is done.** Once the trade limit is reached or
the last entry time has passed, the engine stops testing rather than
reporting findings it cannot act on. The 25/08 log carried 35 such lines.

---

## Collapse and break, for the client

These are the two halves of one signal, and **neither counts on its own.**

The **collapse** is the leg the market has abandoned. Its candle must be red,
must sit *wholly* below its borrowed low — wicks included, not just the close
— and its body must be at least half the candle's height so that a
long-wicked indecisive candle does not qualify.

The **break** is the leg money is moving into. Its candle must be green and
must *close* above its borrowed high. That is all: it need not clear the line
completely, and there is no body requirement.

We insist on both because one leg moving is only noise in a single premium.
Both legs moving in opposite directions at the same moment means the index
itself has moved, and that is what we are trading.

The two cards each report their own test across **both** legs, so a line
like

    24450 CE: not red          ·  24450 PE: high 101.85 not below 101.85
    24450 CE: YES — closed 114.05 above 110.50  ·  24450 PE: not green

reads as: the call broke out, the put did not collapse — its high touched
101.85 exactly, and touching the line is not the same as being below it — so
there is no trade. A one-paisa miss, and the rule working as written.

---

## v2.4 — any one, any two, or all three indices

NIFTY, BANKNIFTY and SENSEX can now run together. Tick the ones you want in
the settings panel.

**Each index is a separate strategy.** Its own ladder of ten, its own pair,
its own four lines, its own signals, its own trades and its own daily limit.
Nothing is shared but the broker account.

That is also how it is built: rather than teaching the engine to hold three of
everything — and putting every rule and guard already in it at risk — the
supervisor runs one engine per index, each handed a copy of the settings
carrying a single name. A one-index session is byte-for-byte the same code
path it has always been.

What the supervisor does have to think about, because it *is* shared:

- **Daily state.** Each index writes `ovl_daily_state_<INDEX>.json`. One
  shared file would mean the second index to start wiped the first one's
  trade count.
- **The polling rate.** Three ladders is thirty charts through one
  four-per-second gate, so a cycle physically takes about eight seconds.
  Asking for four would not make it faster, it would just mean the feed never
  rests — so the poll is raised to something achievable and the reason is
  said out loud rather than left to be discovered.
- **Order refusals.** A permanent refusal is about the account, not the
  index. When one engine is halted by DH-905 the others are halted too,
  because they would be refused for exactly the same reason.

In the interface, a tab appears per index above the merge banner — **only
when more than one is running**, so a single-index session looks exactly as
it did. The tab you are on drives the condition cards, the merge banner and
the ladder; the P&L and trade count in the top strip show every index added
together. Log lines are prefixed with the index they came from.

The live confirmation dialog lists every index going live and the total trade
limit across them.

---

## Before live mode will work at all

Dhan requires the machine's **public IP to be whitelisted for order placement**.
Everything else — funds, positions, history, the websocket — works without it,
so a session can look completely healthy right up until the first order:

```
[ORDER] HTTP 400: {"errorCode":"DH-905","errorMessage":"Invalid IP"}
```

Add the IP under the DhanHQ API section of the account profile. Check the
machine's public address from that machine, not another one. If the ISP hands
out a dynamic IP this will break again whenever it changes, so a static IP is
worth arranging for daily use.

Place one manual test order through the app in live mode before trusting it
with a session.

---

## Fix in v2.3 — STOP now stops orders

`stop()` set the stop flag, killed the REST feed and closed the websocket —
and never touched the order path. On 09-Sep an entry sequence begun at
12:57:18 was still running when STOP was pressed at 12:57:20, and finished
afterwards:

```
12:57:18   [LIVE] Sending BUY 65 NIFTY 23500 PE...
12:57:20   Stopped.          <- STOP pressed
12:57:20   Entry not filled  <- the sequence completed after
```

An entry is four orders — three LIMIT+IOC then a MARKET fallback — so at
least the fallback went out at or after the STOP. Only the unwhitelisted IP
kept it from becoming a real position nobody asked for.

Three checks now:

- `_on_trade_tick` returns immediately once stopped, so a tick already in
  flight cannot begin an entry
- `_enter` and `_place_entry` check before doing anything
- the order layer itself asks before **every** attempt and before the market
  fallback, so a sequence already under way is cut short

**Exits are deliberately exempt.** A SELL is still attempted after a stop —
leaving a position open because someone pressed STOP would be worse than the
order. And stopping while a live position is open now says so:

```
Stopping. No further orders will be sent.
  A LIVE POSITION IS STILL OPEN — 65 of NIFTY 23500 PE. Stopping the app
  does not close it. Square it off in the broker terminal, or restart and
  let the app manage it.
```

---

## Fix in v2.2 — permanent refusals stop everything

On 09-Sep an unwhitelisted IP produced **60 rejected orders in 62 seconds** —
fifteen entry attempts, each firing three LIMIT+IOC retries and a MARKET
fallback, every one refused for the same unchangeable reason.

Refusals are now split into two kinds. A **permanent** one — the IP is not
whitelisted, the token cannot trade, the segment is not permitted — halts
trading for the session on the **first** occurrence:

```
TRADING HALTED — the broker refused the order (DH-905).
  This machine's IP address is not whitelisted for order placement on the
  Dhan account. Add it under the DhanHQ API section of the account profile.
  No further orders will be sent this session. Fix the cause, then restart.
```

No retries, no MARKET fallback, no further attempts on any signal. Ordinary
rejections — insufficient funds, a price out of range — still retry as before.

If a permanent refusal arrives while a position is **open**, the trade is kept
open and the log says so plainly, because the API can no longer close it:

```
A POSITION IS STILL OPEN — 65 of NIFTY 23500 PE. It cannot be closed through
the API while this is refused. Square it off in the broker terminal now.
```

---

## Fixes in v2.1 — first live session

Two defects, found on the first live run.

**Order failures were invisible.** `dhan_api` logged rejections through the
Python logger, which reaches `logs/` but never the interface — so the screen
said only `Entry not filled` while the reason sat in a file. Rejections, HTTP
errors and unconfirmed market orders now all surface in the activity log:

```
[REJECTED] <the broker's own words> (order 12345)
[ORDER] HTTP 400: <response body>
```

**A failing entry was retried on every tick.** The signal stayed armed, so
each new tick inside the band triggered another attempt — and each attempt is
up to four orders (three LIMIT+IOC, then a MARKET fallback). Five attempts
went out in fifteen seconds; as many as twenty orders could have reached the
exchange.

Entries are now capped at `Entry attempts` (3) per armed signal, with
`Entry retry gap` (10s) between them. After the last one the signal is
dropped rather than left hammering:

```
Entry not filled on NIFTY 23500 PE (attempt 3 of 3, none left).
Signal dropped — 3 entry attempts all failed. Not trying again on this
signal — fix the cause before restarting.
```

---

## v2.0 — live trading

Paper is still the default and always will be. Live is a per-session choice:

- **`Mode` in the settings** — `paper` or `live`
- **Live must be typed out.** A dialog lists the index, quantity, trade limit,
  target and safety stop, and the word `LIVE` has to be entered before
  anything starts. A dropdown is too easy to nudge.
- **Live is never restored from a settings file.** Reopening the app always
  comes up in paper, whatever was running yesterday.
- The badge turns red and reads `● LIVE` while orders can reach the exchange.

### The three things that make live different from paper

**One order per signal, always.** The entry lock used to be released before
the order was sent, so two ticks arriving during the round trip would both
send a BUY. In paper the window is microseconds; against a real broker it is
seconds. An `_order_in_progress` flag now spans the whole round trip, and
there is a test that fires a second tick mid-order and asserts one order.

**A failed exit is not an exit.** The trade is no longer cleared before the
sell confirms — clearing first would leave us believing we are flat while
still holding the option, which is the worst state to be in. The sell is
retried, and between attempts the broker is asked what we actually hold, so
an order that went through without confirming is recognised rather than
duplicated. If every attempt fails the trade stays open and the log says so
in as many words:

```
EXIT FAILED — 65 of NIFTY 24450 CE could not be sold after 5 attempts.
THE POSITION IS STILL OPEN. Square it off in the broker terminal now.
```

**The broker is asked what we hold.** Every 15 seconds during a live trade.
A position can vanish without us — squared off by hand, closed by the
broker's risk system — and carrying on as though it were open would produce a
phantom exit and a P&L that never happened. When it is gone the trade is
booked at the trade-book price with the reason `CLOSED EXTERNALLY`. Fresh
fills get 30 seconds before being judged, since the broker needs a moment to
register them.

### Before an order goes out

- **Slippage** — if the premium has moved more than `Max slippage %` (3% by
  default) from the price that triggered the retest, the entry is abandoned.
  The trade we were about to take is not the one we decided on.
- **Funds** — checked against the cost of the position. A failed funds call
  is reported but does not block trading, since not knowing is not the same
  as knowing there is nothing.
- **Orphans** — if an entry does not confirm, the broker is asked whether it
  filled anyway. A position nobody is watching is worse than a missed trade,
  so it is adopted and squared off normally.

Fill prices come from the trade book (`/trades/{orderId}`, quantity-weighted)
rather than the order's reported average, which is sometimes zero or stale on
a partial fill.

---

## Change in v1.9 — a late start no longer loses the day

Starting at 10:40 meant twenty candles between 09:33 and 10:34 were fetched
and then thrown away unscanned. If the merge happened at 09:45 we held the
candles that proved it and ignored them.

The engine now **replays that history to find the merge and draw the four
lines**, then starts live from the current candle:

```
Started late — 21 candle(s) have already happened. Fetching them to look
for the merge.
Found the merge in the replayed candles. The four lines are drawn and
trading starts from the live candle — nothing that happened while we were
off is acted on.
```

**Only the merge is replayed.** A signal armed an hour ago would have an
entry band nowhere near current price, so arming it now would either sit
unfillable all day or fill badly. History decides the frame; the live candle
decides the trades. The reference candle itself is still never scanned.

Two guards:

- Catch-up waits for every chart to deliver its history before replaying, so
  the merge is never found on a partial ladder, with a 30-second deadline in
  case a chart never answers.
- A span longer than a trading session is refused rather than looped over —
  that means the reference time or the system clock is wrong, and replaying
  it would grind through candles that cannot exist.

`Replay on late start` in the settings turns it off.

**The duplicate startup fetch is gone.** `_seed_history` was pulling the same
bars the REST feed fetches moments later — twenty wasted calls on every late
start. It is skipped when candles come from the broker.

---

## Fix in v1.8 — the REST feed delivered nothing

On its first live morning the feed produced no bars at all and every candle
was abandoned:

```
09:30 abandoned — only 0 of 10 charts ever reported that candle.
09:33 abandoned — only 0 of 10 charts ever reported that candle.
```

`api.fetch_intraday` returns rows keyed **`timestamp`**; the feed reads
**`ts`**. Every poll raised `KeyError: 'ts'`. The translation now happens in
`_fetch_1m`, at the single boundary between the two.

Two things let that reach a live session, and both are fixed:

- **The failure was invisible.** `restfeed` logged through the Python logger,
  which reaches the file but not the interface. Poll errors, stalls,
  abandoned polls and empty responses now all surface in the activity log.
- **The tests used the wrong shape.** Every fake supplied rows keyed `ts` —
  testing the assumption rather than the payload. The suite now drives a
  real-shaped `fetch_intraday` response end to end, and asserts a bar comes
  out of it.

The feed also announces the first bar it receives, so a working feed is
visible rather than merely quiet:

```
First broker bar received — NIFTY 24450 CE 09:27 O102.70 H109.15 L101.85 C108.70
```

---

## Fix in v1.7 — the reference candle on a late start

Started at 09:34 on 09-Sep, after the 09:33 lock. The websocket had no index
ticks for 09:30-09:33, so the reference candle did not exist and the ladder
was built from the **live index price at 09:34:43** instead of the 09:33
close. Today both rounded to 23,500; on a morning with a 30-point move in
that gap they would not.

The reference close is now looked up in this order:

1. the candle we built ourselves, if we were running when it formed
2. **the index's own 1-minute history from the broker**
3. the live index price, and only then with a warning that the ladder may not
   match what the strategy would have chosen

Step 2 is what the SENSEX build already did, and it means a late start
resolves the same ladder the strategy would have picked at 09:33. A
still-forming candle is refused rather than used.

---

## Changes in v1.6 — candles from the broker

Every decision the strategy makes is a comparison of raw OHLC at five-paise
resolution. Candles built from ticks are only as good as the ticks that
arrived, and **nothing backfills a gap** — on 01/09 the feed dropped five
times in the forty minutes before the merge candle formed, and the four lines
drawn from that candle governed the whole session.

The split is now:

| | Source | Decides |
|---|---|---|
| **REST** | the broker's own 1-minute bars, rolled up | the merge, the four lines, the collapse, the break |
| **WebSocket** | LTP only | the retest fill, the target, the safety stop |

Everything judged on a candle close comes from the broker. Everything judged
on live price stays on the socket, where it belongs.

`Source` in the settings switches between **Broker bars (REST)** and
**Live ticks**; the tick engine is untouched, so a session can be run either
way and the results compared.

Ported wholesale from the SENSEX project, because each one is scar tissue:

- **`drop_forming`** — Dhan serves the current minute as it builds and keeps
  changing it. Without this a three-minute bar can look complete while its
  last member is two seconds old.
- **`session_anchor`** — a poll made before the open otherwise returns the
  whole previous session and emits every bar as live.
- **A hard deadline on each poll** — a socket timeout is not a guarantee; a
  call can sit for many minutes on a dead keep-alive. One stuck request must
  not take the feed with it.
- **Latency measured on every bar**, with a verdict, shown in the title bar
  as `BARS 1.4s avg / 2.1s max` and coloured by how healthy it is.

**We wait for the candle to form.** The feed emits a bar only when it is
genuinely finished — a full set of source minutes, or the grace period
elapsed — and the strategy then waits until every polled chart has delivered
that candle before evaluating anything. A merge is never found on a partial
ladder. If a chart has still said nothing a whole candle later the bucket is
**abandoned**, not part-evaluated, and the log says so:

```
10:15 abandoned — only 8 of 10 charts ever reported that candle.
Nothing was evaluated on it.
```

That backstop exists only so a single silent instrument cannot stall the
strategy for the rest of the day.

One thing is ours rather than ported: **polling narrows after the merge.**
Ten charts are polled while hunting the pair; once it is locked the other
eight decide nothing, so only two are polled from then on — which puts us at
the same two-instrument load the SENSEX build runs.

`freshness.py` measures the latency on your own connection before you trust
it:

```bash
python freshness.py --minutes 30
```

---

## Fixes in v1.5

Three defects found in the 01/09 session log.

**Two logins could run at once.** The TOTP retry loop slept for up to 30
seconds with no way out, so STOP could not reach it — the engine did not
exist yet — and a second START spawned a rival login. On 01/09 both fought
over the same rate-limited endpoint and produced
`Token can be generated once every 2 minutes`. Every wait in the token
manager is now interruptible, STOP cancels a login in flight, and START is
refused while a previous worker is still alive.

**A dead worker could re-enable START while another engine was running.**
Each start now carries a sequence number and only the current worker resets
the interface. Without this a third engine could have been started on top of
a live one.

**The feed stayed connected for six and a half hours after the day ended.**
The 01/09 session finished at 10:50 and was still reconnecting at 17:20. The
websocket is now dropped at square-off and does not reconnect.

**The arming message described the wrong stop.** It still said
`Stop will watch ... closing above ...` while the entry message a second
later correctly said `Stop needs BOTH`. Both now describe the two-sided stop.

Note the entry rule is unchanged and deliberate: if price is already inside
the band when the signal arms, the trade is taken on the next tick. There is
no requirement for price to have been outside the band first.

---

## Changes in v1.4 (strategy note v1.6)

**The merge is measured on bodies, not on full ranges.** Wicks take no part.
The two bodies — open to close — must share at least `Min body overlap`
(default 0.05) of price. Touching is not enough, and two candles whose wicks
cross while their bodies stay apart no longer qualify.

**The separate close-gap test is gone**, and nothing is lost by it. The top of
a green body *is* its close and the bottom of a red body *is* its close, so a
five-paise body overlap already guarantees the green close sits five paise
above the red close. One test does the work of two.

`min_gap` is now `min_overlap`. Old settings files still load — the former key
is read as a fallback.

The overlap is rounded to two decimals before comparison, since prices are
quoted in paise and `110.05 - 110.00` otherwise evaluates to 0.049999.

What this changes in practice: the 31/08 merge reported a 7.10 close gap, but
the bodies only shared **0.55**. Every session so far still merges, but the
reported number now says how solid the merge actually was.

---

## Changes in v1.3 (strategy note v1.5)

**The structural stop now needs both charts.** Two conditions, both true on
the same closed candle:

- the **other** leg closes **above** its borrowed high, and
- the leg **we hold** closes **below** its own borrowed low

Either alone leaves us in the trade. The entry asks both charts to agree, and
now so does the exit. When only one side is met the log says
`Stop half-met — ... Holding.` so it is visible.

Set `Two-sided stop` off in the settings for the older behaviour, where the
other chart ended the trade by itself.

Note the consequence: the structural stop is now a later exit than it was,
because our own premium has to fall below its borrowed low before it can fire
at all. That puts more of the real risk on the percentage safety stop, so
turning **both** off would leave a trade with almost no floor.

---

## Changes in v1.2 (strategy note v1.4)

**The collapse measures the body, not the whole candle.** Open and close must
both be below the borrowed low; the wick may cross it and the candle still
counts. The 50% body requirement is unchanged.

**The collapse comes first, or together with the break.** A break that was
already running before the collapse arrived does not count — the engine says
so in the log and waits for a fresh one. This is what stops a break from
09:45 pairing with a collapse at 10:09 to arm a signal whose band price left
long ago.

**A collapse stands until that leg recovers.** No timer: the collapse stays
valid until the collapsed chart closes back above its own borrowed high, the
same test that ends a live trade.

**After a stop, the next entry is relaxed.** Either stop — structural or
safety — drops the collapse requirement for the following trade: a break
above a borrowed high on either chart, then the retest, is enough. A *target*
does not relax anything. The mode is written to the state file, so it
survives a restart. The top strip shows `NEXT ENTRY: FULL` or `RELAXED`.

**The relaxed entry is guarded.** It is not taken while the opposite chart is
already above its own borrowed high, since the stop would fire on the next
candle. Switchable.

`Max break age` has been removed — the ordering rule makes it redundant.

---

## The stale break (26/08)

A leg re-qualifies as broken out on **every** candle it closes above its
borrowed high. So a break from twenty minutes ago keeps looking fresh, and
can pair with a late collapse to arm a signal whose entry band price left
long ago.

On 26/08 the put broke its line at 10:03 and closed at 109.55 — inside the
105.15–113.15 band, so an entry would have filled instantly. The call did not
collapse until 10:09, by which time the put was at 118.60, five points clear
of the band. The signal armed and never filled.

`Max break age` fixes this when set above zero: the break must be no more
than N candles older than the collapse, otherwise the engine says so in the
log and does not arm. Left at **0** the original behaviour is unchanged.

While a signal is armed and unfilled the log now prints a heartbeat on every
candle showing where price sits relative to the band. Before this the log
fell completely silent while armed, which reads as a hung application.

---

### Two things worth knowing

**The buffer is not part of the breakout.** The green candle has to close
above the borrowed high itself, with nothing added. The four points are a
tolerance around that same line for the return journey only. A candle
closing one paisa above the line arms the signal.

**The overlap requirement is what makes the pair unique.** Without it, a
far-OTM call at ₹31 and a deep-ITM put at ₹248 satisfy the close-vs-close
test easily and would be picked as "merged" on the first candle of the
day. `sim_run.py` has an explicit test for this case.

---

## Building the EXE

Push to `main`. GitHub Actions runs both test suites, then builds with
PyInstaller.

**Actions → Build Windows EXE → Artifacts → `Balfund_TheOverlap_EXE`**

---

## Not in this version

- Live order placement (paper only, by design)
- A daily loss cap
- Partial booking or trailing stops — the note specifies a single target
  taken in full
- Re-scanning for a fresh pair after the first trade; the same pair and the
  same four lines serve both trades, as specified
