"""Minimal eToro Public API client: instruments, portfolio, rates, open and close orders.

Real and demo use the same host; trading and portfolio paths differ (ETORO_ENV).
Only unleveraged long ("real asset") buys and closes of existing positions are used.
"""
from datetime import datetime, timezone
import time
import uuid
from dataclasses import dataclass, field

import requests

from . import config


def _stored(key: str) -> str:
    """Key saved via the dashboard (no length limit), if any."""
    try:
        from . import store
        return store.get_secret(key)
    except Exception:  # noqa: BLE001
        return ""


class EtoroError(RuntimeError):
    pass


@dataclass
class Holding:
    instrument_id: int
    symbol: str
    units: float = 0.0
    value_usd: float = 0.0
    pending_usd: float = 0.0           # queued (not yet filled) buy orders
    positions: list = field(default_factory=list)   # [{positionID, units, value_usd, openRate, conv}]

    @property
    def cost_usd(self) -> float | None:
        """What the open positions cost, in USD (openRate converted at the current rate). None if unknown."""
        total = 0.0
        for p in self.positions:
            if not p.get("openRate"):
                return None
            total += float(p["units"]) * float(p["openRate"]) * float(p.get("conv") or 1)
        return total or None


@dataclass
class Book:
    cash_usd: float
    total_usd: float
    holdings: dict                     # instrument_id -> Holding


class EtoroClient:
    def __init__(self, api_key=None, user_key=None, env=None, base=None, session=None):
        self.api_key = api_key or _stored("etoro_api_key") or config.ETORO_API_KEY
        self.user_key = user_key or _stored("etoro_user_key") or config.ETORO_USER_KEY
        self.env = (env or config.ETORO_ENV).lower()
        self.base = (base or config.ETORO_BASE).rstrip("/")
        self.s = session or requests.Session()
        self._symbols: dict[int, str] = {}

    # ---- plumbing -------------------------------------------------------
    @property
    def demo(self) -> bool:
        return self.env != "real"

    def _headers(self) -> dict:
        if not (self.api_key and self.user_key):
            raise EtoroError("eToro API keys are not configured")
        return {"x-api-key": self.api_key, "x-user-key": self.user_key,
                "x-request-id": str(uuid.uuid4()), "Content-Type": "application/json"}

    def _req(self, method: str, path: str, *, params=None, json=None, headers=None, retries=2):
        url = self.base + path
        for attempt in range(retries + 1):
            h = headers or self._headers()
            r = self.s.request(method, url, params=params, json=json, headers=h, timeout=30)
            if r.status_code == 429 and attempt < retries:
                time.sleep(int(r.headers.get("Retry-After", "10")))
                continue
            if r.status_code >= 400:
                raise EtoroError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
            return r.json() if r.content else {}
        raise EtoroError(f"{method} {path} rate limited")

    # ---- market data ----------------------------------------------------
    def find_instrument(self, symbol: str) -> dict:
        """Exact symbol match, e.g. META, BTC, IUHC.L -> {instrumentId, symbol, tradable, rate, ...}."""
        data = self._req("GET", "/api/v1/market-data/search", params={"internalSymbolFull": symbol})
        for it in data.get("items", []):
            if str(it.get("internalSymbolFull", "")).upper() == symbol.upper():
                iid = int(it["internalInstrumentId"])
                self._symbols[iid] = it["internalSymbolFull"]
                return {"instrumentId": iid, "symbol": it["internalSymbolFull"],
                        "name": it.get("internalInstrumentDisplayName"),
                        "tradable": bool(it.get("isCurrentlyTradable")),
                        "exchangeOpen": bool(it.get("isExchangeOpen")),
                        "buyEnabled": bool(it.get("isBuyEnabled", True)),
                        "rate": it.get("currentRate"),
                        "assetClass": it.get("internalAssetClassName")}
        return self._find_v2(symbol)

    def _find_v2(self, symbol: str) -> dict:
        """Fallback: the v1 search does not index some newer instruments (e.g. LSE UCITS ETFs
        like IUCS.L). Look the symbol up directly, then judge tradability from rate freshness."""
        data = self._req("GET", "/api/v2/market-data/instruments", params={"symbols": symbol})
        for it in data.get("results", []):
            if str(it.get("symbol", "")).upper() != symbol.upper():
                continue
            iid = int(it["instrumentId"])
            self._symbols[iid] = it["symbol"]
            r = self.rates([iid]).get(iid, {})
            fresh = False
            try:
                ts = datetime.strptime(str(r.get("date", ""))[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                fresh = (datetime.now(timezone.utc) - ts).total_seconds() < 15 * 60
            except ValueError:
                pass
            return {"instrumentId": iid, "symbol": it["symbol"], "name": it.get("displayName"),
                    "tradable": fresh, "exchangeOpen": fresh, "buyEnabled": True,
                    "rate": r.get("ask") or r.get("lastExecution"), "assetClass": it.get("type")}
        raise EtoroError(f"instrument {symbol!r} not found on eToro")

    def search_item(self, symbol: str) -> dict | None:
        """Raw v1 search record (daily change, logo, buy/sell sentiment...) for an exact symbol, else None."""
        data = self._req("GET", "/api/v1/market-data/search", params={"internalSymbolFull": symbol})
        for it in data.get("items", []):
            if str(it.get("internalSymbolFull", "")).upper() == symbol.upper():
                return it
        return None

    def daily_candles(self, instrument_id: int, count: int = 365) -> list[dict]:
        """Daily candles, oldest first: [{date, open, high, low, close}]."""
        return self.candles(instrument_id, "OneDay", count)

    def intraday_candles(self, instrument_id: int, minutes: int = 60) -> list[dict]:
        """One-minute candles for the last `minutes` minutes of trading, oldest first."""
        return self.candles(instrument_id, "OneMinute", minutes)

    def candles(self, instrument_id: int, interval: str, count: int) -> list[dict]:
        data = self._req("GET", f"/api/v1/market-data/instruments/{int(instrument_id)}/history/candles/desc/{interval}/{int(count)}")
        out = []
        for block in data.get("candles", []):
            for c in block.get("candles", []):
                try:
                    out.append({"date": c["fromDate"], "open": float(c["open"]), "high": float(c["high"]),
                                "low": float(c["low"]), "close": float(c["close"])})
                except (KeyError, TypeError, ValueError):
                    continue
        return sorted(out, key=lambda c: c["date"])

    def rates(self, instrument_ids: list[int]) -> dict[int, dict]:
        if not instrument_ids:
            return {}
        data = self._req("GET", "/api/v1/market-data/instruments/rates",
                         params={"instrumentIds": ",".join(str(i) for i in sorted(set(instrument_ids)))})
        return {int(r["instrumentID"]): r for r in data.get("rates", [])}

    def symbol_for(self, instrument_id: int) -> str:
        if instrument_id in self._symbols:
            return self._symbols[instrument_id]
        try:
            data = self._req("GET", "/api/v1/market-data/instruments",
                             params={"instrumentIds": str(instrument_id)})
            items = data.get("instrumentDisplayDatas") or data.get("items") or []
            for it in items:
                sym = it.get("symbolFull") or it.get("internalSymbolFull")
                if sym:
                    self._symbols[instrument_id] = sym
                    return sym
        except EtoroError:
            pass
        return f"ID{instrument_id}"

    # ---- portfolio ------------------------------------------------------
    def portfolio_raw(self) -> dict:
        path = "/api/v1/trading/info/demo/portfolio" if self.demo else "/api/v1/trading/info/portfolio"
        return self._req("GET", path).get("clientPortfolio", {})

    def book(self) -> Book:
        p = self.portfolio_raw()
        positions = [x for x in p.get("positions", [])
                     if x.get("isBuy") and int(x.get("leverage", 1)) == 1 and not x.get("mirrorID")]
        pending = [o for o in p.get("ordersForOpen", []) if o.get("isBuy")]
        ids = {int(x["instrumentID"]) for x in positions} | {int(o["instrumentID"]) for o in pending}
        rt = self.rates(list(ids))
        holdings: dict[int, Holding] = {}
        for x in positions:
            iid = int(x["instrumentID"])
            r = rt.get(iid, {})
            px = float(r.get("bid") or x.get("openRate") or 0) * float(r.get("conversionRateBid") or 1)
            val = float(x["units"]) * px
            h = holdings.setdefault(iid, Holding(iid, self.symbol_for(iid)))
            h.units += float(x["units"])
            h.value_usd += val
            h.positions.append({"positionID": int(x["positionID"]), "units": float(x["units"]),
                                "value_usd": round(val, 2), "openRate": x.get("openRate"),
                                "conv": float(r.get("conversionRateBid") or 1),
                                "opened": x.get("openDateTime") or x.get("openTime")})
        frozen = 0.0
        for o in pending:
            iid = int(o["instrumentID"])
            amt = float(o.get("frozenAmount") or o.get("amount") or 0)
            frozen += amt
            holdings.setdefault(iid, Holding(iid, self.symbol_for(iid))).pending_usd += amt
        cash = max(0.0, float(p.get("credit", 0)) - frozen)
        total = cash + sum(h.value_usd + h.pending_usd for h in holdings.values())
        return Book(cash_usd=round(cash, 2), total_usd=round(total, 2), holdings=holdings)

    # ---- trading --------------------------------------------------------
    def open_buy(self, symbol: str, instrument_id: int, amount_usd: float,
                 stop_loss: float | None = None, take_profit: float | None = None) -> dict:
        path = "/api/v2/trading/execution/demo/orders" if self.demo else "/api/v2/trading/execution/orders"
        # eToro rejects open orders that carry both symbol and instrumentId ("exactly one" must be given).
        # The id is unambiguous (eToro 'CVX' is a crypto token, Chevron is 'CVX.US'), so send only that.
        body = {"action": "open", "transaction": "buy", "instrumentId": int(instrument_id),
                "orderType": "mkt", "leverage": 1, "amount": round(float(amount_usd), 2),
                "orderCurrency": "usd"}
        if stop_loss:
            body["stopLossRate"] = round(float(stop_loss), 6)
        if take_profit:
            body["takeProfitRate"] = round(float(take_profit), 6)
        if self.demo:
            body["settlement"] = "real"
        return self._req("POST", path, json=body, retries=0)

    def close_position(self, position_id: int, instrument_id: int, units: float | None = None) -> dict:
        base = "/api/v1/trading/execution/demo" if self.demo else "/api/v1/trading/execution"
        body = {"InstrumentId": instrument_id, "UnitsToDeduct": units}
        return self._req("POST", f"{base}/market-close-orders/positions/{position_id}", json=body, retries=0)

    def order_status(self, order_id: int) -> dict:
        env = "demo" if self.demo else "real"
        return self._req("GET", f"/api/v1/trading/info/{env}/orders/{order_id}")
