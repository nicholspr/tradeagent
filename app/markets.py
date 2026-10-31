"""Read-only market overview of the watchlist from eToro: price, daily change, 52-week range, sentiment.

Display only. Nothing here can place or change an order.
"""
import threading
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

REFRESH_MINUTES = 15
QUOTE_TTL = REFRESH_MINUTES * 60   # quotes are re-read from eToro this often; page loads in between use the cache
HISTORY_TTL = 3600      # daily candles only change once a day
SPARK_DAYS = 30

_lock = threading.Lock()
_cache: dict = {}
_snap: dict = {}
_updated: float | None = None      # when quotes were last read from eToro


def updated_at() -> float | None:
    return _updated


# Regular trading sessions by listing suffix: (timezone, open, close). No suffix = US listing.
# Lunch breaks and exchange holidays are not modelled; a holiday just costs a harmless refetch.
_SESSIONS = {
    "": ("America/New_York", dtime(9, 30), dtime(16, 0)),
    "L": ("Europe/London", dtime(8, 0), dtime(16, 30)),
    "DE": ("Europe/Berlin", dtime(9, 0), dtime(17, 30)),
    "PA": ("Europe/Paris", dtime(9, 0), dtime(17, 30)),
    "AS": ("Europe/Amsterdam", dtime(9, 0), dtime(17, 30)),
    "MC": ("Europe/Madrid", dtime(9, 0), dtime(17, 30)),
    "MI": ("Europe/Rome", dtime(9, 0), dtime(17, 30)),
    "ZU": ("Europe/Zurich", dtime(9, 0), dtime(17, 30)),
    "ST": ("Europe/Stockholm", dtime(9, 0), dtime(17, 30)),
    "CO": ("Europe/Copenhagen", dtime(9, 0), dtime(17, 0)),
    "HE": ("Europe/Helsinki", dtime(10, 0), dtime(18, 30)),
    "OL": ("Europe/Oslo", dtime(9, 0), dtime(16, 20)),
    "HK": ("Asia/Hong_Kong", dtime(9, 30), dtime(16, 0)),
    "TO": ("America/Toronto", dtime(9, 30), dtime(16, 0)),
}
_ALWAYS = ("crypto",)
_FX_LIKE = ("currenc", "commodit", "indic")   # trade around the clock on weekdays


def session(symbol: str, asset_class: str | None, now: datetime | None = None) -> tuple[bool, datetime | None]:
    """(is_open, most_recent_close). Unknown markets count as open so they are never skipped by mistake."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cls = (asset_class or "").lower()
    if cls.startswith(_ALWAYS):
        return True, None
    if cls.startswith(_FX_LIKE):                       # Sun 22:00 UTC to Fri 22:00 UTC
        wd, t = now.weekday(), now.time()
        closed = wd == 5 or (wd == 4 and t >= dtime(22)) or (wd == 6 and t < dtime(22))
        if not closed:
            return True, None
        days_back = (wd - 4) % 7
        return False, datetime.combine(now.date() - timedelta(days=days_back), dtime(22), tzinfo=timezone.utc)
    suffix = symbol.rsplit(".", 1)[1].upper() if "." in symbol else ""
    if suffix not in _SESSIONS:
        return True, None
    tz_name, opens, closes = _SESSIONS[suffix]
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    if local.weekday() < 5 and opens <= local.time() < closes:
        return True, None
    for i in range(8):
        day = local.date() - timedelta(days=i)
        close = datetime.combine(day, closes, tzinfo=tz)
        if day.weekday() < 5 and close <= now:
            return False, close
    return False, None


def _settled(sym: str, entry) -> bool:
    """True when the market is closed and the cached data was read after the last close: nothing can have changed."""
    ts, val = entry
    if isinstance(val, Exception):
        return False
    is_open, last_close = session(sym, val[1].get("internalAssetClassName"), datetime.fromtimestamp(time.time(), timezone.utc))
    return not is_open and last_close is not None and ts >= last_close.timestamp()


def invalidate(open_only: bool = False) -> None:
    """Drop cached quotes and rates (not the daily history) so the next read goes to eToro.

    open_only keeps symbols whose market is closed and already captured after its close.
    """
    with _lock:
        for k in [k for k in _cache if k[0] in ("quote", "rates")]:
            if open_only and k[0] == "quote" and _settled(k[1], _cache[k]):
                continue
            del _cache[k]
        if not open_only:
            _snap.clear()


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    if not isinstance(val, Exception):   # never keep a failure for the whole TTL
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


def holding_sparks(ec, instrument_ids: list[int]) -> dict[int, list[float]]:
    """30-day closes per instrument for the portfolio card. A failure just means no sparkline for that holding."""
    def one(iid):
        try:
            candles = _cached(("candles", iid), HISTORY_TTL, lambda: ec.daily_candles(iid, 365))
            return iid, [c["close"] for c in candles[-SPARK_DAYS:]]
        except Exception:  # noqa: BLE001
            return iid, []
    if not instrument_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(6, len(instrument_ids))) as pool:
        return dict(pool.map(one, instrument_ids))


def holding_intraday(ec, instrument_ids: list[int], minutes: int = 60) -> dict[int, list[float]]:
    """Last `minutes` one-minute closes per instrument (for a closed market: its final hour of trading).
    A failure just means no sparkline for that holding."""
    def one(iid):
        try:
            return iid, [c["close"] for c in ec.intraday_candles(iid, minutes)]
        except Exception:  # noqa: BLE001
            return iid, []
    if not instrument_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(6, len(instrument_ids))) as pool:
        return dict(pool.map(one, instrument_ids))


def _quote(ec, sym: str):
    """Cached (instrument_id, search item, candles) for a symbol, or the Exception if it failed."""
    def load():
        global _updated
        try:
            res = _fetch_one(ec, sym)
            _updated = time.time()
            return res
        except Exception as e:  # noqa: BLE001
            return e
    with _lock:
        hit = _cache.get(("quote", sym))
    if hit and _settled(sym, hit):
        return hit[1]
    return _cached(("quote", sym), QUOTE_TTL, load)


def market_info(ec, symbols: list[str], now: datetime | None = None) -> dict[str, dict]:
    """symbol -> {open: True/False/None, logo, name} for the portfolio card."""
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
        got = list(pool.map(lambda s: _quote(ec, s), symbols))
    out: dict[str, dict] = {}
    for sym, f in zip(symbols, got):
        if isinstance(f, Exception):
            out[sym] = {"open": None, "logo": None, "name": None}
            continue
        item = f[1]
        out[sym] = {"open": session(sym, item.get("internalAssetClassName"), now)[0],
                    "logo": item.get("logo50x50") or item.get("logo35x35"),
                    "name": item.get("internalInstrumentDisplayName")}
    return out


def market_states(ec, symbols: list[str], now: datetime | None = None) -> dict[str, bool | None]:
    """symbol -> True open / False closed / None unknown."""
    return {s: v["open"] for s, v in market_info(ec, symbols, now).items()}


# Measured against eToro: BTC's bid/ask moves every few seconds while its market is open, and the rate
# timestamp is just the request time. So eToro is effectively live; these TTLs are what we choose.
PORTFOLIO_OPEN_SECONDS = 60          # at least one holding's market is open
PORTFOLIO_CLOSED_SECONDS = 900       # everything held is closed: nothing moves, only cash/trades can change


def snapshot_ttl(any_open: bool) -> int:
    return PORTFOLIO_OPEN_SECONDS if any_open else PORTFOLIO_CLOSED_SECONDS


def portfolio_snapshot(ec, now: float | None = None) -> dict:
    """Cached {book, sparks, info, fetched, any_open}. Re-read from eToro when older than the TTL for its state."""
    now = time.time() if now is None else now
    with _lock:
        snap = _snap.get("v")
    if snap and now - snap["fetched"] < snapshot_ttl(snap["any_open"]):
        return snap
    book = ec.book()
    live = [h for h in book.holdings.values() if h.units > 0]
    info = market_info(ec, [h.symbol for h in live], datetime.fromtimestamp(now, timezone.utc))
    ids = [h.instrument_id for h in live]
    snap = {"book": book, "info": info, "fetched": now,
            "sparks": holding_sparks(ec, ids), "intraday": holding_intraday(ec, ids),
            "any_open": any(v["open"] is not False for v in info.values()) if info else False}
    with _lock:
        _snap["v"] = snap
    return snap


def invalidate_portfolio() -> None:
    with _lock:
        _snap.clear()


def watchlist_rows(ec, symbols: list[str]) -> list[dict]:
    """One row per symbol. A symbol that fails shows an error row instead of breaking the page."""
    def one(sym):
        return _quote(ec, sym)

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
        row = build_row(sym, item, candles, rates.get(iid))
        with _lock:
            hit = _cache.get(("quote", sym))
        row["fetched"] = hit[0] if hit else None       # when eToro was last read for this symbol
        rows.append(row)
    return rows
