"""风控执行模块 — 五层决策漏斗第五层.

P0 实现两项核心风控:
  1. ATR 移动止损 (Trailing Stop):
     每只持仓标的独立计算 ATR(14), 维护移动最高价,
     止损线 = 最高价 - N × ATR, 只上移不下移.
     现价触及止损线 → 退出该标的.

  2. 宏观骤降强制减仓:
     单日 MacroScore.composite_score 下降 > plunge_threshold →
     强制总仓位降至 safe_position_pct.

Usage:
    rm = RiskManager(atr_period=14, atr_multiplier=2.0)
    report = rm.evaluate(ohlcv_df, current_score, prev_score,
                         position_cap=0.70, weights=[...])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class StopLevel:
    """单标的移动止损状态."""

    symbol: str
    atr: float                # 当前 ATR(14)
    trailing_high: float      # 移动最高价 (close)
    stop_price: float         # 止损线 = trailing_high - N × ATR
    current_close: float      # 最新收盘价
    distance_pct: float       # (close - stop) / close, 正 = 安全距离
    is_triggered: bool        # close <= stop_price → 止损触发


@dataclass
class RiskReport:
    """风控评估报告."""

    stops: list[StopLevel]                  # 各标的止损状态
    plunge_detected: bool                   # 是否触发宏观骤降
    plunge_score_drop: float                # 宏观得分下降幅度 (非负)
    original_position_cap: float            # 原仓位上限 (来自宏观择时)
    effective_position_cap: float           # 骤降修正后的有效仓位上限
    forced_exits: list[str]                 # 因止损需强制退出的标的
    message: str = ""                       # 人类可读摘要

    def __post_init__(self) -> None:
        if not self.message:
            self.message = self._build_message()

    def _build_message(self) -> str:
        parts: list[str] = []

        if self.plunge_detected:
            parts.append(
                f"⚠️ 宏观骤降: 得分下降 {self.plunge_score_drop:.0f} 分, "
                f"仓位上限 {self.original_position_cap:.0%} → {self.effective_position_cap:.0%}"
            )

        if self.forced_exits:
            parts.append(f"🛑 止损触发: {', '.join(self.forced_exits)}")

        if not parts:
            # 给出最近的止损距离
            closest = min(
                self.stops, key=lambda s: s.distance_pct
            ) if self.stops else None
            if closest:
                parts.append(
                    f"✅ 风控正常: 最近止损距离 {closest.symbol} "
                    f"({closest.distance_pct:.1%})"
                )
            else:
                parts.append("✅ 风控正常: 无持仓")

        return " | ".join(parts)

    def summary(self) -> str:
        """多行可读摘要."""
        lines = [self.message, ""]
        for s in self.stops:
            flag = " [TRIGGERED]" if s.is_triggered else ""
            lines.append(
                f"  {s.symbol}: close={s.current_close:.3f} "
                f"stop={s.stop_price:.3f} "
                f"ATR={s.atr:.4f} "
                f"trail_high={s.trailing_high:.3f} "
                f"dist={s.distance_pct:.1%}{flag}"
            )
        return "\n".join(lines)


# ------------------------------------------------------------------
# 核心类
# ------------------------------------------------------------------

class RiskManager:
    """ATR 移动止损 + 宏观骤降减仓风控器.

    Args:
        atr_period: ATR 计算周期 (默认 14)
        atr_multiplier: 止损 N 倍 ATR (默认 2.0)
        safe_position_pct: 宏观骤降后安全仓位比例 (默认 0.30)
        plunge_threshold: 单日得分骤降阈值 (默认 30)
        min_hold_days: 最低持仓天数 (暂用于日志, 调度在 pipeline 层)
    """

    def __init__(
        self,
        atr_period: int = 14,
        atr_multiplier: float = 2.0,
        safe_position_pct: float = 0.30,
        plunge_threshold: float = 30.0,
        min_hold_days: int = 3,
    ) -> None:
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.safe_position_pct = safe_position_pct
        self.plunge_threshold = plunge_threshold
        self.min_hold_days = min_hold_days

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        ohlcv_df: pl.DataFrame,
        current_score: float,
        prev_score: float,
        position_cap: float,
        symbols: Optional[list[str]] = None,
    ) -> RiskReport:
        """执行风控评估.

        Args:
            ohlcv_df: OHLCV 数据 (需含 symbol, date, open, high, low, close)
            current_score: 当日 MacroScore.composite_score
            prev_score: 前一日 MacroScore.composite_score
            position_cap: 原仓位上限 (来自宏观择时)
            symbols: 持仓标的列表; 若为 None, 自动从 ohlcv_df 提取

        Returns:
            RiskReport — 止损信号 + 骤降判定 + 有效仓位上限
        """
        self._validate_input(ohlcv_df)

        if symbols is None:
            symbols = ohlcv_df["symbol"].unique().to_list()

        # 1. 逐标的 ATR 移动止损
        stops: list[StopLevel] = []
        forced_exits: list[str] = []
        for sym in symbols:
            stop = self._evaluate_stop(ohlcv_df, sym)
            if stop is not None:
                stops.append(stop)
                if stop.is_triggered:
                    forced_exits.append(sym)

        # 2. 宏观骤降检测
        score_drop = max(0.0, prev_score - current_score)
        plunge_detected = score_drop > self.plunge_threshold

        effective_cap = position_cap
        if plunge_detected:
            effective_cap = min(position_cap, self.safe_position_pct)
            logger.warning(
                "Macro plunge detected: score dropped %.1f (> %.0f). "
                "Forcing position cap %.0f%% → %.0f%%",
                score_drop, self.plunge_threshold,
                position_cap * 100, effective_cap * 100,
            )

        return RiskReport(
            stops=stops,
            plunge_detected=plunge_detected,
            plunge_score_drop=round(score_drop, 1),
            original_position_cap=position_cap,
            effective_position_cap=round(effective_cap, 4),
            forced_exits=forced_exits,
        )

    # ------------------------------------------------------------------
    # ATR 移动止损
    # ------------------------------------------------------------------

    def _evaluate_stop(
        self,
        ohlcv_df: pl.DataFrame,
        symbol: str,
    ) -> Optional[StopLevel]:
        """计算单只标的的移动止损线.

        研究近似 (C06): trailing_high 取近 2×ATR 周期窗口的收盘价最高值,
        非"实际持有期"高水位 (持仓状态化在 M4 落地); 判定用 T 日收盘价,
        未使用日内最低价; 触发后最早下一可交易时段执行, 跳空时不保证止损价成交.

        Args:
            ohlcv_df: 全量 OHLCV
            symbol: 标的代码

        Returns:
            StopLevel, 或 None (数据不足)
        """
        sym_df = (
            ohlcv_df
            .filter(pl.col("symbol") == symbol)
            .sort("date")
        )

        if len(sym_df) < self.atr_period + 1:
            logger.warning("%s: insufficient data for ATR(%d), skip",
                           symbol, self.atr_period)
            return None

        # 确保所需列存在
        for col in ["high", "low", "close"]:
            if col not in sym_df.columns:
                logger.warning("%s: missing column '%s', skip", symbol, col)
                return None

        atr = self._calc_atr(sym_df)
        if atr is None or atr <= 0:
            return None

        # 移动最高价: 回溯窗口内的最高 close
        # 使用 atr_period * 2 窗口 (既不短视也不过度依赖远古数据)
        lookback = min(len(sym_df), self.atr_period * 2)
        recent = sym_df.tail(lookback)
        trailing_high = float(recent["close"].max())

        current_close = float(sym_df["close"].tail(1)[0])

        # 止损线 = 最高价 - N × ATR
        stop_price = trailing_high - self.atr_multiplier * atr

        # 止损线不能低于 0
        stop_price = max(0.0, stop_price)

        # 判断触发
        is_triggered = current_close <= stop_price

        # 距离止损线的安全边际
        if current_close > 0:
            distance_pct = (current_close - stop_price) / current_close
        else:
            distance_pct = 0.0

        if is_triggered:
            logger.warning(
                "%s: STOP TRIGGERED! close=%.4f <= stop=%.4f "
                "(ATR=%.4f, trail_high=%.4f)",
                symbol, current_close, stop_price, atr, trailing_high,
            )

        return StopLevel(
            symbol=symbol,
            atr=round(atr, 6),
            trailing_high=round(trailing_high, 6),
            stop_price=round(stop_price, 6),
            current_close=round(current_close, 6),
            distance_pct=round(distance_pct, 6),
            is_triggered=is_triggered,
        )

    # ------------------------------------------------------------------
    # ATR 计算 (Wilder 平滑法)
    # ------------------------------------------------------------------

    def _calc_atr(self, sym_df: pl.DataFrame) -> Optional[float]:
        """计算 Average True Range (Wilder 平滑).

        True Range = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        初始 ATR = mean(TR over first N periods)
        后续 ATR_t = (ATR_{t-1} * (N-1) + TR_t) / N
        """
        n = self.atr_period

        high = sym_df["high"].to_numpy()
        low = sym_df["low"].to_numpy()
        close = sym_df["close"].to_numpy()

        if len(high) < n + 1:
            return None

        # True Range
        prev_close = np.roll(close, 1)
        prev_close[0] = close[0]  # 第一个 TR 用 high-low

        tr = np.maximum(
            high - low,
            np.maximum(
                np.abs(high - prev_close),
                np.abs(low - prev_close),
            ),
        )

        # 初始 ATR: 简单平均前 N 个 TR
        atr_val = float(np.mean(tr[1:n + 1]))  # tr[0] 用了 close[0] 作 prev_close, 跳过

        # Wilder 平滑: TR 从第 n+1 个开始 (索引 n+1)
        atr_series = [atr_val]
        for i in range(n + 1, len(tr)):
            atr_val = (atr_val * (n - 1) + tr[i]) / n
            atr_series.append(atr_val)

        return float(atr_series[-1])

    # ------------------------------------------------------------------
    # 纯函数工具: 止损线上移约束 (供 pipeline 层调用)
    # ------------------------------------------------------------------

    @staticmethod
    def enforce_stop_ratchet(
        current_stop: float,
        prev_stop: Optional[float],
    ) -> float:
        """确保止损线只上移不下移.

        若之前已记录止损线, 当前止损 = max(current, prev).
        这是 ATR 移动止损的核心约束: 止损线只能朝有利方向移动.

        Args:
            current_stop: 当前计算的止损价
            prev_stop: 前一日记录的止损价 (None = 首次持仓)

        Returns:
            修正后的止损价
        """
        if prev_stop is None:
            return current_stop
        return max(current_stop, prev_stop)

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_input(df: pl.DataFrame) -> None:
        """校验输入数据完整性."""
        required = ["symbol", "date", "high", "low", "close"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(
                f"OHLCV DataFrame missing required columns: {missing}"
            )


# ------------------------------------------------------------------
# 工厂函数
# ------------------------------------------------------------------

def from_settings(settings: dict) -> RiskManager:
    """从 settings.yaml 读取参数创建风控器.

    Usage:
        import yaml
        with open("config/settings.yaml") as f:
            settings = yaml.safe_load(f)
        rm = from_settings(settings)
    """
    cfg = settings.get("risk_management", {})
    return RiskManager(
        atr_period=cfg.get("atr_period", 14),
        atr_multiplier=cfg.get("atr_multiplier", 2.0),
        safe_position_pct=cfg.get("safe_position_pct", 0.30),
        plunge_threshold=settings.get("macro_timing", {}).get(
            "score_plunge_threshold", 30.0,
        ),
        min_hold_days=cfg.get("min_hold_days", 3),
    )
