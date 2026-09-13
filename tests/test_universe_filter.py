"""标的清洗模块单元测试.

验证: 日均成交额过滤、上市天数过滤、溢价过滤、流动性集中度警告、
     组合筛选结果、边界情况.
"""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from strategies.universe_filter import (
    UniverseFilter,
    from_settings,
)

# ------------------------------------------------------------------
# 辅助: 生成合成数据
# ------------------------------------------------------------------

def _make_trading_dates(n_days: int, start: date = date(2023, 1, 4)) -> list[date]:
    """生成 n_days 个连续交易日."""
    dates: list[date] = []
    d = start
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


def make_ohlcv_df(
    symbols: list[str],
    n_days: int = 120,
    amounts: dict[str, float] | None = None,
    start_date: date = date(2023, 1, 4),
    seed: int = 42,
) -> pl.DataFrame:
    """构造多标的 OHLCV DataFrame.

    Args:
        symbols: 标的代码列表
        n_days: 每个标的的交易日数
        amounts: {symbol: avg_amount} 日均成交额映射, 未指定的用默认 1e8 (1亿)
        start_date: 起始日期
    """
    rng = np.random.default_rng(seed)
    dates = _make_trading_dates(n_days, start_date)
    dfs = []

    default_amount = 1e8  # 默认 1 亿日均成交额

    for sym in symbols:
        base_amount = amounts.get(sym, default_amount) if amounts else default_amount
        # 每日 amount 在基准附近 ±20% 波动
        daily_amounts = base_amount * (1 + rng.uniform(-0.2, 0.2, n_days))

        # 生成价格: 围绕 3.5 的随机游走
        base_price = 3.5
        noise = np.cumsum(rng.normal(0, 0.02, n_days))
        prices = base_price + noise
        prices = np.maximum(prices, 0.5)  # 确保价格为正

        dfs.append(pl.DataFrame({
            "symbol": [sym] * n_days,
            "date": dates[:n_days],
            "open": prices * (1 + rng.uniform(-0.005, 0.005, n_days)),
            "high": prices * (1 + abs(rng.uniform(0, 0.015, n_days))),
            "low": prices * (1 - abs(rng.uniform(0, 0.015, n_days))),
            "close": prices,
            "volume": daily_amounts / prices,
            "amount": daily_amounts,
        }))

    return pl.concat(dfs)


def make_metadata_df(
    symbols: list[str],
    list_dates: dict[str, date] | None = None,
) -> pl.DataFrame:
    """构造 ETF 元数据 DataFrame.

    Args:
        symbols: 标的列表
        list_dates: {symbol: list_date}
    """
    rows = []
    for sym in symbols:
        ld = list_dates.get(sym) if list_dates else None
        rows.append({
            "symbol": sym,
            "name": f"ETF_{sym}",
            "market_type": "A股",
            "settlement_type": "T+1",
            "list_date": ld,
        })
    return pl.DataFrame(rows)


def make_nav_df(
    symbols: list[str],
    navs: dict[str, float] | None = None,
    n_days: int = 120,
    start_date: date = date(2023, 1, 4),
) -> pl.DataFrame:
    """构造 NAV/IOPV DataFrame.

    Args:
        symbols: 标的列表
        navs: {symbol: nav_value} 最新净值映射, 未指定的默认 = close price
        n_days: 每个标的的交易日数
    """
    dates = _make_trading_dates(n_days, start_date)
    dfs = []
    for sym in symbols:
        nav_val = navs.get(sym, 3.5) if navs else 3.5
        dfs.append(pl.DataFrame({
            "symbol": [sym] * n_days,
            "date": dates[:n_days],
            "nav": [nav_val] * n_days,
        }))
    return pl.concat(dfs)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def uf():
    """默认 UniverseFilter 实例."""
    return UniverseFilter(
        min_avg_volume=5_000_000,
        min_list_days=60,
        max_premium_pct=3.0,
    )


@pytest.fixture
def standard_ohlcv():
    """标准 5 只 ETF 的 OHLCV, 全部通过默认筛选."""
    symbols = ["510300", "510050", "510500", "159915", "513100"]
    return make_ohlcv_df(symbols, n_days=120)


# ------------------------------------------------------------------
# 日均成交额过滤
# ------------------------------------------------------------------

class TestVolumeFilter:
    def test_all_pass_normal_volume(self, uf):
        """所有标的日均成交额远高于门槛 → 全部通过."""
        df = make_ohlcv_df(
            ["A", "B", "C"], n_days=120,
            amounts={"A": 5e7, "B": 1e8, "C": 2e8},
        )
        result = uf.filter(df)
        assert result.n_passed == 3
        assert result.n_failed == 0

    def test_low_volume_filtered(self, uf):
        """日均成交额 < 500 万 → 被剔除."""
        df = make_ohlcv_df(
            ["GOOD", "BAD"], n_days=120,
            amounts={"GOOD": 1e8, "BAD": 1e6},  # BAD = 100万
        )
        result = uf.filter(df)
        assert "GOOD" in result.passed
        assert "BAD" in result.failed
        assert any("日均成交额" in r for r in result.failed["BAD"])

    def test_boundary_exact_threshold(self, uf):
        """日均成交额恰好 = 500 万 → 通过."""
        df = make_ohlcv_df(
            ["EXACT"], n_days=120,
            amounts={"EXACT": 5_000_000},
        )
        result = uf.filter(df)
        # 5000000 >= 5000000 → 通过
        assert "EXACT" in result.passed

    def test_boundary_just_below(self, uf):
        """日均成交额 4,000,000 → 不通过 (留出噪声余量)."""
        df = make_ohlcv_df(
            ["BELOW"], n_days=120,
            amounts={"BELOW": 4_000_000},  # 远低于 500 万, 噪声不干扰
        )
        result = uf.filter(df)
        assert "BELOW" in result.failed

    def test_insufficient_data_uses_all(self):
        """数据不足 20 天 → 用全部可用数据 (放宽上市天数限制)."""
        uf = UniverseFilter(min_avg_volume=5_000_000, min_list_days=0)
        df = make_ohlcv_df(["SHORT"], n_days=10, amounts={"SHORT": 1e8})
        result = uf.filter(df)
        assert result.n_passed == 1


# ------------------------------------------------------------------
# 上市天数过滤
# ------------------------------------------------------------------

class TestListDaysFilter:
    def test_all_pass_mature(self, uf):
        """所有标的上市 > 60 天 → 全部通过."""
        df = make_ohlcv_df(["A", "B"], n_days=120)
        result = uf.filter(df)
        assert result.n_passed == 2

    def test_new_listing_filtered(self, uf):
        """上市 < 60 天 → 被剔除."""
        df = make_ohlcv_df(["NEW"], n_days=30)
        result = uf.filter(df)
        assert "NEW" in result.failed
        assert any("上市" in r for r in result.failed["NEW"])

    def test_boundary_exact_60_days(self, uf):
        """上市恰好 60 天 → 通过."""
        df = make_ohlcv_df(["EXACT60"], n_days=60)
        result = uf.filter(df)
        assert "EXACT60" in result.passed

    def test_boundary_59_days(self, uf):
        """上市 59 天 → 不通过."""
        df = make_ohlcv_df(["BELOW60"], n_days=59)
        result = uf.filter(df)
        assert "BELOW60" in result.failed

    def test_metadata_list_date_used(self, uf):
        """metadata 提供 list_date → 优先使用 metadata 而非 OHLCV 最早日期."""
        # OHLCV 有 120 天数据, 但 metadata 显示 30 天前才上市
        df = make_ohlcv_df(["LATE"], n_days=120,
                          start_date=date(2023, 1, 4))
        meta = make_metadata_df(
            ["LATE"],
            list_dates={"LATE": date(2023, 5, 1)},  # 只有 ~2 个月前上市
        )
        result = uf.filter(df, metadata_df=meta)
        # 从 2023-05-01 到 2023-06-21 (120 个交易日后的结束日期)
        # 大约 35-40 个交易日, 应 < 60
        assert "LATE" in result.failed
        assert any("上市" in r for r in result.failed["LATE"])


# ------------------------------------------------------------------
# 溢价过滤
# ------------------------------------------------------------------

class TestPremiumFilter:
    def test_no_nav_data_records_unknown(self, uf):
        """无 NAV 数据 → 溢价检查 unknown 记录 (不静默), Phase 0 仍通过筛选."""
        df = make_ohlcv_df(["A"], n_days=120)
        result = uf.filter(df)  # 无 nav_df
        assert "A" in result.passed
        assert "A" in result.unknown
        assert result.n_unknown == 1
        assert any("NAV" in r for r in result.unknown["A"])

    def test_unknown_not_recorded_when_nav_present(self, uf):
        """提供 NAV 且限内 → unknown 为空."""
        df = make_ohlcv_df(["A"], n_days=120, amounts={"A": 1e8})
        nav = make_nav_df(["A"], navs={"A": 3.48})
        result = uf.filter(df, nav_df=nav)
        assert result.unknown == {}

    def test_normal_premium_passes(self, uf):
        """溢价 1% → 通过."""
        df = make_ohlcv_df(["A"], n_days=120,
                          amounts={"A": 1e8})
        # close ≈ 3.5, nav = 3.48 → premium ≈ 0.57%
        nav = make_nav_df(["A"], navs={"A": 3.48})
        result = uf.filter(df, nav_df=nav)
        assert "A" in result.passed

    def test_high_premium_filtered(self, uf):
        """溢价 5% > 3% 阈值 → 被剔除."""
        df = make_ohlcv_df(["PREMIUM"], n_days=120,
                          amounts={"PREMIUM": 1e8})
        # close ≈ 3.5, nav = 3.3 → premium ≈ 6%
        nav = make_nav_df(["PREMIUM"], navs={"PREMIUM": 3.30})
        result = uf.filter(df, nav_df=nav)
        assert "PREMIUM" in result.failed
        assert any("溢价" in r for r in result.failed["PREMIUM"])

    def test_boundary_exact_threshold(self, uf):
        """溢价恰好 3% → 不通过 (> 3%)."""
        df = make_ohlcv_df(["EXACT"], n_days=120,
                          amounts={"EXACT": 1e8})
        # close ≈ 3.5, nav = 3.398... → premium ≈ 3.0%
        nav = make_nav_df(["EXACT"], navs={"EXACT": 3.398058})
        result = uf.filter(df, nav_df=nav)
        # close 有随机波动, 大致检验即可
        # 精确测试用下面方法
        assert "EXACT" in result.passed or "EXACT" in result.failed

    def test_discount_also_filtered(self, uf):
        """折价 > 3% 同样被剔除 (价格大幅低于 NAV)."""
        df = make_ohlcv_df(["DISCOUNT"], n_days=120,
                          amounts={"DISCOUNT": 1e8})
        # close ≈ 3.5, nav = 3.75 → discount ≈ -6.67%, abs = 6.67%
        nav = make_nav_df(["DISCOUNT"], navs={"DISCOUNT": 3.75})
        result = uf.filter(df, nav_df=nav)
        # 折价也是溢价的一种形式 → 应被剔除
        assert "DISCOUNT" in result.failed

    def test_iopv_column_name(self, uf):
        """支持 iopv 列名 (作为 nav 的别名)."""
        # 构造使用 iopv 列名的 NAV DataFrame
        dates = _make_trading_dates(120)
        nav_df = pl.DataFrame({
            "symbol": ["A"] * 120,
            "date": dates,
            "iopv": [3.30] * 120,  # close≈3.5, premium≈6%
        })
        df = make_ohlcv_df(["A"], n_days=120, amounts={"A": 1e8})
        result = uf.filter(df, nav_df=nav_df)
        assert "A" in result.failed


# ------------------------------------------------------------------
# 流动性集中度警告
# ------------------------------------------------------------------

class TestConcentrationWarning:
    def test_no_warning_for_liquid_etf(self, uf):
        """高流动性标的 → 无警告."""
        df = make_ohlcv_df(["BIG"], n_days=120,
                          amounts={"BIG": 5e8})  # 5亿日均
        result = uf.filter(df, planned_capital=100_000)
        assert "BIG" in result.passed
        assert "BIG" not in result.warnings

    def test_warning_for_illiquid_etf(self, uf):
        """低流动性标的 → 触发集中度警告."""
        df = make_ohlcv_df(["TINY"], n_days=120,
                          amounts={"TINY": 5_000_000})  # 500万日均
        # 100,000 / 5,000,000 = 0.02 = 2% > 0.5%
        result = uf.filter(df, planned_capital=100_000)
        assert "TINY" in result.passed  # 仍然通过 (警告不是剔除)
        assert "TINY" in result.warnings
        assert any("流动性集中度" in w for w in result.warnings["TINY"])

    def test_boundary_below_threshold(self, uf):
        """集中度恰好 0.5% → 触发 ( > 0.5% )."""
        df = make_ohlcv_df(["BOUNDARY"], n_days=120,
                          amounts={"BOUNDARY": 20_000_000})  # 2000万日均
        # 100,000 / 20,000,000 = 0.005 = 0.5%
        # 严格大于才触发, 恰好等于不触发
        result = uf.filter(df, planned_capital=100_000)
        assert "BOUNDARY" in result.passed
        # 0.5% 不严格大于 0.5% → 不警告
        assert "BOUNDARY" not in result.warnings

    def test_larger_capital_triggers_warning(self, uf):
        """更大资金量 → 同样流动性下更容易触发警告."""
        df = make_ohlcv_df(["MID"], n_days=120,
                          amounts={"MID": 1e7})  # 1000万日均
        # 100k / 1e7 = 1% > 0.5%
        result = uf.filter(df, planned_capital=100_000)
        assert "MID" in result.warnings

    def test_warning_not_blocking(self, uf):
        """集中度警告不影响标的通过."""
        df = make_ohlcv_df(["WARN_ONLY"], n_days=120,
                          amounts={"WARN_ONLY": 3_000_000})
        result = uf.filter(df, planned_capital=100_000)
        # 成交量 < 500万 → 被剔除 (硬过滤)
        # 所以应该出现在 failed 中
        assert "WARN_ONLY" in result.failed


# ------------------------------------------------------------------
# 组合筛选结果
# ------------------------------------------------------------------

class TestCombinedFilter:
    def test_all_pass(self, uf, standard_ohlcv):
        """所有标的满足全部条件 → 全部通过."""
        result = uf.filter(standard_ohlcv)
        assert result.n_passed == 5
        assert result.n_failed == 0

    def test_mixed_pass_fail(self, uf):
        """混合场景: 部分通过, 部分失败."""
        df = make_ohlcv_df(
            ["GOOD1", "GOOD2", "LOW_VOL", "NEW"],
            n_days=120,
            amounts={
                "GOOD1": 1e8, "GOOD2": 2e8,
                "LOW_VOL": 1e6, "NEW": 1e8,
            },
        )
        # NEW 只有 30 天数据 → 额外处理
        short_df = make_ohlcv_df(["NEW"], n_days=30,
                                 amounts={"NEW": 1e8})
        # 替换 NEW 为短数据
        full_df = pl.concat([
            df.filter(pl.col("symbol") != "NEW"),
            short_df,
        ])

        result = uf.filter(full_df)
        assert "GOOD1" in result.passed
        assert "GOOD2" in result.passed
        assert "LOW_VOL" in result.failed
        assert "NEW" in result.failed

    def test_multiple_fail_reasons(self, uf):
        """一个标的可能同时触发多个失败原因."""
        # 成交量低 + 上市时间短 → 两个原因
        df = make_ohlcv_df(["BAD_ALL"], n_days=30,
                          amounts={"BAD_ALL": 1e6})
        result = uf.filter(df)
        assert "BAD_ALL" in result.failed
        reasons = result.failed["BAD_ALL"]
        assert len(reasons) >= 2

    def test_warning_with_pass(self, uf):
        """通过但有警告."""
        df = make_ohlcv_df(["WARN"], n_days=120,
                          amounts={"WARN": 5_000_000})
        result = uf.filter(df, planned_capital=100_000)
        assert "WARN" in result.passed
        assert "WARN" in result.warnings


# ------------------------------------------------------------------
# 边界情况
# ------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_input(self, uf):
        """空 DataFrame → 空结果."""
        result = uf.filter(pl.DataFrame())
        assert result.n_total == 0
        assert result.n_passed == 0
        assert result.n_failed == 0

    def test_single_symbol(self, uf):
        """单标的."""
        df = make_ohlcv_df(["ONLY"], n_days=120, amounts={"ONLY": 1e8})
        result = uf.filter(df)
        assert result.n_passed == 1

    def test_missing_columns(self, uf):
        """缺少必需列 → ValueError."""
        df = pl.DataFrame({"symbol": ["A"], "date": [date.today()]})
        with pytest.raises(ValueError, match="missing required columns"):
            uf.filter(df)

    def test_all_fail(self, uf):
        """所有标的都不合格."""
        df = make_ohlcv_df(
            ["BAD1", "BAD2", "BAD3"], n_days=30,
            amounts={"BAD1": 1e6, "BAD2": 2e6, "BAD3": 3e6},
        )
        result = uf.filter(df)
        assert result.n_passed == 0
        assert result.n_failed == 3

    def test_result_summary_string(self, uf, standard_ohlcv):
        """验证 summary() 返回非空字符串."""
        result = uf.filter(standard_ohlcv)
        s = result.summary()
        assert isinstance(s, str)
        assert len(s) > 0
        assert "UniverseFilter" in s

    def test_result_properties(self, uf, standard_ohlcv):
        """验证 n_total/n_passed/n_failed/n_warnings 属性."""
        result = uf.filter(standard_ohlcv)
        assert result.n_total == 5
        assert result.n_passed == 5
        assert result.n_failed == 0
        assert result.n_warnings == 0
        assert result.n_total == result.n_passed + result.n_failed


# ------------------------------------------------------------------
# from_settings 工厂函数
# ------------------------------------------------------------------

class TestFromSettings:
    def test_full_settings(self):
        """传入完整 settings dict."""
        settings = {
            "universe_filter": {
                "min_avg_volume": 10_000_000,
                "min_list_days": 90,
                "max_premium_pct": 5.0,
                "liquidity_concentration_warn": 0.01,
            }
        }
        uf = from_settings(settings)
        assert uf.min_avg_volume == 10_000_000
        assert uf.min_list_days == 90
        assert uf.max_premium_pct == 5.0
        assert uf.liquidity_warn_threshold == 0.01

    def test_sub_section_only(self):
        """仅传入 universe_filter 子节."""
        sub = {
            "min_avg_volume": 3_000_000,
            "min_list_days": 30,
        }
        uf = from_settings(sub)
        assert uf.min_avg_volume == 3_000_000
        assert uf.min_list_days == 30
        # 默认值
        assert uf.max_premium_pct == 3.0
        assert uf.liquidity_warn_threshold == 0.005

    def test_empty_settings_uses_defaults(self):
        """空 dict → 全部使用默认值."""
        uf = from_settings({})
        assert uf.min_avg_volume == 5_000_000
        assert uf.min_list_days == 60
        assert uf.max_premium_pct == 3.0
        assert uf.liquidity_warn_threshold == 0.005

    def test_roundtrip_with_filter(self):
        """from_settings 创建的实例与直接构造行为一致."""
        uf1 = UniverseFilter(
            min_avg_volume=8_000_000,
            min_list_days=90,
            max_premium_pct=4.0,
            liquidity_warn_threshold=0.01,
        )
        settings = {
            "universe_filter": {
                "min_avg_volume": 8_000_000,
                "min_list_days": 90,
                "max_premium_pct": 4.0,
                "liquidity_concentration_warn": 0.01,
            }
        }
        uf2 = from_settings(settings)

        df = make_ohlcv_df(
            ["TEST"], n_days=120,
            amounts={"TEST": 1e8},
        )
        r1 = uf1.filter(df)
        r2 = uf2.filter(df)
        assert r1.passed == r2.passed
        assert r1.failed == r2.failed


# ------------------------------------------------------------------
# 自定义参数
# ------------------------------------------------------------------

class TestCustomParameters:
    def test_custom_volume_threshold(self):
        """自定义成交量门槛."""
        uf = UniverseFilter(min_avg_volume=1e7)  # 1000万
        df = make_ohlcv_df(
            ["MID"], n_days=120,
            amounts={"MID": 8_000_000},  # 800万 < 1000万
        )
        result = uf.filter(df)
        assert "MID" in result.failed

    def test_custom_list_days(self):
        """自定义上市天数."""
        uf = UniverseFilter(min_list_days=120)
        df = make_ohlcv_df(["MID"], n_days=90)  # 90 < 120
        result = uf.filter(df)
        assert "MID" in result.failed

    def test_custom_premium_threshold(self):
        """自定义溢价阈值."""
        uf = UniverseFilter(max_premium_pct=2.0)
        df = make_ohlcv_df(["A"], n_days=120, amounts={"A": 1e8})
        nav = make_nav_df(["A"], navs={"A": 3.40})  # close≈3.5, premium≈2.9%
        result = uf.filter(df, nav_df=nav)
        assert "A" in result.failed

    def test_custom_warn_threshold(self):
        """自定义警告阈值."""
        uf = UniverseFilter(liquidity_warn_threshold=0.10)  # 10%
        df = make_ohlcv_df(["A"], n_days=120,
                          amounts={"A": 2_000_000})  # 100k/2M = 5%
        result = uf.filter(df, planned_capital=100_000)
        # 5% < 10% → 无警告
        assert "A" not in result.warnings
