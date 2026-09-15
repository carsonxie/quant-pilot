"""标的清洗模块 — 五层决策漏斗第二层.

决定可交易池: 剔除流动性枯竭、严重溢价、上市过短的标的,
并对流动性集中度触发警告.

筛选规则 (来自 spec.md §3 第二层):
  1. 剔除日均成交额 < 500 万 (流动性枯竭)
  2. 剔除价格偏离 IOPV/NAV > 3% (严重溢价, 跨境/行业 ETF 重点)
  3. 剔除上市 < 60 个交易日 (新标的观察期)
  4. 流动性集中度检查: 计划持仓 / 日均成交额 > 0.5% → 警告

Usage:
    uf = UniverseFilter(min_avg_volume=5_000_000, min_list_days=60)
    result = uf.filter(ohlcv_df, metadata_df)
    print(result.summary())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import polars as pl

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class UniverseFilterResult:
    """标的清洗结果.

    Attributes:
        passed: 通过筛选的标的 symbol 列表 (按 symbol 排序)
        failed: symbol → 失败原因列表 的映射
        warnings: symbol → 警告信息列表 的映射 (通过但存在风险)
        unknown: symbol → 待核验原因列表 的映射 (如 NAV 缺失; C10: 不得静默通过)
        stats: 统计摘要 (n_total, n_passed, n_failed, n_warnings, n_unknown)
    """

    passed: list[str] = field(default_factory=list)
    failed: dict[str, list[str]] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)
    unknown: dict[str, list[str]] = field(default_factory=dict)

    @property
    def n_total(self) -> int:
        """候选标的总数."""
        return len(self.passed) + len(self.failed)

    @property
    def n_passed(self) -> int:
        """通过数."""
        return len(self.passed)

    @property
    def n_failed(self) -> int:
        """剔除数."""
        return len(self.failed)

    @property
    def n_warnings(self) -> int:
        """警告数."""
        return len(self.warnings)

    @property
    def n_unknown(self) -> int:
        """待核验 (unknown) 数."""
        return len(self.unknown)

    def summary(self) -> str:
        """人类可读摘要."""
        lines = [
            f"UniverseFilter: {self.n_passed}/{self.n_total} passed",
        ]
        if self.failed:
            lines.append("  Failed:")
            for sym, reasons in sorted(self.failed.items()):
                lines.append(f"    {sym}: {', '.join(reasons)}")
        if self.warnings:
            lines.append("  Warnings:")
            for sym, msgs in sorted(self.warnings.items()):
                lines.append(f"    {sym}: {', '.join(msgs)}")
        if self.unknown:
            lines.append("  Unknown (待核验, 不视为通过; 完整禁买规则见 M3):")
            for sym, msgs in sorted(self.unknown.items()):
                lines.append(f"    {sym}: {', '.join(msgs)}")
        return "\n".join(lines)


@dataclass
class SymbolEvaluation:
    """单标的分项检查结果 (pass/fail/unknown/not_applicable + 原因, 不静默通过)."""

    symbol: str
    checks: dict[str, str] = field(default_factory=dict)
    fail_reasons: list[str] = field(default_factory=list)
    warn_msgs: list[str] = field(default_factory=list)
    unknown_msgs: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# 核心类
# ------------------------------------------------------------------

class UniverseFilter:
    """标的清洗过滤器.

    Args:
        min_avg_volume: 日均成交额最低门槛 (元), 默认 5,000,000 (500万)
        min_list_days: 最低上市交易日数, 默认 60
        max_premium_pct: 最大溢价率 (%), 默认 3.0
        volume_lookback: 日均成交额计算回溯窗口, 默认 20
        liquidity_warn_threshold: 流动性集中度警告阈值,
            计划持仓/日均成交额超过此值触发警告, 默认 0.005 (0.5%)
    """

    def __init__(
        self,
        min_avg_volume: float = 5_000_000,
        min_list_days: int = 60,
        max_premium_pct: float = 3.0,
        volume_lookback: int = 20,
        liquidity_warn_threshold: float = 0.005,
    ) -> None:
        self.min_avg_volume = min_avg_volume
        self.min_list_days = min_list_days
        self.max_premium_pct = max_premium_pct
        self.volume_lookback = volume_lookback
        self.liquidity_warn_threshold = liquidity_warn_threshold

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def filter(
        self,
        ohlcv_df: pl.DataFrame,
        metadata_df: pl.DataFrame | None = None,
        nav_df: pl.DataFrame | None = None,
        planned_capital: float = 100_000,
    ) -> UniverseFilterResult:
        """对候选标的池执行全部筛选规则.

        Args:
            ohlcv_df: OHLCV 日线数据
                必须包含: symbol, date, close, amount
                至少需要 volume_lookback 个交易日的数据
            metadata_df: ETF 元数据 (可选)
                若提供, 需包含: symbol, list_date
                若为 None, 从 ohlcv_df 推断 list_date
            nav_df: NAV/IOPV 净值数据 (可选)
                若提供, 需包含: symbol, date, nav (或 iopv)
                若为 None, 溢价检查跳过并记录 warning
            planned_capital: 计划投入总资金 (元), 用于流动性集中度检查,
                默认 100,000

        Returns:
            UniverseFilterResult 包含通过/剔除/警告的完整结果
        """
        evaluations = self.evaluate(
            ohlcv_df, metadata_df=metadata_df, nav_df=nav_df,
            planned_capital=planned_capital,
        )
        result = UniverseFilterResult()
        for sym, ev in evaluations.items():
            if ev.fail_reasons:
                result.failed[sym] = ev.fail_reasons
            else:
                result.passed.append(sym)
                if ev.warn_msgs:
                    result.warnings[sym] = ev.warn_msgs
            if ev.unknown_msgs:
                result.unknown[sym] = ev.unknown_msgs

        result.passed.sort()
        logger.info(
            "UniverseFilter: %d/%d passed, %d warnings, %d unknown",
            result.n_passed, result.n_total, result.n_warnings, result.n_unknown,
        )
        return result

    def evaluate(
        self,
        ohlcv_df: pl.DataFrame,
        metadata_df: pl.DataFrame | None = None,
        nav_df: pl.DataFrame | None = None,
        planned_capital: float = 100_000,
    ) -> dict[str, SymbolEvaluation]:
        """结构化分项检查 (pass/fail/unknown/not_applicable + 原因).

        供四层池 (etf_pool) 生成买卖许可; filter() 由此构建兼容旧接口的结果.
        """
        symbols = self._validate_and_get_symbols(ohlcv_df)
        if not symbols:
            return {}

        metrics = self._compute_metrics(ohlcv_df, metadata_df, nav_df)
        out: dict[str, SymbolEvaluation] = {}
        for sym in symbols:
            m = metrics.get(sym, {})
            ev = SymbolEvaluation(symbol=sym)

            # 成交额
            avg_amount = float(m.get("avg_amount", 0.0))
            if avg_amount < self.min_avg_volume:
                ev.checks["volume"] = "fail"
                ev.fail_reasons.append(
                    f"日均成交额 {avg_amount:,.0f} < {self.min_avg_volume:,.0f}"
                )
            else:
                ev.checks["volume"] = "pass"

            # 上市天数
            n_days = int(m.get("n_days", 0))
            if n_days < self.min_list_days:
                ev.checks["listing"] = "fail"
                ev.fail_reasons.append(
                    f"上市 {n_days} 个交易日 < {self.min_list_days}"
                )
            else:
                ev.checks["listing"] = "pass"

            # 溢价 (NAV 缺失 → unknown, 不静默)
            nav = m.get("latest_nav")
            if nav is None or nav <= 0:
                ev.checks["premium"] = "unknown"
                ev.unknown_msgs.append(
                    "NAV/IOPV 数据缺失, 溢价检查 unknown (不得视为通过)"
                )
            else:
                close = float(m.get("latest_close", 0.0))
                if close <= 0:
                    ev.checks["premium"] = "unknown"
                    ev.unknown_msgs.append("最新收盘价缺失, 溢价检查 unknown")
                else:
                    premium_pct = abs(close - nav) / nav * 100.0
                    if premium_pct > self.max_premium_pct:
                        ev.checks["premium"] = "fail"
                        ev.fail_reasons.append(
                            f"溢价率 {premium_pct:.2f}% > {self.max_premium_pct}%"
                        )
                    else:
                        ev.checks["premium"] = "pass"

            # 流动性集中度警告 (仅对无失败项的标的)
            if not ev.fail_reasons:
                self._check_concentration(
                    sym, m, planned_capital, ev.warn_msgs
                )
            out[sym] = ev
        return out

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_and_get_symbols(df: pl.DataFrame) -> list[str]:
        """校验输入 DataFrame 并返回标的列表."""
        if df.is_empty():
            logger.warning("UniverseFilter: empty input DataFrame")
            return []

        required = {"symbol", "date", "close", "amount"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"OHLCV DataFrame missing required columns: {missing}. "
                f"Got: {sorted(df.columns)}"
            )

        symbols = df["symbol"].unique().sort().to_list()
        logger.debug("UniverseFilter: %d candidate symbols", len(symbols))
        return symbols

    # ------------------------------------------------------------------
    # 指标计算
    # ------------------------------------------------------------------

    def _compute_metrics(
        self,
        ohlcv_df: pl.DataFrame,
        metadata_df: pl.DataFrame | None,
        nav_df: pl.DataFrame | None,
    ) -> dict[str, dict]:
        """为每个标的预计算筛选所需指标.

        Returns:
            {symbol: {avg_amount, n_days, latest_close, latest_nav, ...}}
        """
        metrics: dict[str, dict] = {}

        for sym in ohlcv_df["symbol"].unique().to_list():
            sym_df = ohlcv_df.filter(pl.col("symbol") == sym).sort("date")
            m: dict = {}

            # 日均成交额 (20日滚动均值的最新值)
            amount_series = sym_df["amount"]
            if len(amount_series) >= self.volume_lookback:
                avg_amount = float(
                    amount_series.tail(self.volume_lookback).mean()
                )
            elif len(amount_series) > 0:
                # 数据不足回溯窗口, 用全部可用数据
                avg_amount = float(amount_series.mean())
            else:
                avg_amount = 0.0
            m["avg_amount"] = avg_amount

            # 最新收盘价
            m["latest_close"] = float(sym_df["close"].tail(1)[0])

            # 上市天数: 优先 metadata.list_date, 否则用最早 OHLCV date
            m["list_date"] = self._resolve_list_date(sym, sym_df, metadata_df)
            if m["list_date"] is not None:
                latest_date = sym_df["date"].max()
                m["n_days"] = _count_trading_days(m["list_date"], latest_date, sym_df)
            else:
                m["n_days"] = len(sym_df)

            # 最新 NAV (如果可用)
            m["latest_nav"] = None
            if nav_df is not None and not nav_df.is_empty():
                nav_col = "nav" if "nav" in nav_df.columns else (
                    "iopv" if "iopv" in nav_df.columns else None
                )
                if nav_col is not None:
                    sym_nav = nav_df.filter(
                        pl.col("symbol") == sym
                    ).sort("date")
                    if not sym_nav.is_empty():
                        m["latest_nav"] = float(sym_nav[nav_col].tail(1)[0])

            metrics[sym] = m

        return metrics

    @staticmethod
    def _resolve_list_date(
        sym: str,
        sym_df: pl.DataFrame,
        metadata_df: pl.DataFrame | None,
    ) -> pl.Date | None:
        """获取标的上市场日期.

        优先从 metadata_df 读取, 否则以 OHLCV 最早日期作为近似.
        """
        if (
            metadata_df is not None
            and not metadata_df.is_empty()
            and "list_date" in metadata_df.columns
        ):
            meta_row = metadata_df.filter(pl.col("symbol") == sym)
            if not meta_row.is_empty():
                ld = meta_row["list_date"][0]
                if ld is not None:
                    return ld

        # fallback: OHLCV 最早日期
        return sym_df["date"].min()

    # ------------------------------------------------------------------
    # 筛选方法
    # ------------------------------------------------------------------

    def _check_volume(
        self, sym: str, metrics: dict, reasons: list[str],
    ) -> None:
        """日均成交额检查."""
        avg_amount = metrics.get("avg_amount", 0.0)
        if avg_amount < self.min_avg_volume:
            reasons.append(
                f"日均成交额 {avg_amount:,.0f} < {self.min_avg_volume:,.0f}"
            )

    def _check_list_days(
        self, sym: str, metrics: dict, reasons: list[str],
    ) -> None:
        """上市天数检查."""
        n_days = metrics.get("n_days", 0)
        if n_days < self.min_list_days:
            reasons.append(
                f"上市 {n_days} 个交易日 < {self.min_list_days}"
            )

    def _check_premium(
        self, sym: str, metrics: dict, reasons: list[str],
        unknown_reasons: list[str],
    ) -> None:
        """溢价/折价检查 (价格偏离 NAV > 阈值).

        NAV 缺失/无效 → unknown 记录 (spec §4.4: 不能静默通过; 完整禁买规则 M3).
        """
        nav = metrics.get("latest_nav")
        if nav is None or nav <= 0:
            unknown_reasons.append(
                "NAV/IOPV 数据缺失, 溢价检查 unknown (不得视为通过; 禁新买待 M3)"
            )
            return

        close = metrics.get("latest_close", 0.0)
        if close <= 0:
            return

        premium_pct = abs(close - nav) / nav * 100.0
        if premium_pct > self.max_premium_pct:
            reasons.append(
                f"溢价率 {premium_pct:.2f}% > {self.max_premium_pct}%"
            )

    def _check_concentration(
        self, sym: str, metrics: dict, planned_capital: float,
        warnings: list[str],
    ) -> None:
        """流动性集中度检查 (警告, 不剔除)."""
        avg_amount = metrics.get("avg_amount", 0.0)
        if avg_amount <= 0:
            return

        concentration = planned_capital / avg_amount
        if concentration > self.liquidity_warn_threshold:
            warnings.append(
                f"流动性集中度 {concentration:.4f} "
                f"({concentration*100:.2f}%) > {self.liquidity_warn_threshold:.4f} "
                f"({self.liquidity_warn_threshold*100:.2f}%), "
                f"日均成交额 {avg_amount:,.0f}"
            )


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------

def _count_trading_days(
    start_date: pl.Date,
    end_date: pl.Date,
    sym_df: pl.DataFrame,
) -> int:
    """计算 start_date 到 end_date 之间该标的实际交易天数.

    使用 DataFrame 中实际存在的行数, 而非日历日差,
    因为不同标的可能在不同日期停牌/无数据.
    """
    filtered = sym_df.filter(
        (pl.col("date") >= start_date) & (pl.col("date") <= end_date)
    )
    return len(filtered)


# ------------------------------------------------------------------
# 便捷工厂函数
# ------------------------------------------------------------------

def from_settings(settings: dict) -> UniverseFilter:
    """从 settings.yaml 的 universe_filter 节创建 UniverseFilter 实例.

    Args:
        settings: 完整的 settings.yaml 字典 (或 universe_filter 子节)

    Returns:
        配置好的 UniverseFilter 实例
    """
    # 兼容传入完整 settings 或 universe_filter 子节
    uf_config = settings.get("universe_filter", settings)

    return UniverseFilter(
        min_avg_volume=uf_config.get("min_avg_volume", 5_000_000),
        min_list_days=uf_config.get("min_list_days", 60),
        max_premium_pct=uf_config.get("max_premium_pct", 3.0),
        liquidity_warn_threshold=uf_config.get("liquidity_concentration_warn", 0.005),
    )
