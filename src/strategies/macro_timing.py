"""宏观择时模块 — 五层决策漏斗第一层.

决定总仓位上限, 防范系统性风险.

P0 实现:
  维度① 趋势动量 (30%): 沪深300(510300) MA20/MA60 排列状态 + 价格偏离
  维度② ETF 代理宽度 (25%): 标的池中站上 MA20 的比例
    (分母 = 当前标的池, 仅代表 ETF 代理宽度; 不得称为"全 A 市场宽度", spec §5.2)

复合打分 0-100, 映射到仓位上限 0-100%.

Usage:
    mt = MacroTiming(ma_short=20, ma_long=60)
    score = mt.evaluate(benchmark_df, universe_df)
    cap = mt.score_to_position_cap(score)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import polars as pl

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class MacroScore:
    """宏观择时打分结果."""

    trend_score: float       # 趋势动量得分 0-100
    breadth_score: float     # ETF 代理宽度得分 0-100 (分母=标的池)
    composite_score: float   # 加权复合得分 0-100
    position_cap: float      # 仓位上限 0.0-1.0

    # 诊断信息
    ma_short: Optional[float] = None   # 短期均线
    ma_long: Optional[float] = None    # 长期均线
    latest_close: Optional[float] = None
    breadth_pct: Optional[float] = None  # 站上 MA20 的比例
    regime: str = "unknown"  # bull / bear / ranging

    def is_plunge(self, prev_score: float, threshold: float = 30.0) -> bool:
        """检查单日宏观得分是否骤降."""
        return (prev_score - self.composite_score) > threshold

    def summary(self) -> str:
        """人类可读摘要."""
        return (
            f"MacroScore(composite={self.composite_score:.0f}, "
            f"cap={self.position_cap:.0%}, regime={self.regime}, "
            f"trend={self.trend_score:.0f}, etf_breadth={self.breadth_score:.0f})"
        )


# ------------------------------------------------------------------
# 核心类
# ------------------------------------------------------------------

class MacroTiming:
    """宏观择时打分器.

    Args:
        ma_short: 短期均线周期 (默认 20)
        ma_long: 长期均线周期 (默认 60)
        weight_trend: 趋势动量权重 (默认 0.30)
        weight_breadth: ETF 代理宽度权重 (默认 0.25)
        plunge_threshold: 单日得分骤降阈值 (默认 30, 触发强制减仓)
    """

    def __init__(
        self,
        ma_short: int = 20,
        ma_long: int = 60,
        weight_trend: float = 0.30,
        weight_breadth: float = 0.25,
        plunge_threshold: float = 30.0,
    ) -> None:
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.weight_trend = weight_trend
        self.weight_breadth = weight_breadth
        self.plunge_threshold = plunge_threshold

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        benchmark_df: pl.DataFrame,
        universe_df: pl.DataFrame,
    ) -> MacroScore:
        """计算宏观择时得分和仓位上限.

        Args:
            benchmark_df: 沪深300 (510300) 的 OHLCV, 按 date 排序
            universe_df: 全标的池的 OHLCV, 最后一日用于宽度计算

        Returns:
            MacroScore 对象
        """
        trend = self._calc_trend_score(benchmark_df)
        breadth = self._calc_breadth_score(universe_df)

        # 权重归一化: P0 阶段维度①②合计占 55%, 归一化到 100%
        total_weight = self.weight_trend + self.weight_breadth
        if total_weight > 0:
            composite = (trend * self.weight_trend + breadth * self.weight_breadth) / total_weight
        else:
            composite = 50.0

        cap = self.score_to_position_cap(composite)

        # 诊断
        regime, ma_s, ma_l, latest_close = self._diagnose(benchmark_df)
        breadth_pct = self._calc_breadth_pct(universe_df)

        return MacroScore(
            trend_score=round(trend, 1),
            breadth_score=round(breadth, 1),
            composite_score=round(composite, 1),
            position_cap=round(cap, 4),
            ma_short=ma_s,
            ma_long=ma_l,
            latest_close=latest_close,
            breadth_pct=breadth_pct,
            regime=regime,
        )

    # ------------------------------------------------------------------
    # 维度①: 趋势动量 (0-100)
    # ------------------------------------------------------------------

    def _calc_trend_score(self, df: pl.DataFrame) -> float:
        """计算趋势动量得分.

        逻辑:
          1. MA20 > MA60 ("黄金交叉") → 趋势偏多 → 基础分 60-100
          2. MA20 < MA60 ("死亡交叉") → 趋势偏空 → 基础分 0-40
          3. Price 相对 MA20 的位置做微调 (±20 分)
          4. 均线斜率加分 (MA20 斜率 > 0 则加分)
        """
        if df.is_empty() or "close" not in df.columns:
            return 50.0

        close = df["close"]

        # 计算均线
        ma20_val = self._rolling_mean_val(close, self.ma_short)
        ma60_val = self._rolling_mean_val(close, self.ma_long)

        if ma20_val is None or ma60_val is None:
            return 50.0  # 数据不足, 返回中性分

        if ma20_val > ma60_val:
            # 多头排列
            ratio = ma20_val / ma60_val
            # ratio 1.00-1.10 → 基础 60-100
            base_score = min(100.0, 60.0 + (ratio - 1.0) * 400)
        else:
            # 空头排列
            ratio = ma60_val / ma20_val
            # ratio 1.00-1.10 → 基础 40-0
            base_score = max(0.0, 40.0 - (ratio - 1.0) * 400)

        # 2. 价格偏离 MA20 微调
        close_val = float(close.tail(1)[0])
        price_deviation = (close_val - ma20_val) / ma20_val  # e.g. +0.02 = 2% above MA20
        adjustment = price_deviation * 200  # ±20 分范围 (for ±10% deviation)
        adjustment = max(-20.0, min(20.0, adjustment))

        # 3. MA20 斜率加分
        ma20_list = self._rolling_mean_series(close, self.ma_short)
        if ma20_list is not None and len(ma20_list) >= 2:
            slope = ma20_list[-1] - ma20_list[-2]
            slope_bonus = min(10.0, max(-10.0, slope * 200))
            adjustment += slope_bonus

        return max(0.0, min(100.0, base_score + adjustment))

    # ------------------------------------------------------------------
    # 维度②: ETF 代理宽度 (0-100, 分母 = 标的池)
    # ------------------------------------------------------------------

    def _calc_breadth_score(self, universe_df: pl.DataFrame) -> float:
        """计算 ETF 代理宽度得分 (spec §5.2: 分母 = 标的池, 不得称全 A).

        = 标的池中收盘价 > MA20 的比例 × 100

        高比例 → 多数 ETF 上行 (得分高)
        低比例 → 少数 ETF 上行
        """
        pct = self._calc_breadth_pct(universe_df)
        if pct is None:
            return 50.0
        return pct * 100.0  # 0.0-1.0 → 0-100

    def _calc_breadth_pct(self, universe_df: pl.DataFrame) -> Optional[float]:
        """计算 ETF 代理宽度: 站上 MA20 的标的比例 (分母 = 标的池, spec §5.2)."""
        if universe_df.is_empty():
            return None

        if "symbol" not in universe_df.columns or "close" not in universe_df.columns:
            return None

        symbols = universe_df["symbol"].unique().to_list()
        if not symbols:
            return None

        above_count = 0
        valid_count = 0

        for sym in symbols:
            sym_df = universe_df.filter(pl.col("symbol") == sym).sort("date")
            if len(sym_df) < self.ma_short:
                continue

            close = sym_df["close"]
            ma20_val = self._rolling_mean_val(close, self.ma_short)

            if ma20_val is None:
                continue

            valid_count += 1
            if float(close.tail(1)[0]) > ma20_val:
                above_count += 1

        if valid_count == 0:
            return None

        return above_count / valid_count

    # ------------------------------------------------------------------
    # 仓位映射
    # ------------------------------------------------------------------

    @staticmethod
    def score_to_position_cap(score: float) -> float:
        """将 0-100 宏观得分映射到仓位上限 0.0-1.0.

        采用分段线性映射:
          score 0-20  → cap 0%   (系统性风险, 空仓)
          score 20-40 → cap 20-40%
          score 40-60 → cap 40-70%
          score 60-80 → cap 70-90%
          score 80-100→ cap 90-100%

        低分区惩罚更重, 高分区边际递减.
        """
        score = max(0.0, min(100.0, score))

        if score <= 20.0:
            return 0.0
        elif score <= 40.0:
            return 0.20 + (score - 20.0) / 20.0 * 0.20  # 0.20 → 0.40
        elif score <= 60.0:
            return 0.40 + (score - 40.0) / 20.0 * 0.30  # 0.40 → 0.70
        elif score <= 80.0:
            return 0.70 + (score - 60.0) / 20.0 * 0.20  # 0.70 → 0.90
        else:
            return 0.90 + (score - 80.0) / 20.0 * 0.10  # 0.90 → 1.00

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def _diagnose(
        self, df: pl.DataFrame,
    ) -> tuple[str, Optional[float], Optional[float], Optional[float]]:
        """判断趋势状态 (牛市/熊市/震荡, 基于基准 510300)."""
        if df.is_empty() or "close" not in df.columns:
            return "unknown", None, None, None

        close = df["close"]
        ma20_val = self._rolling_mean_val(close, self.ma_short)
        ma60_val = self._rolling_mean_val(close, self.ma_long)
        latest_close = float(close.tail(1)[0])

        if ma20_val is None or ma60_val is None:
            return "unknown", None, None, latest_close
        ratio = ma20_val / ma60_val if ma60_val > 0 else 1.0

        # 判断趋势状态
        if ratio > 1.03:
            regime = "bull"
        elif ratio < 0.97:
            regime = "bear"
        else:
            regime = "ranging"

        return regime, ma20_val, ma60_val, latest_close

    # ------------------------------------------------------------------
    # 工具: 滚动均线
    # ------------------------------------------------------------------

    @staticmethod
    def _rolling_mean_val(series: pl.Series, window: int) -> Optional[float]:
        """计算滚动均值, 返回最后一个有效标量值."""
        if len(series) < window:
            return None
        result = series.rolling_mean(window_size=window, min_samples=window)
        valid = result.drop_nulls()
        if valid.is_empty():
            return None
        return float(valid.tail(1)[0])

    @staticmethod
    def _rolling_mean_series(series: pl.Series, window: int) -> Optional[list[float]]:
        """返回完整的滚动均值序列 (列表)."""
        if len(series) < window:
            return None
        result = series.rolling_mean(window_size=window, min_samples=window)
        valid = result.drop_nulls()
        if valid.is_empty():
            return None
        return valid.to_list()
