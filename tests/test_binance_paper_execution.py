from __future__ import annotations

from decimal import Decimal

import pytest

from tradingagents.execution.binance_paper import (
    BinancePaperConfig,
    BinancePaperExecutionError,
    BinancePaperExecutor,
    BinanceSpotTestnetClient,
    rating_to_side,
    resolve_binance_symbol,
)


@pytest.mark.parametrize(
    "rating,side",
    [
        ("Buy", "BUY"),
        ("Overweight", "BUY"),
        ("Hold", None),
        ("Underweight", "SELL"),
        ("Sell", "SELL"),
    ],
)
def test_rating_to_side(rating, side):
    assert rating_to_side(rating) == side


@pytest.mark.parametrize(
    "ticker,symbol",
    [
        ("BTC-USD", "BTCUSDT"),
        ("BTC-USDT", "BTCUSDT"),
        ("BTC", "BTCUSDT"),
        ("BTCUSDT", "BTCUSDT"),
        ("eth_usd", "ETHUSDT"),
    ],
)
def test_resolve_binance_symbol(ticker, symbol):
    assert resolve_binance_symbol(ticker) == symbol


def test_resolve_binance_symbol_allows_override():
    assert resolve_binance_symbol("BTC-USD", override="ETHUSDT") == "ETHUSDT"


def test_rejects_production_binance_endpoint():
    cfg = BinancePaperConfig(base_url="https://api.binance.com/api")
    with pytest.raises(BinancePaperExecutionError, match="Spot Testnet"):
        BinanceSpotTestnetClient(cfg)


def test_rejects_testnet_url_without_api_prefix():
    cfg = BinancePaperConfig(base_url="https://testnet.binance.vision")
    with pytest.raises(BinancePaperExecutionError, match="/api"):
        BinanceSpotTestnetClient(cfg)


def test_buy_dry_run_writes_plan_without_credentials(tmp_path):
    cfg = BinancePaperConfig(
        execution_mode="paper",
        enable_order_execution=False,
        max_order_usdt=Decimal("25"),
    )
    result = BinancePaperExecutor(cfg).execute(
        ticker="BTC-USD",
        rating="Buy",
        final_decision="**Rating**: Buy\n",
        log_dir=tmp_path,
    )

    assert result.status == "dry_run"
    assert result.order_request == {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": "25",
    }
    assert (tmp_path / "binance_paper_execution.json").exists()


def test_hold_skips_order(tmp_path):
    cfg = BinancePaperConfig(execution_mode="paper")
    result = BinancePaperExecutor(cfg).execute(
        ticker="BTC-USD",
        rating="Hold",
        final_decision="**Rating**: Hold\n",
        log_dir=tmp_path,
    )

    assert result.status == "skipped"
    assert result.action == "HOLD"
    assert result.order_request == {}


class _FakeClient:
    def exchange_info(self, symbol):
        assert symbol == "BTCUSDT"
        return {
            "symbols": [
                {
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "filters": [
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.00000100",
                            "stepSize": "0.00000100",
                        }
                    ],
                }
            ]
        }

    def ticker_price(self, symbol):
        assert symbol == "BTCUSDT"
        return Decimal("100000")

    def account(self):
        return {"balances": [{"asset": "BTC", "free": "0.01000000"}]}

    def create_market_order(self, **kwargs):
        return {"orderId": 123, **kwargs}


def test_sell_submission_resolves_quantity_from_testnet_balance():
    cfg = BinancePaperConfig(
        execution_mode="paper",
        enable_order_execution=True,
        api_key="test-key",
        api_secret="test-secret",
        max_order_usdt=Decimal("25"),
    )
    result = BinancePaperExecutor(cfg, client=_FakeClient()).execute(
        ticker="BTC-USD",
        rating="Underweight",
        final_decision="**Rating**: Underweight\n",
    )

    assert result.status == "submitted"
    assert result.order_request == {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "quantity": Decimal("0.00012500"),
    }
    assert result.order_response["orderId"] == 123
