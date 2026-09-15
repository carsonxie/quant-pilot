"""产品对照与相关性诊断 (M3-06; C15 数据侧).

原则 (spec §4.3):
  - 相关性矩阵/聚类只用于发现冗余, 必须报告窗口、有效样本与未知字段,
    不按单一阈值自动删掉所有相似产品.
  - 同指数产品比较属于工具比较 (跟踪质量/费率/流动性); 不同指数属于暴露设计,
    不在本模块裁决优劣; 缺字段留空说明, 不伪造产品优劣.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import polars as pl


@dataclass
class CorrelationReport:
    """相关性诊断报告 (矩阵行列均为 symbol)."""

    window_start: date | None
    window_end: date | None
    n_days: int                       # 对齐后的窗口交易日数
    min_samples: int
    symbols: list[str] = field(default_factory=list)
    matrix: pl.DataFrame = field(default_factory=pl.DataFrame)
    samples: dict[str, int] = field(default_factory=dict)   # 各标的进入矩阵的共同收益日数
    skipped: dict[str, str] = field(default_factory=dict)   # 样本不足等原因

    def summary(self) -> str:
        lines = [
            f"相关性诊断: {len(self.symbols)} 只 | 窗口 {self.window_start}~"
            f"{self.window_end} ({self.n_days} 交易日, min_samples={self.min_samples})",
        ]
        if self.skipped:
            lines.append(
                "  跳过: " + "; ".join(f"{s}({r})" for s, r in self.skipped.items())
            )
        return "\n".join(lines)


def correlation_report(
    ohlcv: pl.DataFrame,
    window_days: int = 250,
    min_samples: int = 60,
) -> CorrelationReport:
    """基于共同交易日对数收益的 Pearson 相关矩阵 (逐点对齐, 报告样本).

    样本不足的标的进入 skipped, 不参与矩阵; 不按阈值自动删池.
    """
    if ohlcv.is_empty():
        return CorrelationReport(None, None, 0, min_samples)
    required = {"symbol", "date", "close"}
    missing = required - set(ohlcv.columns)
    if missing:
        raise ValueError(f"ohlcv 缺少必需列: {sorted(missing)}")

    wide = (
        ohlcv.select(["symbol", "date", "close"])
        .pivot(on="symbol", index="date", values="close")
        .sort("date")
        .tail(window_days)
    )
    n_days = wide.height
    all_symbols = [col for col in wide.columns if col != "date"]

    skipped: dict[str, str] = {}
    kept: list[str] = []
    for symbol in all_symbols:
        non_null = wide[symbol].drop_nulls().len()
        if non_null < min_samples + 1:
            skipped[symbol] = f"有效样本 {non_null} < {min_samples}+1"
        else:
            kept.append(symbol)

    matrix = pl.DataFrame()
    samples: dict[str, int] = {}
    if len(kept) >= 2:
        returns = (
            wide.select(["date"] + kept)
            .with_columns([
                (pl.col(s) / pl.col(s).shift(1)).log().alias(s) for s in kept
            ])
            .drop_nulls()
        )
        if returns.height >= min_samples:
            arr = returns.select(kept).to_numpy()
            corr = np.corrcoef(arr, rowvar=False)
            matrix = pl.DataFrame(
                {"symbol": kept}
                | {sym: [float(v) for v in corr[i]] for i, sym in enumerate(kept)}
            )
            for sym in kept:
                samples[sym] = returns.height
        else:
            for sym in kept:
                skipped[sym] = f"共同收益日 {returns.height} < {min_samples}"
            kept = []
    else:
        for sym in kept:
            skipped[sym] = "可用标的不足 2 只, 无法计算相关"
        kept = []

    return CorrelationReport(
        window_start=wide["date"][0] if n_days else None,
        window_end=wide["date"][-1] if n_days else None,
        n_days=n_days,
        min_samples=min_samples,
        symbols=kept,
        matrix=matrix,
        samples=samples,
        skipped=skipped,
    )


def same_index_groups(instruments_df: pl.DataFrame) -> dict[str, list[str]]:
    """同指数候选分组 (index_name 相同且 ≥2 只)."""
    if instruments_df.is_empty() or "index_name" not in instruments_df.columns:
        return {}
    groups: dict[str, list[str]] = {}
    for row in instruments_df.iter_rows(named=True):
        index_name = row.get("index_name")
        if index_name:
            groups.setdefault(str(index_name), []).append(str(row["symbol"]))
    return {
        name: sorted(symbols)
        for name, symbols in sorted(groups.items())
        if len(symbols) >= 2
    }


def compare_products(
    ohlcv: pl.DataFrame,
    instruments_df: pl.DataFrame,
    index_name: str,
    window_days: int = 250,
    min_samples: int = 60,
) -> pl.DataFrame:
    """同指数产品对照表 (窗口/样本明示; 缺字段留空, 不做优劣裁决)."""
    symbols = same_index_groups(instruments_df).get(index_name, [])
    if not symbols:
        return pl.DataFrame()
    meta: dict[str, dict] = {}
    if not instruments_df.is_empty():
        meta = {str(row["symbol"]): row for row in instruments_df.iter_rows(named=True)}

    rows = []
    for symbol in symbols:
        sub = ohlcv.filter(pl.col("symbol") == symbol).sort("date")
        row: dict = {
            "symbol": symbol,
            "name": (meta.get(symbol) or {}).get("name"),
            "n_days": 0,
            "window_start": None,
            "window_end": None,
            "annualized_vol": None,
            "period_return": None,
            "avg_amount_20d": None,
            "note": "无数据",
        }
        if not sub.is_empty():
            sub = sub.tail(window_days)
            closes = sub["close"].to_numpy()
            n = len(closes)
            returns = np.diff(np.log(closes)) if n >= 2 else np.array([])
            row.update({
                "n_days": n,
                "window_start": sub["date"][0],
                "window_end": sub["date"][-1],
                "annualized_vol": (
                    float(np.std(returns, ddof=1) * np.sqrt(252))
                    if len(returns) >= 2 else None
                ),
                "period_return": (
                    float(closes[-1] / closes[0] - 1.0) if n >= 2 else None
                ),
                "avg_amount_20d": (
                    float(sub["amount"].tail(20).mean())
                    if "amount" in sub.columns else None
                ),
                "note": (None if n >= min_samples else f"样本 {n} < {min_samples}"),
            })
        rows.append(row)
    return pl.DataFrame(rows)
