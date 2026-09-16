"""B0/B1 基准模拟 (M5) — 固定配置买入持有 / 固定目标 + 阈值再平衡.

契约 (spec §5.1/§5.4/§5.5/§6.1/§6.3, plan §M5):
  - B0: 按初始配置买入后不再做日常权重调整; 新增资金政策显式 (B0 留现金, 不买).
  - B1: 固定目标权重 + 绝对百分点 band; 检查日按交易日历 (首版=每周最后交易日);
        用**模拟实际份额**计算漂移 (actual vs target), 不比较新旧目标.
  - 三个账户 (B0/B1/A) 独立现金/份额/净值; 模拟资金不加到真实权益.
  - 同口径: 相同初始映射/资本/产品范围/价格/成本框架; 成本差异只能来自策略行为.
  - 执行基础与实盘建议一致: T 日收盘决策 → T+1 开盘成交; 卖出先于买入 (卖出款
    当日可用); 批次级可卖 (T+1 解锁, 复用 M2 事件回放); 整手 100 份; 佣金与
    滑点与规划器/回测同一公式; 现金不足缩减, 不产生负现金.
  - 无外部现金流为默认研究口径; 出入金测试按预先声明的同额同日注入.
  - 结果可追溯: 费用逐笔记录; NAV 序列 (现金/持仓市值/费用累计) 供 M6 核对;
    实验记录写 experiments 表 (固定研究配置, 不搜索最优 SAA).

不复权价格口径: 现行数据为 Sina 不复权, 不含分红/权益事件 (CALIBER_NOTE, C09);
B0/B1 结果按同口径比较, 完整权益入账在 P1。

Usage:
    uv run baseline --strategy B0 --start 2020-01-01 --end 2026-09-11
    uv run baseline --strategy B1                     # 默认区间 (settings.baselines)
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from data_pipeline.accounting import AccountEvent, AccountService, AccountState
from data_pipeline.duckdb_manager import DuckDBManager

logger = logging.getLogger(__name__)

LOT_SIZE = 100  # ETF 最小交易单位 (与回测引擎/规划器一致)

CALIBER_NOTE = (
    "价格口径: 不复权 (Sina); 不含分红/权益事件 — 完整账本验收待 P1 权益事件入账"
)


# ------------------------------------------------------------------
# 配置
# ------------------------------------------------------------------

@dataclass
class BaselineConfig:
    """固定研究配置 (示例必须显式标注; 不搜索最优 SAA, spec §5.1/§7.3)."""

    strategy: str                       # B0 / B1
    weights: dict[str, float]           # 固定目标权重 (含 __CASH__; Σ=1)
    initial_cash: float = 100000.0
    band_points: float = 0.05           # B1 绝对百分点带宽 (示例)
    check_weekday: int = 4              # 保留 (未来非周五检查日版本); 首版固定
                                        #   检查日 = 每周最后交易日 (spec §5.4)
    lot_size: int = LOT_SIZE
    label: str = "research_example"     # 研究示例标注 (非真实 SAA)

    def symbols(self) -> list[str]:
        """非现金标的 (固定映射, 排序稳定)."""
        return [s for s in sorted(self.weights) if s != "__CASH__"]

    @classmethod
    def from_settings(cls, settings: dict, strategy: str) -> BaselineConfig:
        base = settings.get("baselines", {})
        weights = dict(base.get("weights", {}))
        if not weights:
            raise ValueError("settings.baselines.weights 为空: 无固定配置可运行")
        total = sum(float(w) for w in weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"baselines.weights 合计必须为 1 (含现金), 当前 {total:.6f}")
        return cls(
            strategy=strategy.upper(),
            weights={k: float(v) for k, v in weights.items()},
            initial_cash=float(
                base.get("initial_cash", settings.get("backtest", {}).get("initial_cash", 100000.0))
            ),
            band_points=float(base.get("band_points", 0.05)),
            check_weekday=int(base.get("check_weekday", 4)),
            lot_size=int(base.get("lot_size", LOT_SIZE)),
            label=str(base.get("label", "research_example")),
        )

    def config_fingerprint(self) -> str:
        payload = json.dumps(
            {
                "strategy": self.strategy, "weights": self.weights,
                "initial_cash": self.initial_cash, "band_points": self.band_points,
                "check_weekday": self.check_weekday, "lot_size": self.lot_size,
            },
            sort_keys=True, ensure_ascii=False,
        )
        return "cfg-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def cash_weight(config: BaselineConfig) -> float:
    return float(config.weights.get("__CASH__", 0.0))


# ------------------------------------------------------------------
# 结果
# ------------------------------------------------------------------

@dataclass
class BaselineResult:
    """基准模拟结果 (逐日净值 + 费用 + 事件日志)."""

    strategy: str
    account_id: str
    config_fingerprint: str
    nav: pl.DataFrame                       # date/nav/cash/positions_value/fees_cum/positions_json
    events: list[AccountEvent]
    trades: int = 0
    fees_total: float = 0.0
    cash_flow_policy: str = "无外部现金流 (标准研究)"
    experiment_id: str = ""
    notes: list[str] = field(default_factory=list)

    def period_return(self) -> float:
        """期间收益 (现金流口径: (末/初 − 1); 无现金流时即简单收益)."""
        if self.nav.is_empty():
            return 0.0
        first = float(self.nav["nav"][0])
        last = float(self.nav["nav"][-1])
        return last / first - 1.0

    def max_drawdown(self) -> float:
        """最大回撤 (可比净值相对历史高点; 观察值非未来上限)."""
        if self.nav.is_empty():
            return 0.0
        peak = float("-inf")
        worst = 0.0
        for value in self.nav["nav"].to_list():
            peak = max(peak, float(value))
            if peak > 0:
                worst = min(worst, float(value) / peak - 1.0)
        return worst

    def summary(self) -> str:
        lines = [
            f"===== 基准模拟 {self.strategy} ({self.account_id}) =====",
            f"[配置] {self.config_fingerprint} | 成本情景见 settings.trading_cost "
            f"| {self.cash_flow_policy}",
            f"[区间] {self.nav['date'][0] if not self.nav.is_empty() else '-'} → "
            f"{self.nav['date'][-1] if not self.nav.is_empty() else '-'}",
            f"[结果] 期间收益 {self.period_return():.2%} | 最大回撤 {self.max_drawdown():.2%} | "
            f"成交 {self.trades} 笔 | 费用合计 {self.fees_total:,.2f}",
            f"[口径] {CALIBER_NOTE}",
        ]
        lines.extend(f"[注] {note}" for note in self.notes)
        return "\n".join(lines)


# ------------------------------------------------------------------
# 成本 (与规划器/回测同公式)
# ------------------------------------------------------------------

def _commission_of(amount: float, cost_cfg: dict) -> float:
    rate = float(cost_cfg.get("commission_rate", 0.0))
    minimum = float(cost_cfg.get("min_commission_per_trade", 0.0))
    return max(amount * rate, minimum)


def _slippage_rate(
    symbol: str,
    exec_date: date,
    amount_est: float,
    ohlcv: pl.DataFrame,
    cost_cfg: dict,
    lookback: int = 20,
) -> float:
    base = float(cost_cfg.get("slippage_base", 0.0))
    impact = float(cost_cfg.get("impact_coefficient", 0.0))
    if impact <= 0 or ohlcv.is_empty() or "amount" not in ohlcv.columns:
        return base
    hist = ohlcv.filter(
        (pl.col("symbol") == symbol) & (pl.col("date") < exec_date)
        & pl.col("amount").is_not_null() & (pl.col("amount") > 0)
    )
    if hist.is_empty():
        return base
    amounts = [float(v) for v in hist.sort("date").tail(lookback)["amount"].to_list()]
    if not amounts:
        return base
    avg = statistics.fmean(amounts)
    if avg <= 0:
        return base
    return max(base, amount_est / avg * impact)


# ------------------------------------------------------------------
# 模拟器
# ------------------------------------------------------------------

class BaselineSimulator:
    """B0/B1 模拟器 (写入账户账本路径; 每笔成交带费用留痕).

    - B0: init → 一次性买入并持有; 后续入金按政策留现金.
    - B1: 每周最后交易日盘后检查 actual vs target 偏离; 越界 → 次日开盘调回.
    """

    def __init__(
        self,
        db: DuckDBManager,
        settings: dict,
        config: BaselineConfig,
        account_id: str | None = None,
        mode: str = "backtest",
        live_write: bool = False,
    ) -> None:
        self.db = db
        self.settings = settings
        self.config = config
        self.mode = mode
        self.live_write = live_write
        self.account_id = account_id or f"BASE-{config.strategy}"
        self.cost_cfg = settings.get("trading_cost", {})
        self.cash_policy = str(
            settings.get("baselines", {}).get(
                "b0_cash_policy", "hold_cash",  # B0 新增资金: 留现金 (首版默认)
            )
        ) if config.strategy == "B0" else "n/a"

        self.events: list[AccountEvent] = []
        self.state = AccountState(account_id=self.account_id)
        self.trade_count = 0
        self.fees_total = 0.0
        self.notes: list[str] = []

        # 行情准备 (open/close 逐日, 全部截至 as_of 可用)
        self.ohlcv = db.get_ohlcv()
        if self.ohlcv.is_empty():
            raise RuntimeError("ohlcv_daily 为空, 无法运行基准模拟")
        self.closes: dict[str, dict[date, float]] = {}
        self.opens: dict[str, dict[date, float]] = {}
        for sym in self.ohlcv["symbol"].unique().to_list():
            sub = self.ohlcv.filter(pl.col("symbol") == sym).sort("date")
            self.closes[sym] = {
                d: float(c) for d, c in zip(
                    sub["date"].to_list(), sub["close"].to_list(), strict=True,
                ) if d is not None and c is not None
            }
            self.opens[sym] = {
                d: float(o) for d, o in zip(
                    sub["date"].to_list(), sub["open"].to_list(), strict=True,
                ) if d is not None and o is not None
            }
        # 交易日历: 全库日期并集 (任一标的缺失不改变日历)
        all_days: set[date] = set()
        for series in self.closes.values():
            all_days.update(series.keys())
        self.trading_days: list[date] = sorted(all_days)

    # ---- 主循环 ----

    def run(
        self,
        start: date,
        end: date,
        deposits: list[tuple[date, float]] | None = None,
    ) -> BaselineResult:
        """执行模拟.

        Args:
            start/end: 区间 (含)
            deposits: 外部现金流 [(日期, 金额)]; 预先声明, 各账户同额同日
        """
        deposits = deposits or []
        deposit_map: dict[date, float] = {}
        for d, amount in deposits:
            deposit_map[d] = deposit_map.get(d, 0.0) + float(amount)

        days = [d for d in self.trading_days if start <= d <= end]
        if not days:
            raise ValueError(f"区间 {start}~{end} 无交易日数据")
        symbols = self.config.symbols()

        pending: list[dict] | None = None
        initialized = False
        nav_rows: list[dict] = []

        for day in days:
            # ---- 1. 开盘: 执行昨日决定 (T+1 开盘成交, 卖出先于买入) ----
            if pending is not None:
                self._execute_orders(pending, day)
                pending = None

            # ---- 2. 外部现金流 (预先声明; 同额同日) ----
            if day in deposit_map and deposit_map[day] > 0:
                amount = deposit_map[day]
                if self.config.strategy == "B0" and self.cash_policy == "hold_cash":
                    self._emit("deposit", day, amount=amount,
                               note=f"B0 新增资金政策={self.cash_policy} (留现金)")
                else:
                    self._emit("deposit", day, amount=amount,
                               note="外部现金流 (同额同日, 研究测试)")

            # ---- 3. 初始化: 首日盘后按初始配置下单 (次日开盘成交) ----
            if not initialized:
                if not self._try_init(day, symbols):
                    continue
                initialized = True
                pending = self._build_full_orders(
                    day, symbols, target_weight=self.config.weights,
                )
                nav_rows.append(self._nav_row(day))
                continue

            # ---- 4. 盘后: B1 检查日 (每周最后交易日) ----
            if self.config.strategy == "B1" and self._is_check_day(day, days):
                orders = self._build_rebalance_orders(day, symbols)
                if orders:
                    pending = orders

            nav_rows.append(self._nav_row(day))

        # 结束: 若最后决策未执行, 记录未成交说明 (不虚构)
        if pending:
            self.notes.append("区间末存在未执行决策 (次日开盘超出区间): 不虚构成交")

        if self.live_write:
            self._persist()

        result = BaselineResult(
            strategy=self.config.strategy,
            account_id=self.account_id,
            config_fingerprint=self.config.config_fingerprint(),
            nav=pl.DataFrame(nav_rows, schema={
                "date": pl.Date, "nav": pl.Float64, "cash": pl.Float64,
                "positions_value": pl.Float64, "fees_cum": pl.Float64,
                "positions_json": pl.Utf8,
            }) if nav_rows else pl.DataFrame(),
            events=self.events,
            trades=self.trade_count,
            fees_total=round(self.fees_total, 6),
            cash_flow_policy=(
                f"外部现金流: {len(deposits)} 笔 (同额同日)"
                if deposits else "无外部现金流 (标准研究)"
            ),
            notes=self.notes,
        )
        return result

    # ---- 初始化 ----

    def _try_init(self, day: date, symbols: list[str]) -> bool:
        """首个可交易日: 记录初始现金事件 (无持仓)."""
        if self.state.last_event_time is not None:
            return True
        if not any(
            self.config.weights.get(s, 0) > 0 and self.opens.get(s, {}).get(day)
            for s in symbols
        ):
            return False
        self._emit(
            "initial", day, amount=self.config.initial_cash,
            note=(
                f"{self.config.strategy} 初始资本 {self.config.initial_cash:,.0f} 元 "
                f"(研究示例配置 {self.config.label}, {self.config.config_fingerprint()})"
            ),
        )
        logger.info("%s: 初始资本 %.0f 元 (%s)", self.account_id, self.config.initial_cash, day)
        return True

    # ---- 下单构建 ----

    def _build_full_orders(
        self, day: date, symbols: list[str], target_weight: dict[str, float],
    ) -> list[dict]:
        """按目标权重构建买入单 (整手/现金约束; 卖出单用于调仓)."""
        nav = self._nav(day)
        orders: list[dict] = []
        # 减仓单 (B1): 卖出先在开盘执行并释放资金
        for sym in symbols:
            if target_weight.get(sym, 0.0) > 0:
                continue
            held = self.state.shares_of(sym)
            if held <= 0:
                continue
            sellable = sum(
                lot.shares for lot in self.state.lots.get(sym, [])
                if lot.sellable_date <= day
            )
            shares = int(min(held, sellable)) // self.config.lot_size * self.config.lot_size
            if shares > 0:
                orders.append({"symbol": sym, "side": "SELL", "shares": shares})
        # 买入单: 目标市值取整手
        for sym in symbols:
            weight = target_weight.get(sym, 0.0)
            if weight <= 0:
                continue
            price = self.opens.get(sym, {}).get(day)
            if not price or price <= 0:
                if self.state.shares_of(sym) > 0:
                    self.notes.append(f"{sym}: {day} 无开盘价, 维持持仓 (停牌不虚构成交)")
                continue
            target_value = nav * weight
            current_value = self.state.shares_of(sym) * price
            delta_value = target_value - current_value
            if delta_value <= 0:
                continue
            shares = int(delta_value / price) // self.config.lot_size * self.config.lot_size
            if shares > 0:
                orders.append({"symbol": sym, "side": "BUY", "shares": shares})
        return orders

    def _build_rebalance_orders(self, day: date, symbols: list[str]) -> list[dict]:
        """B1: 检查实际 vs 目标偏离; 只对越界资产调回目标 (spec §5.4)."""
        nav = self._nav(day)
        if nav <= 0:
            return []
        orders: list[dict] = []
        for sym in symbols:
            target = self.config.weights.get(sym, 0.0)
            if target <= 0:
                continue
            price = self.closes.get(sym, {}).get(day)
            if not price or price <= 0:
                continue
            actual = self.state.shares_of(sym) * price / nav
            if abs(actual - target) < self.config.band_points - 1e-12:
                continue  # 未越界不追精确对齐
            target_shares = int(target * nav / price)
            delta = target_shares - int(self.state.shares_of(sym))
            # 整手向最近调回 (不追求精确对齐; 费用/整手允许残余偏离, spec §5.4)
            lot_size = self.config.lot_size
            delta_lots = int(round(delta / lot_size))
            if delta_lots == 0 and abs(delta) >= lot_size:
                delta_lots = 1 if delta > 0 else -1
            delta = delta_lots * lot_size
            if delta < 0:
                sellable = sum(
                    item.shares for item in self.state.lots.get(sym, [])
                    if item.sellable_date <= day
                )
                shares = min(abs(delta), int(sellable)) // lot_size * lot_size
                if shares > 0:
                    orders.append({"symbol": sym, "side": "SELL", "shares": shares})
            elif delta > 0:
                shares = delta // lot_size * lot_size
                if shares > 0:
                    orders.append({"symbol": sym, "side": "BUY", "shares": shares})
        return orders

    def _is_check_day(self, day: date, days: list[date]) -> bool:
        """每周最后交易日 (检查日; 节假日随交易日历, 首版约定 spec §5.4)."""
        idx = days.index(day)
        if idx == len(days) - 1:
            return True
        return days[idx + 1].isocalendar()[1] != day.isocalendar()[1]

    # ---- 成本 (与规划器/回测同公式) ----

    def _commission_of(self, amount: float) -> float:
        return _commission_of(amount, self.cost_cfg)

    # ---- 成交执行 ----

    def _execute_orders(self, orders: list[dict], day: date) -> None:
        """T+1 开盘执行: 卖出先于买入; 现金不足缩减买入; 逐笔费用留痕."""
        sells = [o for o in orders if o["side"] == "SELL"]
        buys = [o for o in orders if o["side"] == "BUY"]

        for order in sells + buys:
            sym = order["symbol"]
            price0 = self.opens.get(sym, {}).get(day)
            if price0 is None:
                self.notes.append(f"{sym}: {day} 无开盘价, 委托未成交 (不虚构)")
                continue
            shares = int(order["shares"])
            if order["side"] == "SELL":
                sellable = sum(
                    lot.shares for lot in self.state.lots.get(sym, [])
                    if lot.sellable_date <= day
                )
                shares = min(shares, int(sellable))
                if shares <= 0:
                    continue
                amount_est = shares * price0
                slip = _slippage_rate(sym, day, amount_est, self.ohlcv, self.cost_cfg)
                price = price0 * (1.0 - slip)
            else:
                budget_check = shares * price0 * (
                    1.0 + _slippage_rate(sym, day, shares * price0, self.ohlcv, self.cost_cfg)
                )
                if budget_check + self._commission_of(budget_check) > self.state.cash + 1e-9:
                    shares = self._affordable_shares(sym, price0, day, self.state.cash)
                if shares <= 0:
                    continue
                amount_est = shares * price0
                slip = _slippage_rate(sym, day, amount_est, self.ohlcv, self.cost_cfg)
                price = price0 * (1.0 + slip)

            amount = shares * price
            fee = self._commission_of(amount)
            if order["side"] == "BUY" and amount + fee > self.state.cash + 1e-9:
                # 滑点后超预算: 再缩减一档
                shares = self._affordable_shares(sym, price, day, self.state.cash)
                if shares <= 0:
                    continue
                amount = shares * price
                fee = self._commission_of(amount)
            self._emit(
                "trade", day, symbol=sym, side=order["side"], shares=float(shares),
                price=round(price, 8), fee=round(fee, 8),
                note=f"{self.config.strategy} 开盘执行 (滑点 {slip:.4%})",
            )
            self.trade_count += 1
            self.fees_total += fee

    def _affordable_shares(
        self, symbol: str, exec_price: float, day: date, cash: float,
    ) -> int:
        """可负担整手份额 (含滑点与佣金, 与规划器同口径)."""
        if exec_price <= 0 or cash <= 0:
            return 0
        lot = self.config.lot_size
        best = 0
        lots = int(cash / (exec_price * lot)) + 1
        for n in range(lots, 0, -1):
            shares = n * lot
            amount_est = shares * exec_price
            slip = _slippage_rate(symbol, day, amount_est, self.ohlcv, self.cost_cfg)
            amount = shares * exec_price * (1.0 + slip)
            fee = self._commission_of(amount)
            if amount + fee <= cash + 1e-9:
                best = shares
                break
        return best

    # ---- 事件与估值 ----

    def _emit(
        self,
        event_type: str,
        day: date,
        amount: float | None = None,
        symbol: str | None = None,
        side: str | None = None,
        shares: float | None = None,
        price: float | None = None,
        fee: float | None = None,
        note: str | None = None,
    ) -> None:
        """生成事件并应用到内存状态 (回放纯函数, 与库内路径同一实现)."""
        from data_pipeline.accounting import apply_event, make_event_id

        event_time = datetime.combine(day, datetime.min.time()) + timedelta(hours=15)
        event_id = make_event_id(
            self.account_id, event_type, event_time,
            symbol=symbol, side=side, shares=shares, price=price,
            amount=amount, reference=f"baseline-{len(self.events)}",
        )
        event = AccountEvent(
            event_id=event_id, account_id=self.account_id, event_type=event_type,
            event_time=event_time, symbol=symbol, side=side, shares=shares,
            price=price, amount=amount, fee=fee, source="simulated",
            note=note, payload=None,
        )
        apply_event(self.state, event)
        self.events.append(event)

    def _nav(self, day: date) -> float:
        """按当日收盘价估值 (缺价 → 最近可用价, 标注在 nav_row)."""
        value = self.state.cash + self.state.receivable
        for sym in self.state.lots:
            shares = self.state.shares_of(sym)
            if shares <= 0:
                continue
            price = self._price_on_or_before(sym, day)
            if price is None:
                continue
            value += shares * price
        return value

    def _price_on_or_before(self, symbol: str, day: date) -> float | None:
        series = self.closes.get(symbol, {})
        if day in series:
            return series[day]
        candidates = [d for d in series if d <= day]
        if not candidates:
            return None
        return series[max(candidates)]

    def _nav_row(self, day: date) -> dict:
        positions: dict[str, float] = {}
        positions_value = 0.0
        for sym in sorted(self.state.lots):
            shares = self.state.shares_of(sym)
            if shares <= 0:
                continue
            price = self._price_on_or_before(sym, day)
            if price is None:
                continue
            value = shares * price
            positions[sym] = shares
            positions_value += value
        return {
            "date": day,
            "nav": round(self.state.cash + self.state.receivable + positions_value, 6),
            "cash": round(self.state.cash, 6),
            "positions_value": round(positions_value, 6),
            "fees_cum": round(self.fees_total, 6),
            "positions_json": json.dumps(positions, ensure_ascii=False),
        }

    # ---- 落库 ----

    def _persist(self) -> None:
        """写库: 账户 → 事件 (回放生成批次/快照). 幂等 (event_id 确定性)."""
        service = AccountService(self.db)
        service.ensure_account(
            self.account_id,
            name=f"{self.config.strategy} 基准 (研究示例)",
            mode=self.mode,
            note=f"固定研究配置 {self.config.config_fingerprint()}; {self.config.label}",
        )
        result = service.import_events(self.events)
        logger.info(
            "%s 落库: 新增 %d, 去重 %d, 现金 %.2f",
            self.account_id, result.inserted, result.skipped, result.state.cash,
        )
