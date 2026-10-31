"""Opportunity scan: Claude with live web search proposes ideas; each is checked against eToro.

Informational only. Nothing here places or queues a trade; ideas must go through the normal
analysis -> proposal -> limits -> approval path (via the watchlist) before any order exists.
"""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import config, engine, store
from .etoro import EtoroError

COUNT = 5
MAX_SCANS_PER_DAY = 3        # cost guard: each scan uses web searches plus a long Claude answer
MAX_SEARCHES = 8

SYSTEM = (
    "You are a research assistant for a retail investor who trades long-only, unleveraged positions on eToro "
    "(US shares, ETFs and major crypto). Use web search to find current, well-sourced trading opportunities "
    "from reputable sources such as company filings and press releases, Reuters, Bloomberg, CNBC, FT, "
    "MarketWatch, Seeking Alpha, Yahoo Finance, eToro's own market news and popular-investor commentary, "
    "central-bank and economic-data releases. Prefer ideas with a concrete, recent catalyst. "
    "Never invent facts, prices or URLs: only use what the search results show. This is research, not advice."
)

PROMPT = """Today is {today}.
Find exactly {n} different potential trading opportunities that could be bought on eToro in the next days to weeks.
Mix asset types if justified (shares, ETFs, crypto). Avoid leveraged or inverse products and penny stocks.
{avoid}
Return ONLY a JSON object, no prose before or after, in this shape:
{{"opportunities": [{{
  "symbol": "eToro ticker, e.g. NVDA or BTC",
  "name": "company or asset name",
  "asset_type": "stock | etf | crypto",
  "thesis": "2-3 sentences: why this could be an opportunity now",
  "catalyst": "the specific recent or upcoming event or data point",
  "risks": "main risks, one or two sentences",
  "horizon": "e.g. days, weeks, months",
  "sources": [{{"title": "page title", "url": "https://..."}}]
}}]}}
Every opportunity needs at least one source URL that appeared in your search results."""


def _client():
    import anthropic
    return anthropic.Anthropic(timeout=300.0)


def call_model(prompt: str) -> tuple[str, set[str]]:
    """Ask Claude (with web search). Returns (final text, set of URLs the searches actually returned)."""
    resp = _client().messages.create(
        model=config.TA_QUICK_MODEL, max_tokens=6000, system=SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}],
        messages=[{"role": "user", "content": prompt}])
    text, urls = [], set()
    for b in resp.content:
        t = getattr(b, "type", "")
        if t == "text":
            text.append(b.text)
        elif t == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
            urls.update(r.url for r in b.content if getattr(r, "url", None))
    return "".join(text), urls


def parse(text: str) -> list[dict]:
    """Pull the JSON object out of the model's answer (tolerates code fences and stray prose)."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += (text[i] == "{") - (text[i] == "}")
            if depth == 0:
                try:
                    data = json.loads(text[start:i + 1])
                except ValueError:
                    break
                if isinstance(data, dict) and isinstance(data.get("opportunities"), list):
                    return [o for o in data["opportunities"] if isinstance(o, dict)]
                break
        start = text.find("{", start + 1)
    raise ValueError("model did not return a parsable opportunities list")


def _clean_url(u: str) -> str | None:
    u = (u or "").strip()
    p = urlparse(u)
    return u if p.scheme in ("http", "https") and p.netloc else None


def _norm(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", u.strip().lower()).rstrip("/")


def clean(raw: list[dict], seen_urls: set[str]) -> list[dict]:
    """Keep only well-formed ideas whose sources really came from the search results."""
    seen = {_norm(u) for u in seen_urls}
    out, symbols = [], set()
    for o in raw:
        sym = re.sub(r"[^A-Z0-9.\-]", "", str(o.get("symbol", "")).upper())
        if not sym or sym in symbols:
            continue
        srcs = []
        for s in o.get("sources") or []:
            url = _clean_url(s.get("url", "")) if isinstance(s, dict) else None
            if url and (not seen or _norm(url) in seen):
                srcs.append({"title": str(s.get("title") or urlparse(url).netloc)[:120], "url": url})
        if not srcs:
            continue
        symbols.add(sym)
        out.append({"symbol": sym, "name": str(o.get("name", ""))[:80], "asset_type": str(o.get("asset_type", ""))[:10],
                    "thesis": str(o.get("thesis", ""))[:700], "catalyst": str(o.get("catalyst", ""))[:400],
                    "risks": str(o.get("risks", ""))[:500], "horizon": str(o.get("horizon", ""))[:40],
                    "sources": srcs[:4]})
    return out[:COUNT]


def verify_on_etoro(items: list[dict]) -> list[dict]:
    """Annotate each idea with whether eToro lists it, whether it is open now, and the live rate."""
    try:
        ec = engine.client()
    except Exception as e:  # noqa: BLE001
        store.log("warn", f"opportunity scan: eToro check unavailable: {e}")
        ec = None
    for o in items:
        o["etoro"] = None
        if not ec:
            continue
        try:
            inst = ec.find_instrument(o["symbol"])
            o["etoro"] = {"found": True, "tradable": inst["tradable"], "name": inst.get("name"),
                          "rate": inst.get("rate"), "asset_class": inst.get("assetClass")}
        except EtoroError:
            o["etoro"] = {"found": False}
        except Exception as e:  # noqa: BLE001
            store.log("warn", f"opportunity scan: eToro lookup of {o['symbol']} failed: {e}")
    return items


def stale(scan_row: dict, minutes: int = 15) -> bool:
    """An unfinished scan this old died with its process; do not let it block new scans."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(scan_row["started"])).total_seconds() > minutes * 60
    except (TypeError, ValueError):
        return True


def can_scan() -> str | None:
    """Return a reason the scan must not start now, or None."""
    last = store.latest_scan()
    if last and not last["finished"] and not stale(last):
        return "a scan is already running"
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    if store.scans_since(day) >= MAX_SCANS_PER_DAY:
        return f"daily scan limit reached ({MAX_SCANS_PER_DAY} per day)"
    if not config.env("ANTHROPIC_API_KEY", ""):
        return "ANTHROPIC_API_KEY is not set"
    return None


def scan() -> dict:
    sid = store.start_scan()
    store.log("info", f"opportunity scan #{sid} started")
    try:
        from . import policy
        held = [i.symbol for i in policy.parse_watchlist(store.get_settings()["watchlist"])]
        avoid = f"The investor already watches: {', '.join(held)}. Prefer ideas not on that list." if held else ""
        text, urls = call_model(PROMPT.format(today=datetime.now(timezone.utc).date().isoformat(), n=COUNT, avoid=avoid))
        items = verify_on_etoro(clean(parse(text), urls))
        if not items:
            raise ValueError("no usable opportunities with verifiable sources were returned")
        store.finish_scan(sid, items)
        store.log("info", f"opportunity scan #{sid} finished: {', '.join(i['symbol'] for i in items)}")
        return {"id": sid, "items": items}
    except Exception as e:  # noqa: BLE001
        store.finish_scan(sid, None, str(e)[:500])
        store.log("error", f"opportunity scan #{sid} failed: {e}")
        return {"id": sid, "error": str(e)}
