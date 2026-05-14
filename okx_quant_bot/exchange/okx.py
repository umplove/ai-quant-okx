from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from okx_quant_bot.config import Settings
from okx_quant_bot.models import Candle, MarketTicker, OrderRequest, OrderResult, Side, StopLossOrder


class OkxAPIError(RuntimeError):
    pass


DEFAULT_USER_AGENT = "okx-quant-bot/0.1"


@dataclass(frozen=True)
class InstrumentRules:
    min_size: float
    lot_size: float
    tick_size: float


class OkxRestClient:
    def __init__(self, settings: Settings, timeout: float = 10.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self._instrument_rules: dict[str, InstrumentRules | None] = {}

    def get_candles(self, symbol: str, bar: str = "1H", limit: int = 300) -> list[Candle]:
        payload = self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": symbol, "bar": bar, "limit": str(limit)},
            auth=False,
        )
        rows = payload.get("data", [])
        candles = [Candle.from_okx(symbol, row) for row in rows]
        return sorted(candles, key=lambda c: c.ts)

    def get_balance(self, currency: str | None = None) -> dict[str, Any]:
        params = {"ccy": currency} if currency else None
        return self._request("GET", "/api/v5/account/balance", params=params, auth=True)

    def get_spot_instruments(self) -> dict[str, Any]:
        return self._request(
            "GET", "/api/v5/account/instruments", params={"instType": "SPOT"}, auth=True
        )

    def get_public_instruments(self, inst_type: str = "SPOT") -> dict[str, Any]:
        return self._request(
            "GET", "/api/v5/public/instruments", params={"instType": inst_type}, auth=False
        )

    def get_market_tickers(self, inst_type: str = "SPOT") -> list[MarketTicker]:
        payload = self._request(
            "GET", "/api/v5/market/tickers", params={"instType": inst_type}, auth=False
        )
        return [
            ticker
            for ticker in (MarketTicker.from_okx(row) for row in payload.get("data", []))
            if ticker.symbol and ticker.last > 0
        ]

    def place_order(self, order: OrderRequest) -> OrderResult:
        size_text, price_text, error = self._normalized_order_fields(order)
        if error:
            return OrderResult(
                ok=False,
                symbol=order.symbol,
                side=order.side,
                order_id=None,
                client_order_id=order.client_order_id,
                raw={"local_validation": True},
                error=error,
            )
        body: dict[str, str] = {
            "instId": order.symbol,
            "tdMode": "cash",
            "clOrdId": order.client_order_id,
            "side": order.side.value,
            "ordType": order.order_type,
            "sz": size_text,
        }
        if price_text is not None:
            body["px"] = price_text
        if order.target_currency:
            body["tgtCcy"] = order.target_currency
        if order.stop_loss_price is not None:
            body["attachAlgoOrds"] = [
                {
                    "attachAlgoClOrdId": self.client_order_id("SL", order.symbol),
                    "slTriggerPx": self._format_float(order.stop_loss_price),
                    "slOrdPx": "-1",
                    "slTriggerPxType": "last",
                }
            ]
        try:
            payload = self._request("POST", "/api/v5/trade/order", body=body, auth=True)
        except Exception as exc:
            return OrderResult(
                ok=False,
                symbol=order.symbol,
                side=order.side,
                order_id=None,
                client_order_id=order.client_order_id,
                raw={},
                error=str(exc),
            )
        data = payload.get("data", [{}])[0] if payload.get("data") else {}
        s_code = str(data.get("sCode", "0"))
        ok = payload.get("code") == "0" and s_code == "0"
        return OrderResult(
            ok=ok,
            symbol=order.symbol,
            side=order.side,
            order_id=data.get("ordId"),
            client_order_id=order.client_order_id,
            raw=payload,
            error=None if ok else data.get("sMsg") or payload.get("msg"),
        )

    def place_market_buy_quote(
        self,
        symbol: str,
        quote_amount: float,
        reason: str,
        stop_loss_price: float | None = None,
    ) -> tuple[OrderRequest, OrderResult]:
        order = OrderRequest(
            symbol=symbol,
            side=Side.BUY,
            size=quote_amount,
            order_type="market",
            price=None,
            client_order_id=self.client_order_id("MB", symbol),
            reason=reason,
            target_currency="quote_ccy",
            stop_loss_price=stop_loss_price,
        )
        return order, self.place_order(order)

    def place_market_sell_base(
        self,
        symbol: str,
        base_size: float,
        reason: str,
    ) -> tuple[OrderRequest, OrderResult]:
        order = OrderRequest(
            symbol=symbol,
            side=Side.SELL,
            size=base_size,
            order_type="market",
            price=None,
            client_order_id=self.client_order_id("MS", symbol),
            reason=reason,
        )
        return order, self.place_order(order)

    def place_limit_buy_quote(
        self,
        symbol: str,
        quote_amount: float,
        price: float,
        reason: str,
    ) -> tuple[OrderRequest, OrderResult]:
        base_size = 0.0 if price <= 0 else quote_amount / price
        order = OrderRequest(
            symbol=symbol,
            side=Side.BUY,
            size=base_size,
            order_type="limit",
            price=price,
            client_order_id=self.client_order_id("LB", symbol),
            reason=reason,
        )
        return order, self.place_order(order)

    def place_limit_sell_base(
        self,
        symbol: str,
        base_size: float,
        price: float,
        reason: str,
    ) -> tuple[OrderRequest, OrderResult]:
        order = OrderRequest(
            symbol=symbol,
            side=Side.SELL,
            size=base_size,
            order_type="limit",
            price=price,
            client_order_id=self.client_order_id("LS", symbol),
            reason=reason,
        )
        return order, self.place_order(order)

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v5/trade/cancel-order",
            body={"instId": symbol, "ordId": order_id},
            auth=True,
        )

    def list_open_orders(self, symbol: str | None = None) -> dict[str, Any]:
        params = {"instType": "SPOT"}
        if symbol:
            params["instId"] = symbol
        return self._request("GET", "/api/v5/trade/orders-pending", params=params, auth=True)

    def cancel_stop_loss_order(self, symbol: str, algo_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=[{"instId": symbol, "algoId": algo_id}],
            auth=True,
        )

    def place_stop_loss_order(self, symbol: str, size: float, stop_price: float) -> StopLossOrder:
        client_order_id = self.client_order_id("SL", symbol)
        size_text, stop_price_text, error = self._normalized_algo_fields(symbol, size, stop_price)
        if error:
            return StopLossOrder(
                symbol=symbol,
                algo_id=None,
                client_order_id=client_order_id,
                stop_price=stop_price,
                size=size,
                ok=False,
                raw={"local_validation": True},
                error=error,
            )
        body = {
            "instId": symbol,
            "tdMode": "cash",
            "side": "sell",
            "ordType": "conditional",
            "sz": size_text,
            "slTriggerPx": stop_price_text,
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
            "algoClOrdId": client_order_id,
        }
        try:
            payload = self._request("POST", "/api/v5/trade/order-algo", body=body, auth=True)
        except Exception as exc:
            return StopLossOrder(
                symbol=symbol,
                algo_id=None,
                client_order_id=client_order_id,
                stop_price=stop_price,
                size=size,
                ok=False,
                raw={},
                error=str(exc),
            )
        data = payload.get("data", [{}])[0] if payload.get("data") else {}
        s_code = str(data.get("sCode", "0"))
        ok = payload.get("code") == "0" and s_code == "0"
        return StopLossOrder(
            symbol=symbol,
            algo_id=data.get("algoId"),
            client_order_id=client_order_id,
            stop_price=stop_price,
            size=size,
            ok=ok,
            raw=payload,
            error=None if ok else data.get("sMsg") or payload.get("msg"),
        )

    def get_order_details(self, symbol: str, order_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v5/trade/order",
            params={"instId": symbol, "ordId": order_id},
            auth=True,
        )

    def instrument_rules(self, symbol: str) -> InstrumentRules | None:
        if symbol in self._instrument_rules:
            return self._instrument_rules[symbol]
        rules: InstrumentRules | None = None
        try:
            payload = self.get_public_instruments("SPOT")
            for item in payload.get("data", []):
                if item.get("instId") == symbol:
                    rules = InstrumentRules(
                        min_size=float(item.get("minSz") or 0),
                        lot_size=float(item.get("lotSz") or 0),
                        tick_size=float(item.get("tickSz") or 0),
                    )
                    break
        except Exception:
            rules = None
        self._instrument_rules[symbol] = rules
        return rules

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: Any | None = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        method = method.upper()
        query = urllib.parse.urlencode(params or {})
        path_with_query = f"{path}?{query}" if query else path
        body_text = json.dumps(body or {}, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json", "User-Agent": DEFAULT_USER_AGENT}
        if auth:
            headers.update(self._auth_headers(method, path_with_query, body_text))
        if self.settings.okx_demo and self.settings.simulated_trading_header:
            headers["x-simulated-trading"] = "1"

        request = urllib.request.Request(
            f"{self.settings.okx_base_url}{path_with_query}",
            data=body_text.encode("utf-8") if body is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OkxAPIError(f"OKX HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise OkxAPIError(f"OKX network error: {exc.reason}") from exc

        payload = json.loads(raw)
        if payload.get("code") not in {None, "0"}:
            raise OkxAPIError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        return payload

    def _auth_headers(self, method: str, path_with_query: str, body_text: str) -> dict[str, str]:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        prehash = f"{timestamp}{method}{path_with_query}{body_text}"
        signature = base64.b64encode(
            hmac.new(
                self.settings.okx_secret_key.encode("utf-8"),
                prehash.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return {
            "OK-ACCESS-KEY": self.settings.okx_api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.settings.okx_passphrase,
        }

    @staticmethod
    def _format_float(value: float) -> str:
        return f"{value:.12f}".rstrip("0").rstrip(".")

    def _normalized_order_fields(self, order: OrderRequest) -> tuple[str, str | None, str | None]:
        rules = self.instrument_rules(order.symbol)
        size = order.size
        price = order.price
        size_text = self._format_float(size)
        price_text = self._format_float(price) if price is not None else None
        if rules is not None:
            if price is not None and rules.tick_size > 0:
                price_text = self._floor_to_step_text(price, rules.tick_size)
                price = float(price_text)
                if price <= 0:
                    return "", None, f"{order.symbol} price is below tick size"
            rounds_base_size = not (
                order.side == Side.BUY and order.order_type == "market" and order.target_currency == "quote_ccy"
            )
            if rounds_base_size and rules.lot_size > 0:
                size_text = self._floor_to_step_text(size, rules.lot_size)
                size = float(size_text)
                if size <= 0 or (rules.min_size > 0 and size < rules.min_size):
                    return "", None, f"{order.symbol} size is below minSz={rules.min_size}"
        return size_text, price_text, None

    def _normalized_algo_fields(self, symbol: str, size: float, stop_price: float) -> tuple[str, str, str | None]:
        rules = self.instrument_rules(symbol)
        size_text = self._format_float(size)
        stop_price_text = self._format_float(stop_price)
        if rules is not None:
            if rules.tick_size > 0:
                stop_price_text = self._floor_to_step_text(stop_price, rules.tick_size)
                if float(stop_price_text) <= 0:
                    return "", "", f"{symbol} stop price is below tick size"
            if rules.lot_size > 0:
                size_text = self._floor_to_step_text(size, rules.lot_size)
                rounded_size = float(size_text)
                if rounded_size <= 0 or (rules.min_size > 0 and rounded_size < rules.min_size):
                    return "", "", f"{symbol} size is below minSz={rules.min_size}"
        return size_text, stop_price_text, None

    @staticmethod
    def _floor_to_step_text(value: float, step: float) -> str:
        try:
            value_dec = Decimal(str(value))
            step_dec = Decimal(str(step))
            if step_dec <= 0:
                return OkxRestClient._format_float(value)
            units = (value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
            rounded = units * step_dec
            return format(rounded.normalize(), "f").rstrip("0").rstrip(".") or "0"
        except (InvalidOperation, ValueError):
            return OkxRestClient._format_float(value)

    @staticmethod
    def client_order_id(prefix: str, symbol: str) -> str:
        safe_symbol = symbol.replace("-", "")
        return f"{prefix}{safe_symbol}{int(time.time() * 1000)}"[:32]
