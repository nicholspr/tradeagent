"""Runs the TradingAgents multi-agent graph for one ticker with Claude."""
import os
from datetime import date

from . import config


def build_config() -> dict:
    from tradingagents.default_config import build_default_config

    cfg = build_default_config()
    home = os.path.join(config.DATA_DIR, "tradingagents")
    cfg.update({
        "llm_provider": "anthropic",
        "deep_think_llm": config.model_for("deep"),
        "quick_think_llm": config.model_for("quick"),
        "results_dir": os.path.join(home, "logs"),
        "data_cache_dir": os.path.join(home, "cache"),
        "memory_log_path": os.path.join(home, "memory", "trading_memory.md"),
        "memory_log_max_entries": 500,
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "llm_max_retries": 4,
    })
    return cfg


def analyze(ticker: str, asset_type: str = "stock", portfolio: dict | None = None,
            trade_date: str | None = None) -> tuple[str, str]:
    """Return (rating, final decision text). Rating: Buy/Overweight/Hold/Underweight/Sell/REVIEW."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.portfolio import PortfolioContext

    ctx = PortfolioContext.model_validate(portfolio) if portfolio is not None else None
    graph = TradingAgentsGraph(debug=False, config=build_config())
    final_state, rating = graph.propagate(ticker, trade_date or date.today().isoformat(),
                                          asset_type=asset_type, portfolio=ctx)
    return str(rating), str(final_state.get("final_trade_decision") or "")
