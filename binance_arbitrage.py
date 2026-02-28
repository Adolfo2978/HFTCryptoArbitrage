"""Binance triangular arbitrage scanner (base and end in USDT)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import requests

BINANCE_BASE_URL = "https://api.binance.com"


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


class BinanceArbitrageScanner:
    def __init__(self, fee_rate: float = 0.001, timeout: int = 10):
        self.fee_rate = fee_rate
        self.timeout = timeout

    def _get(self, endpoint: str, params: Optional[dict] = None, headers: Optional[dict] = None):
        response = requests.get(
            f"{BINANCE_BASE_URL}{endpoint}", params=params, headers=headers, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def fetch_exchange_info(self) -> List[dict]:
        payload = self._get("/api/v3/exchangeInfo")
        symbols = payload.get("symbols", [])
        return [s for s in symbols if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)]

    def fetch_book_tickers(self) -> Dict[str, Tuple[float, float]]:
        payload = self._get("/api/v3/ticker/bookTicker")
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
            symbol = row["symbol"]
            base = row["baseAsset"]
            quote = row["quoteAsset"]

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

    def scan(self, start_usdt: float = 100.0, max_paths: int = 10, max_assets: int = 120) -> List[ArbitrageResult]:
        symbol_rows = self.fetch_exchange_info()
        prices = self.fetch_book_tickers()
        graph = self._build_edges(symbol_rows)

        opportunities: List[ArbitrageResult] = []
        for path, edges in self._enumerate_usdt_triangles(graph, max_assets=max_assets):
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
        return opportunities[:max_paths]

    def validate_api_keys(self, api_key: str, api_secret: str) -> Tuple[bool, str]:
        if not api_key or not api_secret:
            return False, "Faltan API key/secret"

        # Lightweight validation against signed endpoint omitted to avoid secret handling complexity.
        # We still test connectivity and key format basic sanity.
        if len(api_key) < 20 or len(api_secret) < 20:
            return False, "Formato de API key/secret parece inválido"

        try:
            _ = self._get("/api/v3/time")
            return True, "Conectividad OK. Claves almacenadas localmente (no validadas con firma)."
        except Exception as exc:  # noqa: BLE001
            return False, f"Error de conectividad Binance: {exc}"


if __name__ == "__main__":
    scanner = BinanceArbitrageScanner()
    rows = scanner.scan(start_usdt=100, max_paths=5)
    for row in rows:
        print(row)
