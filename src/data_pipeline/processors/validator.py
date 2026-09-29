"""数据质量护栏 — 写入 DuckDB 前的最后一道防线.

检查项目:
  1. 负价格拦截 (阻断)
  2. 产品感知的单日涨跌幅检查 (C10, spec §4.4):
     限内 pass / 超限容差内 warning (待核验) / 严重超限阻断 (疑似数据损坏或未调整权益事件)
  3. 缺失值比例报警 (警告)
  4. 时间戳连续性校验 (警告)
  5. 数据行数偏离监控 (警告)

设计原则:
  - 阻断级: 抛出异常, 阻止数据写入 DuckDB
  - 警告级: 记录日志, 不中断流程, 但触发报警
  - 真实极端行情不修改数据; 权益事件 (拆分/分红) 未调整时按疑似异常隔离 (M2/M3 处理)
  - 涨跌幅限制按产品区分: 默认 10% 板; 20% 板产品 (创业板/科创板 ETF) 经 overrides 传入

Usage:
    from data_pipeline.processors.validator import validate_ohlcv

    issues = validate_ohlcv(df, critical_only=False)
    if any(i.severity == "critical" for i in issues):
        raise DataQualityError(issues)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import polars as pl

logger = logging.getLogger(__name__)

# ---- 涨跌幅检查参数 (产品感知, C10) ----
DEFAULT_DAILY_LIMIT = 0.10   # 默认 10% 涨跌幅板 (主板/跨境/商品 ETF)
DEFAULT_LIMIT_BUFFER = 0.02  # 超限容差: (limit, limit+buffer] → 警告复核; > → 阻断


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class QualityIssue:
    """数据质量问题描述."""

    check: str          # 检查项名称
    severity: str       # "critical" | "warning"
    symbol: str         # 涉及的标的 (或 "ALL")
    detail: str         # 具体描述
    sample_rows: int = 0  # 受影响行数


class DataQualityError(Exception):
    """数据质量阻断异常 — 包含所有 critical 级别问题."""

    def __init__(self, issues: list[QualityIssue]) -> None:
        self.issues = issues
        detail = "\n  ".join(
            f"[{i.severity}] {i.check}: {i.detail} (rows={i.sample_rows})"
            for i in issues
        )
        super().__init__(f"Data quality check FAILED ({len(issues)} issues):\n  {detail}")


# ------------------------------------------------------------------
# 公共入口
# ------------------------------------------------------------------

def validate_ohlcv(
    df: pl.DataFrame,
    critical_only: bool = False,
    historical_stats: Optional[dict] = None,
    price_limits: dict[str, float] | None = None,
    default_price_limit: float = DEFAULT_DAILY_LIMIT,
    price_limit_buffer: float = DEFAULT_LIMIT_BUFFER,
    registered_equity_events: pl.DataFrame | None = None,
) -> list[QualityIssue]:
    """对 OHLCV DataFrame 执行全量质量检查.

    Args:
        df: OHLCV 数据, 列: symbol, date, open, high, low, close, volume, amount
        critical_only: True 时仅执行阻断级检查 (适用于实时 pipeline)
        historical_stats: 历史元数据 {"avg_row_count": float, "std_row_count": float}
        price_limits: symbol → 涨跌幅限制 (小数, 如 0.20); 未列出用 default_price_limit
        default_price_limit: 默认涨跌幅限制 (0.10 = 10% 板)
        price_limit_buffer: 超限容差; (limit, limit+buffer] 为警告复核区
        registered_equity_events: 已登记权益事件 (M8/I-09 方案1) — 列 symbol/event_date/ratio;
            命中日期与预期因子一致的超限跳变 → 记为已登记 (info, 不阻断); 未登记仍按原规则。

    Returns:
        QualityIssue 列表 (空列表 = 全部通过)
    """
    issues: list[QualityIssue] = []

    # ---- 阻断级: 必须全部通过, 否则不进库 ----
    issues.extend(_check_negative_prices(df))
    issues.extend(_check_daily_change(
        df,
        limits=price_limits,
        default_limit=default_price_limit,
        buffer=price_limit_buffer,
        registered_events=registered_equity_events,
    ))

    if critical_only:
        return issues

    # ---- 警告级: 记录但不阻断 ----
    issues.extend(_check_missing_values(df))
    issues.extend(_check_timestamp_continuity(df))

    if historical_stats is not None:
        issues.extend(_check_row_count(df, historical_stats))

    # 汇总日志
    criticals = [i for i in issues if i.severity == "critical"]
    warnings = [i for i in issues if i.severity == "warning"]
    infos = [i for i in issues if i.severity == "info"]

    if criticals:
        logger.error("Data quality: %d critical issue(s)", len(criticals))
        for ci in criticals:
            logger.error("  CRITICAL [%s]: %s", ci.check, ci.detail)

    if warnings:
        logger.warning("Data quality: %d warning(s)", len(warnings))
        for wi in warnings:
            logger.warning("  WARNING [%s]: %s", wi.check, wi.detail)

    for inf in infos:
        logger.info("  INFO [%s]: %s", inf.check, inf.detail)

    if not issues:
        logger.info("Data quality: all checks passed (%d rows)", len(df))

    return issues


# ------------------------------------------------------------------
# 阻断级检查
# ------------------------------------------------------------------

def _check_negative_prices(df: pl.DataFrame) -> list[QualityIssue]:
    """H4.1 子项: 价格为负的脏数据必须拦截.

    检查 open, high, low, close 是否包含负值.
    """
    issues: list[QualityIssue] = []

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            continue

        neg_mask = df[col] < 0
        neg_count = neg_mask.sum()
        if neg_count > 0:
            neg_symbols = df.filter(neg_mask)["symbol"].unique().to_list()
            issues.append(QualityIssue(
                check="negative_price",
                severity="critical",
                symbol=", ".join(neg_symbols[:5]),
                detail=(
                    f"Column '{col}' contains {neg_count} negative value(s) "
                    f"in symbol(s): {neg_symbols[:5]}{'...' if len(neg_symbols) > 5 else ''}"
                ),
                sample_rows=neg_count,
            ))

    return issues


def _check_daily_change(
    df: pl.DataFrame,
    limits: dict[str, float] | None = None,
    default_limit: float = DEFAULT_DAILY_LIMIT,
    buffer: float = DEFAULT_LIMIT_BUFFER,
    registered_events: pl.DataFrame | None = None,
) -> list[QualityIssue]:
    """产品感知的单日涨跌幅检查 (C10, spec §4.4; M8 拆分感知).

    规则:
      - |涨跌| ≤ 产品限制 → pass
      - 限制 < |涨跌| ≤ 限制+容差 → warning (疑似权益事件/舍入, 保留数据待核验)
      - |涨跌| > 限制+容差 →
          命中已登记权益事件 (日期 + 比例一致) → 已登记 (info, 不阻断)
          未登记 → critical (疑似数据损坏或未调整权益事件, 阻断)

    真实极端行情不修改数据; 20% 板产品 (如 159915/588000) 经 limits 传入。

    Args:
        df: OHLCV 数据 (需 symbol/date/close)
        limits: symbol → 涨跌幅限制 (小数)
        default_limit: 默认限制 (0.10 = 10% 板)
        buffer: 超限容差 (0.02 = 2 个百分点)
        registered_events: 已登记权益事件 (列 symbol/event_date/ratio; M8/I-09 方案1);
            ratio 语义: price_factor — 期望后一收盘 ≈ 前一收盘 / ratio。
            比例一致性判据: |实际变动 / 期望变动 − 1| ≤ 5% (比例 > 2 时) 否则 2pp。
    """
    issues: list[QualityIssue] = []

    if "close" not in df.columns or "symbol" not in df.columns or "date" not in df.columns:
        return issues

    limits = limits or {}
    events_by_key: dict[tuple[str, object], float] = {}
    if registered_events is not None and not registered_events.is_empty():
        required = {"symbol", "event_date", "ratio"}
        if required <= set(registered_events.columns):
            events_by_key = {
                (str(row["symbol"]), row["event_date"]): float(row["ratio"])
                for row in registered_events.iter_rows(named=True)
                if row["ratio"] is not None
            }

    for sym in df["symbol"].unique().to_list():
        sym_df = df.filter(pl.col("symbol") == sym).sort("date")
        if len(sym_df) < 2:
            continue

        limit = float(limits.get(sym, default_limit))
        sym_df = sym_df.with_columns(
            pl.col("close").shift(1).alias("_prev_close"),
            pl.col("close").pct_change().alias("_ret"),
        )

        registered_dates: list[object] = []
        registered_details: list[str] = []
        if events_by_key:
            for row in sym_df.iter_rows(named=True):
                key = (str(row["symbol"]), row["date"])
                if key not in events_by_key:
                    continue
                ret = row["_ret"]
                prev_close = row["_prev_close"]
                if ret is None or prev_close in (None, 0):
                    continue
                ratio = events_by_key[key]
                if ratio <= 0:
                    continue
                expected = 1.0 / ratio - 1.0  # price_factor: 拆分后理论变动
                if (
                    abs(ret - expected) <= 0.02  # 2 个百分点容差 (源数据/舍入)
                    or abs(ret - expected) <= 0.05 * abs(expected)
                ):
                    registered_dates.append(row["date"])
                    registered_details.append(
                        f"{row['date']}(实际{ret:+.2%} vs 登记因子{ratio:g}≈{expected:+.2%})"
                    )

        registered_mask = pl.Series(
            "_registered",
            [d in registered_dates for d in sym_df["date"].to_list()],
            dtype=pl.Boolean,
        )
        sym_df = sym_df.with_columns(registered_mask)
        abs_pct = sym_df["_ret"].abs()
        soft_mask = (abs_pct > limit) & (abs_pct <= limit + buffer)
        hard_mask = (abs_pct > limit + buffer) & ~sym_df["_registered"]

        if registered_dates:
            issues.append(QualityIssue(
                check="registered_equity_event",
                severity="info",
                symbol=sym,
                detail=(
                    f"{len(registered_dates)} row(s) 命中已登记权益事件 (M8/I-09 方案1): "
                    + "; ".join(registered_details[:4])
                ),
                sample_rows=len(registered_dates),
            ))

        soft_count = int(soft_mask.sum())
        hard_count = int(hard_mask.sum())

        if soft_count > 0:
            max_dev = float(abs_pct.filter(soft_mask).max())
            issues.append(QualityIssue(
                check="daily_change_review",
                severity="warning",
                symbol=sym,
                detail=(
                    f"{soft_count} row(s) move beyond {limit:.0%} limit but within "
                    f"+{buffer:.0%} tolerance (max deviation {max_dev:.2%}) — "
                    f"保留数据, 待核验权益事件/舍入"
                ),
                sample_rows=soft_count,
            ))

        if hard_count > 0:
            extreme_dates = (
                sym_df.filter(hard_mask).select("date").head(5)["date"].to_list()
            )
            max_deviation = float(abs_pct.filter(hard_mask).max())
            hint = (
                "疑似数据损坏或未调整权益事件(拆分/分红), 需人工核验"
                if not events_by_key
                else (
                    "疑似数据损坏或未登记权益事件, 需人工核验; 如为已知权益事件可登记 "
                    "(scripts/register_equity_events.py)"
                )
            )
            issues.append(QualityIssue(
                check="extreme_daily_change",
                severity="critical",
                symbol=sym,
                detail=(
                    f"{hard_count} row(s) with |pct_change| > {limit + buffer:.0%} "
                    f"(product limit {limit:.0%} + tolerance {buffer:.0%}), "
                    f"max deviation = {max_deviation:.2%}, "
                    f"example dates: {[str(d) for d in extreme_dates]} — "
                    f"{hint}"
                ),
                sample_rows=hard_count,
            ))

    return issues


# ------------------------------------------------------------------
# 警告级检查
# ------------------------------------------------------------------

def _check_missing_values(
    df: pl.DataFrame,
    max_missing_pct: float = 0.05,
) -> list[QualityIssue]:
    """检查关键列的缺失值比例是否超标.

    对 OHLCV 的 4 个价格列和 volume 列检查 null 比例.
    """
    issues: list[QualityIssue] = []

    critical_cols = ["open", "high", "low", "close", "volume", "date", "symbol"]
    total_rows = len(df)

    for col in critical_cols:
        if col not in df.columns:
            issues.append(QualityIssue(
                check="missing_column",
                severity="warning",
                symbol="ALL",
                detail=f"Expected column '{col}' is missing entirely",
                sample_rows=total_rows,
            ))
            continue

        null_count = df[col].null_count()
        null_pct = null_count / total_rows if total_rows > 0 else 0

        if null_pct > max_missing_pct:
            issues.append(QualityIssue(
                check="excessive_missing",
                severity="warning",
                symbol="ALL",
                detail=(
                    f"Column '{col}': {null_count}/{total_rows} "
                    f"({null_pct:.1%}) missing, exceeds threshold {max_missing_pct:.0%}"
                ),
                sample_rows=null_count,
            ))

    return issues


def _check_timestamp_continuity(
    df: pl.DataFrame,
    max_gap_days: int = 5,
) -> list[QualityIssue]:
    """检查时间戳连续性 — 相邻交易日之间不应有过大间隔.

    对每只标的独立检查, 相邻两行的 date 差若超过 max_gap_days
    个自然日, 记录警告. (注意: 周五→周一 只有 3 天, 属于正常)

    Args:
        max_gap_days: 允许的最大自然日间隔 (默认 5 天 = 跨一周)
    """
    issues: list[QualityIssue] = []

    if "symbol" not in df.columns or "date" not in df.columns:
        return issues

    symbols = df["symbol"].unique().to_list()
    for sym in symbols:
        sym_df = df.filter(pl.col("symbol") == sym).sort("date")

        if len(sym_df) < 2:
            continue

        dates = sym_df["date"]
        gaps = dates.diff().drop_nulls()
        gap_days = gaps.dt.total_days()

        # 自然日间隔 > max_gap_days 视为异常
        gap_mask = gap_days > max_gap_days
        gap_count = int(gap_mask.sum())

        if gap_count > 0:
            # 找出第一个断点的日期
            gap_indices = gap_mask.arg_true().head(3).to_list()
            gap_details = []
            for idx in gap_indices:
                prev_date = dates[int(idx)]
                next_date = dates[int(idx) + 1]
                gap_d = gap_days[int(idx)]
                gap_details.append(f"{prev_date}→{next_date} ({gap_d}d)")

            issues.append(QualityIssue(
                check="timestamp_gap",
                severity="warning",
                symbol=sym,
                detail=(
                    f"{gap_count} timestamp gap(s) > {max_gap_days} days: "
                    f"{gap_details}"
                ),
                sample_rows=gap_count,
            ))

    return issues


def _check_row_count(
    df: pl.DataFrame,
    historical_stats: dict,
    threshold_sigma: float = 3.0,
) -> list[QualityIssue]:
    """H4.3: 当日拉取行数与历史均值偏离 > threshold_sigma 个标准差时报警.

    Args:
        df: 当日数据
        historical_stats: {"avg_row_count": float, "std_row_count": float}
        threshold_sigma: 标准差倍数阈值 (默认 3)
    """
    issues: list[QualityIssue] = []

    avg = historical_stats.get("avg_row_count")
    std = historical_stats.get("std_row_count")

    if avg is None or std is None or std == 0:
        logger.debug("Insufficient historical stats for row count check")
        return issues

    current_rows = len(df)
    deviation = abs(current_rows - avg)
    sigma = deviation / std

    if sigma > threshold_sigma:
        issues.append(QualityIssue(
            check="row_count_deviation",
            severity="warning",
            symbol="ALL",
            detail=(
                f"Current row count ({current_rows}) deviates from "
                f"20-day mean ({avg:.0f} ± {std:.0f}) by {sigma:.1f}σ "
                f"(threshold: {threshold_sigma:.0f}σ)"
            ),
            sample_rows=current_rows,
        ))

    return issues


# ------------------------------------------------------------------
# 辅助: 计算历史统计 (供 daily_pipeline.py 使用)
# ------------------------------------------------------------------

def compute_historical_stats(
    historical_df: pl.DataFrame,
    lookback_days: int = 20,
) -> dict:
    """从历史数据的每日行数计算均值和标准差.

    Args:
        historical_df: 包含 'date' 列的历史数据
        lookback_days: 回溯天数

    Returns:
        {"avg_row_count": float, "std_row_count": float}
    """
    if historical_df.is_empty() or "date" not in historical_df.columns:
        return {"avg_row_count": None, "std_row_count": None}

    daily_counts = (
        historical_df
        .group_by("date")
        .agg(pl.len().alias("row_count"))
        .sort("date")
        .tail(lookback_days)
    )

    if len(daily_counts) < 5:
        return {"avg_row_count": None, "std_row_count": None}

    avg = daily_counts["row_count"].mean()
    std = daily_counts["row_count"].std(ddof=0)

    return {
        "avg_row_count": avg,
        "std_row_count": std if std is not None and std > 0 else None,
    }


# ------------------------------------------------------------------
# CLI 入口 (用于独立测试)
# ------------------------------------------------------------------

def main() -> None:
    """CLI: 对指定 DuckDB 中的数据运行质量检查."""
    import sys
    from pathlib import Path

    import yaml
    from data_pipeline.duckdb_manager import DuckDBManager

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    project_root = Path(__file__).resolve().parents[2]
    settings_path = project_root / "config" / "settings.yaml"

    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    db_path = project_root / settings["storage"]["db_path"]
    db = DuckDBManager(str(db_path))

    if not db.table_exists("ohlcv_daily"):
        logger.error("Table 'ohlcv_daily' not found. Run fetch-data first.")
        sys.exit(1)

    df = db.get_ohlcv()
    logger.info("Loaded %d rows for quality check", len(df))

    stats = compute_historical_stats(df)
    issues = validate_ohlcv(df, historical_stats=stats)

    if issues:
        logger.warning("Quality check found %d issue(s)", len(issues))
    else:
        logger.info("Quality check: ALL CLEAR")

    db.close()


if __name__ == "__main__":
    main()
