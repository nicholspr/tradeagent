# TradeAgent

A personal trading agent for an eToro account. The [TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent framework (v0.7.1), running Claude, researches each share on your watchlist. A team of analyst agents and a bull-versus-bear debate produce a 5-tier rating: Buy, Overweight, Hold, Underweight or Sell. This app turns that rating into a sized eToro order. Fixed safety limits that the AI cannot change are applied before anything is placed.

It runs 24x7 on the VPS behind Traefik at `https://nichsoft.io/trader/`, using the site's existing login.

## Modes (switch any time on the dashboard)

| Mode | What happens |
| --- | --- |
| **Signals** | The agents' calls are sent to you by SMS and email. Nothing is ever traded. |
| **Approval** (default) | Each proposed trade is sent to you with a link. Nothing is placed until you tap Approve. |
| **Automatic** | Buys are placed automatically within the limits. Sells still need approval unless "Auto mode may sell" is on. |

The **kill switch** stops all new orders immediately in every mode.

## Safety limits (enforced in code, editable on the dashboard)

- Max per trade (default $25), max buys per day (default $50), max trades per day (default 2)
- Cash buffer kept back (default $5); eToro minimum order is $10
- Max share of portfolio in one holding after a buy (default 40%)
- Cooldown between trades in the same holding (default 3 days)
- Buys are unleveraged long positions only (real shares or crypto). No shorting or leverage.
- Sells close only existing unleveraged, non-copy positions. Underweight halves the largest position; Sell closes all positions in that holding.
- Approval links expire after a set time (default 6 hours). Opening a link only shows the trade; approving needs a button press, so link previews in SMS or email can't trigger a trade.
- Every proposal is re-checked against the live portfolio and limits at the moment of execution.

## How it runs

- On weekdays at 15:05 UK time (US market open), it analyses the next *N* watchlist shares, least recently analysed first. *N* is "Analyses per day", default 2, which keeps Claude costs down.
- Crypto (`@crypto` entries) is analysed daily at 09:05.
- Every 5 minutes, approved trades waiting for their market to open are placed, and stale approvals expire.
- A daily summary email is sent at 21:30 on weekdays.
- You can also trigger an analysis of any watchlist share from the dashboard.

Watchlist format: `META, GOOG, IUHC.L, BTC=BTC-USD@crypto`. The first part is the eToro symbol. After `=` comes the Yahoo Finance ticker the agents use, if it's different. `@crypto` selects the crypto pipeline.

**Cost:** each analysis makes many Claude calls, roughly $0.50–$3 with Sonnet. Two per weekday comes to about $20–$130 a month. Keep this in proportion to the size of the account.

## Setup

1. **eToro key:** in eToro go to Settings > Trading > API Key Management > Create New Key. Choose Environment **Real** (or Demo) and Permissions **Write**. eToro shows a public API key and a user key.
2. **Claude key:** create one at console.anthropic.com and set a monthly spend limit there.
3. **Twilio:** you need your Account SID, auth token, Twilio phone number, and your mobile number.
4. **Gmail:** turn on 2-step verification, then create an App Password at myaccount.google.com/apppasswords.
5. **Hostinger:** in hPanel > VPS > Docker Manager, open the `tradeagent` project and fill in the environment values listed in `.env.example`. Keep `DRY_RUN=true` for the first day if you want to watch it without placing orders. Then redeploy.
6. Open `https://nichsoft.io/trader/`, check the portfolio loads, set your limits, and run one analysis manually.

## Development

```bash
pip install -r requirements.txt
python -m pytest -q tests
DATA_DIR=./data uvicorn app.web:app --reload
```

### Project context

This is an existing personal trading application, not a broker simulator. TradingAgents researches a watchlist item and returns a rating and written decision; this app maps the rating to a proposal and, depending on the selected mode, sends a signal, waits for approval, or attempts an eToro order. Treat all changes to proposal sizing, approval, execution, or account selection as safety-sensitive.

The main code paths are:

- `app/analyst.py` adapts the TradingAgents graph and passes in the portfolio context.
- `app/policy.py` parses the watchlist, maps ratings to proposals, applies the hard trading limits, and decides when approval is required. Its functions are pure and covered by tests.
- `app/engine.py` orchestrates analysis, proposal creation, approval, execution, scheduling, and audit events. It re-checks limits against the current portfolio before execution.
- `app/etoro.py` implements the eToro API calls and builds a portfolio view from eligible positions and pending orders.
- `app/store.py` persists settings, secrets, runs, proposals, and events in SQLite under `DATA_DIR`.
- `app/web.py` serves the dashboard, settings, approval pages, and scheduled jobs.
- `app/notify.py` sends optional SMS and email alerts.
- `tests/test_all.py` exercises policy, eToro client behavior, and application flows using fakes.

### Development and safety notes

- The dashboard's `Approval` mode is the default. `Automatic` may place buys without approval; sells still require approval unless `auto_sells` is enabled. `Signals` never places orders.
- `DRY_RUN=true` is required for the dashboard's AI-free test-trade tool. It records a simulated execution but does not send an order to eToro. Confirm the environment and mode before testing any trade workflow.
- Do not remove the execution-time limit recheck, kill-switch check, cash buffer, daily caps, position cap, cooldown, or the restrictions on leveraged/short positions without an explicit product decision and corresponding tests.
- Approval-link GET requests are deliberately read-only. Only the explicit approve/reject POST action decides a proposal; preserve this behavior to prevent link-preview bots from approving trades.
- The eToro v2 instrument-search fallback uses a recent rate as a tradability heuristic when the ordinary search result is missing. A fresh quote is not a definitive exchange-hours or order-acceptance guarantee; execution must continue to handle eToro rejection and queued market-closed orders.
- The Claude project handoff reported adding FRED configuration, but did not conclusively verify that an analysis actually used FRED data. Check a concrete analysis report or upstream logs before treating that integration as confirmed.
- The application has been developed for deployment behind Traefik at `/trader/`, with shared site authentication on dashboard routes and separate token-protected approval links. Local behavior and live VPS configuration can differ; verify deployment settings independently rather than assuming a code change is live.

Before deploying a change, run `python -m pytest -q tests`. For deployment, use `DRY_RUN=true` first and verify the resulting proposal, audit trail, notification, and account environment. Never assume that pushing to GitHub redeploys the VPS; redeploy separately and confirm the running version.

*This is a research tool, not financial advice. AI trading decisions can lose money, and past results do not predict future ones.*
