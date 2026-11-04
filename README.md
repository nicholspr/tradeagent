# TradeAgent

A personal AI trading agent for an eToro account. The [TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent framework (v0.7.1), running Claude, researches each entry on a watchlist. Analyst agents gather evidence, two debates test it, and a Portfolio Manager agent gives a 5-tier rating: **Buy, Overweight, Hold, Underweight or Sell**. This app turns that rating into a sized eToro order, but only after fixed safety limits have been applied in code that the AI cannot see or change.

It runs 24x7 in Docker on a Hostinger VPS behind Traefik at `https://nichsoft.io/trader/`, protected by the site's existing login. Approval links in SMS and email are protected separately by a signed token.

*This is a research tool, not financial advice. AI trading decisions can lose money, and past results do not predict future ones.*

---

## Contents

1. [How a trade decision is made](#1-how-a-trade-decision-is-made)
2. [Modes and the kill switch](#2-modes-and-the-kill-switch)
3. [Settings explained](#3-settings-explained)
4. [The daily schedule](#4-the-daily-schedule)
5. [Watch levels (conditions checked overnight)](#5-watch-levels-conditions-checked-overnight)
6. [Dashboard pages](#6-dashboard-pages)
7. [Notifications and the daily summary](#7-notifications-and-the-daily-summary)
8. [Safety design](#8-safety-design)
9. [Watchlist format and eToro symbols](#9-watchlist-format-and-etoro-symbols)
10. [AI models and cost](#10-ai-models-and-cost)
11. [Setup and deployment](#11-setup-and-deployment)
12. [Development](#12-development)

---

## 1. How a trade decision is made

Every analysis, whether scheduled or run by hand, goes through these steps:

1. **Check the symbol on eToro.** The app finds the instrument by its exact symbol. If eToro's search doesn't list it (some newer London-listed funds such as IUCS.L), it looks the symbol up directly instead. It then checks that the eToro instrument is the same kind of asset as the watchlist entry: a share entry must not resolve to a crypto token. eToro's `CVX` is Convex Finance, a crypto token; Chevron is `CVX.US`.
2. **Read the portfolio.** It reads cash (free cash minus money reserved for pending orders) and every unleveraged, non-copy holding with its units and average purchase price. Holdings are labelled with the ticker the agents research, so eToro `BTC` appears as `BTC-USD`.
3. **Run the agents** (TradingAgents, about 3–4 minutes):
   - **Four analysts** gather evidence at the same time:
     - **Market:** price history and technical indicators.
     - **Sentiment:** social media; StockTwits is usually unavailable.
     - **News:** company and world news, FRED economic data (inflation, rates, jobs) and prediction-market odds.
     - **Fundamentals:** SEC filings and Yahoo financials. There is little of this for funds and crypto.
   - **Bull versus Bear debate**, judged by the **Research Manager**, which writes an investment plan.
   - **Trader:** turns the plan into a concrete proposal.
   - **Risk debate** between Aggressive, Conservative and Neutral analysts.
   - **Portfolio Manager:** gives the final rating and a written decision.
   - Every deciding agent is told your portfolio and **this account's rules**: trade sizes, daily caps, cash to keep, the position cap, the cooldown, and "market orders only, no limit or stop orders". If the share is being analysed because a [watch level](#5-watch-levels-conditions-checked-overnight) was met, they're told that too.
4. **Turn the rating into a proposal.** From here on, everything is plain code with no AI:

   | Rating | Proposal |
   | --- | --- |
   | Buy | Buy **Max per trade** |
   | Overweight | Buy **half** of Max per trade |
   | Hold | Nothing |
   | Underweight | Sell half of the largest position in that holding (only if "Agent may propose sells" is on) |
   | Sell | Close the whole holding (only if "Agent may propose sells" is on) |

   Only the rating is used. Amounts, limit levels or stop-losses suggested in the written report are ignored.
5. **Apply the limits.** A buy may be shrunk or blocked. See [Settings](#3-settings-explained) and [Safety design](#8-safety-design).
6. **Act according to the mode:**
   - **Signals:** you're told the result.
   - **Approval:** you get an approval link.
   - **Automatic:** a buy is placed immediately. A sell still waits for approval unless "Auto mode may sell" is on.
7. **Re-check at execution.** Just before any order is sent, the proposal is checked again against the live portfolio, the limits, the kill switch and whether the market is open. An order for a closed market is queued until it opens.
8. **Record the outcome.** Every step is written to the audit trail. If the analysis didn't trade, any price condition it set is saved as a watch level.

## 2. Modes and the kill switch

Change these on the **Settings** page, in the Control card.

| Mode | What happens |
| --- | --- |
| **Signals** | You're sent every call. Nothing is ever traded by the app. |
| **Approval** | Each proposed trade is sent to you by SMS and email with a link. Nothing is placed until you press Approve. Unanswered approvals expire after "Approval expiry". |
| **Automatic** | Buys are placed immediately, within the limits. Sells still need your approval unless "Auto mode may sell without approval" is on. |

**Kill switch:** while it is on, no new order of any kind can be placed, in any mode, including approved trades waiting in the queue. Analyses still run.

**Test mode (`DRY_RUN=true`):** set in the Hostinger environment. Everything runs as normal, but no order is ever sent to eToro, and results are labelled "TEST (not sent)". It also enables the Test tools card.

## 3. Settings explained

All of these are on the **Settings** page and take effect immediately; schedule changes reschedule the jobs. Each save is written to the log.

### Watchlist and schedule

| Setting | Current | Code default | What it does |
| --- | --- | --- | --- |
| Watchlist | 16 entries | `META,GOOG,MU,UPB,VRT,BTC=BTC-USD@crypto` | The only instruments the agents research and the only ones the app can buy. See [Watchlist format](#9-watchlist-format-and-etoro-symbols). |
| Analyses per day | 5 | 2 | Total analyses per day, **manual runs included**. This is the main cost control. Crypto entries are analysed at the crypto time, and the remaining slots go to shares. |
| Share run time | 15:05 | 15:05 | UK time, weekdays. Just after the US open (14:30 UK); London is open until 16:30. |
| Crypto run time | 09:05 | 09:05 | UK time, every day, for `@crypto` entries. |

**How shares are chosen for the 15:05 run:** first, any share whose watch level was met overnight. Then the **least recently analysed** shares, with never-analysed shares first in watchlist order, until the day's slots are used.

### Trading limits

| Setting | Current | Code default | What it does |
| --- | --- | --- | --- |
| Max per trade | $75 | $25 | The size of a Buy; Overweight is half. Also a hard ceiling on any single buy. |
| Max buys per day | $150 | $50 | Total spent on buys since midnight (or since a manual reset). |
| Max trades per day | 5 | 2 | Buys and sells together. |
| Cash to keep | $50 | $5 | The app never spends below this. |
| Max in one holding | 25% | 40% | A buy is shrunk or blocked so that no holding (including pending buys) goes above this share of the portfolio. |
| Days between trades | 0 | 3 | Minimum gap between app trades in the same holding. **0 means no cooldown**: in Automatic mode the same share could be bought on consecutive days. |

eToro's minimum order is **$10**. A buy that limits shrink below $10 is blocked.

### Approvals and sells

| Setting | Current | Code default | What it does |
| --- | --- | --- | --- |
| Approval expiry | 6 hours | 6 hours | An unanswered approval expires after this. A 15:05 proposal lasts until about 21:05. |
| Agent may propose sells | on | on | If off, Underweight and Sell ratings do nothing; the app can only buy. |
| Auto mode may sell without approval | off | off | Keep off, so sells of your holdings always need your OK. |

### AI models

| Setting | Current | What it controls |
| --- | --- | --- |
| Evidence and debate agents | Claude Haiku 5.5 | 10 agents: the four analysts, Bull, Bear, the trader and three risk debaters. Most of the cost is here. Also used to read watch levels from reports. |
| Deciding agents | Claude Sonnet 5 | 2 agents: the Research Manager and the Portfolio Manager. |
| Opportunities scan | Claude Sonnet 5 | The web research on the Opportunities page. |

Changes apply from the next analysis. Each analysis's log line records the models it used.

### Other controls on the Settings page

- **eToro connection:** the Public Key and Private key, stored on the server only and shown masked (last 4 characters and length). Keys saved here override the Hostinger environment values. The Private key can be longer than Hostinger's 256-character limit, which is why it can be entered here.
- **Reset daily limits:** restarts today's spend, trade and analysis counters from now. Per-holding cooldowns are not affected.

### Current live settings (4 Nov 2026)

Mode **Automatic**, kill switch off, real account, test mode off.

Watchlist:

`IUCS.L, IUHC.L, META, GOOG, MU, VRT, BTC=BTC-USD@crypto, CAT, LLY, CVX.US=CVX, PLTR, IGLN.L, AMD, CEG, QCOM, ETH=ETH-USD@crypto`

With 5 analyses a day and 2 crypto entries, each day has 2 crypto analyses at 09:05 and 3 share analyses at 15:05. Each of the 14 shares is looked at roughly once every 4–5 weekdays, sooner if a watch level is met.

## 4. The daily schedule

All times are UK time (`TZ=Europe/London`).

| Time | Job |
| --- | --- |
| 06:30 daily | **Watch levels check** against the previous day's completed close. |
| 09:05 daily | **Crypto analyses** (`@crypto` entries). |
| 15:05 weekdays | **Share analyses:** met watch levels first, then least recently analysed. |
| Every 5 min | **Queue:** places approved trades whose market has opened, and expires stale approvals (after Approval expiry; approved-but-queued trades after 24 hours). |
| Every 15 min | **Watchlist prices** refreshed from eToro, for open markets only. |
| 21:30 daily | **Daily summary** email. |

You can also run an analysis of any watchlist entry at any time from the Overview page. It counts towards Analyses per day.

## 5. Watch levels (conditions checked overnight)

Reports often say "wait for X" rather than "buy now", for example *"buy on a daily close above 11.015"* or *"buy a pull-back into 10.46–10.68; the idea is broken below 10.17"*. Watch levels make the app act on those conditions.

1. **Recording.** After every analysis that did not trade (Hold, Underweight or Sell), the report is passed to the quick model (Haiku, a fraction of a cent), which extracts only numeric buy conditions:
   - **Breakout:** met on a daily close above the level.
   - **Entry zone (pull-back):** met when the day's low reaches the top of the zone. A single price is treated as a zone of one level.
   - **Cancel level:** a daily close below it cancels all of that share's conditions.
   - **Expiry:** the report's stated time horizon, capped at 30 days, or 14 days by default.

   Numbers more than 40% away from the current price are discarded as misreadings. A new analysis of the same share replaces its earlier conditions. A Buy or Overweight analysis clears them, because it has already acted.
2. **Checking.** At 06:30 each condition is compared with the previous **completed** daily candle from eToro; today's still-forming candle is ignored. The app marks it **Met**, **Cancelled** or **Expired**, and records the close it checked.
3. **Acting.** A met condition moves that share to the **front** of its next scheduled run. The agents are told which condition was met and when, and asked to re-check it with fresh data. **Nothing is bought from a condition alone.** The new analysis's rating and your limits decide.
4. **Where you see them:**
   - the **Watch levels** card on the Overview page: symbol, condition, status, last close, expiry and the report it came from;
   - a **"Watch levels" email** when a condition is met or cancelled;
   - a section in the daily summary.
5. **Backfill.** The **Read conditions from latest reports** button on the card fills in symbols that have no conditions, from their most recent report (up to 7 days old), without running a new analysis. This also ran once automatically when the feature was installed.

This gets around eToro not accepting waiting orders on some instruments (for example IUCS.L), because the app does the watching.

## 6. Dashboard pages

The top menu has **Overview, Opportunities, Scorecard, Logs and Settings**. The layout is a full-width grid on a PC and a single column on a phone. It uses a dark theme and refreshes every 30 seconds while an analysis is running.

### Overview
- **Status strip and warning banners:** the account (REAL or DEMO), test mode, kill switch, and warnings such as missing keys.
- **Portfolio:**
  - the total, cash and a live clock;
  - one row per holding: eToro logo, value, units, current price, gain or loss against cost, share of the portfolio against the "Max in one holding" cap, whether its market is open, 30-day and 60-minute sparklines, and "price as of";
  - sorted by gain against cost, and refreshed more often while markets are open.
- **Watchlist:** every watchlist entry with 1-day change, 60-minute and 30-day sparklines, bid and ask, 52-week range, eToro sentiment and "price as of". Sorted by 1-day change.
- **Watch levels:** see section 5.
- **Waiting for you:** pending approvals with Approve and Reject buttons and their expiry, plus approved trades queued for market open, which can be cancelled.
- **Run an analysis now:** pick a watchlist entry. A **pre-flight check** shows, before any AI cost, whether a Buy or Sell would currently be allowed and which setting to change if not.
- **Test tools** (test mode only): create a test proposal instantly with no AI, to exercise approval, limits and notifications.

### Run page (`/run/<id>`, linked from emails and logs)
- **The report:** the full written decision and rating, with run time and duration.
- **Execute a suggested trade:** place a trade from a pending approval or a Signals-mode signal. Every limit is re-checked.
- **Override a blocked trade:** for this one trade only, waive the soft limits (daily trade count, cooldown, daily spend, position cap, cash buffer). The kill switch, a closed market, Max per trade, the $10 minimum and the cash actually available still apply. The waiver is recorded in the audit trail.
- **Reject** a pending proposal.

### Opportunities
- **The scan:** Claude, using the Opportunities model and live web search (up to 8 searches), proposes 5 trading ideas with a thesis, catalyst, risks and sources.
- **Checking the ideas:** every idea must cite a source that the search actually returned, and each one is checked against eToro (found, tradable, price).
- **Adding to the watchlist:** **Watch** adds an idea to the watchlist in the right format.
- **Limits:** at most **3 scans a day**. The page never places or queues a trade.

### Scorecard: does it work?
- **What it measures:** every finished analysis, from the price at the time of the call to the close **5, 20 and 60 trading days** later. Each result is compared with **VWRL.L** (Vanguard FTSE All-World) over the same dates.
- **What counts as worked:** a Buy or Overweight **worked** if the share then beat the benchmark; an Underweight or Sell worked if it lagged it. Holds are shown but not scored.
- **Tables:**
  - **by rating:** whether Buy calls really beat Holds;
  - **by AI models**, evidence agents / deciding agents: whether Haiku performs as well as Sonnet;
  - **by symbol;**
  - **every call**, linked to its report.

  Each table shows average return, average against the benchmark, and the share of calls that worked.
- **How prices are recorded:** from 4 Nov 2026 each analysis records its price and models at the time. Earlier runs use that day's close, and are treated as all-Sonnet if before 1 Nov 22:44 UTC.
- **Caching and refresh:** prices come from eToro daily candles, cached for 4 hours; **Refresh prices** forces a reload. The daily summary includes a one-line headline.
- **How to read it:** treat fewer than about 20 scored calls as anecdote. The first 5-day results appear a week after the first calls, and 60-day results after about three months.

### Logs
- **Recent calls:** every proposal with rating, trade, status and reason.
- **Agent runs:** each analysis with rating and duration, linking to its report.
- **Log:** the last 40 events.
- **Full audit trail** (`/audit`) and **CSV export** (`/audit.csv`): every proposal event (created, blocked, approved and by whom, rejected, executed with the eToro order ID, failed, expired, cancelled, overridden), including the approver's IP address and whether they used the dashboard or an approval link.

### Settings
Control (mode and kill switch), limits and schedule with on-page validation, AI models, the eToro keys, and Reset daily limits.

## 7. Notifications and the daily summary

Email is sent through Gmail and SMS through Twilio. Both are optional.

| Event | SMS | Email |
| --- | --- | --- |
| Approval needed | ✓ (with link) | ✓ (with link and report) |
| Trade placed, queued, blocked at execution or failed | ✓ | ✓ |
| Every analysis result, including scheduled Holds | | ✓ (subject marked "(scheduled)" for automatic runs) |
| Analysis failed | | ✓ |
| Watch level met or cancelled | | ✓ |
| Daily summary (21:30, every day) | | ✓ |

**The daily summary contains:**
1. **Portfolio performance today:**
   - the total, split into holdings and cash;
   - the change in holdings since the previous day's close;
   - the total against the previous day's saved total (this includes any cash paid in or out);
   - holdings against what they cost.

   It's followed by a colour-coded table per holding with value, today's change in $ and %, cost, and gain or loss against cost in $ and %. Positions bought that day are measured from their purchase price. Cost covers only the units still held.
2. **Analyses today:** time, symbol, rating, whether scheduled or manual, and a link to the report, with the count against your daily limit.
3. **Trades proposed by the app**, with their outcome and the reason for any failure or block.
4. **Bought outside the app today:** positions opened in the eToro app or website.
5. **Watch levels** and their status.
6. **Scorecard headline:** how Buy and Overweight calls have done against VWRL.L so far.
7. **Next share run:** the shares lined up for the next 15:05.
8. **Mode and kill switch.**

Each evening's totals are saved, so the next day can compare against them.

## 8. Safety design

- **Hard limits in code** (`app/policy.py`): kill switch, trades per day, daily spend, Max per trade, cash buffer, position cap, cooldown, the $10 minimum and whether the market is open. The AI is told the rules but cannot change them.
- **Re-check at execution** against the live portfolio. A closed market queues the order rather than placing it.
- **Unleveraged long buys only.** Orders are sent with `leverage: 1` and eToro's instrument ID only (eToro rejects orders that give both symbol and ID). No shorting, leverage, CFDs or copy trades. Sells only close existing unleveraged, non-copy positions.
- **CFD-only US funds** (for example XLV and GLD on UK accounts) should not be on the watchlist. Use UCITS equivalents (IUHC.L, IUCS.L, IUES.L, IGLN.L).
- **Asset-type guard:** an analysis stops if the eToro symbol is a different kind of asset from the watchlist entry.
- **Approval links:** HMAC-signed with `APP_SECRET`, single-use and expiring. Opening a link (GET) only shows the trade; approving needs a button press (POST), so link-preview bots in SMS or email apps can't approve anything.
- **Sells in Automatic mode** need approval unless explicitly allowed.
- **Overrides are per trade, recorded, and never waive the kill switch,** a closed market, Max per trade, the minimum or the available cash.
- **One analysis at a time**, and one execution at a time, so a double-click can't send two orders.
- **One-off settings fixes made at go-live only ever tightened limits:** trades per day ≤ 3, position cap ≤ 25%, cash buffer ≥ $50, auto-sells off. Later edits are respected.

## 9. Watchlist format and eToro symbols

Comma-separated entries:

`EtoroSymbol[=ResearchTicker][@crypto]`

| Example | Meaning |
| --- | --- |
| `META` | Same symbol on eToro and Yahoo Finance. |
| `IUCS.L` | London-listed UCITS fund. |
| `CVX.US=CVX` | Buy eToro `CVX.US` (Chevron); research Yahoo `CVX`. |
| `BTC=BTC-USD@crypto` | Buy eToro `BTC`; research `BTC-USD` with the crypto pipeline, at the crypto run time. |

Removing an entry does not sell it; holdings stay until sold. London (`.L`) instruments trade 08:00–16:30 UK. US shares trade 14:30–21:00 UK, and eToro also offers extended hours with wider spreads.

## 10. AI models and cost

- **The analyses** use Anthropic's API directly (`ANTHROPIC_API_KEY`). OpenRouter was considered: it passes through the same Claude prices but adds a 5.5% fee on credit purchases, so it would not reduce costs.
- **Cost per analysis:** about $0.15–$0.70 with Haiku 5.5 for the 10 evidence agents and Sonnet 5 for the 2 deciders, against $0.50–$3 with Sonnet throughout. Check actual spend in the Anthropic console under Usage, and set a monthly limit there.
- **Watch-level extraction:** one small Haiku call per non-trading analysis.
- **Opportunities scan:** Sonnet with web search, at most 3 a day.
- **Optional data keys:**
  - `FRED_API_KEY`: economic data. Confirmed working: reports quote figures such as the 10-year Treasury yield.
  - `SEC_EDGAR_USER_AGENT`: your name and email, as the SEC asks for. Set it in the Hostinger environment.

## 11. Setup and deployment

1. **eToro:** Settings > Trading > API Key Management > Create New Key, with Environment **Real** (or Demo) and Permissions **Write**. The **Public Key** goes in `ETORO_API_KEY` and the **Private key** in `ETORO_USER_KEY`, or paste both on the Settings page.
2. **Claude:** create a key at console.anthropic.com and set a monthly spend limit.
3. **Twilio:** Account SID, auth token, your Twilio number and your mobile number.
4. **Gmail:** turn on 2-step verification, then create an App Password at myaccount.google.com/apppasswords.
5. **Hostinger:** hPanel > VPS > Docker Manager > project `tradeagent`. Fill in the variables from `.env.example`, starting with `DRY_RUN=true`. The compose file builds straight from `github.com/nicholspr/tradeagent#main` (`pull_policy: build`). **Pushing to GitHub does not redeploy**; redeploy the project (or ask Claude to) and confirm in the logs, which show `started (REAL account[, DRY RUN])` and a `settings:` line on every start.
6. Open `https://nichsoft.io/trader/`, check that the portfolio loads, set your limits, and run one analysis by hand.
7. **Data:** settings, keys, runs, proposals, watch levels, snapshots and the log live in SQLite in the `tradeagent-data` Docker volume (`/data`). They survive redeploys. None of it is in GitHub.

**Deploys restart the app and interrupt a running analysis**, so deploy outside 09:05–09:15 and 15:05–15:20.

## 12. Development

```bash
pip install -r requirements.txt
python -m pytest -q tests          # 60 tests, all offline (fakes for eToro, Claude, email)
DATA_DIR=./data uvicorn app.web:app --reload
```

On Windows, `run-local.bat` runs the full app at `http://localhost:8000/`. It always forces `DRY_RUN=true` and turns the scheduler off, so a local copy can never place orders or duplicate the VPS's scheduled analyses. Settings come from a git-ignored `.env` file.

### Code map

| File | Responsibility |
| --- | --- |
| `app/analyst.py` | Adapts the TradingAgents graph. Builds the portfolio context plus the account rules the agents see. |
| `app/policy.py` | Watchlist parsing, rating → proposal, hard limits, approval rules, and the account-rules text. Pure functions, fully tested. |
| `app/engine.py` | Analysis, proposals, approval, execution, the queue, scheduled runs, pre-flight, overrides, the daily summary, the audit trail and one-off fixes. |
| `app/levels.py` | Watch levels: extraction, the overnight check, queue priority, agent notes and backfill. |
| `app/scorecard.py` | Scorecard: returns after each call at 5, 20 and 60 days against VWRL.L, hit rates by rating, models and symbol. |
| `app/performance.py` | End-of-day performance per holding, daily snapshots, and the summary's text and HTML. |
| `app/etoro.py` | eToro API client: instrument lookup (with fallback), rates, candles, portfolio "book", open and close orders. |
| `app/markets.py` | Read-only market data for the dashboard: sparklines, 52-week range, sentiment, market open or closed, caching. |
| `app/opportunities.py` | Web-search opportunity scan with source and eToro checks. |
| `app/store.py` | SQLite: settings, secrets, runs, proposals, events, scans, levels, snapshots. |
| `app/web.py` | Dashboard pages, approval pages, settings validation, scheduler jobs. |
| `app/notify.py` | Twilio SMS and Gmail email. |
| `app/config.py` | Environment settings and the model choices. |
| `tests/test_all.py` | Policy, eToro client, engine flows, web, watch levels, performance and summary tests. |

### Safety notes for changes

- Treat any change to sizing, approval, execution, account selection or order bodies as safety-sensitive. Add tests, and try it in `DRY_RUN` before going live.
- Do not remove the execution-time re-check, the kill switch, the cash buffer, the daily caps, the position cap, the cooldown, the asset-type guard or the unleveraged-long restriction without a deliberate decision and tests.
- Keep approval-link GETs read-only.
- The v2 instrument-lookup fallback judges whether a market is open from how fresh its last price is. That's a heuristic, so execution must keep handling eToro rejections and market-closed queuing.
- `levels.call_model` and `opportunities.call_model` are the only direct Claude calls outside TradingAgents. Tests replace both.
