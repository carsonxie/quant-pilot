"""行情序列访问 (共享工具) — 逐日价格查找与交易日历.

供基准模拟 (M5)、绩效核算 (M6) 与面板共用, 避免各模块重复实现:
  - PriceSeries: symbol → {date: 价格} 查找, 提供"≤ 指定日最近价";
  - trading_days(): 价格序列的日期并集 (交易日历近似).

口径: 使用库内 OHLCV 原始价 (不复权, Sina; 不含分红/权益事件, C09/E I-09),
研究口径比较用同一序列; 成交/账本由调用方决定价格来源。
"""

from __future__ import annotations

from datetime import date

import polars as pl


class PriceSeries:
    """行情价格查找 (逐日 open/close; 缺失返回 None 或最近可用)."""

    def __init__(self, ohlcv: pl.DataFrame) -> None:
        self.closes: dict[str, dict[date, float]] = {}
        self.opens: dict[str, dict[date, float]] = {}
        if ohlcv.is_empty():
            self.trading_days: list[date] = []
            return
        for sym in ohlcv["symbol"].unique().to_list():
            sub = ohlcv.filter(pl.col("symbol") == sym).sort("date")
            dates = sub["date"].to_list()
            self.closes[sym] = {
                d: float(c) for d, c in zip(dates, sub["close"].to_list(), strict=True)
                if d is not None and c is not None
            }
            self.opens[sym] = {
                d: float(o) for d, o in zip(dates, sub["open"].to_list(), strict=True)
                if d is not None and o is not None
            }
        all_days: set[date] = set()
        for series in self.closes.values():
            all_days.update(series.keys())
        self.trading_days = sorted(all_days)

    @classmethod
    def from_db(cls, db, symbols: list[str] | None = None) -> PriceSeries:
        """从 DuckDBManager 读取行情构造 (symbols=None → 全部标的)."""
        return cls(db.get_ohlcv(symbols=symbols))

    def close_on(self, symbol: str, day: date) -> float | None:
        """指定日收盘价 (无当日数据 → None, 不虚构)."""
        return self.closes.get(symbol, {}).get(day)

    def close_on_or_before(self, symbol: str, day: date) -> tuple[float, date] | None:
        """≤ 指定日最近可用收盘价 (返回价格与价格日期; 无数据 → None)."""
        series = self.closes.get(symbol, {})
        if day in series:
            return series[day], day
        candidates = [d for d in series if d <= day]
        if not candidates:
            return None
        last = max(candidates)
        return series[last], last

    def days_until(self, day: date) -> list[date]:
        """≤ day 的交易日 (升序)."""
        return [d for d in self.trading_days if d <= day]
