"""SQLite storage: settings, analysis runs, trade proposals and an event log."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

DEFAULTS = {
    "mode": "approval",              # signals | approval | auto
    "kill_switch": "off",            # on = no new orders of any kind
    "watchlist": "META,GOOG,MU,UPB,VRT,BTC=BTC-USD@crypto",
    "analyses_per_day": "2",         # LLM cost control: tickers analysed per scheduled day
    "schedule_time": "15:05",        # UK time, weekdays (US market open 14:30 UK)
    "crypto_time": "09:05",          # UK time, daily, for @crypto entries
    "max_trade_usd": "25",
    "max_daily_usd": "50",
    "max_trades_per_day": "2",
    "cash_buffer_usd": "5",
    "max_position_pct": "40",        # max % of portfolio in one instrument after a buy
    "cooldown_days": "3",            # min days between trades in the same instrument
    "allow_sells": "on",             # agent may propose closing positions
    "auto_sells": "off",             # in auto mode, sells still need approval unless on
    "approval_ttl_hours": "6",
}

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> str:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    return os.path.join(config.DATA_DIR, "tradebot.sqlite3")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, started TEXT, finished TEXT,
                rating TEXT, decision TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ticker TEXT, instrument_id INTEGER,
                action TEXT, amount_usd REAL, units REAL, position_ids TEXT, rating TEXT, reason TEXT,
                status TEXT, mode TEXT, created TEXT, decided TEXT, executed TEXT, order_ref TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS secrets (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT);
            """
        )
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def get_settings() -> dict:
    with _lock, connect() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULTS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


def set_setting(key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    with _lock, connect() as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(value).strip()))
    log("info", f"setting {key} = {value}")


def log(level: str, msg: str) -> None:
    print(f"[tradeagent] {level.upper()} {msg[:500]}", flush=True)   # visible in docker logs
    with _lock, connect() as c:
        c.execute("INSERT INTO events(ts, level, msg) VALUES (?, ?, ?)", (_now(), level, msg[:2000]))


def events(limit: int = 50, level: str | None = None) -> list:
    q, args = "SELECT * FROM events", []
    if level:
        q += " WHERE level=?"
        args.append(level)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock, connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def start_run(ticker: str) -> int:
    with _lock, connect() as c:
        cur = c.execute("INSERT INTO runs(ticker, started) VALUES (?, ?)", (ticker, _now()))
        return cur.lastrowid


def finish_run(run_id: int, rating: str | None, decision: str | None, error: str | None = None) -> None:
    with _lock, connect() as c:
        c.execute("UPDATE runs SET finished=?, rating=?, decision=?, error=? WHERE id=?",
                  (_now(), rating, (decision or "")[:40000], error, run_id))


def runs(limit: int = 30) -> list:
    with _lock, connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def proposals_for_run(run_id: int) -> list:
    with _lock, connect() as c:
        out = [dict(r) for r in c.execute("SELECT * FROM proposals WHERE run_id=? ORDER BY id", (run_id,))]
    for d in out:
        d["position_ids"] = json.loads(d["position_ids"] or "[]")
    return out


def get_run(run_id: int) -> dict | None:
    with _lock, connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None


def last_run_for(ticker: str) -> str | None:
    with _lock, connect() as c:
        r = c.execute("SELECT started FROM runs WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        return r["started"] if r else None


def runs_since(iso: str) -> int:
    with _lock, connect() as c:
        return c.execute("SELECT COUNT(*) FROM runs WHERE started >= ?", (iso,)).fetchone()[0]


def add_proposal(p: dict) -> int:
    p = dict(p)
    p.setdefault("created", _now())
    p["position_ids"] = json.dumps(p.get("position_ids") or [])
    cols = ["run_id", "ticker", "instrument_id", "action", "amount_usd", "units", "position_ids",
            "rating", "reason", "status", "mode", "created"]
    with _lock, connect() as c:
        cur = c.execute(f"INSERT INTO proposals({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [p.get(k) for k in cols])
        return cur.lastrowid


def update_proposal(pid: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock, connect() as c:
        c.execute(f"UPDATE proposals SET {sets} WHERE id=?", [*fields.values(), pid])


def get_proposal(pid: int) -> dict | None:
    with _lock, connect() as c:
        r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["position_ids"] = json.loads(d["position_ids"] or "[]")
    return d


def proposals(status: str | None = None, limit: int = 50) -> list:
    q, args = "SELECT * FROM proposals", []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock, connect() as c:
        out = [dict(r) for r in c.execute(q, args)]
    for d in out:
        d["position_ids"] = json.loads(d["position_ids"] or "[]")
    return out


def executed_since(iso: str) -> list:
    with _lock, connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM proposals WHERE status='executed' AND executed >= ?", (iso,))]


def last_executed_for(instrument_id: int) -> str | None:
    with _lock, connect() as c:
        r = c.execute("SELECT executed FROM proposals WHERE status='executed' AND instrument_id=? "
                      "ORDER BY executed DESC LIMIT 1", (instrument_id,)).fetchone()
        return r["executed"] if r else None


SECRET_KEYS = {"etoro_api_key", "etoro_user_key"}


def set_secret(key: str, value: str) -> None:
    if key not in SECRET_KEYS:
        raise KeyError(key)
    with _lock, connect() as c:
        c.execute("INSERT OR REPLACE INTO secrets(key, value, updated) VALUES (?, ?, ?)",
                  (key, "".join(value.split()), _now()))
    log("info", f"{key} updated from dashboard")


def get_secret(key: str) -> str:
    try:
        with _lock, connect() as c:
            r = c.execute("SELECT value FROM secrets WHERE key=?", (key,)).fetchone()
        return r["value"] if r else ""
    except sqlite3.OperationalError:
        return ""
