"""Holding period per watchlist symbol, and stop-loss / take-profit levels for short-term ideas.

A symbol can be tagged "short" (days to a few weeks), for example when it is added from a short-term opportunity
scan. Its analyses are then told to judge the idea over that horizon and to give a stop-loss (below the price) and a
take-profit (above it). When a short-term Buy or Overweight is placed, those levels are attached to the eToro order,
so eToro itself closes the position if either price is reached, even if this app is offline.
"""
import json
import re
from datetime import datetime, timezone

from . import config, store

SHORT = "short"
# Sanity bounds relative to the price at the time of the call: a stop must sit 1-25% below it, a take-profit 1-50% above.
STOP_MIN, STOP_MAX = 0.01, 0.25
TP_MIN, TP_MAX = 0.01, 0.50


def _meta() -> dict:
    try:
        return json.loads(store.get_settings().get("watch_meta") or "{}")
    except ValueError:
        return {}


def term(symbol: str) -> str:
    return (_meta().get(symbol.upper()) or {}).get("term") or "medium"


def set_term(symbol: str, value: str, source: str = "") -> None:
    meta = _meta()
    sym = symbol.upper()
    if value == SHORT:
        meta[sym] = {"term": SHORT, "since": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source}
    else:
        meta.pop(sym, None)
    store.set_setting("watch_meta", json.dumps(meta, sort_keys=True))


def forget(symbol: str) -> None:
    set_term(symbol, "medium")


def agent_note(symbol: str) -> str:
    """Extra instructions for the deciding agents when the symbol is a short-term idea."""
    if term(symbol) != SHORT:
        return ""
    return ("Holding period for this instrument: SHORT TERM, a few days to about four weeks. Judge the rating on "
            "that horizon, not on the long-term story: a Buy or Overweight should mean the setup is likely to pay off "
            "within weeks and clearly more than the spread. For any Buy or Overweight, state one stop-loss price below "
            "the current price and one take-profit price above it, written as 'Stop-loss: <price>' and "
            "'Take-profit: <price>'. They will be attached to the eToro order, and eToro will close the position "
            "automatically at either price.")


SYSTEM = "You extract numbers from a trading analysis. Reply with JSON only. Never invent a number."
PROMPT = """From this analysis of {ticker}, give the recommended stop-loss price (below the current price) and
take-profit price (above it), if the text states them. Current price: {price}
Return {{"stop_loss": number or null, "take_profit": number or null}}.

Analysis:
{text}"""


def call_model(prompt: str) -> str:
    import anthropic
    resp = anthropic.Anthropic(timeout=120.0).messages.create(
        model=config.model_for("quick"), max_tokens=200, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in resp.content)


def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def stops_from(ticker: str, decision: str, price: float | None) -> tuple[float | None, float | None, str]:
    """(stop_loss, take_profit, note). Levels outside the sanity bounds are dropped, with the reason in note."""
    if not price:
        return None, None, "no current price to check levels against"
    m_sl = re.search(r"stop[- ]?loss[^0-9$]{0,20}\$?([0-9][0-9,]*\.?[0-9]*)", decision or "", re.I)
    m_tp = re.search(r"take[- ]?profit[^0-9$]{0,20}\$?([0-9][0-9,]*\.?[0-9]*)", decision or "", re.I)
    sl = _num(m_sl.group(1).replace(",", "")) if m_sl else None
    tp = _num(m_tp.group(1).replace(",", "")) if m_tp else None
    if sl is None or tp is None:
        try:
            text = call_model(PROMPT.format(ticker=ticker, price=price, text=(decision or "")[:12000]))
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0)) if m else {}
        except Exception:  # noqa: BLE001
            data = {}
        sl = sl if sl is not None else _num(data.get("stop_loss"))
        tp = tp if tp is not None else _num(data.get("take_profit"))
    notes = []
    if sl is not None and not (price * (1 - STOP_MAX) <= sl <= price * (1 - STOP_MIN)):
        notes.append(f"stop-loss {sl:g} ignored (must be 1-25% below {price:g})")
        sl = None
    if tp is not None and not (price * (1 + TP_MIN) <= tp <= price * (1 + TP_MAX)):
        notes.append(f"take-profit {tp:g} ignored (must be 1-50% above {price:g})")
        tp = None
    if sl is None and not notes:
        notes.append("no stop-loss given")
    return sl, tp, "; ".join(notes)
