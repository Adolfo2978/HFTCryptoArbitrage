"""HFTCryptoArbitrage - Sistema Unificado con GUI de alto rendimiento visual."""

from __future__ import annotations

import json
import math
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONFIG_PATH = Path(".binance_gui_config.json")


# ==========================
# DATA MODELS
# ==========================

@dataclass(frozen=True)
class Edge:
    from_asset: str
    to_asset: str
    symbol: str
    side: str


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


# ==========================
# UTILS
# ==========================


def http_get_json(url: str, timeout: int = 10) -> dict | list:
    req = Request(url, headers={"User-Agent": "HFTCryptoArbitrage/2.0"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP error {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


# ==========================
# BINANCE SCANNER
# ==========================


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
            return "https://testnet.binance.vision/" if self.testnet else "https://api.binance.com"
        if self.market_type == "perpetual":
            return "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
        raise ValueError("market_type debe ser 'spot' o 'perpetual'")

    def _endpoint(self, key: str) -> str:
        if self.market_type == "spot":
            m = {"exchange_info": "/api/v3/exchangeInfo", "book_ticker": "/api/v3/ticker/bookTicker", "time": "/api/v3/time"}
        else:
            m = {"exchange_info": "/fapi/v1/exchangeInfo", "book_ticker": "/fapi/v1/ticker/bookTicker", "time": "/fapi/v1/time"}
        return m[key]

    def _get(self, endpoint: str, params: Optional[dict] = None):
        query = f"?{urlencode(params)}" if params else ""
        return http_get_json(f"{self._api_base_url().rstrip('/')}{endpoint}{query}", timeout=self.timeout)

    def fetch_exchange_info(self) -> List[dict]:
        payload = self._get(self._endpoint("exchange_info"))
        symbols = payload.get("symbols", [])
        if self.market_type == "spot":
            return [s for s in symbols if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)]
        return [s for s in symbols if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL" and s.get("quoteAsset")]

    def fetch_book_tickers(self) -> Dict[str, Tuple[float, float]]:
        payload = self._get(self._endpoint("book_ticker"))
        if not isinstance(payload, list):
            raise RuntimeError("Respuesta inválida de Binance bookTicker")

        prices: Dict[str, Tuple[float, float]] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).strip()
            if not symbol:
                continue
            try:
                bid = float(row.get("bidPrice", 0))
                ask = float(row.get("askPrice", 0))
            except (TypeError, ValueError):
                continue
            if bid > 0 and ask > 0:
                prices[symbol] = (bid, ask)

        if not prices:
            raise RuntimeError("No se recibieron precios válidos en bookTicker")
        return prices

    @staticmethod
    def _build_edges(symbol_rows: List[dict]) -> Dict[str, Dict[str, Edge]]:
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
            return amount / ask if ask > 0 else None
        return amount * bid if bid > 0 else None

    def _convert_with_fee(self, amount: float, edge: Edge, prices: Dict[str, Tuple[float, float]]) -> Optional[float]:
        out = self._convert_once(amount, edge, prices)
        if out is None:
            return None
        return out * (1 - self.fee_rate)

    def _enumerate_triangles(self, graph: Dict[str, Dict[str, Edge]], max_assets: int = 120):
        neighbors = list(graph.get("USDT", {}).keys())[:max_assets]
        for a in neighbors:
            e1 = graph["USDT"].get(a)
            if not e1:
                continue
            for b, e2 in graph.get(a, {}).items():
                if b == "USDT":
                    continue
                e3 = graph.get(b, {}).get("USDT")
                if e2 and e3:
                    yield ("USDT", a, b, "USDT"), (e1, e2, e3)

    def _eval_cycle(self, start_usdt: float, edges: Tuple[Edge, Edge, Edge], prices: Dict[str, Tuple[float, float]]):
        gross = start_usdt
        net = start_usdt
        for edge in edges:
            gross = self._convert_once(gross, edge, prices)
            net = self._convert_with_fee(net, edge, prices)
            if gross is None or net is None:
                return None
        fees = max(0.0, gross - net)
        net_profit = net - start_usdt
        profit_pct = (net_profit / start_usdt) * 100 if start_usdt > 0 else 0.0
        return gross, net, fees, net_profit, profit_pct

    def scan(self, start_usdt: float, max_paths: int = 40, max_assets: int = 120, min_clean_profit_usdt: float = 0.0) -> ScanOutput:
        t0 = time.time()
        symbols = self.fetch_exchange_info()
        prices = self.fetch_book_tickers()
        graph = self._build_edges(symbols)

        all_rows: List[ArbitrageResult] = []
        scanned = 0
        for path, edges in self._enumerate_triangles(graph, max_assets=max_assets):
            scanned += 1
            cycle = self._eval_cycle(start_usdt, edges, prices)
            if cycle is None:
                continue
            gross, net, fees, net_profit, profit_pct = cycle
            all_rows.append(
                ArbitrageResult(
                    path=path, symbols=(edges[0].symbol, edges[1].symbol, edges[2].symbol),
                    sides=(edges[0].side, edges[1].side, edges[2].side),
                    gross_final_usdt=gross, final_usdt=net, total_fees_usdt=fees,
                    net_profit_usdt=net_profit, profit_pct=profit_pct,
                    is_clean_profitable=net_profit > min_clean_profit_usdt,
                )
            )

        clean = [r for r in all_rows if r.is_clean_profitable]
        clean.sort(key=lambda x: x.profit_pct, reverse=True)
        profits = [r.profit_pct for r in all_rows]
        median = 0.0
        if profits:
            s = sorted(profits)
            median = s[len(s) // 2]

        stats = ScanStats(
            market_type=self.market_type, testnet=self.testnet, scanned_paths=scanned,
            valid_paths=len(all_rows), clean_profitable_paths=len(clean),
            success_rate_pct=(len(clean) / len(all_rows) * 100.0) if all_rows else 0.0,
            best_profit_pct=max(profits) if profits else 0.0,
            avg_profit_pct=(sum(profits) / len(profits)) if profits else 0.0,
            median_profit_pct=median, scan_ms=int((time.time() - t0) * 1000),
        )
        return ScanOutput(opportunities=clean[:max_paths], stats=stats)

    @staticmethod
    def simulate_compound_plan(initial_capital_usdt: float, per_cycle_net_pct: float, cycles: int, trigger_multiple: float = 2.0, compound_stake_pct: float = 0.10, pre_trigger_stake_pct: float = 1.0) -> CompoundPlanResult:
        capital = initial_capital_usdt
        trigger = initial_capital_usdt * trigger_multiple
        reached = False
        trigger_cycle = -1
        for i in range(1, cycles + 1):
            if capital >= trigger:
                if not reached:
                    reached = True
                    trigger_cycle = i
                stake_pct = compound_stake_pct
            else:
                stake_pct = pre_trigger_stake_pct
            capital += (capital * stake_pct) * (per_cycle_net_pct / 100.0)
        return CompoundPlanResult(
            initial_capital=initial_capital_usdt, current_capital=capital,
            cycles_simulated=cycles, trigger_reached=reached, trigger_cycle=trigger_cycle,
            stake_mode=f"pre:{pre_trigger_stake_pct*100:.1f}% | post:{compound_stake_pct*100:.1f}%",
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
        except Exception as exc:
            return False, f"Error conectividad Binance: {exc}"


class AISignalEngine:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def _fetch_binance(self, symbol: str, interval: str, limit: int) -> List[list]:
        q = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
        rows = http_get_json(f"https://api.binance.com/api/v3/klines?{q}", timeout=self.timeout)
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Sin klines Binance")
        return rows

    def run(self, symbol: str, interval: str, limit: int, exchange: str = "binance") -> Dict[str, float | str]:
        rows = self._fetch_binance(symbol, interval, limit)
        closes = [float(r[4]) for r in rows]
        ts = int(rows[-1][0])
        if len(closes) < 40:
            raise RuntimeError("Datos insuficientes para IA")
        short = sum(closes[-8:]) / 8
        long_ = sum(closes[-21:]) / 21
        momentum = (short / long_) - 1.0 if long_ else 0.0
        rets = []
        for i in range(1, min(30, len(closes))):
            prev = closes[-i - 1]
            cur = closes[-i]
            if prev > 0:
                rets.append((cur / prev) - 1.0)
        vol = math.sqrt(sum(r * r for r in rets) / len(rets)) if rets else 0.0
        score = momentum * 120 - vol * 8
        prob_up = 1.0 / (1.0 + math.exp(-max(-8.0, min(8.0, score))))
        signal = "BUY" if prob_up >= 0.58 else "SELL" if prob_up <= 0.42 else "HOLD"
        return {
            "exchange": exchange, "symbol": symbol, "interval": interval,
            "rows": len(closes), "last_close": closes[-1],
            "last_candle_utc": datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).isoformat(),
            "test_accuracy": round(max(0.5, 1.0 - vol * 30), 4),
            "probability_up": round(prob_up, 4), "signal": signal,
        }


# ==========================
# PALETTE & CONSTANTS
# ==========================

COLORS = {
    # Deep space background layers
    "bg":           "#07090f",
    "bg2":          "#0c0f1a",
    "surface":      "#0f1624",
    "surface2":     "#141d2e",
    "surface3":     "#19243a",
    "surface4":     "#1e2d47",
    # Borders with glow potential
    "border":       "#1a3050",
    "border2":      "#223a5e",
    "border_glow":  "#00a8cc",
    # Cyan accent family
    "accent":       "#00c8f0",
    "accent_bright":"#30e0ff",
    "accent2":      "#0090b8",
    "accent3":      "#004f70",
    "accent_glow":  "#003d55",
    # Signal colors
    "green":        "#00e5a0",
    "green_bright": "#30ffb8",
    "green_dim":    "#0d3a28",
    "green_glow":   "#001f15",
    "red":          "#ff3366",
    "red_bright":   "#ff6088",
    "red_dim":      "#3a0f1e",
    "red_glow":     "#1f0010",
    "yellow":       "#ffc200",
    "yellow_bright":"#ffd740",
    "yellow_dim":   "#3a2800",
    "purple":       "#b388ff",
    "purple_dim":   "#2a1a4a",
    # Typography
    "text":         "#dce8f5",
    "text_bright":  "#f0f8ff",
    "text_dim":     "#6888a8",
    "text_muted":   "#334460",
    "white":        "#ffffff",
}

# Sidebar width
SIDEBAR_W = 230

FONT_MONO    = ("Consolas", 10)
FONT_MONO_SM = ("Consolas", 9)
FONT_MONO_LG = ("Consolas", 11, "bold")
FONT_HEADING = ("Segoe UI Semibold", 15, "bold")
FONT_SUBHEADING = ("Segoe UI", 11, "bold")
FONT_BODY    = ("Segoe UI", 10)
FONT_SMALL   = ("Segoe UI", 9)
FONT_TINY    = ("Segoe UI", 8)
FONT_MICRO   = ("Segoe UI", 7)
FONT_KPI_LG  = ("Segoe UI", 22, "bold")
FONT_KPI_SM  = ("Segoe UI", 16, "bold")
FONT_LABEL   = ("Segoe UI", 9)


# ==========================
# CUSTOM WIDGETS
# ==========================

def draw_rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Draw a filled rounded rectangle on a Canvas."""
    fill = kw.get("fill", "")
    outline = kw.get("outline", "")
    w = kw.get("width", 1)
    canvas.create_arc(x1,     y1,     x1+2*r, y1+2*r, start=90,  extent=90,  style="pieslice", fill=fill, outline=fill)
    canvas.create_arc(x2-2*r, y1,     x2,     y1+2*r, start=0,   extent=90,  style="pieslice", fill=fill, outline=fill)
    canvas.create_arc(x1,     y2-2*r, x1+2*r, y2,     start=180, extent=90,  style="pieslice", fill=fill, outline=fill)
    canvas.create_arc(x2-2*r, y2-2*r, x2,     y2,     start=270, extent=90,  style="pieslice", fill=fill, outline=fill)
    canvas.create_rectangle(x1+r, y1,   x2-r, y2,   fill=fill, outline="")
    canvas.create_rectangle(x1,   y1+r, x2,   y2-r, fill=fill, outline="")
    if outline:
        # Draw outline arcs
        canvas.create_arc(x1,     y1,     x1+2*r, y1+2*r, start=90,  extent=90,  style="arc", outline=outline, width=w)
        canvas.create_arc(x2-2*r, y1,     x2,     y1+2*r, start=0,   extent=90,  style="arc", outline=outline, width=w)
        canvas.create_arc(x1,     y2-2*r, x1+2*r, y2,     start=180, extent=90,  style="arc", outline=outline, width=w)
        canvas.create_arc(x2-2*r, y2-2*r, x2,     y2,     start=270, extent=90,  style="arc", outline=outline, width=w)
        canvas.create_line(x1+r, y1,   x2-r, y1,   fill=outline, width=w)
        canvas.create_line(x1+r, y2,   x2-r, y2,   fill=outline, width=w)
        canvas.create_line(x1,   y1+r, x1,   y2-r, fill=outline, width=w)
        canvas.create_line(x2,   y1+r, x2,   y2-r, fill=outline, width=w)


class GlowButton(tk.Canvas):
    """Premium button with glow effect and smooth hover."""
    _STYLES = {
        "primary": {
            "bg_n": "#003a52", "bg_h": "#004e6e", "bg_a": "#002d40",
            "fg": "#00c8f0", "border_n": "#006a8a", "border_h": "#00c8f0",
            "glow": "#00304a",
        },
        "success": {
            "bg_n": "#002e1e", "bg_h": "#003d28", "bg_a": "#001f14",
            "fg": "#00e5a0", "border_n": "#005a3a", "border_h": "#00e5a0",
            "glow": "#001a10",
        },
        "danger": {
            "bg_n": "#350b18", "bg_h": "#4a1024", "bg_a": "#250810",
            "fg": "#ff3366", "border_n": "#7a1535", "border_h": "#ff3366",
            "glow": "#1a0510",
        },
        "warn": {
            "bg_n": "#2e1f00", "bg_h": "#3d2b00", "bg_a": "#201500",
            "fg": "#ffc200", "border_n": "#6a4800", "border_h": "#ffc200",
            "glow": "#1a1000",
        },
        "ghost": {
            "bg_n": "#141d2e", "bg_h": "#1a2840", "bg_a": "#0f1624",
            "fg": "#6888a8", "border_n": "#1a3050", "border_h": "#2a4868",
            "glow": "#0c1520",
        },
        "purple": {
            "bg_n": "#1e0f3a", "bg_h": "#2a1550", "bg_a": "#150a28",
            "fg": "#b388ff", "border_n": "#4a2090", "border_h": "#b388ff",
            "glow": "#120820",
        },
    }

    def __init__(self, parent, text, command=None, style="primary", width=148, height=36, icon="", **kwargs):
        super().__init__(parent, width=width, height=height, bd=0, highlightthickness=0,
                         bg=COLORS["bg"], cursor="hand2")
        self.command = command
        self._style_key = style
        self.label = text
        self.icon = icon
        self.w = width
        self.h = height
        self._hover = False
        self._press = False
        self._disabled = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self._STYLES.get(self._style_key, self._STYLES["primary"])
        if self._disabled:
            bg, fg, bd = COLORS["surface2"], COLORS["text_muted"], COLORS["border"]
        elif self._press:
            bg, fg, bd = s["bg_a"], s["fg"], s["border_h"]
        elif self._hover:
            bg, fg, bd = s["bg_h"], s["fg"], s["border_h"]
            # Outer glow when hovered
            draw_rounded_rect(self, -2, -2, self.w+2, self.h+2, 8, fill=s["glow"], outline="")
        else:
            bg, fg, bd = s["bg_n"], s["fg"], s["border_n"]

        draw_rounded_rect(self, 1, 1, self.w-1, self.h-1, 7, fill=bg, outline=bd)

        # Top shine line
        if not self._disabled and not self._press:
            shine = "#ffffff" if self._hover else "#ffffff"
            self.create_line(9, 2, self.w-9, 2, fill=shine, width=1,
                             stipple="" if self._hover else "")

        full_text = f"{self.icon}  {self.label}" if self.icon else self.label
        self.create_text(self.w // 2, self.h // 2, text=full_text, fill=fg,
                         font=("Segoe UI", 9, "bold"), anchor="center")

    def _on_enter(self, e):
        if not self._disabled:
            self._hover = True; self._draw()

    def _on_leave(self, e):
        self._hover = False; self._press = False; self._draw()

    def _on_press(self, e):
        if not self._disabled:
            self._press = True; self._draw()

    def _on_release(self, e):
        if not self._disabled:
            self._press = False; self._draw()
            if self.command: self.command()

    def configure(self, **kwargs):
        if "state" in kwargs:
            self._disabled = (kwargs.pop("state") == "disabled")
            self._draw()
        if "text" in kwargs:
            self.label = kwargs.pop("text"); self._draw()
        super().configure(**kwargs)


class KPICard(tk.Canvas):
    """Sleek metric card with canvas-drawn gradient border and glow."""
    def __init__(self, parent, title, value_var, unit="", accent_color=None,
                 icon="", width=170, height=90, **kwargs):
        super().__init__(parent, width=width, height=height, bd=0,
                         highlightthickness=0, bg=COLORS["bg"])
        self._accent = accent_color or COLORS["accent"]
        self._title  = title.upper()
        self._unit   = unit
        self._icon   = icon
        # Avoid shadowing tkinter's internal widget name attribute `self._w`
        self._width_px  = width
        self._height_px = height
        self._var    = value_var
        self._draw_bg()
        # Live value label via trace
        self._val_id = self.create_text(width//2, height//2 + 8, text="—",
                                        fill=self._accent, font=FONT_KPI_SM, anchor="center")
        self._title_id = self.create_text(width//2, 20, text=self._title,
                                          fill=COLORS["text_muted"], font=FONT_MICRO, anchor="center")
        if unit:
            self.create_text(width//2, height - 14, text=unit, fill=COLORS["text_muted"],
                             font=FONT_MICRO, anchor="center")
        value_var.trace_add("write", lambda *_: self._update_val())
        self._update_val()

    def _draw_bg(self):
        w, h = self._width_px, self._height_px
        # Outer dark container
        draw_rounded_rect(self, 0, 0, w, h, 8, fill=COLORS["surface2"], outline="")
        # Subtle inner border
        draw_rounded_rect(self, 1, 1, w-1, h-1, 7, fill="", outline=COLORS["border"])
        # Accent top stripe
        self.create_rectangle(8, 1, w-8, 3, fill=self._accent, outline="")
        # Corner dots accent
        self.create_oval(w-10, 4, w-4, 10, fill=self._accent, outline="")

    def _update_val(self):
        try:
            val = self._var.get()
            self.itemconfig(self._val_id, text=val)
        except Exception:
            pass


class StyledEntry(tk.Frame):
    """Dark entry field with animated focus border."""
    def __init__(self, parent, textvariable=None, show=None, width=None, **kwargs):
        super().__init__(parent, bg=COLORS["border"], bd=0, padx=1, pady=1)
        inner = tk.Frame(self, bg=COLORS["surface3"])
        inner.pack(fill="both", expand=True)
        kw = dict(textvariable=textvariable, bg=COLORS["surface3"], fg=COLORS["text"],
                  insertbackground=COLORS["accent"], relief="flat", font=FONT_BODY,
                  bd=0, highlightthickness=0)
        if show:   kw["show"] = show
        if width:  kw["width"] = width
        self.entry = tk.Entry(inner, **kw)
        self.entry.pack(fill="both", expand=True, ipady=7, ipadx=10)
        self.entry.bind("<FocusIn>",  lambda e: self.configure(bg=COLORS["accent"]))
        self.entry.bind("<FocusOut>", lambda e: self.configure(bg=COLORS["border"]))

    def configure(self, **kwargs):
        if "show" in kwargs: self.entry.configure(show=kwargs.pop("show"))
        super().configure(**kwargs)


class StyledCombobox(tk.Frame):
    """Custom dropdown with dark popup."""
    def __init__(self, parent, textvariable=None, values=(), **kwargs):
        super().__init__(parent, bg=COLORS["border"], bd=0, padx=1, pady=1)
        self.var    = textvariable
        self.values = list(values)
        self._open  = False
        self._evar  = textvariable or tk.StringVar()
        inner = tk.Frame(self, bg=COLORS["surface3"])
        inner.pack(fill="both", expand=True)
        self.entry = tk.Entry(inner, textvariable=self._evar, bg=COLORS["surface3"],
                              fg=COLORS["text"], relief="flat", font=FONT_BODY, bd=0,
                              highlightthickness=0, state="readonly",
                              readonlybackground=COLORS["surface3"], cursor="hand2")
        self.entry.pack(side="left", fill="both", expand=True, ipady=7, ipadx=10)
        arrow = tk.Label(inner, text="▾", font=("Segoe UI", 9), fg=COLORS["accent"],
                         bg=COLORS["surface3"], cursor="hand2", padx=8)
        arrow.pack(side="right")
        for w in [self.entry, arrow]: w.bind("<Button-1>", self._toggle)

    def _toggle(self, e=None):
        self._close() if self._open else self._open_menu()

    def _open_menu(self):
        self._open = True
        self.configure(bg=COLORS["accent"])
        root = self.winfo_toplevel()
        self._popup = tk.Toplevel(root)
        self._popup.overrideredirect(True)
        self._popup.configure(bg=COLORS["border"])
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        w = max(self.winfo_width(), 140)
        h = len(self.values) * 32 + 4
        self._popup.geometry(f"{w}x{h}+{x}+{y}")
        self._popup.attributes("-topmost", True)
        tk.Frame(self._popup, bg=COLORS["border"], height=1).pack(fill="x")
        for val in self.values:
            lbl = tk.Label(self._popup, text=val, bg=COLORS["surface3"], fg=COLORS["text"],
                           font=FONT_BODY, anchor="w", padx=12, cursor="hand2")
            lbl.pack(fill="x", ipady=5)
            lbl.bind("<Enter>", lambda e, l=lbl: l.configure(bg=COLORS["accent3"], fg=COLORS["accent_bright"]))
            lbl.bind("<Leave>", lambda e, l=lbl: l.configure(bg=COLORS["surface3"], fg=COLORS["text"]))
            lbl.bind("<Button-1>", lambda e, v=val: self._select(v))
        self._popup.bind("<FocusOut>", lambda e: self._close())

    def _close(self):
        self._open = False
        self.configure(bg=COLORS["border"])
        if hasattr(self, "_popup") and self._popup.winfo_exists():
            self._popup.destroy()

    def _select(self, val):
        self._evar.set(val)
        if self.var and self.var != self._evar: self.var.set(val)
        self._close()

    def get(self): return self._evar.get()


class ActivityLog(tk.Frame):
    """Terminal-style log with syntax highlighting."""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=COLORS["surface"], highlightthickness=1,
                         highlightbackground=COLORS["border"], **kwargs)
        # Header bar
        hdr = tk.Frame(self, bg=COLORS["surface2"], padx=12, pady=5)
        hdr.pack(fill="x")
        dot_row = tk.Frame(hdr, bg=COLORS["surface2"])
        dot_row.pack(side="left")
        for color in [COLORS["red"], COLORS["yellow"], COLORS["green"]]:
            tk.Label(dot_row, text="●", font=("Segoe UI", 8), fg=color,
                     bg=COLORS["surface2"]).pack(side="left", padx=2)
        tk.Label(hdr, text="ACTIVITY  LOG", font=FONT_TINY, fg=COLORS["text_muted"],
                 bg=COLORS["surface2"], padx=8).pack(side="left")
        # Text area
        scr = tk.Frame(self, bg=COLORS["surface"])
        scr.pack(fill="both", expand=True)
        self.text = tk.Text(scr, bg=COLORS["surface"], fg=COLORS["text"],
                            insertbackground=COLORS["accent"], relief="flat", bd=0,
                            font=FONT_MONO_SM, wrap="word", padx=12, pady=8,
                            highlightthickness=0, selectbackground=COLORS["accent3"],
                            spacing1=2, spacing3=2)
        vsb = tk.Scrollbar(scr, orient="vertical", command=self.text.yview,
                           bg=COLORS["surface2"], troughcolor=COLORS["surface"],
                           activebackground=COLORS["accent"], relief="flat", width=6)
        self.text.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for tag, fg in [("INFO", COLORS["accent"]), ("OK", COLORS["green"]),
                        ("WARN", COLORS["yellow"]), ("ERROR", COLORS["red"]),
                        ("TIME", COLORS["text_muted"]), ("DIM", COLORS["text_muted"]),
                        ("PREFIX", COLORS["surface4"])]:
            self.text.tag_configure(tag, foreground=fg)

    def log(self, message: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.text.insert("end", f"  {ts} ", "TIME")
        lmap = {"INFO": "·", "OK": "✓", "WARN": "⚠", "ERROR": "✗"}
        sym = lmap.get(level, "·")
        self.text.insert("end", f"{sym} ", level if level in lmap else "DIM")
        self.text.insert("end", f"{message}\n")
        lines = int(float(self.text.index("end-1c").split(".")[0]))
        if lines > 800: self.text.delete("1.0", f"{lines - 600}.0")
        self.text.see("end")

    def clear(self): self.text.delete("1.0", tk.END)


class StyledTreeview(tk.Frame):
    """Dark treeview with alternating row colors and custom scrollbar."""
    def __init__(self, parent, columns, headings, widths, anchors=None, height=12, **kwargs):
        super().__init__(parent, bg=COLORS["bg"], bd=0,
                         highlightthickness=1, highlightbackground=COLORS["border"])
        uid = f"T{id(self)}.Treeview"
        s = ttk.Style()
        s.configure(uid,
                    background=COLORS["surface"],
                    fieldbackground=COLORS["surface"],
                    foreground=COLORS["text"],
                    rowheight=30,
                    borderwidth=0,
                    font=FONT_MONO_SM)
        s.configure(f"{uid}.Heading",
                    background=COLORS["surface2"],
                    foreground=COLORS["text_muted"],
                    font=("Segoe UI", 8, "bold"),
                    relief="flat", borderwidth=0)
        s.map(uid,
              background=[("selected", COLORS["accent3"])],
              foreground=[("selected", COLORS["accent_bright"])])
        s.layout(uid, [("Treeview.treearea", {"sticky": "nswe"})])

        self.tree = ttk.Treeview(self, style=uid, columns=columns,
                                 show="headings", height=height)
        anchors = anchors or ["w"] * len(columns)
        for col, head, w, anch in zip(columns, headings, widths, anchors):
            self.tree.heading(col, text=head.upper(), anchor="center")
            self.tree.column(col, width=w, anchor=anch, stretch=False)

        vsb = tk.Scrollbar(self, orient="vertical", command=self.tree.yview,
                           bg=COLORS["surface2"], troughcolor=COLORS["surface"],
                           activebackground=COLORS["accent"], relief="flat", width=6)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        # Tag styles
        self.tree.tag_configure("profit",  foreground=COLORS["green"])
        self.tree.tag_configure("loss",    foreground=COLORS["red"])
        self.tree.tag_configure("neutral", foreground=COLORS["text_dim"])
        self.tree.tag_configure("best",    background="#041c2e", foreground=COLORS["accent"])
        self.tree.tag_configure("odd",     background=COLORS["surface2"])
        self.tree.tag_configure("even",    background=COLORS["surface"])

    def clear(self):
        for item in self.tree.get_children(): self.tree.delete(item)

    def insert(self, values, tags=()):
        n = len(self.tree.get_children())
        row_tag = "odd" if n % 2 else "even"
        final_tags = tuple(tags) + (row_tag,)
        self.tree.insert("", "end", values=values, tags=final_tags)

    def bind(self, seq, func): self.tree.bind(seq, func)


def scrollable_frame(parent):
    canvas = tk.Canvas(parent, bg=COLORS["bg"], bd=0, highlightthickness=0)
    vsb = tk.Scrollbar(parent, orient="vertical", command=canvas.yview,
                       bg=COLORS["surface2"], troughcolor=COLORS["bg"],
                       activebackground=COLORS["accent"], relief="flat", width=6)
    canvas.configure(yscrollcommand=vsb.set)
    inner = tk.Frame(canvas, bg=COLORS["bg"])
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    def _cfg(e):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfig(win, width=canvas.winfo_width())
    inner.bind("<Configure>", _cfg)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1*(e.delta//120), "units"))
    return inner


# ==========================
# SIDEBAR NAV
# ==========================

class Sidebar(tk.Frame):
    def __init__(self, parent, on_navigate, **kwargs):
        super().__init__(parent, bg=COLORS["surface"], width=SIDEBAR_W, **kwargs)
        self.pack_propagate(False)
        self.on_navigate = on_navigate
        self._active = None
        self._buttons = {}
        self._build()

    def _build(self):
        # Logo area with canvas-drawn accent
        logo_canvas = tk.Canvas(self, width=SIDEBAR_W, height=80, bd=0,
                                highlightthickness=0, bg=COLORS["surface"])
        logo_canvas.pack(fill="x")
        # Cyan accent strip
        logo_canvas.create_rectangle(0, 0, SIDEBAR_W, 2, fill=COLORS["accent"], outline="")
        # Glow behind logo text
        logo_canvas.create_rectangle(14, 18, SIDEBAR_W-14, 62,
                                     fill=COLORS["accent_glow"], outline=COLORS["border"], width=1)
        logo_canvas.create_text(SIDEBAR_W//2, 34, text="◈  HFT ARB",
                                font=("Segoe UI", 13, "bold"), fill=COLORS["accent"], anchor="center")
        logo_canvas.create_text(SIDEBAR_W//2, 52, text="Crypto Arbitrage System",
                                font=FONT_MICRO, fill=COLORS["text_muted"], anchor="center")

        # Divider
        tk.Frame(self, bg=COLORS["border"], height=1).pack(fill="x")

        # Network badge
        badge_frame = tk.Frame(self, bg=COLORS["surface"], pady=10, padx=16)
        badge_frame.pack(fill="x")
        badge = tk.Frame(badge_frame, bg=COLORS["green_dim"],
                         highlightthickness=1, highlightbackground=COLORS["green"])
        badge.pack(fill="x")
        tk.Label(badge, text="● TESTNET  CONNECTED", font=FONT_MICRO,
                 fg=COLORS["green"], bg=COLORS["green_dim"], pady=4).pack()

        # Section label
        tk.Label(self, text="  NAVIGATION", font=FONT_MICRO,
                 fg=COLORS["text_muted"], bg=COLORS["surface"],
                 anchor="w").pack(fill="x", pady=(10, 4))

        nav_items = [
            ("scanner", "⬡", "Scanner",        "Buscar oportunidades"),
            ("config",  "⚙", "Configuración",  "API y parámetros"),
            ("stats",   "▲", "Estadísticas",   "Historial y análisis"),
            ("exec",    "⚡","Ejecución",       "Trading en vivo"),
        ]

        nav_frame = tk.Frame(self, bg=COLORS["surface"])
        nav_frame.pack(fill="x", padx=8)

        for key, icon, label, hint in nav_items:
            btn = self._nav_btn(nav_frame, key, icon, label, hint)
            btn.pack(fill="x", pady=2)
            self._buttons[key] = btn

        # Spacer
        tk.Frame(self, bg=COLORS["surface"]).pack(fill="both", expand=True)

        # Bottom section label
        tk.Label(self, text="  SYSTEM", font=FONT_MICRO,
                 fg=COLORS["text_muted"], bg=COLORS["surface"],
                 anchor="w").pack(fill="x", pady=(0, 4))

        # Hotkeys quick reference
        ref = tk.Frame(self, bg=COLORS["surface2"], padx=12, pady=10)
        ref.pack(fill="x")
        for shortcut, action in [("F5", "Escanear"), ("Ctrl+S", "Guardar"), ("Esc", "Stop auto")]:
            row = tk.Frame(ref, bg=COLORS["surface2"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=shortcut, font=FONT_MICRO, fg=COLORS["accent"],
                     bg=COLORS["surface2"], width=8, anchor="w").pack(side="left")
            tk.Label(row, text=action, font=FONT_MICRO, fg=COLORS["text_muted"],
                     bg=COLORS["surface2"], anchor="w").pack(side="left")

        # Footer
        footer = tk.Frame(self, bg=COLORS["bg"], pady=8, padx=12)
        footer.pack(fill="x", side="bottom")
        tk.Label(footer, text="v2.1 · Python 3.10+", font=FONT_MICRO,
                 fg=COLORS["text_muted"], bg=COLORS["bg"]).pack(anchor="w")

        self.select("scanner")

    def _nav_btn(self, parent, key, icon, label, hint=""):
        outer = tk.Frame(parent, bg=COLORS["surface"], cursor="hand2")
        # Left indicator bar (initially hidden)
        indicator = tk.Frame(outer, bg=COLORS["surface"], width=3)
        indicator.pack(side="left", fill="y")
        # Main content
        inner = tk.Frame(outer, bg=COLORS["surface"], padx=8, pady=10)
        inner.pack(side="left", fill="x", expand=True)
        icon_lbl = tk.Label(inner, text=icon, font=("Segoe UI", 11), bg=COLORS["surface"],
                            fg=COLORS["text_muted"], width=2, anchor="center")
        icon_lbl.pack(side="left", padx=(0, 8))
        text_frame = tk.Frame(inner, bg=COLORS["surface"])
        text_frame.pack(side="left", fill="x", expand=True)
        name_lbl = tk.Label(text_frame, text=label, font=("Segoe UI", 9, "bold"),
                            bg=COLORS["surface"], fg=COLORS["text_dim"], anchor="w")
        name_lbl.pack(anchor="w")
        hint_lbl = tk.Label(text_frame, text=hint, font=FONT_MICRO,
                            bg=COLORS["surface"], fg=COLORS["text_muted"], anchor="w")
        hint_lbl.pack(anchor="w")

        outer._indicator = indicator
        outer._icon = icon_lbl
        outer._name = name_lbl
        outer._hint = hint_lbl
        outer._inner = inner
        outer._text_frame = text_frame

        all_widgets = [outer, inner, icon_lbl, text_frame, name_lbl, hint_lbl]

        def on_click(e=None):
            self.select(key)
            self.on_navigate(key)

        def on_enter(e=None):
            if key != self._active:
                for w in all_widgets:
                    w.configure(bg=COLORS["surface2"])

        def on_leave(e=None):
            if key != self._active:
                for w in all_widgets:
                    w.configure(bg=COLORS["surface"])

        for w in all_widgets:
            w.bind("<Button-1>", on_click)
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)

        return outer

    def select(self, key):
        # Deactivate old
        if self._active and self._active in self._buttons:
            old = self._buttons[self._active]
            bg = COLORS["surface"]
            for w in [old, old._inner, old._icon, old._name, old._hint, old._text_frame]:
                w.configure(bg=bg)
            old._indicator.configure(bg=bg)
            old._name.configure(fg=COLORS["text_dim"], font=("Segoe UI", 9, "bold"))
            old._icon.configure(fg=COLORS["text_muted"])
        self._active = key
        if key in self._buttons:
            btn = self._buttons[key]
            bg = COLORS["surface3"]
            for w in [btn, btn._inner, btn._icon, btn._name, btn._hint, btn._text_frame]:
                w.configure(bg=bg)
            btn._indicator.configure(bg=COLORS["accent"])
            btn._name.configure(fg=COLORS["accent_bright"], font=("Segoe UI", 9, "bold"))
            btn._icon.configure(fg=COLORS["accent"])


# ==========================
# STATUS BAR
# ==========================

class StatusBar(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=COLORS["surface"], height=28, **kwargs)
        self.pack_propagate(False)
        # Left accent line
        tk.Frame(self, bg=COLORS["border"], width=1).pack(side="left", fill="y")
        # Pulse dot
        self._dot = tk.Label(self, text="◉", font=("Segoe UI", 9), fg=COLORS["text_muted"],
                             bg=COLORS["surface"])
        self._dot.pack(side="left", padx=(10, 5))
        # Status text
        self._status = tk.Label(self, text="Sistema listo", font=FONT_SMALL,
                                fg=COLORS["text_dim"], bg=COLORS["surface"], anchor="w")
        self._status.pack(side="left", fill="x", expand=True)
        # Right section: market badge + clock
        right = tk.Frame(self, bg=COLORS["surface"])
        right.pack(side="right", padx=12)
        self._market_badge = tk.Label(right, text="SPOT · TESTNET", font=FONT_MICRO,
                                      fg=COLORS["accent"], bg=COLORS["accent_glow"],
                                      padx=6, pady=2)
        self._market_badge.pack(side="left", padx=(0, 12))
        self._time = tk.Label(right, text="", font=FONT_MONO_SM,
                              fg=COLORS["text_muted"], bg=COLORS["surface"])
        self._time.pack(side="left")
        # Top border
        tk.Frame(self, bg=COLORS["border"], height=1).pack(side="top", fill="x")
        self._pulse_state = False
        self._update_clock()

    def _update_clock(self):
        self._time.configure(text=datetime.now().strftime("%H:%M:%S"))
        self.after(1000, self._update_clock)

    def set(self, msg: str, level: str = "info"):
        self._status.configure(text=f"  {msg}")
        color_map = {
            "info":       COLORS["text_dim"],
            "success":    COLORS["green"],
            "error":      COLORS["red"],
            "warn":       COLORS["yellow"],
            "processing": COLORS["accent"],
        }
        dot_map = {
            "info":       COLORS["text_muted"],
            "success":    COLORS["green"],
            "error":      COLORS["red"],
            "warn":       COLORS["yellow"],
            "processing": COLORS["accent"],
        }
        self._status.configure(fg=color_map.get(level, COLORS["text_dim"]))
        self._dot.configure(fg=dot_map.get(level, COLORS["text_muted"]))

    def start_pulse(self):
        self._pulse_state = True
        self._pulse()

    def stop_pulse(self):
        self._pulse_state = False
        self._dot.configure(fg=COLORS["text_muted"])

    def _pulse(self):
        if not self._pulse_state:
            return
        cur = self._dot.cget("fg")
        self._dot.configure(fg=COLORS["accent"] if cur != COLORS["accent"] else COLORS["bg"])
        self.after(300, self._pulse)


# ==========================
# TOAST
# ==========================

class Toast:
    def __init__(self, root):
        self.root = root
        self._current: Optional[tk.Toplevel] = None

    def show(self, message: str, level: str = "info"):
        if self._current and self._current.winfo_exists():
            self._current.destroy()
        accent = {
            "info":    COLORS["accent"],
            "success": COLORS["green"],
            "error":   COLORS["red"],
            "warn":    COLORS["yellow"],
        }.get(level, COLORS["accent"])
        bg_dim = {
            "info":    COLORS["accent_glow"],
            "success": COLORS["green_glow"],
            "error":   COLORS["red_glow"],
            "warn":    COLORS["yellow_dim"],
        }.get(level, COLORS["accent_glow"])

        tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.attributes("-alpha", 0.97)
        x = self.root.winfo_rootx() + self.root.winfo_width() - 350
        y = self.root.winfo_rooty() + 50
        tw.geometry(f"340x52+{x}+{y}")
        tw.configure(bg=COLORS["surface2"])
        # Top border glow
        tk.Frame(tw, bg=accent, height=2).pack(fill="x", side="top")
        body = tk.Frame(tw, bg=COLORS["surface2"])
        body.pack(fill="both", expand=True)
        # Left color bar
        tk.Frame(body, bg=accent, width=4).pack(side="left", fill="y")
        # Icon
        icons = {"info": "◆", "success": "✓", "error": "✗", "warn": "⚠"}
        tk.Label(body, text=icons.get(level, "◆"), font=("Segoe UI", 11), fg=accent,
                 bg=COLORS["surface2"], padx=10).pack(side="left")
        tk.Label(body, text=message, bg=COLORS["surface2"], fg=COLORS["text"],
                 font=FONT_SMALL, anchor="w").pack(side="left", fill="x", expand=True)
        self._current = tw
        self.root.after(2600, lambda: tw.destroy() if tw.winfo_exists() else None)


# ==========================
# PAGES
# ==========================

class PageScanner(tk.Frame):
    def __init__(self, parent, app, **kwargs):
        super().__init__(parent, bg=COLORS["bg"], **kwargs)
        self.app = app
        self._build()

    def _build(self):
        # ── Top header bar ──────────────────────────────────────────
        hdr = tk.Frame(self, bg=COLORS["surface"], pady=0)
        hdr.pack(fill="x")
        # Accent top line
        tk.Frame(hdr, bg=COLORS["accent"], height=2).pack(fill="x")
        hdr_inner = tk.Frame(hdr, bg=COLORS["surface"], padx=20, pady=12)
        hdr_inner.pack(fill="x")

        # Title block
        title_block = tk.Frame(hdr_inner, bg=COLORS["surface"])
        title_block.pack(side="left")
        tk.Label(title_block, text="Arbitrage Scanner", font=FONT_HEADING,
                 fg=COLORS["text_bright"], bg=COLORS["surface"]).pack(anchor="w")
        tk.Label(title_block, text="Triangular USDT · Binance Real-time",
                 font=FONT_SMALL, fg=COLORS["text_muted"], bg=COLORS["surface"]).pack(anchor="w")

        # Action buttons
        btn_row = tk.Frame(hdr_inner, bg=COLORS["surface"])
        btn_row.pack(side="right")
        b_scan = GlowButton(btn_row, "Escanear", command=self.app._scan_once,
                            style="primary", width=130, icon="⬡")
        b_scan.pack(side="left", padx=3)
        b_on = GlowButton(btn_row, "Auto ON", command=self.app._start_autoscan,
                          style="success", width=100, icon="▶")
        b_on.pack(side="left", padx=3)
        b_off = GlowButton(btn_row, "Auto OFF", command=self.app._stop_autoscan,
                           style="ghost", width=100, icon="■")
        b_off.pack(side="left", padx=3)
        b_clr = GlowButton(btn_row, "Limpiar", command=self.app._clear_scan_results,
                           style="ghost", width=90, icon="✕")
        b_clr.pack(side="left", padx=3)
        self.app._controls_disable_on_busy += [b_scan, b_on]

        # ── Scan progress bar ────────────────────────────────────────
        prog_frame = tk.Frame(self, bg=COLORS["bg"])
        prog_frame.pack(fill="x")
        self.progress = ttk.Progressbar(prog_frame, mode="indeterminate")
        s = ttk.Style()
        s.configure("Scan.Horizontal.TProgressbar",
                    troughcolor=COLORS["surface2"], background=COLORS["accent"],
                    thickness=3, borderwidth=0)
        self.progress.configure(style="Scan.Horizontal.TProgressbar")
        self.progress.pack(fill="x")

        # ── KPI row ──────────────────────────────────────────────────
        kpi_bg = tk.Frame(self, bg=COLORS["bg"], padx=16, pady=12)
        kpi_bg.pack(fill="x")
        kpi_data = [
            ("Tiempo de Scan",    self.app.kpi_scan_ms,           "ms",  COLORS["accent"]),
            ("Tasa de Éxito",     self.app.kpi_success,           "",    COLORS["green"]),
            ("Mejor Oportunidad", self.app.kpi_best,              "%",   COLORS["yellow"]),
            ("Promedio",          self.app.kpi_avg,               "%",   COLORS["text_dim"]),
            ("Rutas Limpias",     self.app.kpi_vars["opportunities"], "", COLORS["purple"]),
        ]
        for title, var, unit, color in kpi_data:
            card = KPICard(kpi_bg, title, var, unit, accent_color=color, width=175, height=86)
            card.pack(side="left", padx=(0, 10), pady=2)

        # ── Filter toolbar ───────────────────────────────────────────
        toolbar = tk.Frame(self, bg=COLORS["surface2"], padx=16, pady=8)
        toolbar.pack(fill="x", padx=16, pady=(0, 10))
        tk.Label(toolbar, text="⬡", font=("Segoe UI", 10), fg=COLORS["accent"],
                 bg=COLORS["surface2"]).pack(side="left", padx=(0, 6))
        tk.Label(toolbar, text="Filtrar:", font=FONT_SMALL, fg=COLORS["text_muted"],
                 bg=COLORS["surface2"]).pack(side="left")
        StyledEntry(toolbar, textvariable=self.app.search_var, width=26).pack(side="left", padx=8)
        tk.Frame(toolbar, bg=COLORS["border"], width=1).pack(side="left", fill="y", padx=8)
        tk.Label(toolbar, text="Auto-scan (s):", font=FONT_SMALL, fg=COLORS["text_muted"],
                 bg=COLORS["surface2"]).pack(side="left")
        StyledEntry(toolbar, textvariable=self.app.autoscan_interval_var, width=5).pack(side="left", padx=6)
        self.app.search_var.trace_add("write", lambda *_: self.app._apply_filter())

        # Scan counter badge
        self._scan_lbl = tk.Label(toolbar, text="Esperando primer scan...",
                                  font=FONT_MICRO, fg=COLORS["text_muted"], bg=COLORS["surface2"])
        self._scan_lbl.pack(side="right")

        # ── Results table ────────────────────────────────────────────
        tbl_wrap = tk.Frame(self, bg=COLORS["bg"], padx=16, pady=0)
        tbl_wrap.pack(fill="both", expand=True, pady=(0, 10))

        # Table header label
        lbl_row = tk.Frame(tbl_wrap, bg=COLORS["bg"], pady=4)
        lbl_row.pack(fill="x")
        tk.Label(lbl_row, text="OPORTUNIDADES ENCONTRADAS", font=FONT_MICRO,
                 fg=COLORS["accent"], bg=COLORS["bg"]).pack(side="left")

        cols    = ("rank", "path", "symbols", "net_profit", "fees", "gross_final", "net_final", "profit_pct")
        heads   = ("#",    "Ruta de Arbitraje", "Símbolos", "Ganancia Neta", "Comisiones", "Bruto Final", "Neto Final", "% Profit")
        widths  = [44,     240,                  220,        110,             95,            110,           110,          100]
        anchors = ["center","w",                 "w",        "e",             "e",           "e",           "e",          "e"]
        self.app.tree = StyledTreeview(tbl_wrap, cols, heads, widths, anchors, height=19)
        self.app.tree.pack(fill="both", expand=True)
        self.app.tree.bind("<<TreeviewSelect>>", self.app._on_row_select)

    def set_busy(self, busy: bool):
        if busy: self.progress.start(6)
        else:    self.progress.stop()


class PageConfig(tk.Frame):
    def __init__(self, parent, app, **kwargs):
        super().__init__(parent, bg=COLORS["bg"], **kwargs)
        self.app = app
        self._build()

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=COLORS["surface"], pady=0)
        hdr.pack(fill="x")
        tk.Frame(hdr, bg=COLORS["yellow"], height=2).pack(fill="x")
        hdr_inner = tk.Frame(hdr, bg=COLORS["surface"], padx=20, pady=12)
        hdr_inner.pack(fill="x")
        title_block = tk.Frame(hdr_inner, bg=COLORS["surface"])
        title_block.pack(side="left")
        tk.Label(title_block, text="Configuración del Sistema", font=FONT_HEADING,
                 fg=COLORS["text_bright"], bg=COLORS["surface"]).pack(anchor="w")
        tk.Label(title_block, text="API · Mercado · Compuesto · Señal IA",
                 font=FONT_SMALL, fg=COLORS["text_muted"], bg=COLORS["surface"]).pack(anchor="w")
        btn_row = tk.Frame(hdr_inner, bg=COLORS["surface"])
        btn_row.pack(side="right")
        GlowButton(btn_row, "Guardar", command=self.app._save_config,
                   style="warn", width=120, icon="💾").pack(side="left", padx=3)
        GlowButton(btn_row, "Validar API", command=self.app._validate_keys,
                   style="success", width=120, icon="🔗").pack(side="left", padx=3)
        GlowButton(btn_row, "Copiar", command=self.app._copy_config,
                   style="ghost", width=90, icon="📋").pack(side="left", padx=3)

        scroll_area = scrollable_frame(self)

        def section_hdr(parent, title, color=COLORS["accent"], description=""):
            f = tk.Frame(parent, bg=COLORS["bg"], padx=20, pady=16)
            f.pack(fill="x")
            hf = tk.Frame(f, bg=COLORS["bg"])
            hf.pack(fill="x")
            # Color pill
            pill = tk.Frame(hf, bg=color, width=4)
            pill.pack(side="left", fill="y", padx=(0, 10))
            tb = tk.Frame(hf, bg=COLORS["bg"])
            tb.pack(side="left")
            tk.Label(tb, text=title, font=FONT_SUBHEADING, fg=COLORS["text_bright"],
                     bg=COLORS["bg"]).pack(anchor="w")
            if description:
                tk.Label(tb, text=description, font=FONT_MICRO, fg=COLORS["text_muted"],
                         bg=COLORS["bg"]).pack(anchor="w")
            tk.Frame(f, bg=color, height=1).pack(fill="x", pady=(8, 0))
            return f

        def field_grid(parent, items, cols=2):
            """Render a 2-col grid of labeled fields."""
            body = tk.Frame(parent, bg=COLORS["bg"], padx=20)
            body.pack(fill="x", pady=4)
            for i, (lbl, wclass, *args) in enumerate(items):
                col_i = i % cols
                row_i = i // cols
                if col_i == 0:
                    row_frame = tk.Frame(body, bg=COLORS["bg"])
                    row_frame.pack(fill="x", pady=5)
                cell = tk.Frame(row_frame, bg=COLORS["bg"])
                cell.pack(side="left", fill="x", expand=True, padx=(0 if col_i == 0 else 12, 0))
                tk.Label(cell, text=lbl, font=FONT_LABEL, fg=COLORS["text_dim"],
                         bg=COLORS["bg"], anchor="w").pack(fill="x", pady=(0, 3))
                if wclass:
                    w = wclass(cell, *args)
                    w.pack(fill="x")

        # ── API Section ──────────────────────────────────────────────
        section_hdr(scroll_area, "Credenciales API",
                    COLORS["accent"], "Clave y secreto de Binance")
        api_body = tk.Frame(scroll_area, bg=COLORS["bg"], padx=20)
        api_body.pack(fill="x", pady=4)
        r0 = tk.Frame(api_body, bg=COLORS["bg"])
        r0.pack(fill="x", pady=5)
        c0 = tk.Frame(r0, bg=COLORS["bg"])
        c0.pack(fill="x", expand=True)
        tk.Label(c0, text="API Key", font=FONT_LABEL, fg=COLORS["text_dim"],
                 bg=COLORS["bg"], anchor="w").pack(fill="x", pady=(0, 3))
        StyledEntry(c0, textvariable=self.app.api_key_var).pack(fill="x")

        r1 = tk.Frame(api_body, bg=COLORS["bg"])
        r1.pack(fill="x", pady=5)
        c1 = tk.Frame(r1, bg=COLORS["bg"])
        c1.pack(fill="x", expand=True)
        tk.Label(c1, text="API Secret", font=FONT_LABEL, fg=COLORS["text_dim"],
                 bg=COLORS["bg"], anchor="w").pack(fill="x", pady=(0, 3))
        ent_s = StyledEntry(c1, textvariable=self.app.api_secret_var, show="*")
        ent_s.pack(fill="x")
        self.app.api_secret_entry = ent_s

        pref_row = tk.Frame(api_body, bg=COLORS["bg"])
        pref_row.pack(fill="x", pady=(8, 4))
        for txt, var, cmd in [
            ("Guardar claves en disco", self.app.save_keys_var, None),
            ("Mostrar secreto",         self.app.show_secret_var, self.app._toggle_secret_visibility),
        ]:
            chk = tk.Checkbutton(pref_row, text=txt, variable=var, command=cmd,
                                 bg=COLORS["bg"], fg=COLORS["text_dim"], font=FONT_SMALL,
                                 activebackground=COLORS["bg"], activeforeground=COLORS["accent"],
                                 selectcolor=COLORS["surface3"], cursor="hand2",
                                 highlightthickness=0)
            chk.pack(side="left", padx=(0, 20))
        GlowButton(pref_row, "Limpiar claves", command=self.app._clear_keys,
                   style="danger", width=120, height=30).pack(side="right")

        # ── Market Params ────────────────────────────────────────────
        section_hdr(scroll_area, "Parámetros de Mercado",
                    COLORS["green"], "Capital · Fees · Filtros")
        field_grid(scroll_area, [
            ("Capital Inicial (USDT)", StyledEntry, self.app.usdt_var),
            ("Fee por Trade",          StyledEntry, self.app.fee_var),
            ("Max Assets",             StyledEntry, self.app.max_assets_var),
            ("Ganancia Mínima Limpia", StyledEntry, self.app.min_clean_profit_var),
            ("Tipo de Mercado",        StyledCombobox, self.app.market_var, ("spot", "perpetual")),
            ("Red",                    StyledCombobox, self.app.network_var, ("mainnet", "testnet")),
        ])

        # ── Compound ─────────────────────────────────────────────────
        section_hdr(scroll_area, "Interés Compuesto",
                    COLORS["yellow"], "Simulación de ciclos acumulativos")
        field_grid(scroll_area, [
            ("Ciclos",                   StyledEntry, self.app.compound_cycles_var),
            ("Trigger (×capital)",       StyledEntry, self.app.compound_trigger_multiple_var),
            ("Stake post-trigger (%)",   StyledEntry, self.app.compound_stake_pct_var),
        ], cols=3)

        # ── AI Signal ────────────────────────────────────────────────
        section_hdr(scroll_area, "Señal IA", COLORS["purple"], "Motor de análisis técnico")
        field_grid(scroll_area, [
            ("Símbolo Binance",  StyledEntry, self.app.binance_symbol_var),
            ("Cantidad (USDT)",  StyledEntry, self.app.binance_qty_var),
            ("Intervalo",        StyledEntry, self.app.binance_interval_var),
            ("Limit (velas)",    StyledEntry, self.app.binance_limit_var),
        ])
        chk_body = tk.Frame(scroll_area, bg=COLORS["bg"], padx=20, pady=8)
        chk_body.pack(fill="x")
        for txt, var in [
            ("Binance Testnet",        self.app.binance_testnet_var),
            ("Ejecución operativa",    self.app.binance_execute_var),
        ]:
            chk = tk.Checkbutton(chk_body, text=txt, variable=var,
                                 bg=COLORS["bg"], fg=COLORS["text_dim"], font=FONT_SMALL,
                                 activebackground=COLORS["bg"], activeforeground=COLORS["purple"],
                                 selectcolor=COLORS["purple_dim"], cursor="hand2",
                                 highlightthickness=0)
            chk.pack(side="left", padx=(0, 24))

        # Bottom padding
        tk.Frame(scroll_area, bg=COLORS["bg"], height=20).pack()


class PageStats(tk.Frame):
    def __init__(self, parent, app, **kwargs):
        super().__init__(parent, bg=COLORS["bg"], **kwargs)
        self.app = app
        self._build()

    def _build(self):
        hdr = tk.Frame(self, bg=COLORS["surface"])
        hdr.pack(fill="x")
        tk.Frame(hdr, bg=COLORS["green"], height=2).pack(fill="x")
        hdr_inner = tk.Frame(hdr, bg=COLORS["surface"], padx=20, pady=12)
        hdr_inner.pack(fill="x")
        title_block = tk.Frame(hdr_inner, bg=COLORS["surface"])
        title_block.pack(side="left")
        tk.Label(title_block, text="Estadísticas & Historial", font=FONT_HEADING,
                 fg=COLORS["text_bright"], bg=COLORS["surface"]).pack(anchor="w")
        tk.Label(title_block, text="Historial de scans y simulación compuesta",
                 font=FONT_SMALL, fg=COLORS["text_muted"], bg=COLORS["surface"]).pack(anchor="w")
        btn_row = tk.Frame(hdr_inner, bg=COLORS["surface"])
        btn_row.pack(side="right")
        GlowButton(btn_row, "Limpiar historial", command=self.app._clear_history,
                   style="ghost", width=140).pack(side="left", padx=3)
        GlowButton(btn_row, "Limpiar detalle", command=self.app._clear_detail,
                   style="ghost", width=130).pack(side="left", padx=3)

        main = tk.Frame(self, bg=COLORS["bg"])
        main.pack(fill="both", expand=True, padx=16, pady=12)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        main.rowconfigure(4, weight=1)

        tk.Label(main, text="HISTORIAL DE ESCANEOS", font=FONT_MICRO, fg=COLORS["accent"],
                 bg=COLORS["bg"], anchor="w").grid(row=0, column=0, sticky="w", pady=(0, 5))

        cols   = ("timestamp", "market", "network", "valid", "clean", "success", "best", "ms")
        heads  = ("Timestamp", "Mercado", "Red", "Válidas", "Limpias", "% Éxito", "Mejor %", "Ms")
        widths = [155, 80, 80, 65, 70, 80, 90, 65]
        anchors= ["w", "center", "center", "center", "center", "center", "center", "center"]
        self.app.hist_tree = StyledTreeview(main, cols, heads, widths, anchors, height=9)
        self.app.hist_tree.grid(row=1, column=0, sticky="nsew", pady=(0, 12))

        # Cycle slider bar
        ctrl = tk.Frame(main, bg=COLORS["surface2"], padx=14, pady=10,
                        highlightthickness=1, highlightbackground=COLORS["border"])
        ctrl.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        tk.Label(ctrl, text="Ciclos compuesto:", font=FONT_LABEL, fg=COLORS["text_dim"],
                 bg=COLORS["surface2"]).pack(side="left")
        s = ttk.Style()
        s.configure("Dark.Horizontal.TScale", background=COLORS["surface2"],
                    troughcolor=COLORS["surface3"])
        self.app.cycle_slider = ttk.Scale(ctrl, from_=10, to=300, orient="horizontal",
                                          command=self.app._on_slider,
                                          style="Dark.Horizontal.TScale")
        self.app.cycle_slider.set(50)
        self.app.cycle_slider.pack(side="left", fill="x", expand=True, padx=12)
        self.app.compound_cycles_var.trace_add("write", lambda *_: None)

        tk.Label(main, text="DETALLE DE RUTA + GRÁFICO ASCII", font=FONT_MICRO, fg=COLORS["green"],
                 bg=COLORS["bg"], anchor="w").grid(row=3, column=0, sticky="w", pady=(0, 5))

        detail_frame = tk.Frame(main, bg=COLORS["surface"],
                                highlightthickness=1, highlightbackground=COLORS["border"])
        detail_frame.grid(row=4, column=0, sticky="nsew")
        vsb = tk.Scrollbar(detail_frame, orient="vertical",
                           bg=COLORS["surface2"], troughcolor=COLORS["surface"],
                           activebackground=COLORS["accent"], relief="flat", width=6)
        self.app.detail_text = tk.Text(detail_frame, bg=COLORS["surface"], fg=COLORS["text"],
                                       insertbackground=COLORS["accent"], relief="flat", bd=0,
                                       font=FONT_MONO_SM, wrap="word", padx=14, pady=10,
                                       highlightthickness=0, yscrollcommand=vsb.set,
                                       spacing1=2, spacing3=2)
        vsb.configure(command=self.app.detail_text.yview)
        vsb.pack(side="right", fill="y")
        self.app.detail_text.pack(side="left", fill="both", expand=True)


class PageExec(tk.Frame):
    def __init__(self, parent, app, **kwargs):
        super().__init__(parent, bg=COLORS["bg"], **kwargs)
        self.app = app
        self._build()

    def _build(self):
        hdr = tk.Frame(self, bg=COLORS["surface"])
        hdr.pack(fill="x")
        tk.Frame(hdr, bg=COLORS["purple"], height=2).pack(fill="x")
        hdr_inner = tk.Frame(hdr, bg=COLORS["surface"], padx=20, pady=12)
        hdr_inner.pack(fill="x")
        title_block = tk.Frame(hdr_inner, bg=COLORS["surface"])
        title_block.pack(side="left")
        tk.Label(title_block, text="Ejecucion Unificada", font=FONT_HEADING,
                 fg=COLORS["text_bright"], bg=COLORS["surface"]).pack(anchor="w")
        tk.Label(title_block, text="IA - Arbitraje - Ordenes en tiempo real",
                 font=FONT_SMALL, fg=COLORS["text_muted"], bg=COLORS["surface"]).pack(anchor="w")
        btn_row = tk.Frame(hdr_inner, bg=COLORS["surface"])
        btn_row.pack(side="right")
        self.app.btn_ai = GlowButton(btn_row, "Analizar IA", command=self.app._run_binance_ai,
                                      style="purple", width=130, icon="O")
        self.app.btn_ai.pack(side="left", padx=3)
        self.app.btn_market = GlowButton(btn_row, "Orden Mercado", command=self.app._run_direct_market_order,
                                          style="warn", width=140, icon="!")
        self.app.btn_market.pack(side="left", padx=3)
        GlowButton(btn_row, "Limpiar log", command=self.app._clear_exec_log,
                   style="ghost", width=110).pack(side="left", padx=3)
        GlowButton(btn_row, "Limpiar tabla", command=self.app._clear_trades,
                   style="ghost", width=110).pack(side="left", padx=3)
        self.app._controls_disable_on_busy += [self.app.btn_ai, self.app.btn_market]

        kpi_row = tk.Frame(self, bg=COLORS["bg"], padx=16, pady=10)
        kpi_row.pack(fill="x")
        for title, var, unit, color in [
            ("Trades Ejecutados", self.app.kpi_exec_trades, "", COLORS["accent"]),
            ("Tasa de Exito",     self.app.kpi_exec_success, "", COLORS["green"]),
            ("Ganancia Total",    self.app.kpi_exec_profit, "USDT", COLORS["yellow"]),
        ]:
            KPICard(kpi_row, title, var, unit, accent_color=color, width=190, height=86).pack(
                side="left", padx=(0, 10), pady=2)

        body = tk.Frame(self, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        body.rowconfigure(3, weight=1)

        tk.Label(body, text="ACTIVITY LOG", font=FONT_MICRO, fg=COLORS["purple"],
                 bg=COLORS["bg"], anchor="w").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.app.activity_log = ActivityLog(body)
        self.app.activity_log.grid(row=1, column=0, sticky="nsew", pady=(0, 10))

        tbl_hdr = tk.Frame(body, bg=COLORS["bg"])
        tbl_hdr.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        tk.Label(tbl_hdr, text="TRADES EJECUTADOS", font=FONT_MICRO, fg=COLORS["accent"],
                 bg=COLORS["bg"]).pack(side="left")
        tk.Frame(tbl_hdr, bg=COLORS["border"], width=1).pack(side="left", fill="y", padx=16)
        tk.Label(tbl_hdr, text="GANANCIAS ACUMULADAS", font=FONT_MICRO, fg=COLORS["green"],
                 bg=COLORS["bg"]).pack(side="left")

        tables = tk.Frame(body, bg=COLORS["bg"])
        tables.grid(row=3, column=0, sticky="nsew")
        tables.columnconfigure(0, weight=3)
        tables.columnconfigure(1, weight=2)
        tables.rowconfigure(0, weight=1)

        tcols   = ("timestamp", "symbol", "signal", "best_path", "net_profit", "mode", "status")
        theads  = ("Timestamp", "Simbolo", "Senal", "Mejor Ruta", "Ganancia", "Modo", "Estado")
        twidths = [130, 68, 58, 195, 82, 82, 90]
        t_anch  = ["w", "center", "center", "w", "e", "center", "center"]
        self.app.trade_tree = StyledTreeview(tables, tcols, theads, twidths, t_anch, height=7)
        self.app.trade_tree.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        pcols   = ("timestamp", "symbol", "net_profit", "cumulative_profit", "status")
        pheads  = ("Timestamp", "Simbolo", "Ganancia", "Acumulado", "Estado")
        pwidths = [130, 76, 88, 108, 95]
        p_anch  = ["w", "center", "e", "e", "center"]
        self.app.profit_tree = StyledTreeview(tables, pcols, pheads, pwidths, p_anch, height=7)
        self.app.profit_tree.grid(row=0, column=1, sticky="nsew", padx=(6, 0))


# ==========================
# MAIN APP
# ==========================

class ArbitrageApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HFT Crypto Arbitrage — Sistema Unificado")
        self.root.geometry("1440x860")
        self.root.minsize(1100, 640)
        self.root.configure(bg=COLORS["bg"])

        self.scanner = BinanceArbitrageScanner()
        self.ai_engine = AISignalEngine()
        self.running = False
        self.scan_history: List[tuple] = []
        self.executed_trades: List[tuple] = []
        self.last_output: Optional[ScanOutput] = None
        self.all_scan_rows: List[ArbitrageResult] = []
        self.filtered_scan_rows: List[ArbitrageResult] = []
        self._busy = False
        self._scan_in_progress = False
        self._controls_disable_on_busy: List = []

        self._init_variables()
        self._build_ui()
        self._setup_keyboard_shortcuts()
        self._load_config()
        self.statusbar.set("Sistema listo · Conectado a Binance Testnet", "info")

    def _init_variables(self):
        self.api_key_var = tk.StringVar()
        self.api_secret_var = tk.StringVar()
        self.market_var = tk.StringVar(value="spot")
        self.network_var = tk.StringVar(value="testnet")
        self.usdt_var = tk.StringVar(value="100.00")
        self.fee_var = tk.StringVar(value="0.001")
        self.max_assets_var = tk.StringVar(value="120")
        self.min_clean_profit_var = tk.StringVar(value="0.01")
        self.min_profit_var = self.min_clean_profit_var
        self.compound_cycles_var = tk.StringVar(value="50")
        self.compound_trigger_multiple_var = tk.StringVar(value="2.0")
        self.compound_stake_pct_var = tk.StringVar(value="0.10")
        self.binance_symbol_var = tk.StringVar(value="BTCUSDT")
        self.binance_qty_var = tk.StringVar(value="100.00")
        self.binance_interval_var = tk.StringVar(value="1m")
        self.binance_limit_var = tk.StringVar(value="300")
        self.binance_testnet_var = tk.BooleanVar(value=True)
        self.binance_execute_var = tk.BooleanVar(value=False)
        self.autoscan_interval_var = tk.StringVar(value="5")
        self.save_keys_var = tk.BooleanVar(value=False)
        self.show_secret_var = tk.BooleanVar(value=False)
        self.search_var = tk.StringVar(value="")
        self.kpi_scan_ms = tk.StringVar(value="—")
        self.kpi_success = tk.StringVar(value="—")
        self.kpi_best = tk.StringVar(value="—")
        self.kpi_avg = tk.StringVar(value="—")
        self.kpi_exec_trades = tk.StringVar(value="0")
        self.kpi_exec_success = tk.StringVar(value="—")
        self.kpi_exec_profit = tk.StringVar(value="0.000000")
        self.kpi_vars = {
            "scan_time": self.kpi_scan_ms,
            "success_rate": self.kpi_success,
            "best_profit": self.kpi_best,
            "avg_profit": self.kpi_avg,
            "opportunities": tk.StringVar(value="0"),
        }

    def _build_ui(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=0)

        self.sidebar = Sidebar(self.root, self._navigate)
        self.sidebar.grid(row=0, column=0, sticky="ns")

        # Vertical divider
        tk.Frame(self.root, bg=COLORS["border"], width=1).grid(row=0, column=0, sticky="nse")

        self.content = tk.Frame(self.root, bg=COLORS["bg"])
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)

        self.pages: Dict[str, tk.Frame] = {}
        for key, cls in [("scanner", PageScanner), ("config", PageConfig),
                          ("stats", PageStats), ("exec", PageExec)]:
            page = cls(self.content, self)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page

        self.statusbar = StatusBar(self.root)
        self.statusbar.grid(row=1, column=0, columnspan=2, sticky="ew")

        self.toast = Toast(self.root)
        self._navigate("scanner")

    def _navigate(self, key: str):
        self.sidebar.select(key)
        for k, page in self.pages.items():
            if k == key:
                page.tkraise()

    def _setup_keyboard_shortcuts(self):
        self.root.bind_all("<Control-s>", lambda _: self._save_config())
        self.root.bind_all("<F5>", lambda _: self._scan_once())
        self.root.bind_all("<Escape>", lambda _: self._stop_autoscan())

    def _set_busy(self, busy: bool):
        self._busy = busy
        if busy:
            self.statusbar.start_pulse()
            self.statusbar.set("Procesando...", "processing")
            scanner_page = self.pages.get("scanner")
            if scanner_page:
                scanner_page.set_busy(True)
        else:
            self.statusbar.stop_pulse()
            scanner_page = self.pages.get("scanner")
            if scanner_page:
                scanner_page.set_busy(False)
        for w in self._controls_disable_on_busy:
            try:
                w.configure(state="disabled" if busy else "normal")
            except Exception:
                pass

    def _log_exec(self, line: str, level: str = "INFO"):
        if hasattr(self, "activity_log"):
            self.activity_log.log(line, level)

    @staticmethod
    def _to_positive_float(raw: str, field: str) -> float:
        try:
            v = float(raw)
        except ValueError as exc:
            raise ValueError(f"{field} debe ser numérico.") from exc
        if v <= 0:
            raise ValueError(f"{field} debe ser > 0")
        return v

    @staticmethod
    def _to_positive_int(raw: str, field: str) -> int:
        try:
            v = int(raw)
        except ValueError as exc:
            raise ValueError(f"{field} debe ser entero.") from exc
        if v <= 0:
            raise ValueError(f"{field} debe ser > 0")
        return v

    @staticmethod
    def _to_non_negative_float(raw: str, field: str) -> float:
        try:
            v = float(raw)
        except ValueError as exc:
            raise ValueError(f"{field} debe ser numérico.") from exc
        if v < 0:
            raise ValueError(f"{field} debe ser >= 0")
        return v

    def _configure_scanner(self):
        self.scanner.configure(
            market_type=self.market_var.get().strip(),
            testnet=self.network_var.get().strip() == "testnet",
            fee_rate=self._to_non_negative_float(self.fee_var.get(), "Fee"),
        )

    def _validate_keys(self):
        try:
            self._configure_scanner()
            ok, msg = self.scanner.validate_api_keys(
                self.api_key_var.get().strip(), self.api_secret_var.get().strip()
            )
            self.statusbar.set(msg, "success" if ok else "warn")
            if ok:
                messagebox.showinfo("Validación", msg)
                self.toast.show(msg, "success")
            else:
                messagebox.showwarning("Validación", msg)
        except Exception as exc:
            self.statusbar.set(f"Error: {exc}", "error")

    def _scan_once(self):
        if self._busy or self._scan_in_progress:
            return
        self._scan_in_progress = True
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            self.root.after(0, lambda: self._set_busy(True))
            self._configure_scanner()
            start_usdt = self._to_positive_float(self.usdt_var.get(), "Capital")
            max_assets = self._to_positive_int(self.max_assets_var.get(), "Max assets")
            min_clean = self._to_non_negative_float(self.min_clean_profit_var.get(), "Ganancia mínima")
            output = self.scanner.scan(start_usdt=start_usdt, max_paths=40,
                                       max_assets=max_assets, min_clean_profit_usdt=min_clean)
            self.root.after(0, lambda: self._render_output(output))
            self.root.after(0, lambda: self.statusbar.set(
                f"Scan completado · {len(output.opportunities)} rutas limpias encontradas", "success"))
            self.root.after(0, lambda: self._log_exec(
                f"Scan OK — {output.stats.valid_paths} válidas, {output.stats.clean_profitable_paths} limpias, "
                f"{output.stats.scan_ms}ms", "OK"))
        except Exception as exc:
            self.root.after(0, lambda: self.statusbar.set(f"Error en scan: {exc}", "error"))
            self.root.after(0, lambda: self._log_exec(f"Scan fallido: {exc}", "ERROR"))
        finally:
            self._scan_in_progress = False
            self.root.after(0, lambda: self._set_busy(False))

    def _render_output(self, output: ScanOutput):
        self.last_output = output
        self.all_scan_rows = list(output.opportunities)
        self._apply_filter()
        self.kpi_scan_ms.set(f"{output.stats.scan_ms}")
        self.kpi_success.set(f"{output.stats.success_rate_pct:.2f}%")
        self.kpi_best.set(f"{output.stats.best_profit_pct:.4f}%")
        self.kpi_avg.set(f"{output.stats.avg_profit_pct:.4f}%")
        self.kpi_vars["opportunities"].set(str(len(output.opportunities)))
        self._append_history(output)
        if output.opportunities:
            self.toast.show(f"Mejor: {output.opportunities[0].profit_pct:.4f}%", "success")

    def _apply_filter(self):
        q = self.search_var.get().strip().lower()
        self.tree.clear()
        rows = self.all_scan_rows
        if q:
            rows = [r for r in rows if q in " ".join(r.path).lower() or q in " ".join(r.symbols).lower()]
        self.filtered_scan_rows = list(rows)
        for i, row in enumerate(rows, start=1):
            values = (
                i,
                " → ".join(row.path),
                " / ".join(row.symbols),
                f"{row.net_profit_usdt:.6f}",
                f"{row.total_fees_usdt:.6f}",
                f"{row.gross_final_usdt:.6f}",
                f"{row.final_usdt:.6f}",
                f"{row.profit_pct:.4f}%",
            )
            tags = []
            tags.append("profit" if row.net_profit_usdt >= 0 else "loss")
            if i == 1:
                tags.append("best")
            self.tree.insert(values=values, tags=tuple(tags))

    def _append_history(self, output: ScanOutput):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = (
            ts, output.stats.market_type,
            "testnet" if output.stats.testnet else "mainnet",
            output.stats.valid_paths, output.stats.clean_profitable_paths,
            f"{output.stats.success_rate_pct:.2f}%",
            f"{output.stats.best_profit_pct:.4f}%",
            output.stats.scan_ms,
        )
        self.scan_history.append(row)
        self.scan_history = self.scan_history[-300:]
        if hasattr(self, "pages") and "scanner" in self.pages:
            scanner_page = self.pages["scanner"]
            if hasattr(scanner_page, "_scan_lbl"):
                scanner_page._scan_lbl.configure(text=f"Scans: {len(self.scan_history)} · Último: {output.stats.scan_ms} ms")
        self.hist_tree.clear()
        for item in self.scan_history[-100:]:
            self.hist_tree.insert(values=item)
        self._render_ascii_chart()

    def _render_ascii_chart(self):
        if not self.scan_history:
            return
        bests = []
        for r in self.scan_history[-20:]:
            bests.append(float(str(r[6]).replace("%", "")))
        max_abs = max([abs(x) for x in bests] + [1.0])
        chart_lines = ["  ASCII Profit Chart (últimos 20 scans):", "  " + "─" * 38]
        for v in bests:
            n = int((abs(v) / max_abs) * 24)
            bar = ("█" * n) if v >= 0 else ("░" * n)
            prefix = "+" if v >= 0 else "-"
            chart_lines.append(f"  {prefix}{abs(v):>7.4f}%  │ {bar}")
        existing = self.detail_text.get("1.0", tk.END)
        if "ASCII Profit Chart" in existing:
            base = existing.split("ASCII Profit Chart")[0].rstrip()
            self.detail_text.delete("1.0", tk.END)
            if base:
                self.detail_text.insert("1.0", base + "\n\n")
        self.detail_text.insert("end", "\n" + "\n".join(chart_lines) + "\n")

    def _on_slider(self, value):
        self.compound_cycles_var.set(str(int(float(value))))

    def _on_row_select(self, _event):
        if not self.last_output:
            return
        sel = self.tree.tree.selection()
        if not sel:
            return
        values = self.tree.tree.item(sel[0], "values")
        if not values:
            return
        idx = int(values[0]) - 1
        rows = self.filtered_scan_rows if self.filtered_scan_rows else self.all_scan_rows
        if idx < 0 or idx >= len(rows):
            return
        row = rows[idx]
        try:
            plan = self.scanner.simulate_compound_plan(
                initial_capital_usdt=self._to_positive_float(self.usdt_var.get(), "Capital"),
                per_cycle_net_pct=row.profit_pct,
                cycles=self._to_positive_int(self.compound_cycles_var.get(), "Ciclos"),
                trigger_multiple=self._to_positive_float(self.compound_trigger_multiple_var.get(), "Trigger"),
                compound_stake_pct=self._to_positive_float(self.compound_stake_pct_var.get(), "Stake"),
                pre_trigger_stake_pct=1.0,
            )
            text = (
                f"  Ruta        : {' → '.join(row.path)}\n"
                f"  Símbolos    : {' / '.join(row.symbols)}\n"
                f"  Ganancia    : {row.net_profit_usdt:.6f} USDT ({row.profit_pct:.4f}%)\n"
                f"  Comisiones  : {row.total_fees_usdt:.6f} USDT\n\n"
                f"  ── Simulación Compuesta ──────────────────────────\n"
                f"  Capital final : {plan.current_capital:.4f} USDT\n"
                f"  Ciclos        : {plan.cycles_simulated}\n"
                f"  Trigger       : {'✓' if plan.trigger_reached else '✗'} (ciclo {plan.trigger_cycle})\n"
                f"  Modo stake    : {plan.stake_mode}\n\n"
            )
            self.detail_text.delete("1.0", tk.END)
            self.detail_text.insert("1.0", text)
            self._render_ascii_chart()
        except Exception as exc:
            self._log_exec(f"Error renderizando detalle: {exc}", "WARN")

    def _start_autoscan(self):
        if self.running:
            return
        try:
            self._get_autoscan_interval_ms()
            self.running = True
            self.statusbar.set(f"Auto-scan activo (cada {self.autoscan_interval_var.get()}s)", "info")
            self._autoscan_loop()
        except Exception as exc:
            self.statusbar.set(f"Error auto-scan: {exc}", "error")

    def _stop_autoscan(self):
        self.running = False
        self.statusbar.set("Auto-scan detenido", "warn")

    def _autoscan_loop(self):
        if not self.running:
            return
        self._scan_once()
        self.root.after(self._get_autoscan_interval_ms(), self._autoscan_loop)

    def _get_autoscan_interval_ms(self) -> int:
        seconds = self._to_positive_float(self.autoscan_interval_var.get(), "Auto-scan (s)")
        return max(1000, int(seconds * 1000))

    def _run_binance_ai(self):
        if self._busy:
            return
        threading.Thread(target=self._run_binance_ai_worker, daemon=True).start()

    def _run_binance_ai_worker(self):
        try:
            self.root.after(0, lambda: self._set_busy(True))
            symbol = self.binance_symbol_var.get().strip().upper()
            interval = self.binance_interval_var.get().strip()
            limit = self._to_positive_int(self.binance_limit_var.get(), "Limit")
            summary = self.ai_engine.run(symbol=symbol, interval=interval, limit=limit)
            sig = summary["signal"]
            self.root.after(0, lambda: self._log_exec(
                f"AI Signal {symbol} @ {interval} → {sig} (p_up={summary['probability_up']:.3f})", "INFO"))
            self.root.after(0, lambda: self.statusbar.set(f"IA OK · Señal: {sig}", "success"))
            self.root.after(0, lambda: self.toast.show(f"Señal IA: {sig}", "success"))
        except Exception as exc:
            self.root.after(0, lambda: self._log_exec(f"Error AI: {exc}", "ERROR"))
            self.root.after(0, lambda: self.statusbar.set(f"Error IA: {exc}", "error"))
        finally:
            self.root.after(0, lambda: self._set_busy(False))

    def _run_direct_market_order(self):
        if self._busy:
            return
        if self.binance_execute_var.get():
            if not messagebox.askyesno("Confirmar", "¿Confirmas ejecución operativa?"):
                return
        threading.Thread(target=self._run_direct_market_order_worker, daemon=True).start()

    def _run_direct_market_order_worker(self):
        try:
            self.root.after(0, lambda: self._set_busy(True))
            symbol = self.binance_symbol_var.get().strip().upper()
            qty = self._to_positive_float(self.binance_qty_var.get(), "Qty")
            execute = self.binance_execute_var.get()
            self.scanner.configure(
                market_type=self.market_var.get().strip(),
                testnet=self.binance_testnet_var.get(),
                fee_rate=self._to_non_negative_float(self.fee_var.get(), "Fee"),
            )
            scan = self.scanner.scan(
                start_usdt=qty, max_paths=3,
                max_assets=self._to_positive_int(self.max_assets_var.get(), "Max assets"),
                min_clean_profit_usdt=self._to_non_negative_float(self.min_clean_profit_var.get(), "Min profit"),
            )
            if scan.opportunities:
                best = scan.opportunities[0]
                path = " → ".join(best.path)
                net = best.net_profit_usdt
                status = "OK" if net > 0 else "NO_RENTABLE"
            else:
                path, net, status = "N/A", 0.0, "SIN_RUTAS"

            if net <= 0 or status != "OK":
                self.root.after(0, lambda: self.statusbar.set("Orden omitida — ganancia insuficiente", "warn"))
                return

            mode = "MERCADO" if execute else "SIM_MERCADO"
            self.root.after(0, lambda: self._log_exec(f"Orden mercado · {path} · net={net:.6f} USDT", "OK"))
            self.root.after(0, lambda: self._record_trade(symbol, "MARKET", path, net, mode, "OK"))
            self.root.after(0, lambda: self.statusbar.set("Orden registrada con ganancia limpia", "success"))
            self.root.after(0, lambda: self.toast.show("Orden de mercado registrada", "success"))
        except Exception as exc:
            self.root.after(0, lambda: self._log_exec(f"Error orden mercado: {exc}", "ERROR"))
            self.root.after(0, lambda: self.statusbar.set(f"Error: {exc}", "error"))
        finally:
            self.root.after(0, lambda: self._set_busy(False))

    def _record_trade(self, symbol, signal, best_path, net_profit, mode, status):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = (ts, symbol, signal, best_path, f"{net_profit:.6f}", mode, status)
        self.executed_trades.append(row)
        self.executed_trades = self.executed_trades[-300:]
        self.trade_tree.clear()
        for item in self.executed_trades[-120:]:
            self.trade_tree.insert(values=item)
        self._append_profit_row(ts, symbol, net_profit, status)
        self._update_execution_kpis()

    def _append_profit_row(self, ts, symbol, net_profit, status):
        cumulative = 0.0
        for item in self.profit_tree.tree.get_children():
            vals = self.profit_tree.tree.item(item, "values")
            if vals:
                try:
                    cumulative = float(vals[3])
                except Exception:
                    pass
        cumulative += net_profit
        row = (ts, symbol, f"{net_profit:.6f}", f"{cumulative:.6f}", status)
        tags = ("profit",) if net_profit >= 0 else ("loss",)
        self.profit_tree.insert(values=row, tags=tags)

    def _update_execution_kpis(self):
        count = len(self.executed_trades)
        self.kpi_exec_trades.set(str(count))
        total_profit, success = 0.0, 0
        for r in self.executed_trades:
            try:
                net = float(r[4])
            except Exception:
                net = 0.0
            total_profit += net
            if net > 0:
                success += 1
        self.kpi_exec_success.set(f"{(success/count*100):.2f}%" if count else "—")
        self.kpi_exec_profit.set(f"{total_profit:.6f}")

    def _toggle_secret_visibility(self):
        if hasattr(self, "api_secret_entry"):
            self.api_secret_entry.configure(show="" if self.show_secret_var.get() else "*")

    def _clear_keys(self):
        self.api_key_var.set("")
        self.api_secret_var.set("")
        self.save_keys_var.set(False)
        self.toast.show("Claves limpiadas", "warn")

    def _clear_scan_results(self):
        self.all_scan_rows = []
        self.last_output = None
        self.tree.clear()
        self.kpi_scan_ms.set("—")
        self.kpi_success.set("—")
        self.kpi_best.set("—")
        self.kpi_avg.set("—")
        self.kpi_vars["opportunities"].set("0")

    def _clear_history(self):
        self.scan_history = []
        self.hist_tree.clear()
        self._clear_detail()

    def _clear_detail(self):
        self.detail_text.delete("1.0", tk.END)

    def _clear_exec_log(self):
        if hasattr(self, "activity_log"):
            self.activity_log.clear()

    def _clear_trades(self):
        self.executed_trades = []
        self.trade_tree.clear()
        self.profit_tree.clear()
        self.kpi_exec_trades.set("0")
        self.kpi_exec_success.set("—")
        self.kpi_exec_profit.set("0.000000")

    def _copy_config(self):
        data = {
            "market": self.market_var.get(),
            "network": self.network_var.get(),
            "usdt": self.usdt_var.get(),
            "fee": self.fee_var.get(),
            "max_assets": self.max_assets_var.get(),
        }
        self.root.clipboard_clear()
        self.root.clipboard_append(json.dumps(data, ensure_ascii=False, indent=2))
        self.toast.show("Configuración copiada", "info")

    def _save_config(self):
        try:
            api_key = self.api_key_var.get().strip() if self.save_keys_var.get() else ""
            api_secret = self.api_secret_var.get().strip() if self.save_keys_var.get() else ""
            payload = {
                "api_key": api_key, "api_secret": api_secret,
                "market": self.market_var.get().strip(), "network": self.network_var.get().strip(),
                "usdt": self.usdt_var.get().strip(), "fee": self.fee_var.get().strip(),
                "max_assets": self.max_assets_var.get().strip(),
                "min_clean_profit": self.min_clean_profit_var.get().strip(),
                "compound_cycles": self.compound_cycles_var.get().strip(),
                "compound_trigger_multiple": self.compound_trigger_multiple_var.get().strip(),
                "compound_stake_pct": self.compound_stake_pct_var.get().strip(),
                "binance_symbol": self.binance_symbol_var.get().strip(),
                "binance_qty": self.binance_qty_var.get().strip(),
                "binance_interval": self.binance_interval_var.get().strip(),
                "binance_limit": self.binance_limit_var.get().strip(),
                "binance_testnet": self.binance_testnet_var.get(),
                "binance_execute": self.binance_execute_var.get(),
                "autoscan_seconds": self.autoscan_interval_var.get().strip(),
                "save_keys": self.save_keys_var.get(),
            }
            CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.statusbar.set("Configuración guardada", "success")
            self.toast.show("Configuración guardada", "success")
        except Exception as exc:
            self.statusbar.set(f"Error guardando: {exc}", "error")
            messagebox.showerror("Error", str(exc))

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.api_key_var.set(payload.get("api_key", ""))
            self.api_secret_var.set(payload.get("api_secret", ""))
            self.market_var.set(payload.get("market", "spot"))
            self.network_var.set(payload.get("network", "testnet"))
            self.usdt_var.set(payload.get("usdt", "100.00"))
            self.fee_var.set(payload.get("fee", "0.001"))
            self.max_assets_var.set(payload.get("max_assets", "120"))
            self.min_clean_profit_var.set(payload.get("min_clean_profit", "0.01"))
            self.compound_cycles_var.set(payload.get("compound_cycles", "50"))
            self.compound_trigger_multiple_var.set(payload.get("compound_trigger_multiple", "2.0"))
            self.compound_stake_pct_var.set(payload.get("compound_stake_pct", "0.10"))
            self.binance_symbol_var.set(payload.get("binance_symbol", "BTCUSDT"))
            self.binance_qty_var.set(payload.get("binance_qty", "100.00"))
            self.binance_interval_var.set(payload.get("binance_interval", "1m"))
            self.binance_limit_var.set(payload.get("binance_limit", "300"))
            self.binance_testnet_var.set(bool(payload.get("binance_testnet", True)))
            self.binance_execute_var.set(bool(payload.get("binance_execute", False)))
            self.autoscan_interval_var.set(str(payload.get("autoscan_seconds", "5")))
            self.save_keys_var.set(bool(payload.get("save_keys", False)))
            self.statusbar.set("Configuración cargada", "info")
        except Exception as exc:
            self.statusbar.set(f"Error cargando config: {exc}", "warn")


def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.0)
    except Exception:
        pass
    ArbitrageApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
