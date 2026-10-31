"""Read-only market overview of the watchlist from eToro: price, daily change, 52-week range, sentiment.

Display only. Nothing here can place or change an order.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

QUOTE_TTL = 60          # seconds: the overview page can refresh often without hammering eToro
HISTORY_TTL = 3600      # daily candles only change once a day
SPARK_DAYS = 30

_lock = threading.Lock()
_cache: dict = {}


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def range_position(price: float, low: float, high: float) -> float:
    """0-100: where the price sits between the 52-week low and high."""
    if high <= low:
        return 50.0
    return max(0.0, min(100.0, (price - low) / (high - low) * 100))


def sentiment(item: dict | None) -> tuple[str, float] | None:
    """('Buying'|'Selling', percent) from eToro's share of investors holding long vs short."""
    if not item or item.get("buyHoldingPct") is None:
        return None
    buy = float(item["buyHoldingPct"])
    sell = float(item.get("sellHoldingPct") if item.get("sellHoldingPct") is not None else 100 - buy)
    return ("Buying", buy) if buy >= sell else ("Selling", sell)


def build_row(symbol: str, item: dict | None, candles: list[dict], rate: dict | None) -> dict:
    """Combine eToro search record, daily candles and live rate into one display row (pure)."""
    rate = rate or {}
    item = item or {}
    last_close = candles[-1]["close"] if candles else None
    price = rate.get("bid") or item.get("currentRate") or last_close
    row = {"symbol": symbol, "name": item.get("internalInstrumentDisplayName") or symbol,
           "logo": item.get("logo50x50") or item.get("logo35x35"),
           "asset_class": item.get("internalAssetClassName"),
           "bid": rate.get("bid") or price, "ask": rate.get("ask") or price, "price": price,
           "tradable": item.get("isCurrentlyTradable"), "sentiment": sentiment(item)}
    pct = item.get("dailyPriceChange")
    if pct is None and len(candles) >= 2 and candles[-2]["close"]:
        pct = (candles[-1]["close"] / candles[-2]["close"] - 1) * 100
    row["change_pct"] = None if pct is None else float(pct)
    if row["change_pct"] is not None and price:
        prev = float(price) / (1 + row["change_pct"] / 100) if row["change_pct"] > -100 else 0
        row["change_abs"] = float(price) - prev
    else:
        row["change_abs"] = None
    year = candles[-365:]
    if year and price:
        low, high = min(min(c["low"] for c in year), float(price)), max(max(c["high"] for c in year), float(price))
        row.update(low52=low, high52=high, range_pos=range_position(float(price), low, high))
    else:
        row.update(low52=None, high52=None, range_pos=None)
    row["spark"] = [c["close"] for c in candles[-SPARK_DAYS:]]
    return row


def _fetch_one(ec, symbol: str):
    item = ec.search_item(symbol)
    if item:
        iid = int(item["internalInstrumentId"])
    else:
        inst = ec.find_instrument(symbol)          # v2 fallback for instruments the v1 search does not index
        iid = inst["instrumentId"]
        item = {"internalInstrumentDisplayName": inst.get("name"), "currentRate": inst.get("rate"),
                "isCurrentlyTradable": inst.get("tradable"), "internalAssetClassName": inst.get("assetClass")}
    candles = _cached(("candles", iid), HISTORY_TTL, lambda: ec.daily_candles(iid, 365))
    return iid, item, candles


def watchlist_rows(ec, symbols: list[str]) -> list[dict]:
    """One row per symbol. A symbol that fails shows an error row instead of breaking the page."""
    def one(sym):
        def load():
            try:
                return _fetch_one(ec, sym)
            except Exception as e:  # noqa: BLE001
                return e
        return _cached(("quote", sym), QUOTE_TTL, load)

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(symbols)))) as pool:
        fetched = list(pool.map(one, symbols))
    ids = [f[0] for f in fetched if not isinstance(f, Exception)]
    try:
        rates = _cached(("rates", tuple(sorted(ids))), QUOTE_TTL, lambda: ec.rates(ids)) if ids else {}
    except Exception:  # noqa: BLE001
        rates = {}
    rows = []
    for sym, f in zip(symbols, fetched):
        if isinstance(f, Exception):
            rows.append({"symbol": sym, "error": str(f)[:160]})
            continue
        iid, item, candles = f
        rows.append(build_row(sym, item, candles, rates.get(iid)))
    return rows
