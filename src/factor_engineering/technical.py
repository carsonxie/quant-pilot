"""诊断指标函数库 (Phase 1.1, P11-M3-01).

纯函数, 输入标准行情列 [date, open, high, low, close, volume, amount],
输出追加指标列的 DataFrame。不读取配置文件 — 窗口/参数由规则卡
(`config/diagnostics.yaml`) 经调用方传入 (禁止硬编码, AGENTS §5.2)。

口径声明 (与 docs/task-phase1.1.md 附录 B / factor_registry diagnostic_indicators 一致):
  - MA: 简单移动平均, 使用已完成交易日收盘 (SMA);
  - 均线斜率: ma_t / ma_{t-w} - 1 (窗口 w 的均线变化率);
  - 12-1 动量: P_(t-21) / P_(t-252) - 1 (精确端点比值, spec §7.2; 1 月≈21 交易日);
  - ATR: Wilder 平滑 (复用 RiskManager 已验证实现); ATR% = ATR / 同日收盘;
  - 短期波动率: 20 日对数收益率样本标准差 (ddof=1, 日频未年化);
  - 波动率历史位置: 当前值在过去 N 日窗口内的经验分位 (含当日的 ≤ 计数 / 样本数);
  - RSI: Wilder 平滑, 初始值为前 period 个涨/跌均值; 零涨(avg_loss=0)→100,
    零跌(avg_gain=0)→0, 双零(无变动)定义为 50 (中性, 不虚构方向);
  - 布林带: 中轨 SMA(20), 上下轨 ±2×总体标准差 (ddof=0, TA-Lib 口径);
    带宽 = (上轨-下轨)/中轨; 位置 = (close-下轨)/(上轨-下轨), 带宽为 0 时位置为空
    (不可评估, 不虚构 0.5);
  - 量额活跃度: 当日成交额 / 此前 window 个完整交易日成交额均值 (基准 shift(1),
    当日不参与自身基准), 基准缺失或为 0 → 空。

缺失语义: 历史不足或数据缺失 → 指标为空值 (null), 不做零填充、不外推;
下游计分对必需指标缺失时输出"不可评估", 不把缺失当正常 (spec §4.4)。
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl

# 规则卡参数键 (compute_indicators 的 params 必填项)
REQUIRED_PARAM_KEYS = (
    "ma_windows", "ma_slope_window", "momentum_lookback", "momentum_skip",
    "atr_period", "vol_window", "vol_pctile_window", "vol_pctile_min_periods",
    "rsi_period", "boll_window", "boll_num_std",
    "amount_window", "amount_min_periods", "atr_ratio_window", "atr_ratio_min_periods",
)


def _eval(expr: pl.Expr, name: str = "value") -> pl.Series:
    """把 Series 上下文表达式求值为 Series (本模块输入均为同长度序列)."""
    return pl.select(expr.alias(name)).to_series()


# ------------------------------------------------------------------
# 基础指标 (单序列纯函数, 返回 pl.Series)
# ------------------------------------------------------------------

def calc_sma(close: pl.Series, window: int, *, min_periods: int | None = None) -> pl.Series:
    """简单移动平均 (null 若样本不足)."""
    min_p = min_periods if min_periods is not None else window
    return close.rolling_mean(window_size=window, min_samples=min_p)


def calc_ma_deviation(close: pl.Series, ma: pl.Series) -> pl.Series:
    """价格相对均线偏离: close / ma - 1 (ma ≤ 0 或空 → null)."""
    expr = pl.when((ma > 0) & ma.is_not_null() & close.is_not_null()).then(
        close / ma - 1.0
    ).otherwise(None)
    return _eval(expr)


def calc_ma_slope(ma: pl.Series, window: int) -> pl.Series:
    """均线斜率 (变化率): ma_t / ma_{t-window} - 1."""
    prev = ma.shift(window)
    expr = pl.when((prev > 0) & prev.is_not_null() & ma.is_not_null()).then(
        ma / prev - 1.0
    ).otherwise(None)
    return _eval(expr)


def calc_momentum_12_1(
    close: pl.Series, *, lookback: int, skip: int,
) -> pl.Series:
    """12-1 区间动量: P_(t-skip) / P_(t-lookback) - 1 (精确端点比值)."""
    end = close.shift(skip)
    start = close.shift(lookback)
    expr = pl.when((start > 0) & start.is_not_null() & end.is_not_null()).then(
        end / start - 1.0
    ).otherwise(None)
    return _eval(expr)


def calc_atr_series(high: pl.Series, low: pl.Series, close: pl.Series, period: int) -> pl.Series:
    """ATR (Wilder 平滑); 复用 strategies.risk_manager 的已验证实现."""
    from strategies.risk_manager import RiskManager

    sym_df = pl.DataFrame({"high": high, "low": low, "close": close})
    series = RiskManager(atr_period=period).calc_atr_series(sym_df)
    if series is None:
        return pl.Series([None] * len(close), dtype=pl.Float64)
    return pl.Series([None if (v is None or math.isnan(v)) else v for v in series])


def calc_atr_pct(atr: pl.Series, close: pl.Series) -> pl.Series:
    """ATR 占价格比 (同口径): ATR / close."""
    expr = pl.when((close > 0) & close.is_not_null() & atr.is_not_null()).then(
        atr / close
    ).otherwise(None)
    return _eval(expr)


def _log_returns(close: pl.Series) -> pl.Series:
    prev = close.shift(1)
    expr = pl.when((prev > 0) & close.is_not_null() & prev.is_not_null()).then(
        (close / prev).log()
    ).otherwise(None)
    return _eval(expr)


def calc_return_volatility(
    close: pl.Series, window: int, *, min_periods: int | None = None,
) -> pl.Series:
    """短期收益波动率: window 日对数收益率样本标准差 (ddof=1, 日频未年化)."""
    min_p = min_periods if min_periods is not None else window
    return _log_returns(close).rolling_std(window_size=window, min_samples=min_p, ddof=1)


def calc_percentile_rank(series: pl.Series, window: int, *, min_periods: int) -> pl.Series:
    """当前值在过去 window 个观测内的经验分位 (0–1; 含当日的 ≤计数/样本数).

    仅使用当时可得样本; 样本数 < min_periods → null。
    """
    values = series.to_numpy()
    out = np.full(len(values), np.nan)
    for i in range(len(values)):
        if np.isnan(values[i]):
            continue
        lo = max(0, i - window + 1)
        window_values = values[lo : i + 1]
        window_values = window_values[~np.isnan(window_values)]
        if len(window_values) < min_periods:
            continue
        out[i] = float(np.sum(window_values <= values[i])) / len(window_values)
    return pl.Series([None if math.isnan(v) else v for v in out], dtype=pl.Float64)


def calc_rsi(close: pl.Series, period: int) -> pl.Series:
    """RSI (Wilder 平滑).

    - 初始值: 前 period 个上涨/下跌均值 (需 period+1 个收盘价);
    - avg_loss == 0 → 100; avg_gain == 0 → 0; 两者皆 0 (无变动) → 50.
    """
    values = close.to_numpy()
    n = len(values)
    out = np.full(n, np.nan)
    if n < period + 1:
        return pl.Series([None] * n, dtype=pl.Float64)

    diffs = np.diff(values)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    def _rsi(gain: float, loss: float) -> float:
        if gain == 0.0 and loss == 0.0:
            return 50.0
        if loss == 0.0:
            return 100.0
        if gain == 0.0:
            return 0.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return pl.Series([None if math.isnan(v) else v for v in out], dtype=pl.Float64)


def calc_bollinger(
    close: pl.Series, window: int, num_std: float, *, min_periods: int | None = None,
) -> dict[str, pl.Series]:
    """布林带: 中轨 SMA, 上下轨 ± num_std × 总体标准差 (ddof=0).

    Returns: {mid, upper, lower, position, bandwidth}
    position 在上下轨相等 (带宽 0) 时为 null (不可评估)。
    """
    min_p = min_periods if min_periods is not None else window
    mid = calc_sma(close, window, min_periods=min_p)
    std = close.rolling_std(window_size=window, min_samples=min_p, ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    band = upper - lower
    position = _eval(
        pl.when((band > 0) & band.is_not_null()).then(
            (close - lower) / band
        ).otherwise(None)
    )
    bandwidth = _eval(
        pl.when((mid > 0) & mid.is_not_null() & band.is_not_null()).then(
            band / mid
        ).otherwise(None)
    )
    return {
        "mid": mid, "upper": upper, "lower": lower,
        "position": position, "bandwidth": bandwidth,
    }


def calc_amount_activity(
    amount: pl.Series, window: int, *, min_periods: int,
) -> pl.Series:
    """量额活跃度: 当日成交额 / 此前 window 个完整交易日基准均值 (shift(1))."""
    baseline = amount.shift(1).rolling_mean(window_size=window, min_samples=min_periods)
    expr = pl.when((baseline > 0) & baseline.is_not_null() & amount.is_not_null()).then(
        amount / baseline
    ).otherwise(None)
    return _eval(expr)


def calc_ratio_to_baseline(
    series: pl.Series, window: int, *, min_periods: int,
) -> pl.Series:
    """相对自身此前 window 日均值的比率 (基准 shift(1); 用于 ATR% 突增检测)."""
    baseline = series.shift(1).rolling_mean(window_size=window, min_samples=min_periods)
    expr = pl.when((baseline > 0) & baseline.is_not_null() & series.is_not_null()).then(
        series / baseline
    ).otherwise(None)
    return _eval(expr)


# ------------------------------------------------------------------
# 组合入口
# ------------------------------------------------------------------

def indicator_columns(params: dict) -> list[str]:
    """由规则卡参数派生指标列名 (窗口可变, 不硬编码)."""
    max_ma = max(int(w) for w in params["ma_windows"])
    cols: list[str] = []
    for window in params["ma_windows"]:
        cols += [f"ma{window}", f"ma{window}_dev"]
    cols += [
        f"ma{max_ma}_slope", "momentum_12_1",
        f"atr{params['atr_period']}", "atr_pct",
        f"vol{params['vol_window']}", "vol_pctile",
        f"rsi{params['rsi_period']}",
        "boll_mid", "boll_upper", "boll_lower", "boll_pos", "boll_bw",
        "amt_ratio", "atr_pct_ratio",
    ]
    return cols


def compute_indicators(prices: pl.DataFrame, params: dict) -> pl.DataFrame:
    """计算全部诊断指标列 (参数来自规则卡; 缺参数 → ValueError).

    Args:
        prices: 标准行情 [date, open, high, low, close, volume, amount] (升序)
        params: 规则卡 `indicators` 节 (键见 REQUIRED_PARAM_KEYS)

    Returns:
        原列 + `indicator_columns(params)` 全部指标列
    """
    missing = [k for k in REQUIRED_PARAM_KEYS if k not in params]
    if missing:
        raise ValueError(f"诊断指标参数缺失 (来自规则卡): {missing}")
    if prices.is_empty():
        return prices

    df = prices.sort("date")
    close, high, low, amount = df["close"], df["high"], df["low"], df["amount"]
    cols: list[pl.Series] = []

    for window in params["ma_windows"]:
        ma = calc_sma(close, int(window))
        cols.append(ma.alias(f"ma{window}"))
        cols.append(calc_ma_deviation(close, ma).alias(f"ma{window}_dev"))

    ma_slope_window = int(params["ma_slope_window"])
    max_ma_window = max(int(w) for w in params["ma_windows"])
    ma_long = calc_sma(close, max_ma_window)
    cols.append(
        calc_ma_slope(ma_long, ma_slope_window).alias(f"ma{max_ma_window}_slope")
    )

    cols.append(
        calc_momentum_12_1(
            close,
            lookback=int(params["momentum_lookback"]),
            skip=int(params["momentum_skip"]),
        ).alias("momentum_12_1")
    )

    atr_period = int(params["atr_period"])
    atr = calc_atr_series(high, low, close, atr_period)
    atr_pct = calc_atr_pct(atr, close)
    cols.append(atr.alias(f"atr{atr_period}"))
    cols.append(atr_pct.alias("atr_pct"))

    vol_window = int(params["vol_window"])
    vol = calc_return_volatility(close, vol_window)
    cols.append(vol.alias(f"vol{vol_window}"))
    cols.append(
        calc_percentile_rank(
            vol,
            int(params["vol_pctile_window"]),
            min_periods=int(params["vol_pctile_min_periods"]),
        ).alias("vol_pctile")
    )

    rsi_period = int(params["rsi_period"])
    cols.append(calc_rsi(close, rsi_period).alias(f"rsi{rsi_period}"))

    boll = calc_bollinger(
        close, int(params["boll_window"]), float(params["boll_num_std"]),
    )
    cols.append(boll["mid"].alias("boll_mid"))
    cols.append(boll["upper"].alias("boll_upper"))
    cols.append(boll["lower"].alias("boll_lower"))
    cols.append(boll["position"].alias("boll_pos"))
    cols.append(boll["bandwidth"].alias("boll_bw"))

    cols.append(
        calc_amount_activity(
            amount,
            int(params["amount_window"]),
            min_periods=int(params["amount_min_periods"]),
        ).alias("amt_ratio")
    )
    cols.append(
        calc_ratio_to_baseline(
            atr_pct,
            int(params["atr_ratio_window"]),
            min_periods=int(params["atr_ratio_min_periods"]),
        ).alias("atr_pct_ratio")
    )

    return df.with_columns(cols)
