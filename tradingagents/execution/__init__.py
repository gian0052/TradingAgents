"""Execution adapters for downstream paper-trading workflows."""

from .binance_paper import (
    BinancePaperConfig,
    BinancePaperExecutor,
    BinancePaperExecutionError,
    ExecutionResult,
    rating_to_side,
    resolve_binance_symbol,
)

__all__ = [
    "BinancePaperConfig",
    "BinancePaperExecutor",
    "BinancePaperExecutionError",
    "ExecutionResult",
    "rating_to_side",
    "resolve_binance_symbol",
]
