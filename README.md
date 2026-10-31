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

Code layout: `app/policy.py` holds the limits (pure, unit-tested). `app/engine.py` handles analyse, propose, approve and execute. `app/etoro.py` is the eToro API client. `app/analyst.py` is the TradingAgents wrapper. `app/web.py` is the dashboard, approval pages and scheduler.

*This is a research tool, not financial advice. AI trading decisions can lose money, and past results do not predict future ones.*
