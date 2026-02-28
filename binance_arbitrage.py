"""Binance triangular arbitrage scanner (USDT base/end) for Spot and USDT Perpetuals."""

from __future__ import annotations

import time
from dataclasses import dataclass
from statistics import median
from typing import Dict, Iterable, List, Optional, Tuple

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


@dataclass(frozen=True)
class Edge:
    from_asset: str
    to_asset: str
    symbol: str
    side: str  # BUY or SELL


@dataclass
class ArbitrageResult:
    path: Tuple[str, str, str, str]
    symbols: Tuple[str, str, str]
    sides: Tuple[str, str, str]
    final_usdt: float
    profit_pct: float


@dataclass
class ScanStats:
    market_type: str
    testnet: bool
    scanned_paths: int
    profitable_paths: int
    success_rate_pct: float
    best_profit_pct: float
    avg_profit_pct: float
    median_profit_pct: float
    scan_ms: int


@dataclass
class ScanOutput:
    opportunities: List[ArbitrageResult]
    stats: ScanStats


class BinanceArbitrageScanner:
    def __init__(self, fee_rate: float = 0.001, timeout: int = 10, market_type: str = "spot", testnet: bool = False):
        self.fee_rate = fee_rate
        self.timeout = timeout
        self.market_type = market_type.lower()
        self.testnet = testnet

    def configure(self, market_type: str, testnet: bool, fee_rate: float):
        self.market_type = market_type.lower()
        self.testnet = testnet
        self.fee_rate = fee_rate

    def _api_base_url(self) -> str:
        if self.market_type == "spot":
            return "https://testnet.binance.vision" if self.testnet else "https://api.binance.com"
        if self.market_type == "perpetual":
            return "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
        raise ValueError("market_type debe ser 'spot' o 'perpetual'")

    def _endpoint(self, key: str) -> str:
        if self.market_type == "spot":
            mapping = {
                "exchange_info": "/api/v3/exchangeInfo",
                "book_ticker": "/api/v3/ticker/bookTicker",
                "time": "/api/v3/time",
            }
        else:
            mapping = {
                "exchange_info": "/fapi/v1/exchangeInfo",
                "book_ticker": "/fapi/v1/ticker/bookTicker",
                "time": "/fapi/v1/time",
            }
        return mapping[key]

    def _get(self, endpoint: str, params: Optional[dict] = None):
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self._api_base_url()}{endpoint}{query}"
        try:
            with urlopen(url, timeout=self.timeout) as response:  # nosec B310 - Binance endpoints only
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"HTTP error {exc.code}: {exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"Network error: {exc.reason}") from exc

    def fetch_exchange_info(self) -> List[dict]:
        payload = self._get(self._endpoint("exchange_info"))
        symbols = payload.get("symbols", [])

        if self.market_type == "spot":
            return [s for s in symbols if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)]

        return [
            s
            for s in symbols
            if s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset")
        ]

    def fetch_book_tickers(self) -> Dict[str, Tuple[float, float]]:
        payload = self._get(self._endpoint("book_ticker"))
        prices: Dict[str, Tuple[float, float]] = {}
        for row in payload:
            symbol = row["symbol"]
            bid = float(row["bidPrice"])
            ask = float(row["askPrice"])
            if bid > 0 and ask > 0:
                prices[symbol] = (bid, ask)
        return prices

    @staticmethod
    def _build_edges(symbol_rows: Iterable[dict]) -> Dict[str, Dict[str, Edge]]:
        graph: Dict[str, Dict[str, Edge]] = {}
        for row in symbol_rows:
            symbol = row.get("symbol")
            base = row.get("baseAsset")
            quote = row.get("quoteAsset")
            if not symbol or not base or not quote:
                continue
            graph.setdefault(quote, {})[base] = Edge(quote, base, symbol, "BUY")
            graph.setdefault(base, {})[quote] = Edge(base, quote, symbol, "SELL")
        return graph

    def _convert(self, amount: float, edge: Edge, prices: Dict[str, Tuple[float, float]]) -> Optional[float]:
        book = prices.get(edge.symbol)
        if not book:
            return None

        bid, ask = book
        if edge.side == "BUY":
            if ask <= 0:
                return None
            out = amount / ask
        else:
            if bid <= 0:
                return None
            out = amount * bid

        return out * (1 - self.fee_rate)

    def _enumerate_usdt_triangles(self, graph: Dict[str, Dict[str, Edge]], max_assets: int = 120):
        neighbors_usdt = list(graph.get("USDT", {}).keys())
        if max_assets > 0:
            neighbors_usdt = neighbors_usdt[:max_assets]

        for a in neighbors_usdt:
            e1 = graph["USDT"].get(a)
            if not e1:
                continue
            for b, e2 in graph.get(a, {}).items():
                if b == "USDT":
                    continue
                e3 = graph.get(b, {}).get("USDT")
                if not e2 or not e3:
                    continue
                yield ("USDT", a, b, "USDT"), (e1, e2, e3)

    def evaluate_paths(
        self,
        symbol_rows: List[dict],
        prices: Dict[str, Tuple[float, float]],
        start_usdt: float,
        max_paths: int,
        max_assets: int,
    ) -> ScanOutput:
        t0 = time.time()
        graph = self._build_edges(symbol_rows)

        opportunities: List[ArbitrageResult] = []
        total = 0
        for path, edges in self._enumerate_usdt_triangles(graph, max_assets=max_assets):
            total += 1
            amount = start_usdt
            ok = True
            for edge in edges:
                converted = self._convert(amount, edge, prices)
                if converted is None:
                    ok = False
                    break
                amount = converted

            if not ok:
                continue

            profit_pct = ((amount / start_usdt) - 1.0) * 100.0
            opportunities.append(
                ArbitrageResult(
                    path=path,
                    symbols=(edges[0].symbol, edges[1].symbol, edges[2].symbol),
                    sides=(edges[0].side, edges[1].side, edges[2].side),
                    final_usdt=amount,
                    profit_pct=profit_pct,
                )
            )

        opportunities.sort(key=lambda x: x.profit_pct, reverse=True)
        top = opportunities[:max_paths]

        profits = [x.profit_pct for x in opportunities]
        profitable = [p for p in profits if p > 0]
        elapsed_ms = int((time.time() - t0) * 1000)

        stats = ScanStats(
            market_type=self.market_type,
            testnet=self.testnet,
            scanned_paths=total,
            profitable_paths=len(profitable),
            success_rate_pct=(len(profitable) / total * 100.0) if total else 0.0,
            best_profit_pct=max(profits) if profits else 0.0,
            avg_profit_pct=(sum(profits) / len(profits)) if profits else 0.0,
            median_profit_pct=median(profits) if profits else 0.0,
            scan_ms=elapsed_ms,
        )

        return ScanOutput(opportunities=top, stats=stats)

    def scan(self, start_usdt: float = 100.0, max_paths: int = 20, max_assets: int = 120) -> ScanOutput:
        symbol_rows = self.fetch_exchange_info()
        prices = self.fetch_book_tickers()
        return self.evaluate_paths(
            symbol_rows=symbol_rows,
            prices=prices,
            start_usdt=start_usdt,
            max_paths=max_paths,
            max_assets=max_assets,
        )

    def validate_api_keys(self, api_key: str, api_secret: str) -> Tuple[bool, str]:
        if not api_key or not api_secret:
            return False, "Faltan API key/secret"
        if len(api_key) < 20 or len(api_secret) < 20:
            return False, "Formato de API key/secret parece inválido"

        try:
            _ = self._get(self._endpoint("time"))
            env = "TESTNET" if self.testnet else "MAINNET"
            return True, f"Conectividad {env} OK para {self.market_type.upper()}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Error de conectividad Binance: {exc}"


if __name__ == "__main__":
    scanner = BinanceArbitrageScanner(market_type="spot", testnet=False)
    out = scanner.scan(start_usdt=100, max_paths=5)
    print(out.stats)
    for row in out.opportunities:
        print(row)
