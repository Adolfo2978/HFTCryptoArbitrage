"""Binance triangular arbitrage scanner (USDT base/end) with clean-profit and compound simulation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from typing import Dict, Iterable, List, Optional, Tuple


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
    gross_final_usdt: float
    final_usdt: float
    total_fees_usdt: float
    net_profit_usdt: float
    profit_pct: float
    is_clean_profitable: bool


@dataclass
class ScanStats:
    market_type: str
    testnet: bool
    scanned_paths: int
    valid_paths: int
    clean_profitable_paths: int
    success_rate_pct: float
    best_profit_pct: float
    avg_profit_pct: float
    median_profit_pct: float
    scan_ms: int


@dataclass
class ScanOutput:
    opportunities: List[ArbitrageResult]
    stats: ScanStats


@dataclass
class CompoundPlanResult:
    initial_capital: float
    current_capital: float
    cycles_simulated: int
    trigger_reached: bool
    trigger_cycle: int
    stake_mode: str
    per_cycle_net_pct: float


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
            with urlopen(url, timeout=self.timeout) as response:  # nosec B310
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

    @staticmethod
    def _convert_once(amount: float, edge: Edge, prices: Dict[str, Tuple[float, float]]) -> Optional[float]:
        book = prices.get(edge.symbol)
        if not book:
            return None
        bid, ask = book
        if edge.side == "BUY":
            if ask <= 0:
                return None
            return amount / ask
        if bid <= 0:
            return None
        return amount * bid

    def _convert_with_fee(self, amount: float, edge: Edge, prices: Dict[str, Tuple[float, float]]) -> Optional[float]:
        out = self._convert_once(amount, edge, prices)
        if out is None:
            return None
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

    def _eval_cycle(self, start_usdt: float, edges: Tuple[Edge, Edge, Edge], prices: Dict[str, Tuple[float, float]]):
        gross = start_usdt
        net = start_usdt
        for edge in edges:
            gross = self._convert_once(gross, edge, prices)
            net = self._convert_with_fee(net, edge, prices)
            if gross is None or net is None:
                return None

        total_fees = max(0.0, gross - net)
        net_profit = net - start_usdt
        profit_pct = (net_profit / start_usdt) * 100.0
        return gross, net, total_fees, net_profit, profit_pct

    def evaluate_paths(
        self,
        symbol_rows: List[dict],
        prices: Dict[str, Tuple[float, float]],
        start_usdt: float,
        max_paths: int,
        max_assets: int,
        min_clean_profit_usdt: float = 0.0,
    ) -> ScanOutput:
        t0 = time.time()
        graph = self._build_edges(symbol_rows)

        rows: List[ArbitrageResult] = []
        scanned = 0
        for path, edges in self._enumerate_usdt_triangles(graph, max_assets=max_assets):
            scanned += 1
            cycle = self._eval_cycle(start_usdt, edges, prices)
            if cycle is None:
                continue

            gross, net, fees, net_profit, profit_pct = cycle
            clean = net_profit > min_clean_profit_usdt
            rows.append(
                ArbitrageResult(
                    path=path,
                    symbols=(edges[0].symbol, edges[1].symbol, edges[2].symbol),
                    sides=(edges[0].side, edges[1].side, edges[2].side),
                    gross_final_usdt=gross,
                    final_usdt=net,
                    total_fees_usdt=fees,
                    net_profit_usdt=net_profit,
                    profit_pct=profit_pct,
                    is_clean_profitable=clean,
                )
            )

        clean_rows = [x for x in rows if x.is_clean_profitable]
        clean_rows.sort(key=lambda x: x.profit_pct, reverse=True)
        top = clean_rows[:max_paths]

        all_profits = [x.profit_pct for x in rows]
        clean_count = len(clean_rows)
        elapsed_ms = int((time.time() - t0) * 1000)

        stats = ScanStats(
            market_type=self.market_type,
            testnet=self.testnet,
            scanned_paths=scanned,
            valid_paths=len(rows),
            clean_profitable_paths=clean_count,
            success_rate_pct=(clean_count / len(rows) * 100.0) if rows else 0.0,
            best_profit_pct=max(all_profits) if all_profits else 0.0,
            avg_profit_pct=(sum(all_profits) / len(all_profits)) if all_profits else 0.0,
            median_profit_pct=median(all_profits) if all_profits else 0.0,
            scan_ms=elapsed_ms,
        )

        return ScanOutput(opportunities=top, stats=stats)

    def scan(
        self,
        start_usdt: float = 100.0,
        max_paths: int = 20,
        max_assets: int = 120,
        min_clean_profit_usdt: float = 0.0,
    ) -> ScanOutput:
        symbol_rows = self.fetch_exchange_info()
        prices = self.fetch_book_tickers()
        return self.evaluate_paths(
            symbol_rows=symbol_rows,
            prices=prices,
            start_usdt=start_usdt,
            max_paths=max_paths,
            max_assets=max_assets,
            min_clean_profit_usdt=min_clean_profit_usdt,
        )

    @staticmethod
    def simulate_compound_plan(
        initial_capital_usdt: float,
        per_cycle_net_pct: float,
        cycles: int,
        trigger_multiple: float = 2.0,
        compound_stake_pct: float = 0.10,
        pre_trigger_stake_pct: float = 1.0,
    ) -> CompoundPlanResult:
        capital = initial_capital_usdt
        trigger_capital = initial_capital_usdt * trigger_multiple
        trigger_reached = False
        trigger_cycle = -1

        for cycle_idx in range(1, cycles + 1):
            if capital >= trigger_capital:
                if not trigger_reached:
                    trigger_reached = True
                    trigger_cycle = cycle_idx
                stake_pct = compound_stake_pct
            else:
                stake_pct = pre_trigger_stake_pct

            stake = capital * stake_pct
            gain = stake * (per_cycle_net_pct / 100.0)
            capital += gain

        mode = f"pre:{pre_trigger_stake_pct*100:.1f}% | post:{compound_stake_pct*100:.1f}%"
        return CompoundPlanResult(
            initial_capital=initial_capital_usdt,
            current_capital=capital,
            cycles_simulated=cycles,
            trigger_reached=trigger_reached,
            trigger_cycle=trigger_cycle,
            stake_mode=mode,
            per_cycle_net_pct=per_cycle_net_pct,
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
    scanner = BinanceArbitrageScanner(market_type="spot", testnet=True)
    out = scanner.scan(start_usdt=10, max_paths=5, min_clean_profit_usdt=0.0001)
    print(out.stats)
    for row in out.opportunities:
        print(row)
