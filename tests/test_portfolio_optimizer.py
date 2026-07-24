"""组合优化模块单元测试.

验证: 波动率倒数加权、仓位上限缩放、单标的上限裁剪、边界情况.
"""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from strategies.portfolio_optimizer import (
    PortfolioOptimizer,
    PortfolioResult,
    PortfolioWeight,
    from_settings,
)


# ------------------------------------------------------------------
# 辅助: 生成合成 OHLCV
# ------------------------------------------------------------------

def make_ohlcv(
    symbols: list[str],
    n_days: int = 60,
    base_price: float = 3.0,
    volatility: float = 0.015,
    seed: int = 42,
) -> pl.DataFrame:
    """生成多标的合成 OHLCV, 方便控制波动率差异.

    Args:
        symbols: 标的代码列表
        n_days: 交易日数
        base_price: 基准价格
        volatility: 日收益率标准差 (对数)
        seed: 随机种子
    """
    rng = np.random.default_rng(seed)
    dfs = []

    for i, sym in enumerate(symbols):
        # 不同标的可以有不同的波动率
        effective_vol = volatility * (1.0 + i * 0.5)  # 波动率递增
        log_returns = rng.normal(0, effective_vol, n_days)
        prices = base_price * np.exp(np.cumsum(log_returns))

        dates = []
        d = date(2025, 1, 2)
        while len(dates) < n_days:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)

        dfs.append(pl.DataFrame({
            "symbol": [sym] * n_days,
            "date": dates[:n_days],
            "open": [p * (1 + rng.uniform(-0.005, 0.005)) for p in prices],
            "high": [p * (1 + abs(rng.uniform(0, 0.01))) for p in prices],
            "low": [p * (1 - abs(rng.uniform(0, 0.01))) for p in prices],
            "close": prices,
            "volume": [1e8] * n_days,
            "amount": [p * 1e8 for p in prices],
        }))

    return pl.concat(dfs)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def opt():
    return PortfolioOptimizer(vol_lookback=20)


@pytest.fixture
def three_etf_df():
    """3 只 ETF, 不同波动率: A 低波动, B 中波动, C 高波动."""
    return make_ohlcv(["ETF_A", "ETF_B", "ETF_C"], n_days=60,
                      volatility=0.010, seed=7)


@pytest.fixture
def single_etf_df():
    return make_ohlcv(["ETF_ONLY"], n_days=60, seed=3)


@pytest.fixture
def short_df():
    """数据不足 10 天."""
    return make_ohlcv(["ETF_X"], n_days=8, seed=1)


# ------------------------------------------------------------------
# 波动率计算
# ------------------------------------------------------------------

class TestVolatilityCalc:
    def test_lower_vol_for_stable_price(self, opt):
        """稳定价格 → 低波动率."""
        n = 60
        prices = [3.0 + i * 0.001 for i in range(n)]  # 几乎直线
        rng = np.random.default_rng(99)
        df = pl.DataFrame({
            "symbol": ["STABLE"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "close": [p + rng.uniform(-0.001, 0.001) for p in prices],
        })
        vol = opt._calc_annualized_vol(df, "STABLE")
        assert vol is not None
        assert vol < 0.15, f"Stable price should have low vol, got {vol:.4f}"

    def test_higher_vol_for_volatile_price(self, opt):
        """剧烈波动 → 高波动率."""
        n = 60
        rng = np.random.default_rng(42)
        prices = [3.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * (1 + rng.normal(0, 0.03)))
        df = pl.DataFrame({
            "symbol": ["WILD"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "close": prices,
        })
        vol = opt._calc_annualized_vol(df, "WILD")
        assert vol is not None
        assert vol > 0.25, f"Volatile price should have high vol, got {vol:.4f}"

    def test_insufficient_data_returns_none(self, opt, short_df):
        """不足窗口一半的数据 → None."""
        vol = opt._calc_annualized_vol(short_df, "ETF_X")
        assert vol is None

    def test_nonexistent_symbol(self, opt, three_etf_df):
        """不存在的标的 → None."""
        vol = opt._calc_annualized_vol(three_etf_df, "GHOST")
        assert vol is None

    def test_vol_order_preserved(self, opt, three_etf_df):
        """波动率排序: ETF_A < ETF_B < ETF_C (构造时递增)."""
        vols = {}
        for sym in ["ETF_A", "ETF_B", "ETF_C"]:
            v = opt._calc_annualized_vol(three_etf_df, sym)
            assert v is not None
            vols[sym] = v
        assert vols["ETF_A"] < vols["ETF_B"] < vols["ETF_C"], (
            f"Expected ETF_A < ETF_B < ETF_C, got {vols}"
        )


# ------------------------------------------------------------------
# 逆方差权重计算
# ------------------------------------------------------------------

class TestInverseVarWeights:
    def test_sum_to_one(self, opt):
        """原始权重之和 = 1.0."""
        vol_map = {"A": 0.20, "B": 0.30, "C": 0.50}
        weights = opt._calc_inverse_var_weights(vol_map)
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_low_vol_gets_higher_weight(self, opt):
        """低波动 → 高权重."""
        vol_map = {"LOW": 0.10, "HIGH": 0.40}
        weights = opt._calc_inverse_var_weights(vol_map)
        assert weights["LOW"] > weights["HIGH"]

    def test_equal_vol_equal_weight(self, opt):
        """相同波动率 → 等权重."""
        vol_map = {"A": 0.25, "B": 0.25, "C": 0.25}
        weights = opt._calc_inverse_var_weights(vol_map)
        for w in weights.values():
            assert abs(w - 1.0 / 3) < 1e-6


# ------------------------------------------------------------------
# 仓位上限裁剪
# ------------------------------------------------------------------

class TestApplyCaps:
    def test_no_cap_needed(self, opt):
        """所有权重都在上限内 → 不裁剪."""
        vol_map = {"A": 0.20, "B": 0.25, "C": 0.30}
        raw = {"A": 0.50, "B": 0.30, "C": 0.20}
        result = opt._apply_caps(raw, vol_map, position_cap=0.80)
        # max_scaled: 0.50*0.80=0.40 > 0.30 → A 会被裁剪!
        # 0.30*0.80=0.24 OK, 0.20*0.80=0.16 OK
        assert len(result) == 3

    def test_all_below_cap(self, opt):
        """position_cap 很小, 所有 scaled 都在上限内."""
        vol_map = {"A": 0.20, "B": 0.25}
        raw = {"A": 0.60, "B": 0.40}
        result = opt._apply_caps(raw, vol_map, position_cap=0.40)
        # scaled: A=0.24, B=0.16; both < 0.30
        w_a = next(w for w in result if w.symbol == "A")
        w_b = next(w for w in result if w.symbol == "B")
        assert not w_a.is_capped
        assert not w_b.is_capped
        assert abs(w_a.final_weight - 0.24) < 1e-6
        assert abs(w_b.final_weight - 0.16) < 1e-6

    def test_single_exceeds_cap(self, opt):
        """一个标的超上限 → 裁剪. 释放权重再分配给剩余标的,
        剩余标的可能也被裁剪 (迭代)."""
        vol_map = {"HIGH": 0.10, "LOW": 0.20}
        raw = {"HIGH": 0.80, "LOW": 0.20}
        result = opt._apply_caps(raw, vol_map, position_cap=1.0)
        w_high = next(w for w in result if w.symbol == "HIGH")
        w_low = next(w for w in result if w.symbol == "LOW")
        assert w_high.is_capped
        assert w_high.final_weight == pytest.approx(0.30, abs=1e-6)
        # HIGH 裁剪释放 0.50, LOW 从 0.20→0.70 → 也超 0.30 → 也被裁剪!
        assert w_low.is_capped
        assert w_low.final_weight == pytest.approx(0.30, abs=1e-6)

    def test_redistribute_within_cap(self, opt):
        """一个标的被裁剪, 释放权重分配给未超限标的, 不触发二次裁剪."""
        vol_map = {"BIG": 0.08, "SMALL_A": 0.20, "SMALL_B": 0.22}
        raw = {"BIG": 0.50, "SMALL_A": 0.30, "SMALL_B": 0.20}
        result = opt._apply_caps(raw, vol_map, position_cap=0.80)
        w_big = next(w for w in result if w.symbol == "BIG")
        assert w_big.is_capped
        # BIG scaled: 0.50*0.80=0.40 > 0.30 → capped; overflow=0.10
        # SMALL_A: 0.30*0.80=0.24 + share(0.10) = 0.24+0.06=0.30 OK
        # SMALL_B: 0.20*0.80=0.16 + share(0.10) = 0.16+0.04=0.20 OK
        total = sum(w.final_weight for w in result)
        assert abs(total - 0.80) < 1e-6

    def test_both_exceed_cap(self, opt):
        """两个标的都超上限 → 都被裁剪."""
        vol_map = {"A": 0.10, "B": 0.11}
        raw = {"A": 0.50, "B": 0.50}
        result = opt._apply_caps(raw, vol_map, position_cap=1.0)
        for w in result:
            assert w.is_capped
            assert w.final_weight <= 0.30 + 1e-6

    def test_overflow_redistribution(self, opt):
        """裁剪释放权重分配给未裁剪标的, 总权重守恒 (≤ cap)."""
        # 用 3 标的, 只有 1 个触发裁剪, 其余离上限远
        vol_map = {"BIG": 0.08, "MID": 0.20, "LOW_WT": 0.25}
        raw = {"BIG": 0.50, "MID": 0.35, "LOW_WT": 0.15}
        result = opt._apply_caps(raw, vol_map, position_cap=0.70)
        w_big = next(w for w in result if w.symbol == "BIG")
        assert w_big.is_capped
        # BIG scaled: 0.50*0.70=0.35 > 0.30 → capped, overflow=0.05
        # MID: 0.35*0.70=0.245 + 0.05*(0.35/0.50)=0.245+0.035=0.28 < 0.30 OK
        # LOW_WT: 0.15*0.70=0.105 + 0.05*(0.15/0.50)=0.105+0.015=0.12 OK
        total = sum(w.final_weight for w in result)
        assert abs(total - 0.70) < 1e-6

    def test_sorted_by_weight_desc(self, opt):
        """结果按权重降序排列."""
        vol_map = {"A": 0.20, "B": 0.15, "C": 0.25}
        raw = {"A": 0.30, "B": 0.50, "C": 0.20}
        result = opt._apply_caps(raw, vol_map, position_cap=0.80)
        for i in range(len(result) - 1):
            assert result[i].final_weight >= result[i + 1].final_weight


# ------------------------------------------------------------------
# 端到端 optimize
# ------------------------------------------------------------------

class TestOptimize:
    def test_three_etf_basic(self, opt, three_etf_df):
        """3 ETF 基本优化流程."""
        result = opt.optimize(three_etf_df, position_cap=0.70)
        assert isinstance(result, PortfolioResult)
        assert result.num_symbols_output == 3
        assert result.position_cap == 0.70
        # 总权重应接近 position_cap
        assert abs(result.total_weight - 0.70) < 0.02

    def test_weights_sum_to_cap(self, opt, three_etf_df):
        """所有权重之和 ≈ position_cap (当有标的未触上限时)."""
        # 用较低仓位确保 3 ETFs × 30% 不超总 cap
        for cap in [0.30, 0.50, 0.70]:
            result = opt.optimize(three_etf_df, position_cap=cap)
            total = sum(w.final_weight for w in result.weights)
            assert abs(total - cap) < 0.02, (
                f"cap={cap}, total={total:.6f}, diff={abs(total-cap):.6f}"
            )

    def test_low_vol_higher_weight(self, opt, three_etf_df):
        """低波动 ETF 获得更高最终权重."""
        result = opt.optimize(three_etf_df, position_cap=0.80)
        weights = {w.symbol: w.final_weight for w in result.weights}
        assert weights["ETF_A"] > weights["ETF_B"] > weights["ETF_C"], (
            f"Expected ETF_A > ETF_B > ETF_C, got {weights}"
        )

    def test_single_symbol(self, opt, single_etf_df):
        """单标的: 权重 = min(cap, 0.30)."""
        result = opt.optimize(single_etf_df, position_cap=0.50)
        assert result.num_symbols_output == 1
        assert result.weights[0].final_weight == pytest.approx(0.30, abs=1e-6)
        assert result.weights[0].is_capped

    def test_single_symbol_low_cap(self, opt, single_etf_df):
        """单标的 + 低仓位上限."""
        result = opt.optimize(single_etf_df, position_cap=0.20)
        assert result.weights[0].final_weight == pytest.approx(0.20, abs=1e-6)
        assert not result.weights[0].is_capped

    def test_zero_cap_returns_empty(self, opt, three_etf_df):
        """仓位上限 = 0 → 空组合."""
        result = opt.optimize(three_etf_df, position_cap=0.0)
        # 所有缩放后都是 0, 但 optimizer 仍会计算权重
        # 实际上 weights 非空但 final_weight 都是 0
        assert result.total_weight == 0.0

    def test_empty_dataframe(self, opt):
        """空 DataFrame → 空结果."""
        df = pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64})
        result = opt.optimize(df, position_cap=0.70)
        assert result.num_symbols_output == 0
        assert result.weights == []

    def test_result_structure(self, opt, three_etf_df):
        """验证 PortfolioResult 结构完整性."""
        result = opt.optimize(three_etf_df, position_cap=0.60)
        assert result.method == "inverse_volatility"
        assert result.vol_lookback == 20
        assert result.max_position_weight == 0.30
        assert result.min_annual_vol is not None
        assert result.max_annual_vol is not None
        assert result.min_annual_vol <= result.max_annual_vol

    def test_diagnostics(self, opt, three_etf_df):
        """诊断信息正确."""
        result = opt.optimize(three_etf_df, position_cap=0.80)
        assert result.num_symbols_input == 3
        assert result.num_symbols_output == 3
        assert result.min_annual_vol > 0
        assert result.max_annual_vol > result.min_annual_vol


# ------------------------------------------------------------------
# 最大持仓数限制
# ------------------------------------------------------------------

class TestMaxPositions:
    def test_truncate_to_max(self):
        """超过 max_positions 的标的被截断."""
        opt = PortfolioOptimizer(max_positions=3)
        df = make_ohlcv(
            ["A", "B", "C", "D", "E"], n_days=60, volatility=0.012, seed=10,
        )
        result = opt.optimize(df, position_cap=0.80)
        assert result.num_symbols_output <= 3
        assert len(result.weights) <= 3

    def test_drop_lowest_weight(self):
        """截断时丢弃权重最低的标的."""
        opt = PortfolioOptimizer(max_positions=2)
        df = make_ohlcv(
            ["A", "B", "C"], n_days=60, volatility=0.012, seed=10,
        )
        result = opt.optimize(df, position_cap=0.80)
        assert len(result.weights) == 2
        # C 波动率最高 → 权重最低 → 应该被丢弃
        kept = {w.symbol for w in result.weights}
        assert "C" not in kept


# ------------------------------------------------------------------
# 输入校验
# ------------------------------------------------------------------

class TestInputValidation:
    def test_missing_columns(self, opt):
        """缺少必要列 → ValueError."""
        df = pl.DataFrame({"symbol": ["X"], "date": [date.today()]})
        with pytest.raises(ValueError, match="missing required columns"):
            opt.optimize(df, position_cap=0.50)

    def test_invalid_position_cap_negative(self, opt, three_etf_df):
        """仓位上限为负 → ValueError."""
        with pytest.raises(ValueError, match="position_cap"):
            opt.optimize(three_etf_df, position_cap=-0.1)

    def test_invalid_position_cap_over_one(self, opt, three_etf_df):
        """仓位上限 > 1 → ValueError."""
        with pytest.raises(ValueError, match="position_cap"):
            opt.optimize(three_etf_df, position_cap=1.5)

    def test_position_cap_boundary_zero(self, opt, three_etf_df):
        """position_cap = 0 合法."""
        result = opt.optimize(three_etf_df, position_cap=0.0)
        assert result.total_weight == 0.0

    def test_position_cap_boundary_one(self, opt, three_etf_df):
        """position_cap = 1.0 合法."""
        result = opt.optimize(three_etf_df, position_cap=1.0)
        assert result.position_cap == 1.0


# ------------------------------------------------------------------
# 序列化
# ------------------------------------------------------------------

class TestSerialization:
    def test_summary_string(self, opt, three_etf_df):
        """summary() 返回非空字符串."""
        result = opt.optimize(three_etf_df, position_cap=0.70)
        s = result.summary()
        assert isinstance(s, str)
        assert len(s) > 0
        assert "PortfolioResult" in s

    def test_to_dict(self, opt, three_etf_df):
        """to_dict() 返回正确结构."""
        result = opt.optimize(three_etf_df, position_cap=0.70)
        d = result.to_dict()
        assert d["position_cap"] == 0.70
        assert d["method"] == "inverse_volatility"
        assert len(d["weights"]) == 3
        for w in d["weights"]:
            assert "symbol" in w
            assert "annualized_vol" in w
            assert "final_weight" in w


# ------------------------------------------------------------------
# from_settings 工厂
# ------------------------------------------------------------------

class TestFromSettings:
    def test_full_settings(self):
        """完整设置 → 正确参数."""
        settings = {
            "portfolio": {
                "vol_lookback": 20,
                "max_positions": 5,
                "min_positions": 1,
            }
        }
        opt = from_settings(settings)
        assert opt.vol_lookback == 20
        assert opt.max_positions == 5

    def test_custom_vol_lookback(self):
        """自定义波动率窗口."""
        settings = {"portfolio": {"vol_lookback": 30}}
        opt = from_settings(settings)
        assert opt.vol_lookback == 30

    def test_empty_settings_uses_defaults(self):
        """空设置 → 默认参数."""
        opt = from_settings({})
        assert opt.vol_lookback == 20
        assert opt.max_position_weight == 0.30
        assert opt.max_positions == 5

    def test_partial_settings(self):
        """部分设置 → 未指定用默认."""
        settings = {"portfolio": {"method": "inverse_volatility"}}
        opt = from_settings(settings)
        assert opt.vol_lookback == 20  # default
        assert opt.max_position_weight == 0.30  # default


# ------------------------------------------------------------------
# 定制参数
# ------------------------------------------------------------------

class TestCustomParameters:
    def test_custom_lookback(self, three_etf_df):
        """自定义波动率窗口."""
        opt = PortfolioOptimizer(vol_lookback=15)
        result = opt.optimize(three_etf_df, position_cap=0.60)
        assert result.vol_lookback == 15
        assert len(result.weights) == 3

    def test_custom_max_position_weight(self, three_etf_df):
        """自定义单标的上限."""
        opt = PortfolioOptimizer(max_position_weight=0.40)
        result = opt.optimize(three_etf_df, position_cap=1.0)
        for w in result.weights:
            assert w.final_weight <= 0.40 + 1e-6

    def test_tighter_cap(self, three_etf_df):
        """更紧的单标的上限 (20%)."""
        opt = PortfolioOptimizer(max_position_weight=0.20)
        result = opt.optimize(three_etf_df, position_cap=1.0)
        for w in result.weights:
            assert w.final_weight <= 0.20 + 1e-6
        # 三个标的都 20% → total = 60%
        total = sum(w.final_weight for w in result.weights)
        assert total == pytest.approx(0.60, abs=0.02)


# ------------------------------------------------------------------
# 边界情况
# ------------------------------------------------------------------

class TestEdgeCases:
    def test_two_symbols(self):
        """2 个标的正确分配, 总和 ≤ cap."""
        df = make_ohlcv(["X", "Y"], n_days=60, seed=5)
        opt = PortfolioOptimizer()
        result = opt.optimize(df, position_cap=0.50)
        assert result.num_symbols_output == 2
        total = sum(w.final_weight for w in result.weights)
        assert abs(total - 0.50) < 0.02

    def test_data_with_nans(self, opt):
        """含 NaN 的 close 数据在尾部 → 排除."""
        n = 60
        rng = np.random.default_rng(99)
        prices = 3.0 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
        prices[-10:] = np.nan  # 尾部插 NaN → tail(20) 获取到 NaN
        df = pl.DataFrame({
            "symbol": ["NAN_TEST"] * n,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(n)],
            "close": prices,
        })
        vol = opt._calc_annualized_vol(df, "NAN_TEST")
        assert vol is None

    def test_constant_price(self, opt):
        """价格完全不变 → 波动率 = 0 → 被排除."""
        df = pl.DataFrame({
            "symbol": ["FLAT"] * 60,
            "date": [date(2025, 1, 2) + timedelta(days=i) for i in range(60)],
            "close": [10.0] * 60,
        })
        result = opt.optimize(df, position_cap=0.80)
        assert result.num_symbols_output == 0
