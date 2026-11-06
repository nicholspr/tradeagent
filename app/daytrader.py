"""Day trader test page data: instrument search and live 30-second candles.

eToro's smallest candle is one minute, so 30-second candles are built here by sampling the
live bid every few seconds while someone has the chart open. The chart starts from the last
30 one-minute candles from eToro, and live 30-second candles take over as they form.
Sampling stops on its own a couple of minutes after the page is closed.
"""
import threading
import time
from datetime import datetime, timezone

from . import engine, store

BUCKET = 30            # seconds per live candle
WINDOW = 30 * 60       # seconds shown on the chart
SAMPLE_EVERY = 5       # seconds between live price samples
IDLE_AFTER = 120       # stop sampling an instrument nobody has asked about for this long
STALE_AFTER = 120      # a quote older than this means the market is closed

_lock = threading.Lock()
_watch: dict[int, dict] = {}     # iid -> {"seen": ts, "buckets": {start: [o,h,l,c]}, "quote": {...}}
_cache: dict = {}
_thread: threading.Thread | None = None


def _cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def search(q: str, limit: int = 12) -> list[dict]:
    """Free-text eToro instrument search by name or symbol."""
    q = (q or "").strip()[:100]
    if not q:
        return []

    def fetch():
        data = engine.client()._req("GET", "/api/v2/market-data/instruments/search",
                                    params={"query": q, "limit": limit})
        return [{"instrumentId": int(r["instrumentId"]), "symbol": r.get("symbol", ""),
                 "name": r.get("displayName", ""), "type": r.get("type", ""),
                 "logo": (r.get("image") or {}).get("uri")}
                for r in data.get("results", []) if r.get("instrumentId")]
    return _cached(("search", q.lower(), limit), 300, fetch)


def _parse_ts(s: str) -> float | None:
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def add_sample(w: dict, ts: float, price: float) -> None:
    """Fold one price sample into its 30-second bucket and drop buckets older than the window."""
    start = int(ts // BUCKET * BUCKET)
    b = w["buckets"].get(start)
    if b is None:
        w["buckets"][start] = [price, price, price, price]
    else:
        b[1] = max(b[1], price)
        b[2] = min(b[2], price)
        b[3] = price
    cutoff = ts - WINDOW - BUCKET
    for k in [k for k in w["buckets"] if k < cutoff]:
        del w["buckets"][k]


def _sample_once() -> None:
    now = time.time()
    with _lock:
        for iid in [i for i, w in _watch.items() if now - w["seen"] > IDLE_AFTER]:
            del _watch[iid]
        ids = list(_watch)
    if not ids:
        return
    rates = engine.client().rates(ids)
    with _lock:
        for iid in ids:
            r, w = rates.get(iid), _watch.get(iid)
            if not r or w is None or r.get("bid") is None:
                continue
            qts = _parse_ts(r.get("date", "")) or now
            w["quote"] = {"bid": float(r["bid"]), "ask": float(r.get("ask") or r["bid"]), "ts": qts}
            if now - qts <= STALE_AFTER:          # only live quotes make candles
                add_sample(w, now, float(r["bid"]))


def _loop() -> None:
    global _thread
    last_warn = 0.0
    while True:
        try:
            _sample_once()
        except Exception as e:  # noqa: BLE001
            if time.time() - last_warn > 300:      # at most one log line every 5 minutes
                last_warn = time.time()
                store.log("warn", f"day trader price sampling failed: {str(e)[:200]}")
        time.sleep(SAMPLE_EVERY)
        with _lock:
            if not _watch:
                _thread = None
                return


def watch(iid: int) -> None:
    """Mark an instrument as being viewed and make sure the sampler is running."""
    global _thread
    with _lock:
        w = _watch.setdefault(iid, {"buckets": {}, "quote": None})
        w["seen"] = time.time()
        if _thread is None:
            _thread = threading.Thread(target=_loop, name="daytrader-sampler", daemon=True)
            _thread.start()


def merge(history: list[dict], buckets: dict, end: float) -> list[dict]:
    """One-minute history up to where live 30-second candles begin, then the live candles,
    trimmed to the 30 minutes ending at `end`. Each candle: t (epoch s), d (seconds), o h l c."""
    live = [{"t": k, "d": BUCKET, "o": b[0], "h": b[1], "l": b[2], "c": b[3]} for k, b in sorted(buckets.items())]
    first_live = live[0]["t"] if live else float("inf")
    hist = []
    for c in history:
        t = _parse_ts(c["date"])
        if t is not None and t + 60 <= first_live:
            hist.append({"t": t, "d": 60, "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]})
    start = end - WINDOW
    return [c for c in hist + live if c["t"] + c["d"] > start and c["t"] <= end]


KINDS = {5: "stock", 6: "etf", 10: "crypto"}     # eToro instrumentTypeID -> fee rules on the page


def _name(iid: int) -> tuple[str, str, str]:
    """Symbol, display name and kind (stock, etf, crypto or other) for an instrument id."""
    try:
        data = engine.client()._req("GET", "/api/v1/market-data/instruments", params={"instrumentIds": str(iid)})
        for it in data.get("instrumentDisplayDatas") or data.get("items") or []:
            return (it.get("symbolFull") or it.get("internalSymbolFull") or f"#{iid}",
                    it.get("instrumentDisplayName") or it.get("internalInstrumentDisplayName") or "",
                    KINDS.get(it.get("instrumentTypeID"), "other"))
    except Exception:  # noqa: BLE001
        pass
    return (f"#{iid}", "", "other")


def quote(iid: int) -> dict:
    """Latest bid/ask for the paper trade panel: the sampler's last price (no extra eToro call),
    or a fresh rate if the sampler has none yet."""
    watch(iid)
    with _lock:
        q = dict(_watch[iid]["quote"]) if _watch.get(iid, {}).get("quote") else None
    if q is None:
        r = engine.client().rates([iid]).get(iid)
        if not r or r.get("bid") is None:
            raise ValueError("no price available")
        q = {"bid": float(r["bid"]), "ask": float(r.get("ask") or r["bid"]),
             "ts": _parse_ts(r.get("date", "")) or time.time()}
    q["open"] = time.time() - q["ts"] <= STALE_AFTER
    q["instrumentId"] = iid
    return q


def chart(iid: int) -> dict:
    """Candles for the chart, plus the latest quote and whether the market looks open."""
    watch(iid)
    ec = engine.client()
    history = _cached(("hist", iid), 25, lambda: ec.candles(iid, "OneMinute", 40))
    with _lock:
        w = _watch.get(iid) or {"buckets": {}, "quote": None}
        buckets = {k: list(v) for k, v in w["buckets"].items()}
        quote = dict(w["quote"]) if w["quote"] else None
    if quote is None:                       # first view: quote straight away rather than wait for the sampler
        r = ec.rates([iid]).get(iid)
        if r and r.get("bid") is not None:
            quote = {"bid": float(r["bid"]), "ask": float(r.get("ask") or r["bid"]),
                     "ts": _parse_ts(r.get("date", "")) or time.time()}
    now = time.time()
    is_open = bool(quote and now - quote["ts"] <= STALE_AFTER)
    last_hist = _parse_ts(history[-1]["date"]) + 60 if history else now
    end = now if is_open else max(last_hist, max(buckets) + BUCKET if buckets else 0)
    name = _cached(("name", iid), 3600, lambda: _name(iid))
    return {"instrumentId": iid, "symbol": name[0], "name": name[1], "kind": name[2], "open": is_open, "quote": quote, "end": end, "window": WINDOW,
            "bucket": BUCKET, "candles": merge(history, buckets, end), "serverTime": now}
