"""组合优化模块 — 五层决策漏斗第四层.

方法 (spec v3.0 §5.3, 算法标识必须与实际行为一致 — C01 修正):
  - inverse_volatility (默认, v0.2 起): w_i ∝ 1/σ_i   — 波动率倒数
  - inverse_variance (v0.1 旧口径, 保留对照): w_i ∝ 1/σ_i² — 方差倒数

低波动标的获得更高权重, 高波动标的获得更低权重.
权重归一化到宏观择时给出的仓位上限, 单标的权重上限 ≤ 30%.

波动率声明: 过去 vol_lookback 个交易日对数收益率标准差 × sqrt(252);
窗口至少 max(2, vol_lookback//2) 个点; 零波动/缺失/短历史标的被剔除, 不产生无限权重.

P1 升级: 风险平价 (Risk Parity) + Ledoit-Wolf 协方差收缩.

Usage:
    opt = PortfolioOptimizer(vol_lookback=20, max_position_weight=0.30)
    result = opt.optimize(ohlcv_df, position_cap=0.7)
    print(result.summary())
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
class PortfolioWeight:
    """单标的权重."""

    symbol: str
    annualized_vol: float       # 年化波动率
    raw_weight: float           # 原始权重 (按 method: 1/σ 或 1/σ², 归一化前)
    final_weight: float         # 最终权重 (× position_cap, 经上限裁剪)
    is_capped: bool = False     # 是否触发了单标的上限


@dataclass
class PortfolioResult:
    """组合优化结果."""

    weights: list[PortfolioWeight]
    position_cap: float         # 目标总仓位 (来自宏观择时)
    total_weight: float         # 实际总权重 (应 == position_cap, 取两位小数验证)
    method: str = "inverse_volatility"

    # 诊断信息
    vol_lookback: int = 20
    max_position_weight: float = 0.30
    num_symbols_input: int = 0
    num_symbols_output: int = 0
    min_annual_vol: Optional[float] = None
    max_annual_vol: Optional[float] = None

    def summary(self) -> str:
        """人类可读摘要, 用于日志和推送通知."""
        lines = [
            f"PortfolioResult(method={self.method}, "
            f"cap={self.position_cap:.1%}, "
            f"total={self.total_weight:.1%}, "
            f"assets={self.num_symbols_output}/{self.num_symbols_input})",
        ]
        for w in self.weights:
            flag = " [CAPPED]" if w.is_capped else ""
            lines.append(
                f"  {w.symbol}: vol={w.annualized_vol:.2%} "
                f"→ weight={w.final_weight:.2%}{flag}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """转为字典, 便于序列化 (通知/日志)."""
        return {
            "position_cap": self.position_cap,
            "total_weight": self.total_weight,
            "method": self.method,
            "weights": [
                {
                    "symbol": w.symbol,
                    "annualized_vol": round(w.annualized_vol, 6),
                    "final_weight": round(w.final_weight, 6),
                }
                for w in self.weights
            ],
        }


# ------------------------------------------------------------------
# 核心类
# ------------------------------------------------------------------

class PortfolioOptimizer:
    """波动率倒数 / 方差倒数加权组合优化器.

    Args:
        vol_lookback: 波动率回溯窗口 (交易日, 默认 20)
        max_position_weight: 单标的最大权重 (默认 0.30)
        min_positions: 最小持仓数 (默认 1)
        max_positions: 最大持仓数 (默认 5)
        method: "inverse_volatility" (w∝1/σ, 默认) | "inverse_variance" (w∝1/σ², 旧口径对照)
    """

    #: 支持的加权算法标识 (spec §5.3)
    METHODS = ("inverse_volatility", "inverse_variance")

    def __init__(
        self,
        vol_lookback: int = 20,
        max_position_weight: float = 0.30,
        min_positions: int = 1,
        max_positions: int = 5,
        method: str = "inverse_volatility",
    ) -> None:
        if method not in self.METHODS:
            raise ValueError(
                f"未知的加权方法 method={method!r}, 支持: {self.METHODS}"
            )
        self.vol_lookback = vol_lookback
        self.max_position_weight = max_position_weight
        self.min_positions = min_positions
        self.max_positions = max_positions
        self.method = method

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def optimize(
        self,
        ohlcv_df: pl.DataFrame,
        position_cap: float,
    ) -> PortfolioResult:
        """对候选标的执行波动率倒数加权.

        Args:
            ohlcv_df: OHLCV 数据, 需含 symbol, date, close
            position_cap: 宏观择时给出的仓位上限 (0.0-1.0)

        Returns:
            PortfolioResult
        """
        # 输入校验
        self._validate_input(ohlcv_df, position_cap)

        symbols = ohlcv_df["symbol"].unique().to_list()
        n_input = len(symbols)

        # 计算每只标的的年化波动率
        vol_map: dict[str, float] = {}
        for sym in symbols:
            vol = self._calc_annualized_vol(ohlcv_df, sym)
            if vol is not None and vol > 0:
                vol_map[sym] = vol
            else:
                logger.warning("%s: cannot compute volatility, excluded", sym)

        if not vol_map:
            logger.warning("No valid volatility data for any symbol, returning empty")
            return PortfolioResult(
                weights=[],
                position_cap=position_cap,
                total_weight=0.0,
                method=self.method,
                num_symbols_input=n_input,
                num_symbols_output=0,
            )

        # 单标的场景: 全部权重给唯一标的, 但受上限约束
        if len(vol_map) == 1:
            sym = next(iter(vol_map))
            final_weight = min(position_cap, self.max_position_weight)
            return PortfolioResult(
                weights=[
                    PortfolioWeight(
                        symbol=sym,
                        annualized_vol=round(vol_map[sym], 6),
                        raw_weight=1.0,
                        final_weight=round(final_weight, 6),
                        is_capped=(position_cap > self.max_position_weight),
                    )
                ],
                position_cap=position_cap,
                total_weight=round(final_weight, 6),
                method=self.method,
                vol_lookback=self.vol_lookback,
                max_position_weight=self.max_position_weight,
                num_symbols_input=n_input,
                num_symbols_output=1,
                min_annual_vol=vol_map[sym],
                max_annual_vol=vol_map[sym],
            )

        # 按算法标识计算原始权重 (C01: 标识与行为必须一致)
        raw_weights = self._calc_raw_weights(vol_map)

        # 应用仓位上限 + 单标的上限裁剪
        capped_weights = self._apply_caps(raw_weights, vol_map, position_cap)

        # 构建结果
        total_w = sum(w.final_weight for w in capped_weights)

        vols = list(vol_map.values())
        return PortfolioResult(
            weights=capped_weights,
            position_cap=position_cap,
            total_weight=round(total_w, 6),
            method=self.method,
            vol_lookback=self.vol_lookback,
            max_position_weight=self.max_position_weight,
            num_symbols_input=n_input,
            num_symbols_output=len(capped_weights),
            min_annual_vol=round(min(vols), 6),
            max_annual_vol=round(max(vols), 6),
        )

    # ------------------------------------------------------------------
    # 波动率计算
    # ------------------------------------------------------------------

    def _calc_annualized_vol(
        self,
        ohlcv_df: pl.DataFrame,
        symbol: str,
        trading_days: int = 252,
    ) -> Optional[float]:
        """计算单只标的的年化波动率.

        使用对数收益率 (CLAUDE.md §5.2), 基于过去 vol_lookback 个交易日.

        Args:
            ohlcv_df: 全量 OHLCV
            symbol: 标的代码
            trading_days: 年化交易日数

        Returns:
            年化波动率, 或 None (数据不足)
        """
        close = (
            ohlcv_df
            .filter(pl.col("symbol") == symbol)
            .sort("date")
            .tail(self.vol_lookback)["close"]
        )

        if len(close) < max(2, self.vol_lookback // 2):
            # 至少需要窗口一半的数据点
            return None

        # 对数收益率
        prices = close.to_numpy()
        if np.any(np.isnan(prices)):
            return None
        log_returns = np.log(prices[1:] / prices[:-1])
        daily_vol = np.std(log_returns, ddof=1)
        annual_vol = daily_vol * np.sqrt(trading_days)

        return float(annual_vol)

    # ------------------------------------------------------------------
    # 权重计算
    # ------------------------------------------------------------------

    def _calc_raw_weights(self, vol_map: dict[str, float]) -> dict[str, float]:
        """按 self.method 计算原始权重 (所有原始权重之和 = 1.0)."""
        if self.method == "inverse_variance":
            return self._calc_inverse_var_weights(vol_map)
        return self._calc_inverse_vol_weights(vol_map)

    @staticmethod
    def _calc_inverse_vol_weights(
        vol_map: dict[str, float],
    ) -> dict[str, float]:
        """波动率倒数加权原始权重 (inverse_volatility, spec §5.3 原意).

        w_i_raw = (1/σ_i) / Σ(1/σ_j); 所有原始权重之和 = 1.0.

        独立期望值: σ={0.10, 0.20} → 权重比 = 2:1.
        """
        inv_vol: dict[str, float] = {}
        for sym, vol in vol_map.items():
            inv_vol[sym] = 1.0 / vol

        total_inv_vol = sum(inv_vol.values())

        if total_inv_vol <= 0:
            # 不应发生 (vol > 0 保证了 inv_vol > 0)
            logger.warning("total_inv_vol == 0, using equal weight")
            n = len(vol_map)
            return {sym: 1.0 / n for sym in vol_map}

        return {sym: iv / total_inv_vol for sym, iv in inv_vol.items()}

    @staticmethod
    def _calc_inverse_var_weights(
        vol_map: dict[str, float],
    ) -> dict[str, float]:
        """方差倒数加权原始权重 (inverse_variance, v0.1 旧口径, 保留对照).

        w_i_raw = (1/σ_i²) / Σ(1/σ_j²)

        所有原始权重之和 = 1.0.

        独立期望值: σ={0.10, 0.20} → 权重比 = 4:1.
        """
        # 1/方差
        inv_var: dict[str, float] = {}
        for sym, vol in vol_map.items():
            inv_var[sym] = 1.0 / (vol * vol)

        total_inv_var = sum(inv_var.values())

        if total_inv_var <= 0:
            # 不应发生 (vol > 0 保证了 inv_var > 0)
            logger.warning("total_inv_var == 0, using equal weight")
            n = len(vol_map)
            return {sym: 1.0 / n for sym in vol_map}

        return {sym: iv / total_inv_var for sym, iv in inv_var.items()}

    def _apply_caps(
        self,
        raw_weights: dict[str, float],
        vol_map: dict[str, float],
        position_cap: float,
    ) -> list[PortfolioWeight]:
        """应用仓位上限和单标的上限裁剪.

        算法:
          1. 每个标的: scaled = raw_weight * position_cap
          2. 若 scaled > max_position_weight → 裁剪到上限
          3. 裁剪释放的权重重新分配给未裁剪标的 (按各自的 raw_weight 比例)
          4. 迭代直到没有标的超出上限, 或达到最大迭代次数

        迭代裁剪防止: A 裁剪后释放的权重分配给 B, 导致 B 也超上限.
        """
        MAX_ITER = 10

        # 初始: scaled = raw × cap
        scaled = {sym: raw_weights[sym] * position_cap for sym in raw_weights}
        capped = {sym: False for sym in raw_weights}

        for _ in range(MAX_ITER):
            overflow = 0.0
            any_capped = False

            for sym in list(scaled.keys()):
                if capped[sym]:
                    continue
                if scaled[sym] > self.max_position_weight:
                    overflow += scaled[sym] - self.max_position_weight
                    scaled[sym] = self.max_position_weight
                    capped[sym] = True
                    any_capped = True

            if not any_capped:
                break

            if overflow <= 1e-8:
                break

            # 将释放的权重按 raw_weight 比例分配给未裁剪标的
            uncapped_syms = [s for s in scaled if not capped[s]]
            if not uncapped_syms:
                # 所有标的都到上限了 (极端情况)
                break

            raw_sum_uncapped = sum(raw_weights[s] for s in uncapped_syms)
            if raw_sum_uncapped <= 0:
                break

            for sym in uncapped_syms:
                share = raw_weights[sym] / raw_sum_uncapped
                scaled[sym] += overflow * share

        # 构建结果
        result: list[PortfolioWeight] = []
        for sym in raw_weights:
            result.append(PortfolioWeight(
                symbol=sym,
                annualized_vol=round(vol_map[sym], 6),
                raw_weight=round(raw_weights[sym], 6),
                final_weight=round(scaled[sym], 6),
                is_capped=capped[sym],
            ))

        # 按权重降序排列
        result.sort(key=lambda w: w.final_weight, reverse=True)

        # 限制最大持仓数 (选 Top N)
        if len(result) > self.max_positions:
            result = result[:self.max_positions]

        return result

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_input(df: pl.DataFrame, position_cap: float) -> None:
        """校验输入数据完整性."""
        required_cols = ["symbol", "date", "close"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(
                f"OHLCV DataFrame missing required columns: {missing}"
            )

        if not (0.0 <= position_cap <= 1.0):
            raise ValueError(
                f"position_cap must be in [0, 1], got {position_cap}"
            )


# ------------------------------------------------------------------
# 工厂函数
# ------------------------------------------------------------------

def from_settings(settings: dict) -> PortfolioOptimizer:
    """从 settings.yaml 读取参数创建优化器.

    Usage:
        import yaml
        with open("config/settings.yaml") as f:
            settings = yaml.safe_load(f)
        opt = from_settings(settings)
    """
    cfg = settings.get("portfolio", {})
    return PortfolioOptimizer(
        vol_lookback=cfg.get("vol_lookback", 20),
        max_position_weight=cfg.get("max_single_weight", 0.30),
        min_positions=cfg.get("min_positions", 1),
        max_positions=cfg.get("max_positions", 5),
        method=cfg.get("method", "inverse_volatility"),
    )
