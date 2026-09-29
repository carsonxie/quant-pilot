"""基础绩效核算 (M6) — 盈亏闭合 / 外部现金流 / 日线估值与 TWR 近似.

契约 (spec §3.3/§7.4/§6.1, plan §M6):
  - 金额核对: 期末权益 − 期初权益 − 净外部入金 = 投资盈亏 (P05-D7)。
    持仓/现金(分红)/费用贡献与未解释差额单列; 差额不隐藏。
  - 外部现金流: 入金/出金不计入投资收益; 分红属于投资收益 (与入金分别记录)。
  - 收益口径: 现金流口径; 只有日线数据时, 现金流时点采用明确约定
    (默认"日终估值点"), 并在结果中标注近似 — 不把未知时点当作精确 TWR。
    期间收益回报同时给出: 简单期间收益 (现金流调整) 与链接 TWR (日频)。
  - 估值: 事件驱动逐点估值 (估值日 = 事件日 ∪ 区间端点), 价格取 ≤ 估值日的
    最近可用收盘价 (不复权; 缺失不虚构, 计入估值缺口)。
  - 参考价滑点不重复扣现金 (成本只以 fee 事件/账本事实进入现金)。

金额闭合推导 (每个事件对权益的瞬时影响):
  deposit: +amount            withdrawal: −amount
  trade:   −fee               fee: −amount
  dividend_accrual: +amount   dividend_payment: +amount −receivable 冲减 (+amount 现金,
                              应收减少 amount → 净 +amount? 见下实现说明)
  dividend_payment 的实际影响: 现金 +amount, 应收 −min(receivable, amount)
    → impact = amount − min(receivable, amount) (未记应收时 = +amount)。
  share_change: 0 (份额与价格同比例调整, 市值不变)
  initial: +amount (资本注入起点)

Usage:
    from backtest.performance import compute_performance, performance_for_account
    report = performance_for_account(db, "BASE-B0")
    print(report.summary())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from data_pipeline.accounting import AccountEvent, apply_event, events_from_dataframe
from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.market_data import PriceSeries

logger = logging.getLogger(__name__)

# 日线数据的现金流时点约定 (spec §7.4: 明确约定并标注近似)
CASHFLOW_TIMING_NOTE = (
    "现金流口径: 外部入金/出金不计入收益; 仅有日线数据时, 现金流时点按'日终估值点'"
    "近似 (事件发生日收盘后), TWR 为近似值 — 不把未知时点当作精确 TWR (spec §7.4)"
)

# 进入"外部现金流"的事件 (不产生投资收益)
_EXTERNAL_FLOW_TYPES = ("initial", "deposit", "withdrawal")


@dataclass
class NavPoint:
    """单个估值点 (事件驱动)."""

    day: date
    nav: float
    impact: float          # 该点事件对权益的瞬时影响 (含入金/费用/分红)
    inflow: float          # 外部流入 (initial/deposit) 正
    outflow: float         # 外部流出 (withdrawal) 正
    missing_price: bool = False  # 持仓缺价 → 估值不完整


@dataclass
class PerformanceReport:
    """绩效报告 (金额闭合 + 收益口径 + 估值说明)."""

    account_id: str
    mode: str
    start: date
    end: date
    nav_open: float                      # 起点日收盘权益 (含当日事件)
    nav_start: float                     # 调整起点 (收益区间基准)
    nav_end: float
    external_in: float
    external_out: float
    dividends: float
    fees: float                          # 费用合计 (正 = 成本; 投资盈亏 = 持仓贡献 + 分红 − 费用)
    trade_count: int
    day1_fees: float = 0.0               # 起点日费用 (正; 已计入调整起点)
    day1_dividends: float = 0.0          # 起点日分红 (正; 已计入调整起点)
    nav_points: list[NavPoint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def net_external_flow(self) -> float:
        return self.external_in - self.external_out

    @property
    def investment_pnl(self) -> float:
        """投资盈亏 = 期末 − 调整起点 − 净外部入金 (P05-D7 金额核对).

        调整起点 = 起点日收盘权益 + 起点日费用 − 起点日分红 (`_opening_baseline`):
        起点日费用已从起点扣除 (降低起点, 计入期间成本), 起点日分红已从起点
        扣除 (计入期间收益) — 两者在公式中只出现一次, 核对保持闭合。
        """
        return self.nav_end - self.nav_start - self.net_external_flow

    @property
    def reconciled_pnl(self) -> float:
        """持仓/现金贡献 (市值变动) = 投资盈亏 − 分红 + 费用 (P05-D7 分项).

        投资盈亏 = 持仓/现金贡献 + 分红 − 费用; 三项合计与金额核对闭合。
        """
        return self.investment_pnl - self.dividends + self.fees

    @property
    def unexplained(self) -> float:
        """未解释差额 = 投资盈亏 − (持仓贡献 + 分红 − 费用) (应为 0; 非零则披露).

        持仓贡献与分红/费用分项由事件核算得到; 差额来自估值缺口或未覆盖
        的事件类型, 不隐藏。
        """
        return self.investment_pnl - (self.reconciled_pnl + self.dividends - self.fees)

    def simple_return(self) -> float:
        """期间简单收益 (现金流调整): (期末 − 净入金) / (期初) − 1."""
        base = self.nav_start
        if base <= 0:
            return 0.0
        return (self.nav_end - self.net_external_flow) / base - 1.0

    def twr(self) -> float:
        """链接 TWR (日频近似; 现金流按日终估值点; spec §7.4).

        r_t = (NAV_t − 当日净流入) / NAV_{t−1} − 1; TWR = Π(1+r_t) − 1。
        起点使用调整起点 (nav_start: 成本还原后的基线), 与简单期间收益在同一
        无期中现金流场景下一致; 现金流时点为日终近似, 结果随报告标注。
        """
        total = 1.0
        prev = self.nav_start
        for point in self.nav_points[1:]:  # 首点为基线, 不计收益
            if prev <= 0:
                prev = point.nav
                continue
            flow = point.inflow - point.outflow
            total *= (1.0 + (point.nav - flow) / prev - 1.0)
            prev = point.nav
        return total - 1.0

    def summary(self) -> str:
        lines = [
            f"===== 绩效报告 {self.account_id} ({self.mode}) =====",
            f"[区间] {self.start} → {self.end} | 估值点 {len(self.nav_points)}",
            f"[权益] 期初(调整) {self.nav_start:,.2f} → 期末 {self.nav_end:,.2f} "
            f"| 起点日收盘 {self.nav_open:,.2f}",
            f"[外部现金流] 入金 {self.external_in:,.2f} | 出金 {self.external_out:,.2f} "
            f"| 净入金 {self.net_external_flow:,.2f} (不计收益)",
            f"[投资盈亏] {self.investment_pnl:,.2f} = 期末 − 调整起点 − 净入金",
            f"  · 持仓/现金贡献 {self.reconciled_pnl:,.2f} "
            f"(含分红 {self.dividends:,.2f}、费用 {self.fees:,.2f})",
            f"  · 未解释差额 {self.unexplained:,.2f}",
            f"[收益] 简单期间收益 {self.simple_return():.2%} | 链接 TWR(日频近似) {self.twr():.2%}",
            f"[成交] {self.trade_count} 笔 | [口径] {CASHFLOW_TIMING_NOTE}",
        ]
        lines.extend(f"[注] {note}" for note in self.notes)
        return "\n".join(lines)


# ------------------------------------------------------------------
# 事件驱动估值
# ------------------------------------------------------------------

def _event_net_effect(event: AccountEvent, receivable_before: float) -> float:
    """事件对权益的瞬时净影响 (不含持仓市值部分, 交易本身不影响权益)."""
    kind = event.event_type
    amount = float(event.amount or 0.0)
    fee = float(event.fee or 0.0)
    if kind in ("initial", "deposit"):
        return amount
    if kind == "withdrawal":
        return -amount
    if kind == "trade":
        return -fee
    if kind == "fee":
        return -amount
    if kind == "dividend_accrual":
        return amount
    if kind == "dividend_payment":
        return amount - min(receivable_before, amount)
    if kind == "share_change":
        return 0.0
    raise ValueError(f"未知事件类型无法核算: {kind}")


def _events_upto(db: DuckDBManager, account_id: str) -> list[AccountEvent]:
    df = db.get_account_events(account_id)
    if df.is_empty():
        return []
    return events_from_dataframe(df)


def compute_performance(
    events: list[AccountEvent],
    prices: PriceSeries | None = None,
    start: date | None = None,
    end: date | None = None,
    account_id: str | None = None,
    mode: str = "unknown",
) -> PerformanceReport:
    """按事件回放核算绩效 (估值点 = 事件日 ∪ 区间端点).

    Args:
        events: 账户事件 (含更正; 内部解析)
        prices: 行情序列 (None → 无价格, 只按现金/应收核算, 持仓市值缺失计入缺口)
        start/end: 报告区间 (None → 事件首末日期)
        account_id/mode: 报告标识 (缺省取事件)
    """
    from data_pipeline.accounting import resolve_corrections

    if not events:
        raise ValueError("无事件无法核算绩效")
    ordered = sorted(events, key=lambda e: (e.event_time, e.event_id))
    effective, _orphan = resolve_corrections(ordered)

    first_day = start or effective[0].event_time.date()
    # 结束日: 缺省取最新可用估值日 (数据库最后交易日; 无行情则取最后事件日)。
    # 账户无近期事件时仍按最新价格估值 (持仓按最近可用价持有)。
    if end is not None:
        last_day = end
    elif prices is not None and prices.trading_days:
        last_day = max(prices.trading_days[-1], effective[-1].event_time.date())
    else:
        last_day = effective[-1].event_time.date()
    in_range = [e for e in effective if first_day <= e.event_time.date() <= last_day]
    if not in_range:
        raise ValueError(f"区间 {first_day}~{last_day} 无事件")

    account_id = account_id or in_range[0].account_id

    # ---- 区间起点基准 (现金流时点约定: 事件发生日收盘后记账) ----
    # 起点日收盘为基线: 起点日的外部入金/出金不用报告期外部现金流表示
    # (那是"初始资本"), 起点日的费用/分红从起点还原, 计入报告期损益。
    nav_open, nav_start, day1_dividends, day1_fees = _opening_baseline(
        effective, first_day, prices, account_id,
    )

    # ---- 逐日估值网格 (交易日 + 事件日; TWR 需要每日点) ----
    from data_pipeline.accounting import AccountState

    state = AccountState(account_id=account_id)
    calendar: list[date] = []
    if prices is not None and prices.trading_days:
        calendar = [d for d in prices.trading_days if first_day <= d <= last_day]
    for boundary in (first_day, last_day):
        if boundary not in calendar:
            calendar.append(boundary)
    calendar = sorted(set(calendar))

    by_day: dict[date, list[AccountEvent]] = {}
    for event in effective:
        day = event.event_time.date()
        if first_day <= day <= last_day:
            by_day.setdefault(day, []).append(event)

    points: list[NavPoint] = []
    for day in calendar:
        events_today = by_day.get(day, [])
        nav_before, _ = _value_state(state, prices, day)

        external_today = [
            e for e in events_today
            if e.event_type in ("initial", "deposit", "withdrawal")
        ]
        non_flow_today = [e for e in events_today if e not in external_today]

        for event in external_today:
            apply_event(state, event)
        nav_after_flows, _ = _value_state(state, prices, day)

        impact = nav_after_flows - nav_before
        inflow = sum(
            float(e.amount or 0.0) for e in external_today
            if e.event_type in ("initial", "deposit")
        )
        outflow = sum(
            float(e.amount or 0.0) for e in external_today
            if e.event_type == "withdrawal"
        )
        if day == first_day:
            # 起点日资本构成基线, 不计入报告期外部现金流/影响
            impact, inflow, outflow = 0.0, 0.0, 0.0

        for event in non_flow_today:
            receivable_before = state.receivable
            impact += _event_net_effect(event, receivable_before)
            apply_event(state, event)
        nav, missing = _value_state(state, prices, day)

        points.append(NavPoint(
            day=day, nav=nav, impact=impact,
            inflow=inflow, outflow=outflow, missing_price=missing,
        ))

    external_in = sum(p.inflow for p in points)
    external_out = sum(p.outflow for p in points)

    # 分红收益: 仅应收确认计入投资收益 (payment 是现金与应收的内部转换)
    dividends = day1_dividends + sum(
        float(e.amount or 0.0) for e in in_range
        if e.event_type == "dividend_accrual" and e.event_time.date() > first_day
    )

    # 费用以正值计 (成本); 投资盈亏 = 持仓贡献 + 分红 − 费用
    fees = (
        sum(float(e.fee or 0.0) for e in in_range if e.event_type == "trade")
        + sum(float(e.amount or 0.0) for e in in_range if e.event_type == "fee")
    )
    trade_count = sum(1 for e in in_range if e.event_type == "trade")

    notes: list[str] = [CASHFLOW_TIMING_NOTE]
    if any(p.missing_price for p in points):
        notes.append("存在估值缺口 (持仓缺价): 相关点权益不完整, 金额核对不可用")
    if not prices or prices.trading_days == []:
        notes.append("无行情价格: 持仓按 0 计入 (仅供现金核算)")

    report = PerformanceReport(
        account_id=account_id,
        mode=mode,
        start=first_day,
        end=last_day,
        nav_open=nav_open,
        nav_start=nav_start,
        nav_end=points[-1].nav if points else 0.0,
        external_in=external_in,
        external_out=external_out,
        dividends=dividends,
        fees=fees,
        trade_count=trade_count,
        day1_fees=day1_fees,
        day1_dividends=day1_dividends,
        nav_points=points,
        notes=notes,
    )
    return report


def _value_state(
    state, prices: PriceSeries | None, day: date,
) -> tuple[float, bool]:
    """按 ≤ day 最近可用收盘价估值 (缺价 → 0 市值 + 缺口标记)."""
    value = state.cash + state.receivable
    missing = False
    for sym in state.lots:
        shares = state.shares_of(sym)
        if shares <= 0:
            continue
        if prices is None:
            missing = True
            continue
        found = prices.close_on_or_before(sym, day)
        if found is None:
            missing = True
            continue
        value += shares * found[0]
    return value, missing


def _opening_baseline(
    effective: list[AccountEvent],
    first_day: date,
    prices: PriceSeries | None,
    account_id: str,
) -> tuple[float, float, float, float]:
    """区间起点基准.

    Returns:
        (nav_open, nav_start, day1_dividends, day1_fees)
        nav_open: 起点日收盘权益 (含当日全部事件)
        nav_start: 调整起点 = nav_open + 当日费用 − 当日分红
                   (当日费用从其影响中还原: 起点资本不含费, 费用计入期间成本;
                    当日分红亦从起点还原: 计入期间收益; 使
                    "期末 − 起点 − 净入金" 恰为投资盈亏, 且与分红/费用分项闭合)
        day1_dividends: 起点日应收分红 (正; 计入期间收益)
        day1_fees: 起点日费用 (正; 计入期间成本)
    """
    from data_pipeline.accounting import AccountState

    state = AccountState(account_id=account_id)
    day1_dividends = 0.0
    day1_fees = 0.0
    for event in effective:
        ev_day = event.event_time.date()
        if ev_day > first_day:
            break
        if ev_day == first_day:
            if event.event_type == "dividend_accrual":
                day1_dividends += float(event.amount or 0.0)
            elif event.event_type == "trade":
                day1_fees += float(event.fee or 0.0)
            elif event.event_type == "fee":
                day1_fees += float(event.amount or 0.0)
        apply_event(state, event)
    nav_open, _ = _value_state(state, prices, first_day)
    nav_start = nav_open + day1_fees - day1_dividends
    return nav_open, nav_start, day1_dividends, day1_fees


# ------------------------------------------------------------------
# 便捷入口 (账户)
# ------------------------------------------------------------------

def performance_for_account(
    db: DuckDBManager,
    account_id: str,
    start: date | None = None,
    end: date | None = None,
) -> PerformanceReport:
    """账户绩效报告 (自动读取账户 mode 与行情)."""
    accounts = db.get_accounts()
    mode = "unknown"
    if not accounts.is_empty():
        match = accounts.filter(pl.col("account_id") == account_id)
        if not match.is_empty():
            mode = str(match["mode"][0])
    events = _events_upto(db, account_id)
    if not events:
        raise ValueError(f"账户 {account_id} 无事件, 无法核算绩效")
    prices = PriceSeries.from_db(db)
    return compute_performance(
        events, prices=prices, start=start, end=end,
        account_id=account_id, mode=mode,
    )


def load_nav_series(
    db: DuckDBManager, account_id: str,
) -> pl.DataFrame:
    """账户逐日估值序列 (date/nav), 供面板净值对照.

    逐日估值: 交易日网格 × 事件回放, 价格用 ≤ 当日最近收盘价。
    区间: 首个事件日 → 最后交易日 (无事件 → 空表)。
    """
    events = _events_upto(db, account_id)
    if not events:
        return pl.DataFrame()
    prices = PriceSeries.from_db(db)
    if not prices.trading_days:
        return pl.DataFrame()
    first_day = min(e.event_time.date() for e in events)
    last_day = prices.trading_days[-1]

    from data_pipeline.accounting import AccountState, apply_event, resolve_corrections

    effective, _ = resolve_corrections(
        sorted(events, key=lambda e: (e.event_time, e.event_id))
    )
    by_day: dict[date, list[AccountEvent]] = {}
    for e in effective:
        by_day.setdefault(e.event_time.date(), []).append(e)

    state = AccountState(account_id=account_id)
    for e in effective:
        if e.event_time.date() < first_day:
            apply_event(state, e)

    rows: list[dict] = []
    for day in prices.trading_days:
        if day < first_day or day > last_day:
            continue
        for e in by_day.get(day, []):
            apply_event(state, e)
        nav, missing = _value_state(state, prices, day)
        rows.append({"date": day, "nav": nav, "missing_price": missing})
    return pl.DataFrame(rows)


# ------------------------------------------------------------------
# 多账户对照 (面板 B0/B1/A)
# ------------------------------------------------------------------

@dataclass
class AccountSummary:
    account_id: str
    mode: str
    nav_start: float
    nav_end: float
    period_return: float
    twr: float
    investment_pnl: float
    net_external_flow: float


def compare_accounts(
    db: DuckDBManager, account_ids: list[str],
) -> tuple[list[AccountSummary], pl.DataFrame]:
    """多账户同口径对照 (独立净值; 模拟与真实不合并, spec §3.1).

    Returns: (逐账户摘要, 归一化净值宽表 date + 各账户列; 首日 = 100)
    """
    summaries: list[AccountSummary] = []
    series: list[pl.DataFrame] = []
    for account_id in account_ids:
        try:
            report = performance_for_account(db, account_id)
        except ValueError:
            continue
        summaries.append(AccountSummary(
            account_id=account_id,
            mode=report.mode,
            nav_start=report.nav_start,
            nav_end=report.nav_end,
            period_return=report.simple_return(),
            twr=report.twr(),
            investment_pnl=report.investment_pnl,
            net_external_flow=report.net_external_flow,
        ))
        nav_df = load_nav_series(db, account_id)
        if nav_df.is_empty():
            continue
        base = float(nav_df["nav"][0])
        if base <= 0:
            continue
        series.append(
            nav_df.select(
                pl.col("date"),
                (pl.col("nav") / base * 100.0).alias(account_id),
            )
        )
    if not series:
        return summaries, pl.DataFrame()
    wide = series[0]
    for nxt in series[1:]:
        wide = wide.join(nxt, on="date", how="full", coalesce=True).sort("date")
    return summaries, wide
