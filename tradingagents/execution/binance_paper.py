"""Binance Spot Testnet paper execution.

The executor is deliberately constrained to Binance's Spot Testnet. It maps
TradingAgents' 5-tier portfolio rating to a small MARKET order plan, writes a
dry-run by default, and only submits an order when explicitly enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlencode, urlparse

import requests


TESTNET_BASE_URL = "https://testnet.binance.vision/api"
ALLOWED_TESTNET_HOSTS = {
    "testnet.binance.vision",
    # Binance has also used demo hosts for sandbox products. Keep this
    # constrained to Binance-owned demo/testnet hosts, never production.
    "demo-api.binance.com",
}

BUY_RATINGS = {"Buy": Decimal("1"), "Overweight": Decimal("0.5")}
SELL_RATINGS = {"Sell": Decimal("1"), "Underweight": Decimal("0.5")}


class BinancePaperExecutionError(RuntimeError):
    """Raised when a paper execution cannot be planned or submitted."""


@dataclass
class BinancePaperConfig:
    """Runtime settings for Binance Spot Testnet execution."""

    execution_mode: str = "off"
    enable_order_execution: bool = False
    base_url: str = TESTNET_BASE_URL
    api_key: str = ""
    api_secret: str = ""
    max_order_usdt: Decimal = Decimal("25")
    default_quote_asset: str = "USDT"
    symbol_override: Optional[str] = None
    recv_window: int = 5000

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "BinancePaperConfig":
        """Build execution config from DEFAULT_CONFIG plus secret env vars."""
        max_order = _decimal(config.get("max_order_usdt", 25.0), "max_order_usdt")
        return cls(
            execution_mode=str(config.get("execution_mode", "off")).lower(),
            enable_order_execution=bool(config.get("enable_order_execution", False)),
            base_url=os.environ.get("BINANCE_TESTNET_BASE_URL", TESTNET_BASE_URL),
            api_key=os.environ.get("BINANCE_TESTNET_API_KEY", ""),
            api_secret=os.environ.get("BINANCE_TESTNET_API_SECRET", ""),
            max_order_usdt=max_order,
            default_quote_asset=str(config.get("binance_default_quote_asset") or "USDT").upper(),
            symbol_override=config.get("binance_symbol") or None,
        )

    @property
    def active(self) -> bool:
        return self.execution_mode == "paper"

    @property
    def dry_run(self) -> bool:
        return not self.enable_order_execution


@dataclass
class ExecutionResult:
    """Serializable result from a dry-run or submitted testnet order."""

    status: str
    dry_run: bool
    rating: str
    action: str
    symbol: str
    max_order_usdt: str
    order_request: dict[str, Any]
    order_response: Optional[dict[str, Any]] = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def rating_to_side(rating: str) -> Optional[str]:
    """Map TradingAgents' 5-tier rating to a Binance order side."""
    rating = (rating or "Hold").strip().capitalize()
    if rating in BUY_RATINGS:
        return "BUY"
    if rating in SELL_RATINGS:
        return "SELL"
    return None


def resolve_binance_symbol(
    ticker: str,
    *,
    default_quote_asset: str = "USDT",
    override: Optional[str] = None,
) -> str:
    """Normalize a TradingAgents ticker into a Binance spot symbol.

    Examples:
    - BTC-USD -> BTCUSDT
    - BTC-USDT -> BTCUSDT
    - BTC -> BTCUSDT
    - BTCUSDT -> BTCUSDT
    """
    if override:
        return _validate_symbol(override)

    cleaned = (ticker or "").strip().upper().replace("/", "-").replace("_", "-")
    if not cleaned:
        raise BinancePaperExecutionError("Cannot build Binance symbol from an empty ticker.")

    quote = _validate_symbol(default_quote_asset)
    if "-" in cleaned:
        base, raw_quote = cleaned.split("-", 1)
        raw_quote = "USDT" if raw_quote == "USD" else raw_quote
        return _validate_symbol(f"{base}{raw_quote}")

    # Common crypto shorthand: BTC -> BTCUSDT. If the user already entered a
    # full Binance symbol ending in the quote asset, leave it unchanged.
    if cleaned.endswith(quote):
        return _validate_symbol(cleaned)
    return _validate_symbol(f"{cleaned}{quote}")


class BinanceSpotTestnetClient:
    """Small REST client for the Binance Spot Testnet endpoints we need."""

    def __init__(
        self,
        config: BinancePaperConfig,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
    ) -> None:
        _assert_testnet_base_url(config.base_url)
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", "/v3/exchangeInfo", {"symbol": symbol}, signed=False)

    def ticker_price(self, symbol: str) -> Decimal:
        data = self._request("GET", "/v3/ticker/price", {"symbol": symbol}, signed=False)
        return _decimal(data["price"], "price")

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/v3/account", {}, signed=True)

    def create_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quote_order_qty: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
        }
        if quote_order_qty is not None:
            params["quoteOrderQty"] = _format_decimal(quote_order_qty)
        if quantity is not None:
            params["quantity"] = _format_decimal(quantity)
        return self._request("POST", "/v3/order", params, signed=True)

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any],
        *,
        signed: bool,
    ) -> dict[str, Any]:
        payload = dict(params)
        headers = {}
        if signed:
            if not self.config.api_key or not self.config.api_secret:
                raise BinancePaperExecutionError(
                    "BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET are required "
                    "when TRADINGAGENTS_ENABLE_ORDER_EXECUTION=true."
                )
            payload["recvWindow"] = self.config.recv_window
            payload["timestamp"] = int(time.time() * 1000)
            payload["signature"] = _sign(payload, self.config.api_secret)
            headers["X-MBX-APIKEY"] = self.config.api_key

        url = f"{self.base_url}{path}"
        if method.upper() == "GET":
            response = self.session.request(
                method, url, params=payload, headers=headers, timeout=self.timeout
            )
        else:
            response = self.session.request(
                method, url, data=payload, headers=headers, timeout=self.timeout
            )

        if response.status_code >= 400:
            raise BinancePaperExecutionError(
                f"Binance Spot Testnet request failed ({response.status_code}): "
                f"{response.text[:500]}"
            )
        return response.json() if response.text else {}


class BinancePaperExecutor:
    """Plan and optionally submit a Binance Spot Testnet paper order."""

    def __init__(
        self,
        config: BinancePaperConfig,
        *,
        client: Optional[BinanceSpotTestnetClient] = None,
    ) -> None:
        self.config = config
        self.client = client or BinanceSpotTestnetClient(config)

    def execute(
        self,
        *,
        ticker: str,
        rating: str,
        final_decision: str,
        log_dir: Optional[Path] = None,
    ) -> ExecutionResult:
        side = rating_to_side(rating)
        symbol = resolve_binance_symbol(
            ticker,
            default_quote_asset=self.config.default_quote_asset,
            override=self.config.symbol_override,
        )

        if side is None:
            result = ExecutionResult(
                status="skipped",
                dry_run=True,
                rating=rating,
                action="HOLD",
                symbol=symbol,
                max_order_usdt=_format_decimal(self.config.max_order_usdt),
                order_request={},
                message="Rating is Hold; no paper order planned.",
            )
            self._write_log(result, final_decision, log_dir)
            return result

        if self.config.dry_run:
            order_request = self._build_dry_run_order_request(
                symbol=symbol, side=side, rating=rating
            )
            result = ExecutionResult(
                status="dry_run",
                dry_run=True,
                rating=rating,
                action=side,
                symbol=symbol,
                max_order_usdt=_format_decimal(self.config.max_order_usdt),
                order_request=order_request,
                message="Dry-run only. Set TRADINGAGENTS_ENABLE_ORDER_EXECUTION=true to submit to Spot Testnet.",
            )
            self._write_log(result, final_decision, log_dir)
            return result

        order_request = self._build_order_request(symbol=symbol, side=side, rating=rating)
        response = self.client.create_market_order(**order_request)
        result = ExecutionResult(
            status="submitted",
            dry_run=False,
            rating=rating,
            action=side,
            symbol=symbol,
            max_order_usdt=_format_decimal(self.config.max_order_usdt),
            order_request=order_request,
            order_response=response,
            message="Submitted MARKET order to Binance Spot Testnet.",
        )
        self._write_log(result, final_decision, log_dir)
        return result

    def _build_dry_run_order_request(
        self, *, symbol: str, side: str, rating: str
    ) -> dict[str, Any]:
        multiplier = BUY_RATINGS.get(rating, SELL_RATINGS.get(rating, Decimal("1")))
        notional = _round_down(self.config.max_order_usdt * multiplier, Decimal("0.01"))
        if side == "BUY":
            return {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quoteOrderQty": _format_decimal(notional),
            }
        return {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "maxQuoteNotional": _format_decimal(notional),
            "note": "Dry-run SELL uses quote notional only; live testnet submission resolves base quantity from account balance and current price.",
        }

    def _build_order_request(self, *, symbol: str, side: str, rating: str) -> dict[str, Any]:
        multiplier = BUY_RATINGS.get(rating, SELL_RATINGS.get(rating, Decimal("1")))
        notional = _round_down(self.config.max_order_usdt * multiplier, Decimal("0.01"))

        if side == "BUY":
            return {"symbol": symbol, "side": side, "quote_order_qty": notional}

        filters = _symbol_filters(self.client.exchange_info(symbol))
        price = self.client.ticker_price(symbol)
        base_asset = filters["base_asset"]
        free_balance = _asset_free_balance(self.client.account(), base_asset)
        max_qty = notional / price
        quantity = min(free_balance, max_qty)
        quantity = _round_down(quantity, filters["step_size"])

        if quantity <= 0:
            raise BinancePaperExecutionError(
                f"No {base_asset} balance available on Spot Testnet for SELL {symbol}."
            )
        if quantity < filters["min_qty"]:
            raise BinancePaperExecutionError(
                f"Planned SELL quantity {quantity} is below Binance minQty {filters['min_qty']}."
            )
        return {"symbol": symbol, "side": side, "quantity": quantity}

    def _write_log(
        self,
        result: ExecutionResult,
        final_decision: str,
        log_dir: Optional[Path],
    ) -> None:
        if log_dir is None:
            return
        log_dir.mkdir(parents=True, exist_ok=True)
        payload = result.to_dict()
        payload["final_decision_excerpt"] = final_decision[:2000]
        (log_dir / "binance_paper_execution.json").write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )


def _assert_testnet_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_TESTNET_HOSTS:
        raise BinancePaperExecutionError(
            "Binance paper execution is restricted to Spot Testnet. "
            f"Refusing base URL: {base_url!r}"
        )
    if parsed.hostname == "testnet.binance.vision" and not parsed.path.rstrip("/").endswith("/api"):
        raise BinancePaperExecutionError(
            "BINANCE_TESTNET_BASE_URL should include the /api prefix, e.g. "
            "https://testnet.binance.vision/api"
        )


def _sign(params: Mapping[str, Any], secret: str) -> str:
    query = urlencode(params)
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BinancePaperExecutionError(f"Invalid decimal for {field_name}: {value!r}") from exc


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _validate_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip().upper()
    if not symbol.isalnum():
        raise BinancePaperExecutionError(f"Invalid Binance symbol: {symbol!r}")
    return symbol


def _symbol_filters(exchange_info: Mapping[str, Any]) -> dict[str, Decimal | str]:
    symbols = exchange_info.get("symbols") or []
    if not symbols:
        raise BinancePaperExecutionError("Binance exchangeInfo did not return symbol metadata.")

    symbol = symbols[0]
    lot_size = None
    for item in symbol.get("filters", []):
        if item.get("filterType") == "LOT_SIZE":
            lot_size = item
            break
    if lot_size is None:
        raise BinancePaperExecutionError("Binance exchangeInfo did not include LOT_SIZE filter.")

    return {
        "base_asset": symbol["baseAsset"],
        "quote_asset": symbol["quoteAsset"],
        "step_size": _decimal(lot_size["stepSize"], "stepSize"),
        "min_qty": _decimal(lot_size["minQty"], "minQty"),
    }


def _asset_free_balance(account: Mapping[str, Any], asset: str) -> Decimal:
    for balance in account.get("balances", []):
        if balance.get("asset") == asset:
            return _decimal(balance.get("free", "0"), f"{asset} free balance")
    return Decimal("0")
