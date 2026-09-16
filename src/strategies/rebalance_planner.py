"""调仓规划器 (M4) — 目标与账户实际权重对比 → 可执行交易建议.

契约 (spec §3.2/§5.4/§5.5/§6.1, plan §M4):
  - 比较 actual_weight 与 target_weight, 不比较新旧目标 (C07).
  - 首版常规触发使用绝对百分点偏离: |d_i| ≥ band_i; band 以比例值保存
    (0.05 = 5 个百分点, 仅示例配置, 不是投资建议).
  - 越界资产调回目标; 未越界不追精确对齐; 整数手与费用允许留残余偏离.
  - 风险优先级顺序 (spec §5.5), 下游不得放宽:
      1. 交易与数据可用性约束 (池 can_buy/can_sell, 停牌, 无有效价格)
      2. 账户硬约束 / 策略暂停 (总权益非正, 关键估值缺失 → 暂停可执行数量;
         现金可用量含预留)
      3. 明确风险退出与暴露上限 (止损退出/清零目标无条件建议, 不受 band
         与最小金额过滤压制)
      4. 常规目标调整 (band + 最小偏离过滤)
      5. 成本过滤 (只约束常规调整)
  - 数量规划: 参考价 (T 日最近可用收盘价), 费用估计 (佣金+滑点分离, 滑点
    用于估算参考价影响, 不重复扣现金), 最小交易单位整手, 可卖份额上限,
    买入按可负担方向取整.
  - 卖出依赖 (spec §6.1): 依赖卖出资金的买入标记 depends_on 条件建议;
    资金不足时缩减买入并记录原因; 不生成负现金.
  - 现金预留 (M4-05): 只影响可用量 (cash − cash_reserved), 不减少净资产;
    规划不消耗预留额度.
  - 冷却/重新进入 (spec §5.5): 止损退出后的冷却期内不出新买单
    (冷却状态由 strategy_states 维护; 本模块只消费 cooldown_symbols).

本模块为纯函数: 不读写数据库、不决定执行 (人工执行, 项目不做自动下单).
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from data_pipeline.accounting import AccountState

logger = logging.getLogger(__name__)

LOT_SIZE = 100  # ETF 最小交易单位 (份/手, 与回测引擎一致)

# 优先级: 数值越大越先执行 (spec §5.5 顺序; 下游过滤不得压制风险退出)
PRIORITY_RISK_EXIT = 90       # 风险退出 / 明确清零
PRIORITY_SELL_REBALANCE = 50  # 常规减持 (释放资金)
PRIORITY_BUY_REBALANCE = 30   # 常规增持
PRIORITY_BUY_DEPENDENT = 10   # 依赖卖出资金的买入 (条件建议)


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class PositionValue:
    """单标的估值 (数量 × 价格)."""

    symbol: str
    shares: float
    price: float
    market_value: float
    price_date: date | None = None


@dataclass
class AccountValuation:
    """账户实际状态估值 (spec §3.3: V = 现金 + Σ 持仓市值 + 应收).

    现金计入 NAV (含预留部分; 预留只影响可用量); 应收权益计入 NAV 并单列.
    无法估值的持仓 → 标记 unknown, 不静默按 0 计价.
    """

    account_id: str
    as_of: datetime | None
    cash: float
    cash_withdrawable: float
    cash_reserved: float
    receivable: float
    positions: dict[str, PositionValue] = field(default_factory=dict)
    nav: float = 0.0
    weights: dict[str, float] = field(default_factory=dict)
    cash_weight: float = 0.0
    valuation_ok: bool = True
    missing_prices: list[str] = field(default_factory=list)
    stale_prices: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def cash_available(self) -> float:
        """可用现金 = 现金 − 预留 (预留不减少净资产, 只减少可用量)."""
        return max(0.0, self.cash - self.cash_reserved)

    def summary(self) -> str:
        lines = [
            f"账户 {self.account_id}: NAV {self.nav:,.2f} | 现金 {self.cash:,.2f} "
            f"(可用 {self.cash_available:,.2f}, 预留 {self.cash_reserved:,.2f}) "
            f"| 应收 {self.receivable:,.2f}",
        ]
        for sym, pos in sorted(self.positions.items()):
            lines.append(
                f"  {sym}: {pos.shares:,.0f} 份 × {pos.price:.3f} "
                f"= {pos.market_value:,.2f} ({self.weights.get(sym, 0.0):.1%})"
            )
        if self.missing_prices:
            lines.append(f"  ⚠️ 缺估值价: {', '.join(self.missing_prices)}")
        if self.stale_prices:
            lines.append(f"  注: 价格较旧: {', '.join(self.stale_prices)}")
        return "\n".join(lines)


@dataclass
class PlannedProposal:
    """规划出的建议 (交由 M2 建议账本落库; 建议价非成交价)."""

    symbol: str
    side: str                        # BUY / SELL
    shares: int
    target_weight: float
    actual_weight: float
    ref_price: float
    ref_time: datetime
    est_amount: float                # 参考价 × 份额 (滑点前)
    est_fee: float                   # 佣金估计 (最低收费已计)
    est_slippage_rate: float         # 估算滑点率 (体现于估算价, 不重复扣现金)
    valid_until: datetime | None
    priority: int
    reason: str
    depends_on: str | None = None    # 依赖的卖出资金 (条件建议)
    proposal_id: str | None = None   # 落库后由账本填充
    conditional: bool = False        # 依赖卖出执行的条件建议
    target_shares: int | None = None  # 计划满额份额 (现金约束前), 供缩减参考


@dataclass
class SkippedAction:
    """被跳过/压制的动作 (不静默: 保留原因)."""

    symbol: str
    side: str
    reason: str
    target_weight: float = 0.0
    actual_weight: float = 0.0


@dataclass
class PlanResult:
    """规划结果 (建议 + 跳过 + 账户估值 + 说明)."""

    proposals: list[PlannedProposal] = field(default_factory=list)
    skipped: list[SkippedAction] = field(default_factory=list)
    valuation: AccountValuation | None = None
    notes: list[str] = field(default_factory=list)
    paused: bool = False                      # 账户关键约束 → 暂停生成可执行数量

    def summary(self) -> str:
        lines = [
            f"[调仓规划] 建议 {len(self.proposals)} 条, 跳过 {len(self.skipped)} 项"
            + (" | ⚠️ 已暂停可执行数量" if self.paused else ""),
        ]
        for p in self.proposals:
            dep = f" (依赖 {p.depends_on})" if p.depends_on else ""
            lines.append(
                f"  {p.side} {p.symbol} {p.shares:,} 份 ≈ {p.est_amount:,.0f} 元 "
                f"| 优先 {p.priority}{dep} | {p.reason}"
            )
        for s in self.skipped:
            lines.append(f"  · 跳过 {s.side} {s.symbol}: {s.reason}")
        lines.extend(f"  注: {n}" for n in self.notes)
        return "\n".join(lines)


@dataclass
class PlannerConfig:
    """规划参数 (全部来自 settings; 示例配置不自动升级为真实配置)."""

    band_points: float = 0.05            # 绝对权重偏离触发带 (0.05 = 5 个百分点; 示例)
    valid_days: int = 3                  # 建议有效期 (近似: ≤ N 个自然日)
    min_trade_amount: float = 0.0        # 常规调整最小金额 (元); 只约束常规调整
    cooldown_days: int = 5               # 止损退出后重新进入冷却 (近似: N 个自然日)
    lot_size: int = LOT_SIZE
    include_sells: bool = True           # 只读"建议复核"场景可关闭

    @classmethod
    def from_settings(cls, settings: dict) -> PlannerConfig:
        cfg = settings.get("rebalancing", {})
        band = float(cfg.get("band_points", 0.05))
        return cls(
            band_points=max(0.0, band),
            valid_days=int(cfg.get("valid_days", 3)),
            min_trade_amount=float(cfg.get("min_trade_amount", 0.0)),
            cooldown_days=int(cfg.get("cooldown_days", 5)),
            lot_size=int(cfg.get("lot_size", LOT_SIZE)),
        )


# ------------------------------------------------------------------
# 账户估值
# ------------------------------------------------------------------

def value_account(
    account_state: AccountState,
    ohlcv: pl.DataFrame,
    as_of: date,
    stale_days: int = 5,
) -> AccountValuation:
    """按截至 as_of 的价格估值账户 (spec §3.3).

    - NAV = 现金 + Σ(份额 × 最近可用收盘价) + 应收.
    - 关键估值缺失 (持仓无价格): valuation_ok=False, 不静默按 0 计价.
    - 价格日期早于 as_of 超过 stale_days → 计入 stale_prices (标注时效).
    """
    cash = float(account_state.cash)
    lots = account_state.lots

    valuation = AccountValuation(
        account_id=account_state.account_id,
        as_of=account_state.last_event_time,
        cash=cash,
        cash_withdrawable=float(account_state.cash_withdrawable),
        cash_reserved=float(account_state.cash_reserved),
        receivable=float(account_state.receivable),
    )

    latest: dict[str, tuple[float, date]] = {}
    if not ohlcv.is_empty():
        priced = ohlcv.filter(
            (pl.col("date") <= as_of)
            & pl.col("close").is_not_null()
            & (pl.col("close") > 0)
        )
        for row in (
            priced.sort("date")
            .group_by("symbol")
            .agg(pl.col("close").last().alias("close"), pl.col("date").last().alias("date"))
            .iter_rows(named=True)
        ):
            latest[row["symbol"]] = (float(row["close"]), row["date"])

    total = cash + float(account_state.receivable)
    for symbol in sorted(lots):
        shares = account_state.shares_of(symbol)
        if shares <= 0:
            continue
        if symbol not in latest:
            valuation.missing_prices.append(symbol)
            valuation.valuation_ok = False
            continue
        price, price_date = latest[symbol]
        market_value = shares * price
        valuation.positions[symbol] = PositionValue(
            symbol=symbol, shares=shares, price=price,
            market_value=market_value, price_date=price_date,
        )
        if price_date < as_of - timedelta(days=stale_days):
            valuation.stale_prices.append(symbol)
        total += market_value

    valuation.nav = total
    if total > 0:
        valuation.weights = {
            sym: pos.market_value / total for sym, pos in valuation.positions.items()
        }
        valuation.cash_weight = (cash + float(account_state.receivable)) / total
    else:
        valuation.valuation_ok = False

    if valuation.missing_prices:
        valuation.notes.append(
            f"缺估值价 {valuation.missing_prices}: NAV 不完整, 不生成可执行数量 (spec §3.3)"
        )
    if valuation.stale_prices:
        valuation.notes.append(
            f"价格较旧 {valuation.stale_prices}: 按最近可用价估值并标注时效"
        )
    if valuation.nav <= 0:
        valuation.notes.append("总权益非正: 暂停可执行数量, 保留研究目标与问题说明 (spec §3.3)")
    return valuation


# ------------------------------------------------------------------
# 费用估计 (与回测引擎同一公式, spec §6.3/C08)
# ------------------------------------------------------------------

def _commission_of(amount: float, cost_cfg: dict) -> float:
    """每笔佣金 = max(成交额 × 单边费率, 最低收费); 不重复计双边费率."""
    rate = float(cost_cfg.get("commission_rate", 0.0))
    minimum = float(cost_cfg.get("min_commission_per_trade", 0.0))
    return max(amount * rate, minimum)


def _slippage_rate(
    symbol: str,
    as_of: date,
    amount_est: float,
    ohlcv: pl.DataFrame,
    cost_cfg: dict,
    lookback_days: int = 20,
) -> float:
    """估算滑点率 = max(下限, 金额/前日为止 20 日均额 × 冲击系数) (旧压力情景, M1-1-04)."""
    base = float(cost_cfg.get("slippage_base", 0.0))
    impact_coef = float(cost_cfg.get("impact_coefficient", 0.0))
    if impact_coef <= 0 or base < 0 or ohlcv.is_empty():
        return base
    cols = set(ohlcv.columns)
    if "amount" not in cols or "date" not in cols:
        return base
    sym_df = ohlcv.filter((pl.col("symbol") == symbol) & (pl.col("date") < as_of))
    if sym_df.is_empty():
        return base
    recent = sym_df.sort("date").tail(lookback_days)
    amounts = [
        float(v) for v in recent["amount"].to_list()
        if v is not None and float(v) > 0
    ]
    if not amounts:
        return base
    avg = statistics.fmean(amounts)
    if avg <= 0:
        return base
    return max(base, amount_est / avg * impact_coef)


def _max_affordable_shares(
    budget: float,
    price: float,
    lot: int,
    cost_cfg: dict,
    slippage_rate: float,
) -> int:
    """买入预算 → 最大可负担整手份额 (含滑点与佣金估计; 不超预算)."""
    if budget <= 0 or price <= 0 or lot <= 0:
        return 0
    gross_per_share = price * (1.0 + slippage_rate)
    rate = float(cost_cfg.get("commission_rate", 0.0))
    minimum = float(cost_cfg.get("min_commission_per_trade", 0.0))
    cost_per_share = gross_per_share * (1.0 + rate)

    def affordable(lots: int) -> bool:
        shares = lots * lot
        cost = shares * cost_per_share
        return max(cost, shares * gross_per_share) + minimum <= budget + 1e-9

    # 上界: 不小于预算对应的手数, 且必须确实不可负担 (二元搜索前提)
    lower = 0
    upper = max(1, int(budget / max(cost_per_share, 1e-12) / lot) + 2)
    guard = 0
    while affordable(upper):
        lower = upper
        upper *= 2
        guard += 1
        if guard > 40:
            return lower * lot
    while lower < upper:
        mid = (lower + upper + 1) // 2
        if affordable(mid):
            lower = mid
        else:
            upper = mid - 1
    return lower * lot


# ------------------------------------------------------------------
# 主规划函数
# ------------------------------------------------------------------

def plan_rebalance(
    account_state: AccountState | None,
    ohlcv: pl.DataFrame,
    target_weights: dict[str, float],
    *,
    as_of: date,
    ref_time: datetime | None = None,
    account_id: str | None = None,
    target_id: str | None = None,
    pool_can_buy: set[str] | None = None,
    pool_can_sell: set[str] | None = None,
    stale_symbols: set[str] | None = None,
    cooldown_symbols: dict[str, str] | None = None,
    risk_exit_symbols: dict[str, str] | None = None,
    settings: dict | None = None,
    config: PlannerConfig | None = None,
) -> PlanResult:
    """目标 vs 账户实际 → 调仓建议 (spec §5.4/§5.5/§6.1).

    Args:
        account_state: 账户状态 (None/空账户 → 计划为空并标注)
        ohlcv: 行情 (用 ≤ as_of 的最近收盘价作参考价与估值价)
        target_weights: 完整目标权重 (含现金 `__CASH__`; 其余项视为现金)
        as_of: 决策日 (T 日盘后; 最早下一可交易时段执行)
        pool_can_buy / pool_can_sell: 四层池许可 (M3); None = 不校验
        stale_symbols: 停牌/无当日价格标的 (不能买卖; 禁买≠清仓)
        cooldown_symbols: symbol → 原因 (冷却期内不出买单)
        risk_exit_symbols: symbol → 原因 (风险退出; 不受带宽/成本过滤压制;
            无可卖份额时保留展示, 不清仓)
        settings: settings.yaml (读取 trading_cost; rebalancing 节可用 config 覆盖)
        config: 显式规划参数 (缺省从 settings 读取)
    """
    settings = settings or {}
    config = config or PlannerConfig.from_settings(settings)
    cost_cfg = settings.get("trading_cost", {})
    pool_can_buy = set(pool_can_buy) if pool_can_buy is not None else None
    pool_can_sell = set(pool_can_sell) if pool_can_sell is not None else None
    stale = set(stale_symbols or ())
    cooldown = dict(cooldown_symbols or {})
    risk_exits = dict(risk_exit_symbols or {})
    ref_time = ref_time or datetime.combine(as_of, datetime.min.time())

    result = PlanResult()
    empty_state = account_state is None or (
        not account_state.lots
        and account_state.last_event_time is None
        and account_state.cash <= 0
    )
    if empty_state:
        result.notes.append(
            "无账户事件/空账户: 无可执行建议 (目标保持研究输出, spec §3.2/C03)"
        )
        return result

    valuation = value_account(account_state, ohlcv, as_of)
    result.valuation = valuation
    result.notes.extend(valuation.notes)

    if not valuation.valuation_ok or valuation.nav <= 0:
        result.paused = True
        result.notes.append(
            "账户关键约束未满足: 暂停生成可执行数量 (保留目标与问题说明, spec §3.3)"
        )
        return result

    if pool_can_sell is None and pool_can_buy is None:
        result.notes.append(
            "未提供四层池许可: 仅按账户与目标规划 (研究用途, 不全量校验当日可交易性)"
        )

    nav = valuation.nav
    valid_until = ref_time + timedelta(days=max(0, config.valid_days))
    skipped: list[SkippedAction] = []

    all_symbols = sorted(
        set(valuation.positions)
        | {s for s in target_weights if s != "__CASH__"}
        | set(risk_exits)
    )

    raw: list[PlannedProposal] = []
    for symbol in all_symbols:
        target_weight = float(target_weights.get(symbol, 0.0))
        actual_weight = float(valuation.weights.get(symbol, 0.0))
        lots = account_state.lots.get(symbol, [])
        held_shares = float(sum(lot.shares for lot in lots))
        sellable_shares = float(
            sum(lot.shares for lot in lots if lot.sellable_date <= as_of)
        )
        pos = valuation.positions.get(symbol)
        price = pos.price if pos is not None else _latest_close(ohlcv, symbol, as_of)
        price_date = pos.price_date if pos is not None else None

        # 默认参数捕获当前迭代值 (B023), 避免闭包晚绑定
        def skip(  # noqa: B023
            side: str,
            reason: str,
            symbol: str = symbol,
            target_weight: float = target_weight,
            actual_weight: float = actual_weight,
        ) -> None:
            skipped.append(SkippedAction(
                symbol, side, reason, target_weight, actual_weight,
            ))

        # ---- 1. 交易与数据可用性约束 (最先校验; 下游不得放宽) ----
        if held_shares > 0 and price is None:
            skip("SELL", "无有效价格: 不虚构成交 (spec §6.2)")
            continue
        if held_shares > 0 and symbol in stale:
            skip("SELL", "停牌/无当日价格: 保留持仓待可交易, 禁买≠清仓 (spec §4.4)")
            result.notes.append(
                f"{symbol}: 停牌/无当日价格 — 风险退出无法执行时持续展示, 不从账本删除持仓"
            )
            continue

        # ---- 2. 明确风险退出 (不受 band / 成本过滤压制; spec §5.5) ----
        if symbol in risk_exits:
            _plan_risk_exit(
                raw, skipped, symbol, risk_exits[symbol], held_shares,
                sellable_shares, price, price_date, target_weight, actual_weight,
                as_of, ref_time, valid_until, nav, ohlcv, cost_cfg, config, result,
            )
            continue

        if price is None:
            if target_weight > 0:
                skip("BUY", "无有效参考价: 不生成买入 (spec §6.2)")
            continue

        # ---- 3. 明确清零 (target=0 且有持仓): 优先于 band, 但受可卖约束 ----
        delta_shares = (
            int(target_weight * nav / price) if target_weight > 0 else 0
        ) - int(held_shares)
        is_full_exit = target_weight <= 0 and held_shares > 0

        if is_full_exit:
            if not config.include_sells:
                skip("SELL", "卖出建议已关闭 (只读复核)")
                continue
            if pool_can_sell is not None and symbol not in pool_can_sell:
                skip("SELL", "不在当日可卖池: 禁卖 ≠ 已清仓 (spec §4.2)")
                continue
            shares, note = _sellable_shares(
                held_shares, sellable_shares, config.lot_size,
            )
            if note:
                skipped.append(SkippedAction(
                    symbol, "SELL",
                    f"{note}; 目标清零但未解锁部分待可交易 (C05)",
                    target_weight, actual_weight,
                ))
            if shares > 0:
                raw.append(_make_proposal(
                    symbol, "SELL", shares, price, price_date, as_of, ref_time,
                    target_weight, actual_weight, valid_until, PRIORITY_RISK_EXIT,
                    "明确退出(目标清零): 不受带宽/成本过滤压制 (spec §5.4)",
                    ohlcv, cost_cfg, config,
                ))
            continue

        # ---- 4/5. 常规调整: band + 冷却 + 池许可 + 成本过滤 ----
        dev = actual_weight - target_weight
        if delta_shares < 0:  # 减持
            if not config.include_sells:
                skip("SELL", "卖出建议已关闭 (只读复核)")
                continue
            if pool_can_sell is not None and symbol not in pool_can_sell:
                skip("SELL", "不在当日可卖池: 禁卖 ≠ 已清仓 (spec §4.2)")
                continue
            if abs(dev) < config.band_points - 1e-12:
                skip(
                    "SELL",
                    f"偏离 {abs(dev):.1%} < 带宽 {config.band_points:.0%}: "
                    "未越界不追精确对齐",
                )
                continue
            delta_value = dev * nav
            if delta_value < config.min_trade_amount - 1e-9:
                skip("SELL", f"差额 {delta_value:,.0f} 元 < 最小交易金额, 成本过滤 (仅常规调整)")
                continue
            shares, note = _sellable_shares(
                held_shares, sellable_shares, config.lot_size, want=abs(delta_shares),
            )
            if note:
                skipped.append(SkippedAction(
                    symbol, "SELL", f"{note} (T+1 可卖约束)", target_weight, actual_weight,
                ))
            if shares <= 0:
                skip("SELL", "无可卖份额: 保留展示, 待可交易 (C05)")
                continue
            raw.append(_make_proposal(
                symbol, "SELL", shares, price, price_date, as_of, ref_time,
                target_weight, actual_weight, valid_until, PRIORITY_SELL_REBALANCE,
                f"偏离 {dev:+.1%} ≥ 带宽 {config.band_points:.0%} ({_pp(dev)}): 调回目标",
                ohlcv, cost_cfg, config,
            ))
        elif delta_shares > 0:  # 增持
            if pool_can_buy is not None and symbol not in pool_can_buy:
                skip("BUY", "不在当日可买池: 禁新买 ≠ 清仓 (缺 NAV/停牌等, spec §4.4/C10)")
                continue
            if symbol in cooldown:
                skip("BUY", f"冷却中不出买单: {cooldown[symbol]} (spec §5.5)")
                continue
            if actual_weight > 0 and abs(dev) < config.band_points - 1e-12:
                skip(
                    "BUY",
                    f"偏离 {abs(dev):.1%} < 带宽 {config.band_points:.0%}: "
                    "未越界不追精确对齐",
                )
                continue
            delta_value = -dev * nav
            if actual_weight > 0 and delta_value < config.min_trade_amount - 1e-9:
                skip("BUY", f"差额 {delta_value:,.0f} 元 < 最小交易金额, 成本过滤 (仅常规调整)")
                continue
            # 满额目标份额: 现金不足由 _apply_cash_constraints 统一缩减
            shares = (
                int(round(target_weight * nav / price)) // config.lot_size * config.lot_size
            )
            if shares <= 0:
                skip("BUY", f"不足一手 ({config.lot_size} 份 × {price:.3f}): 不生成建议")
                continue
            reason = (
                f"首次建仓 {target_weight:.1%} (band 不阻挡首次建仓, spec §5.4)"
                if actual_weight <= 0
                else (
                    f"偏离 {dev:+.1%} ≥ 带宽 {config.band_points:.0%} "
                    f"({actual_weight:.1%} → {target_weight:.1%}): 调回目标"
                )
            )
            proposal = _make_proposal(
                symbol, "BUY", shares, price, price_date, as_of, ref_time,
                target_weight, actual_weight, valid_until, PRIORITY_BUY_REBALANCE,
                reason, ohlcv, cost_cfg, config,
            )
            proposal.target_shares = shares
            raw.append(proposal)
        # delta_shares == 0 且在带内: 无需动作 (不生成噪音建议)

    proposals = _apply_cash_constraints(
        raw, skipped, valuation.cash_available, cost_cfg, config, result,
    )
    result.proposals = proposals
    result.skipped = skipped
    return result


# ------------------------------------------------------------------
# 内部: 单标的规划
# ------------------------------------------------------------------

def _sellable_shares(
    held_shares: float,
    sellable_shares: float,
    lot_size: int,
    want: float | None = None,
) -> tuple[int, str | None]:
    """卖出量 = min(需要, 可卖); 整手向下取整; 全量卖出保留零碎份额.

    Returns:
        (shares, note): note 非空时说明可卖/整手约束对数量的限制.
    """
    note: str | None = None
    target = held_shares if want is None else min(want, held_shares)
    available = min(target, sellable_shares)
    shares = int(math.floor(available + 1e-9))
    if shares >= lot_size:
        shares = shares // lot_size * lot_size
    locked = held_shares - sellable_shares
    if locked > 1e-9:
        note = f"可卖 {sellable_shares:,.0f}/{held_shares:,.0f} 份, 锁定 {locked:,.0f} 份"
    elif shares < int(math.floor(target + 1e-9)):
        note = f"整手取整: {target:,.0f} → {shares:,} 份"
    return shares, note


def _plan_risk_exit(
    raw: list[PlannedProposal],
    skipped: list[SkippedAction],
    symbol: str,
    why: str,
    held_shares: float,
    sellable_shares: float,
    price: float | None,
    price_date: date | None,
    target_weight: float,
    actual_weight: float,
    as_of: date,
    ref_time: datetime,
    valid_until: datetime,
    nav: float,
    ohlcv: pl.DataFrame,
    cost_cfg: dict,
    config: PlannerConfig,
    result: PlanResult,
) -> None:
    """风险退出建议 (最高优先级; 无法卖出时保留展示, 不清仓)."""
    if held_shares <= 0:
        skipped.append(SkippedAction(
            symbol, "SELL", f"风险退出但无持仓: {why}", target_weight, actual_weight,
        ))
        return
    if price is None:
        skipped.append(SkippedAction(
            symbol, "SELL", f"风险退出但无有效价格: 保留提示, 不虚构成交 ({why})",
            target_weight, actual_weight,
        ))
        return
    shares, note = _sellable_shares(held_shares, sellable_shares, config.lot_size)
    if shares <= 0:
        result.notes.append(
            f"{symbol}: 风险退出 ({why}) 但无可卖份额 — 持续展示剩余风险, "
            "不从账本删除持仓 (spec §5.5)"
        )
        skipped.append(SkippedAction(
            symbol, "SELL", f"风险退出但无可卖份额 (锁定 {held_shares:,.0f} 份): 待可交易",
            target_weight, actual_weight,
        ))
        return
    if note:
        skipped.append(SkippedAction(
            symbol, "SELL", f"{note}; 剩余部分待可交易", target_weight, actual_weight,
        ))
    raw.append(_make_proposal(
        symbol, "SELL", shares, price, price_date, as_of, ref_time,
        target_weight, actual_weight, valid_until, PRIORITY_RISK_EXIT,
        f"风险退出: {why}; 不受带宽/成本过滤压制 (spec §5.5)",
        ohlcv, cost_cfg, config,
    ))


def _latest_close(ohlcv: pl.DataFrame, symbol: str, as_of: date) -> float | None:
    if ohlcv.is_empty():
        return None
    df = ohlcv.filter(
        (pl.col("symbol") == symbol) & (pl.col("date") <= as_of)
        & pl.col("close").is_not_null() & (pl.col("close") > 0)
    )
    if df.is_empty():
        return None
    return float(df.sort("date")["close"].last())


def _pp(value: float) -> str:
    return f"{value * 100:+.1f}pp"


def _make_proposal(
    symbol: str,
    side: str,
    shares: int,
    price: float,
    price_date: date | None,
    as_of: date,
    ref_time: datetime,
    target_weight: float,
    actual_weight: float,
    valid_until: datetime,
    priority: int,
    reason: str,
    ohlcv: pl.DataFrame,
    cost_cfg: dict,
    config: PlannerConfig,
) -> PlannedProposal:
    """构造建议并估算金额/费用 (参考价=最近可用收盘价, 标注时效)."""
    est_amount = shares * price
    slip = _slippage_rate(symbol, as_of, est_amount, ohlcv, cost_cfg)
    fee = _commission_of(est_amount, cost_cfg)
    if price_date is not None and price_date < as_of:
        reason += f" (参考价 {price_date} 收盘)"
    return PlannedProposal(
        symbol=symbol, side=side, shares=shares,
        target_weight=target_weight, actual_weight=actual_weight,
        ref_price=price, ref_time=ref_time, est_amount=est_amount, est_fee=fee,
        est_slippage_rate=slip, valid_until=valid_until, priority=priority,
        reason=reason,
    )


def _execution_order(p: PlannedProposal) -> tuple:
    """执行顺序: 风险退出 → 常规卖出 → 依赖买入 → 常规买入; 同层按金额降序."""
    return (0 if p.side == "SELL" else 1, -p.priority, -p.est_amount)


def _apply_cash_constraints(
    raw: list[PlannedProposal],
    skipped: list[SkippedAction],
    cash_available: float,
    cost_cfg: dict,
    config: PlannerConfig,
    result: PlanResult,
) -> list[PlannedProposal]:
    """现金与卖出依赖约束 (卖出先执行、卖出款当日可用; 不产生负现金).

    - 分配预算 = 可用现金 (+ 计卖出净收入); 按最终执行顺序分配, 不足则
      缩减至可负担量 (spec §5.4: 差额留待下一轮, 不重复累加).
    - 累计买入成本超过可用现金的部分由卖出资金支撑 → depends_on
      条件建议 (卖出失败/价格变化需重算, spec §6.1; 执行顺序使现金先用).
    - 完全不可负担 (缩减后不足一手) → 跳过并保留原因.
    """
    buys = [p for p in raw if p.side == "BUY"]
    sells = [p for p in raw if p.side == "SELL"]
    out = list(sells)
    if not buys:
        out.sort(key=_execution_order)
        return out

    sell_proceeds = sum(p.est_amount - p.est_fee for p in sells)
    pool = cash_available + sell_proceeds
    sell_note = ", ".join(f"{p.symbol} {p.shares:,} 份" for p in sells)

    remaining = pool
    total_demand = 0.0
    allocated_total = 0.0
    allocation_shortfall = False
    cumulative = 0.0
    # 按最终执行顺序分配 (现金先用; 同层按满额金额降序, 分配中金额只减不增)
    for p in sorted(buys, key=_execution_order):
        full = p.target_shares if p.target_shares is not None else p.shares
        gross = full * p.ref_price * (1.0 + p.est_slippage_rate)
        cost = gross + _commission_of(gross, cost_cfg)
        total_demand += cost
        if cost <= remaining + 1e-6:
            allocated = cost
            p.shares = full
        else:
            shares = min(
                full,
                _max_affordable_shares(
                    remaining, p.ref_price, config.lot_size,
                    cost_cfg, p.est_slippage_rate,
                ),
            )
            if shares <= 0:
                allocation_shortfall = True
                skipped.append(SkippedAction(
                    p.symbol, "BUY",
                    "现金不足, 缩减后不足一手: 留待下一轮 (spec §5.4)"
                    + (f"; 卖出可释放 {sell_note}" if sells else ""),
                    p.target_weight, p.actual_weight,
                ))
                continue
            gross = shares * p.ref_price * (1.0 + p.est_slippage_rate)
            allocated = gross + _commission_of(gross, cost_cfg)
            p.shares = shares
            allocation_shortfall = True
            p.reason += (
                f"; 现金约束: 由 {full:,} 份缩减至 {shares:,} 份 "
                f"({shares * p.ref_price:,.0f} 元), 差额留待下一轮"
            )
        remaining -= allocated
        allocated_total += allocated
        p.est_amount = p.shares * p.ref_price
        p.est_fee = _commission_of(p.est_amount, cost_cfg)

        # 条件标注: 累计成本超过可用现金的买入由卖出资金支撑 (spec §6.1)
        cumulative += allocated
        if sells and cumulative > cash_available + 1e-6 and p.depends_on is None:
            p.depends_on = f"SELL:{','.join(x.symbol for x in sells)}"
            p.conditional = True
            if p.priority > PRIORITY_BUY_DEPENDENT:
                p.priority = PRIORITY_BUY_DEPENDENT
            p.reason += f"; 资金依赖卖出 ({sell_note}) 执行 (条件建议, spec §6.1)"
        out.append(p)

    if allocation_shortfall and not sells:
        result.notes.append(
            f"现金 {cash_available:,.0f} 元 < 买入需求 {total_demand:,.0f} 元: "
            f"按执行顺序分配 {allocated_total:,.0f} 元, 差额留待下一轮 (spec §5.4)"
        )
    out.sort(key=_execution_order)
    return out


# ------------------------------------------------------------------
# 建议账本适配 (供 orchestration 层落库; 纯数据转换)
# ------------------------------------------------------------------

def plan_to_ledger_proposals(
    plan: PlanResult,
    *,
    target_id: str | None,
    account_id: str,
    account_as_of: datetime | None,
) -> list:
    """PlanResult → ProposalLedger.Proposal 列表 (落库前的纯转换)."""
    from orchestration.proposal_ledger import Proposal, make_proposal_id

    proposals = []
    for p in plan.proposals:
        pid = make_proposal_id(target_id, p.symbol, p.side)
        p.proposal_id = pid
        proposals.append(Proposal(
            proposal_id=pid,
            target_id=target_id,
            account_id=account_id,
            account_as_of=account_as_of,
            symbol=p.symbol,
            side=p.side,
            shares=float(p.shares),
            ref_price=p.ref_price,
            ref_time=p.ref_time,
            est_amount=p.est_amount,
            est_fee=p.est_fee,
            target_weight=p.target_weight,
            actual_weight=p.actual_weight,
            valid_until=p.valid_until,
            priority=p.priority,
            depends_on=p.depends_on,
            status="proposed",
            reason=p.reason,
        ))
    return proposals
