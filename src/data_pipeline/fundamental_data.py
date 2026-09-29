"""股票基本面 / ETF 资料 数据获取与规范化 (P11-M4 数据层).

职责 (只取数与规范化, 不做规则判断 — 规则在 strategies/condition_checks.py):
  - 股票: Sina 三大报表 (含报告期/公告日期) + 财务指标 (扣非) + 估值日频史
          → 紧凑 extract (按报告期合并; 单季/TTM 由检查层派生);
  - ETF:  fund_overview_em 资料/费率/规模 + 净值与折价快照 + 买卖价快照 → extract。

口径与边界 (P11-M1 实测):
  - Sina 财务值为字符串, 缺失为 'None' 文本; ETF 快照缺失为 '---'; 一律规范为 None,
    不零填充、不外推;
  - 单位为 元 / 比例 (规模单独携带"亿元"原文与截止日期, 便于展示时效);
  - 全表快照 (ETF 净值/买卖价) 进程内 TTL 缓存 1 小时, 同一次运行/多个页面不重复拉全表;
  - 任一来源失败记录到 extract["errors"], 由检查层降级为"无法评估" (不阻塞技术诊断)。
"""

from __future__ import annotations

import math
import re
import time as _time
from collections.abc import Callable
from datetime import date, datetime

SNAPSHOT_TTL_SECONDS = 3600.0

_daily_snapshot_cache: dict = {"fetched": 0.0, "frame": None}
_category_snapshot_cache: dict = {"fetched": 0.0, "frame": None}


def reset_snapshot_caches() -> None:
    """清空进程内 ETF 快照缓存 (测试/强制刷新用)."""
    _daily_snapshot_cache.update({"fetched": 0.0, "frame": None})
    _category_snapshot_cache.update({"fetched": 0.0, "frame": None})


# ------------------------------------------------------------------
# 基础解析
# ------------------------------------------------------------------

_MISSING_TOKENS = {"", "none", "nan", "---", "--", "-", "nat"}


def parse_float(value: object) -> float | None:
    """宽松数值解析 (Sina 'None'/ETF '---' → None; 千分位/百分号/后缀自动剥离).

    支持: 纯数字/科学计数 ('5e10'), '0.33%', '0.15%（每年）' (取数字部分与百分号).
    NaN/Inf (pandas 缺失语义) 一律 → None, 不让其泄漏到检查结论。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if (math.isnan(number) or math.isinf(number)) else number
    text = str(value).strip()
    if text.lower() in _MISSING_TOKENS:
        return None
    text = text.replace(",", "")
    try:
        return float(text)  # 纯数字/科学计数优先 (完整字符串解析)
    except ValueError:
        pass
    match = re.match(r"^([-+]?\d+(?:\.\d+)?)\s*(%?)", text)
    if not match:
        return None
    number = float(match.group(1))
    return number / 100.0 if match.group(2) == "%" else number


def parse_date(value: object) -> date | None:
    """解析 YYYYMMDD / YYYY-MM-DD / 'YYYY年MM月DD日' / datetime/date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text.lower() in _MISSING_TOKENS:
        return None
    match = re.search(r"(\d{4})\D?(\d{2})\D?(\d{2})", text)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _row_get(row: dict, *names: str) -> float | None:
    for name in names:
        if name in row:
            value = parse_float(row.get(name))
            if value is not None:
                return value
    return None


def _frame_rows(df) -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    return df.to_dict("records")


def _percentile_of_last(values: list[float], window: int, min_samples: int) -> float | None:
    """当前值在尾部 window 观测内的经验分位 (含当日的 ≤计数/样本数; 样本不足 → None)."""
    clean = [v for v in values if v is not None]
    if len(clean) < min_samples:
        return None
    tail = clean[-window:]
    if len(tail) < min_samples:
        return None
    current = tail[-1]
    return sum(1 for v in tail if v <= current) / len(tail)


# ------------------------------------------------------------------
# 股票: 报表规范化
# ------------------------------------------------------------------

def build_stock_extract(
    symbol: str,
    reports: dict,
    *,
    indicator_df=None,
    valuation_df=None,
    pe_window: int = 1250,
    pe_min_samples: int = 250,
    errors: list[str] | None = None,
) -> dict:
    """把原始报表/指标/估值规范化为紧凑 extract (按报告期升序合并)."""
    errors = list(errors or [])
    income = reports.get("利润表")
    balance = reports.get("资产负债表")
    cashflow = reports.get("现金流量表")

    periods: dict[date, dict] = {}
    for frame in (income, balance, cashflow):
        for row in _frame_rows(frame):
            period = parse_date(row.get("报告日"))
            if period is None:
                continue
            entry = periods.setdefault(period, {
                "period": period, "announced": None, "revenue": None,
                "net_profit": None, "deducted_net_profit": None, "ocf": None,
                "total_assets": None, "total_liabilities": None,
                "equity_parent": None, "cash": None, "short_debt": None,
                "receivables": None,
            })
            announced = parse_date(row.get("公告日期"))
            if announced is not None:
                entry["announced"] = announced

    for row in _frame_rows(income):
        period = parse_date(row.get("报告日"))
        if period is None:
            continue
        entry = periods[period]
        entry["revenue"] = _row_get(row, "营业总收入", "营业收入")
        entry["net_profit"] = _row_get(row, "归属于母公司所有者的净利润", "净利润")

    for row in _frame_rows(balance):
        period = parse_date(row.get("报告日"))
        if period is None:
            continue
        entry = periods[period]
        entry["total_assets"] = _row_get(row, "资产总计")
        entry["total_liabilities"] = _row_get(row, "负债合计")
        # 工商企业与金融企业报表字段名不同 (如银行: "归属于母公司股东的权益")
        entry["equity_parent"] = _row_get(
            row, "归属于母公司股东权益合计", "归属于母公司股东的权益",
        )
        entry["cash"] = _row_get(row, "货币资金")
        short_borrow = _row_get(row, "短期借款") or 0.0
        current_debt = _row_get(row, "一年内到期的非流动负债") or 0.0
        if short_borrow or current_debt:
            entry["short_debt"] = short_borrow + current_debt
        entry["receivables"] = _row_get(row, "应收账款")

    for row in _frame_rows(cashflow):
        period = parse_date(row.get("报告日"))
        if period is None:
            continue
        periods[period]["ocf"] = _row_get(row, "经营活动产生的现金流量净额")

    for row in _frame_rows(indicator_df):
        period = parse_date(row.get("日期"))
        if period is None or period not in periods:
            continue
        periods[period]["deducted_net_profit"] = _row_get(
            row, "扣除非经常性损益后的净利润(元)",
        )

    ordered = [periods[key] for key in sorted(periods)]

    is_financial = bool(
        income is not None and not getattr(income, "empty", True)
        and any("净利息收入" in str(c) for c in income.columns)
    )

    valuation: dict | None = None
    val_rows = _frame_rows(valuation_df)
    if val_rows:
        pe_series = [parse_float(r.get("PE(TTM)")) for r in val_rows]
        pb_series = [parse_float(r.get("市净率")) for r in val_rows]
        last = val_rows[-1]
        valuation = {
            "date": parse_date(last.get("数据日期")),
            "close": parse_float(last.get("当日收盘价")),
            "pe_ttm": parse_float(last.get("PE(TTM)")),
            "pb": parse_float(last.get("市净率")),
            "pe_percentile": _percentile_of_last(pe_series, pe_window, pe_min_samples),
            "pb_percentile": _percentile_of_last(pb_series, pe_window, pe_min_samples),
            "history_rows": len(val_rows),
        }

    return {
        "symbol": symbol,
        "is_financial": is_financial,
        "currency": "CNY",
        "periods": ordered,
        "valuation": valuation,
        "errors": errors,
    }


def load_stock_conditions_input(
    symbol: str,
    *,
    reports_fetcher: Callable[[str], dict] | None = None,
    indicator_fetcher: Callable[[str], object] | None = None,
    valuation_fetcher: Callable[[str], object] | None = None,
    pe_window: int = 1250,
    pe_min_samples: int = 250,
) -> dict:
    """真实取数 + 规范化 (各来源失败记录 errors, 不抛出阻断)."""
    errors: list[str] = []
    reports = reports_fetcher(symbol) if reports_fetcher else _fetch_reports(symbol, errors)
    indicator_df = None
    valuation_df = None
    try:
        indicator_df = (
            indicator_fetcher(symbol) if indicator_fetcher else _fetch_indicator(symbol)
        )
    except Exception as exc:
        errors.append(f"财务指标获取失败: {type(exc).__name__}: {exc}")
    try:
        valuation_df = (
            valuation_fetcher(symbol) if valuation_fetcher else _fetch_valuation(symbol)
        )
    except Exception as exc:
        errors.append(f"估值获取失败: {type(exc).__name__}: {exc}")
    return build_stock_extract(
        symbol, reports or {}, indicator_df=indicator_df, valuation_df=valuation_df,
        pe_window=pe_window, pe_min_samples=pe_min_samples, errors=errors,
    )


def _sina_symbol(symbol: str) -> str:
    from data_pipeline.securities import to_sina_symbol

    return to_sina_symbol(symbol)


def _fetch_reports(symbol: str, errors: list[str]) -> dict:
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401  (代理绕过)

    sina = _sina_symbol(symbol)
    reports: dict = {}
    for name in ("利润表", "资产负债表", "现金流量表"):
        try:
            reports[name] = ak.stock_financial_report_sina(stock=sina, symbol=name)
        except Exception as exc:
            errors.append(f"{name}获取失败: {type(exc).__name__}: {exc}")
            reports[name] = None
    return reports


def _fetch_indicator(symbol: str):
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401

    start_year = str(date.today().year - 6)
    return ak.stock_financial_analysis_indicator(symbol=str(symbol).zfill(6), start_year=start_year)


def _fetch_valuation(symbol: str):
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401

    return ak.stock_value_em(symbol=str(symbol).zfill(6))


# ------------------------------------------------------------------
# ETF: 资料 / 快照规范化
# ------------------------------------------------------------------

def parse_scale(text: object) -> tuple[float | None, date | None, str]:
    """解析基金规模字符串: '948.72亿元（截止至：2026年06月30日）' → (948.72, date, '亿元')."""
    if text is None:
        return None, None, ""
    raw = str(text)
    unit = "亿元" if "亿元" in raw else ("亿份" if "亿份" in raw else "")
    number_match = re.search(r"([\d,]+(?:\.\d+)?)\s*(亿元|亿份)", raw)
    value = parse_float(number_match.group(1)) if number_match else None
    return value, parse_date(raw), unit


def parse_fee(text: object) -> float | None:
    """解析费率字符串: '0.15%（每年）' → 0.0015 (比例)."""
    value = parse_float(text)
    return value


def parse_inception(text: object) -> date | None:
    """成立日期: '2012年05月04日 / 329.686亿份' → date."""
    return parse_date(text)


def parse_overview_frame(overview, symbol: str) -> dict:
    rows = _frame_rows(overview)
    if not rows:
        raise ValueError(f"{symbol}: fund_overview_em 返回空")
    row = rows[0]
    scale_value, scale_date, scale_unit = parse_scale(row.get("净资产规模"))
    return {
        "full_name": str(row.get("基金全称") or ""),
        "fund_type": str(row.get("基金类型") or ""),
        "inception": parse_inception(row.get("成立日期/规模")),
        "track_index": str(row.get("跟踪标的") or ""),
        "benchmark": str(row.get("业绩比较基准") or ""),
        "manager": str(row.get("基金管理人") or ""),
        "custodian": str(row.get("基金托管人") or ""),
        "scale": {"value": scale_value, "unit": scale_unit, "as_of": scale_date},
        "management_fee": parse_fee(row.get("管理费率")),
        "custody_fee": parse_fee(row.get("托管费率")),
    }


_NAV_COL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-单位净值$")


def parse_premium_from_snapshot(snapshot, symbol: str) -> dict:
    """从 fund_etf_fund_daily_em 全表快照解析单只 ETF 的净值/市价/折溢价.

    日期来自列名 '{YYYY-MM-DD}-单位净值'; 净值缺失 ('---') → status=unknown
    (不解除既有禁买; 跨境当日净值可能未更新)。
    """
    rows = _frame_rows(snapshot)
    matched = [r for r in rows if str(r.get("基金代码", "")).zfill(6) == str(symbol).zfill(6)]
    if not matched:
        return {"status": "unknown", "note": f"快照未覆盖 {symbol}", "snapshot_date": None}
    row = matched[0]

    nav_columns: list[tuple[date, str]] = []
    if snapshot is not None:
        for column in snapshot.columns:
            match = _NAV_COL_RE.match(str(column))
            if match:
                nav_columns.append((parse_date(match.group(1)), str(column)))
    nav_columns = [(d, c) for d, c in nav_columns if d is not None]
    nav_columns.sort(reverse=True)

    current_nav_date = nav_columns[0][0] if nav_columns else None
    current_nav = parse_float(row.get(nav_columns[0][1])) if nav_columns else None
    price = parse_float(row.get("市价"))
    provided_discount = parse_float(row.get("折价率"))

    if current_nav is None or price is None or current_nav <= 0:
        return {
            "status": "unknown",
            "note": "当日净值缺失/未更新 或 市价缺失 → 折溢价无法评估 (不解除既有禁买)",
            "snapshot_date": current_nav_date,
            "nav_date": current_nav_date,
            "nav": current_nav,
            "price": price,
            "discount_pct": None,
            "provided_discount": provided_discount,
        }
    discount = (current_nav - price) / current_nav  # 正 = 折价 (市价低于净值)
    return {
        "status": "ok",
        "note": "净值与市价同日快照",
        "snapshot_date": current_nav_date,
        "nav_date": current_nav_date,
        "nav": current_nav,
        "price": price,
        "discount_pct": discount,
        "provided_discount": provided_discount,
    }


def parse_spread_from_category(snapshot, symbol: str) -> dict:
    """从 fund_etf_category_sina ETF 全表解析买卖价快照 (时点数据, 日线不能伪造价差)."""
    rows = _frame_rows(snapshot)
    code = str(symbol).zfill(6)
    matched = [
        r for r in rows
        if str(r.get("代码", "")).endswith(code)
    ]
    if not matched:
        return {"bid": None, "ask": None, "spread_pct": None, "note": "快照未覆盖"}
    row = matched[0]
    bid = parse_float(row.get("买入"))
    ask = parse_float(row.get("卖出"))
    spread = (ask - bid) / bid if (bid and ask and bid > 0) else None
    return {
        "bid": bid, "ask": ask, "spread_pct": spread,
        "note": "新浪 ETF 快照 (时点)",
    }


def _get_cached(cache: dict, fetcher: Callable[[], object]):
    now = _time.monotonic()
    frame = cache["frame"]
    if frame is not None and now - cache["fetched"] < SNAPSHOT_TTL_SECONDS:
        return frame
    frame = fetcher()
    cache.update({"fetched": now, "frame": frame})
    return frame


def _fetch_daily_snapshot():
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401

    return ak.fund_etf_fund_daily_em()


def _fetch_category_snapshot():
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401

    return ak.fund_etf_category_sina(symbol="ETF基金")


def load_etf_conditions_input(
    symbol: str,
    *,
    profile_fetcher: Callable[[str], object] | None = None,
    premium_fetcher: Callable[[], object] | None = None,
    spread_fetcher: Callable[[], object] | None = None,
) -> dict:
    """ETF 条件数据: 资料 + 净值/折价快照 + 买卖价快照 (全表快照进程内缓存)."""
    errors: list[str] = []
    profile = None
    premium = None
    spread = None
    try:
        overview = profile_fetcher(symbol) if profile_fetcher else _fetch_overview(symbol)
        profile = parse_overview_frame(overview, symbol)
    except Exception as exc:
        errors.append(f"ETF 资料获取失败: {type(exc).__name__}: {exc}")
    try:
        snapshot = (
            premium_fetcher() if premium_fetcher
            else _get_cached(_daily_snapshot_cache, _fetch_daily_snapshot)
        )
        premium = parse_premium_from_snapshot(snapshot, symbol)
    except Exception as exc:
        errors.append(f"净值/折价快照获取失败: {type(exc).__name__}: {exc}")
        premium = {"status": "unknown", "note": "快照获取失败"}
    try:
        category = (
            spread_fetcher() if spread_fetcher
            else _get_cached(_category_snapshot_cache, _fetch_category_snapshot)
        )
        spread = parse_spread_from_category(category, symbol)
    except Exception as exc:
        errors.append(f"买卖价快照获取失败: {type(exc).__name__}: {exc}")
        spread = {"bid": None, "ask": None, "spread_pct": None, "note": "快照获取失败"}
    return {
        "symbol": symbol,
        "profile": profile,
        "premium": premium,
        "spread": spread,
        "errors": errors,
    }


def _fetch_overview(symbol: str):
    import akshare as ak

    import data_pipeline.fetchers.akshare_fetcher  # noqa: F401

    return ak.fund_overview_em(symbol=str(symbol).zfill(6))
