"""ETF 四层池 — AllocationUniverse / ProductUniverse / TradableUniverse / Holdings.

契约 (spec §4.2/§4.4, C10/C12):
  - 层1 配置范围 (AllocationUniverse): 资产类别/市场范围, 来自 settings.universe 分类
    + instruments 元数据 (market_type/asset_class/index_name).
  - 层2 产品候选池 (ProductUniverse): 同指数/同类候选与对照 — 见 product_compare.py
    (只做诊断, 不按单一阈值自动删池).
  - 层3 当日可交易池 (TradableUniverse): 分项检查 pass/fail/unknown/not_applicable;
    输出 can_buy / can_sell / can_hold 与原因:
      * 禁新买 ≠ 清仓: 被剔除的持仓仍可 hold, 继续估值 (spec §4.2).
      * 缺 NAV 且该产品溢价检查为必需 (settings.universe_filter.
        premium_check_required_market_types, 默认跨境) → unknown → 禁新买.
      * 停牌/无当日价格 (stale: 最新数据早于池内最新) → 不能买也不能卖,
        持仓保留并按最近价估值 (spec §4.4/§6.2).
  - 层4 实际持仓 (Holdings): 来自账户事件, 独立于筛选结果 (holdings 参数).

历史池 (C12):
  - universe_versions / universe_members 保存成员生效区间与来源.
  - 版本声明日之后为 declared; 之前无法证明历史成员资格 → conditional_research.
  - 任何日期: 上市日前不可买 (当时未上市不可买).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from data_pipeline.duckdb_manager import DuckDBManager
from strategies import universe_filter

CHECK_STATUSES = ("pass", "fail", "unknown", "not_applicable")
DEFAULT_PREMIUM_REQUIRED_TYPES = ["跨境"]


# ------------------------------------------------------------------
# 许可结果
# ------------------------------------------------------------------

@dataclass
class PoolEntry:
    """单标的四层池判定 (分项检查 + 买卖许可)."""

    symbol: str
    checks: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)       # 禁买/禁卖原因
    unknown_msgs: list[str] = field(default_factory=list)  # unknown 明细
    can_buy: bool = False
    can_sell: bool = False
    can_hold: bool = True
    is_held: bool = False
    last_date: date | None = None
    stale: bool = False


@dataclass
class PoolReport:
    """当日可交易池报告."""

    decision_date: date
    entries: list[PoolEntry] = field(default_factory=list)
    pool_version: str | None = None
    membership_mode: str = "declared"   # declared / conditional_research
    notes: list[str] = field(default_factory=list)

    @property
    def by_symbol(self) -> dict[str, PoolEntry]:
        return {entry.symbol: entry for entry in self.entries}

    @property
    def buyable(self) -> list[str]:
        return [entry.symbol for entry in self.entries if entry.can_buy]

    @property
    def sellable(self) -> list[str]:
        return [entry.symbol for entry in self.entries if entry.can_sell]

    @property
    def stale_symbols(self) -> set[str]:
        return {entry.symbol for entry in self.entries if entry.stale}

    @property
    def unknown(self) -> dict[str, list[str]]:
        """存在 unknown 检查项的标的 (不静默通过)."""
        return {
            entry.symbol: list(entry.unknown_msgs)
            for entry in self.entries
            if "unknown" in entry.checks.values()
        }

    def summary(self) -> str:
        lines = [
            f"TradableUniverse: {len(self.buyable)}/{len(self.entries)} 可买 "
            f"| 可卖 {len(self.sellable)} | 停牌/缺数 {len(self.stale_symbols)} "
            f"| 池版本 {self.pool_version or '-'} ({self.membership_mode})",
        ]
        for entry in self.entries:
            if not entry.can_buy and entry.reasons:
                lines.append(f"  禁买 {entry.symbol}: {'; '.join(entry.reasons)}")
        lines.extend(f"  注: {note}" for note in self.notes)
        return "\n".join(lines)


# ------------------------------------------------------------------
# 派生字段
# ------------------------------------------------------------------

def _derive_exchange(symbol: str) -> str | None:
    """按 ETF 代码首位推断交易所 (51/56/58→SH; 15/16→SZ)."""
    if not symbol:
        return None
    if symbol[0] in ("5", "6", "9"):
        return "SH"
    if symbol[0] in ("0", "1", "2", "3"):
        return "SZ"
    return None


def _derive_asset_class(market_type: str) -> str:
    if market_type == "商品":
        return "商品"
    if market_type == "债券":
        return "固收"
    return "权益"


# ------------------------------------------------------------------
# 产品契约与池版本同步 (写入入口)
# ------------------------------------------------------------------

def sync_instruments_and_pool(
    db: DuckDBManager,
    settings: dict,
    symbols: list[str],
    as_of: date | None = None,
) -> str:
    """从 settings + 数据区间派生同步 instruments 与池版本; 返回 version_id.

    - list_date 缺失时用数据起点近似 (source 标注为派生, 真实上市日待核实);
    - 池版本按成员集内容哈希 (uv-…), 幂等: 成员不变不产生新版本;
    - 出池成员标注 effective_to (保留历史, 不删除).
    """
    as_of = as_of or date.today()
    uf_cfg = settings.get("universe_filter", {})
    required_types = set(
        uf_cfg.get("premium_check_required_market_types",
                   DEFAULT_PREMIUM_REQUIRED_TYPES)
    )
    dq_cfg = settings.get("data_quality", {}).get("daily_change", {})
    default_limit = float(dq_cfg.get("default_limit_pct", 10.0))
    overrides = {
        key: float(value)
        for key, value in dq_cfg.get("limit_overrides_pct", {}).items()
    }

    ranges: dict[str, tuple[date | None, date | None]] = {}
    for row in db.get_symbol_date_ranges().iter_rows(named=True):
        ranges[row["symbol"]] = (row.get("first_date"), row.get("last_date"))

    etfs = [
        etf
        for category in settings.get("universe", {}).values()
        if isinstance(category, list)
        for etf in category
    ]
    instrument_rows = []
    for etf in etfs:
        symbol = etf["symbol"]
        first_date, _last = ranges.get(symbol, (None, None))
        market_type = etf.get("market_type", "A股")
        instrument_rows.append({
            "symbol": symbol,
            "name": etf.get("name"),
            "exchange": _derive_exchange(symbol),
            "market_type": market_type,
            "asset_class": _derive_asset_class(market_type),
            "index_name": etf.get("index_name"),
            "underlying_market": None,
            "list_date": first_date,   # 近似: 数据起点
            "delist_date": None,
            "settlement_type": etf.get("settlement"),
            "price_limit_pct": overrides.get(symbol, default_limit),
            "premium_check_required": market_type in required_types,
            "rule_note": "交易规则生效区间待核实 (如创业板/科创 20% 板起始日)",
            "source": "settings+ohlcv_derived",
        })
    if instrument_rows:
        db.upsert_instruments(pl.DataFrame(instrument_rows))

    ordered = sorted({etf["symbol"] for etf in etfs} | set(symbols))
    digest = hashlib.sha1("|".join(ordered).encode()).hexdigest()[:10]
    version_id = f"uv-{digest}"

    if db.get_universe_version(version_id) is None:
        db.upsert_universe_version(pl.DataFrame([{
            "version_id": version_id,
            "strategy_id": "five-funnel",
            "as_of": as_of,
            "source": "settings",
            "note": (
                f"{len(ordered)} 只 ETF; 成员历史无法重建, "
                f"声明日前按条件性研究 (C12)"
            ),
        }]))
        db.insert_universe_members(pl.DataFrame([
            {
                "version_id": version_id,
                "symbol": symbol,
                "effective_from": as_of,
                "include_reason": "settings.universe",
            }
            for symbol in ordered
        ]))
        # 出池成员标注区间终点 (只标区间, 不删历史持仓/事实)
        ordered_set = set(ordered)
        for row in db.get_open_universe_members().iter_rows(named=True):
            if row["symbol"] not in ordered_set:
                db.close_universe_member(row["symbol"], as_of - timedelta(days=1))
    return version_id


# ------------------------------------------------------------------
# 当日可交易池
# ------------------------------------------------------------------

def build_pool_report(
    ohlcv: pl.DataFrame,
    metadata_df: pl.DataFrame | None,
    nav_df: pl.DataFrame | None,
    decision_date: date,
    settings: dict,
    holdings: dict[str, float] | None = None,
    latest_version: dict | None = None,
    date_ranges: pl.DataFrame | None = None,
) -> PoolReport:
    """构建当日可交易池报告 (分项检查 + 买卖许可, 不静默通过)."""
    uf = universe_filter.from_settings(settings)
    evaluations = uf.evaluate(ohlcv, metadata_df=metadata_df, nav_df=nav_df)

    last_dates: dict[str, date] = {}
    if date_ranges is not None and not date_ranges.is_empty():
        for row in date_ranges.iter_rows(named=True):
            if row.get("last_date") is not None:
                last_dates[row["symbol"]] = row["last_date"]
    else:
        for row in (
            ohlcv.group_by("symbol")
            .agg(pl.col("date").max().alias("last_date"))
            .iter_rows(named=True)
        ):
            if row.get("last_date") is not None:
                last_dates[row["symbol"]] = row["last_date"]
    global_last = max(last_dates.values()) if last_dates else decision_date

    required_types = set(
        settings.get("universe_filter", {}).get(
            "premium_check_required_market_types",
            DEFAULT_PREMIUM_REQUIRED_TYPES,
        )
    )
    market_types: dict[str, str] = {}
    if (
        metadata_df is not None and not metadata_df.is_empty()
        and "market_type" in metadata_df.columns
    ):
        for row in metadata_df.select(["symbol", "market_type"]).iter_rows(named=True):
            if row["market_type"]:
                market_types[str(row["symbol"])] = str(row["market_type"])

    holdings = holdings or {}
    entries: list[PoolEntry] = []
    for symbol in sorted(evaluations):
        ev = evaluations[symbol]
        checks = dict(ev.checks)
        reasons = list(ev.fail_reasons)
        unknown_msgs = list(ev.unknown_msgs)

        last_date = last_dates.get(symbol)
        stale = bool(last_date is not None and last_date < global_last)
        checks["data_freshness"] = "fail" if stale else "pass"
        if stale:
            reasons.append(
                f"最新数据 {last_date} 早于池内最新 {global_last} (停牌/数据缺失)"
            )

        market_type = market_types.get(symbol)
        if checks.get("premium") == "unknown":
            if market_type in required_types:
                reasons.append(
                    "溢价检查为必需但 NAV unknown → 禁新买 (spec §4.4)"
                )
            else:
                checks["premium"] = "not_applicable"  # 可选维度: 显式记录
                unknown_msgs = [
                    msg + " (该产品溢价检查非必需, 记为 not_applicable)"
                    for msg in unknown_msgs
                ]

        can_buy = (
            checks.get("volume") == "pass"
            and checks.get("listing") == "pass"
            and checks.get("premium") in ("pass", "not_applicable")
            and checks.get("data_freshness") == "pass"
        )
        can_sell = checks.get("data_freshness") == "pass"
        is_held = float(holdings.get(symbol, 0.0)) > 0

        entries.append(PoolEntry(
            symbol=symbol,
            checks=checks,
            reasons=reasons,
            unknown_msgs=unknown_msgs,
            can_buy=can_buy,
            can_sell=can_sell,
            can_hold=True,   # 禁买/停牌不等于清仓; 持仓始终可 hold 与估值
            is_held=is_held,
            last_date=last_date,
            stale=stale,
        ))

    version_as_of = None
    pool_version = None
    if latest_version:
        pool_version = latest_version.get("version_id")
        version_as_of = latest_version.get("as_of")
    mode = (
        "declared"
        if version_as_of is None or decision_date >= version_as_of
        else "conditional_research"
    )
    notes: list[str] = []
    if mode == "conditional_research":
        notes.append("决策日早于池版本声明日: 历史成员无法重建, 按条件性研究 (C12)")
    if version_as_of is None:
        notes.append("尚无池版本记录 (未运行 sync_instruments_and_pool)")

    return PoolReport(
        decision_date=decision_date,
        entries=entries,
        pool_version=pool_version,
        membership_mode=mode,
        notes=notes,
    )


# ------------------------------------------------------------------
# 历史池判定 (C12)
# ------------------------------------------------------------------

def member_buyable_at(
    symbol: str,
    as_of: date,
    list_date: date | None = None,
    version_as_of: date | None = None,
) -> tuple[bool, str]:
    """历史日期上的成员可买性判定.

    - 上市日期之前: 不可买 ("当时未上市不可买").
    - 池版本声明日之前: 可买但标注 conditional_research (历史成员无法重建).
    """
    if list_date is not None and as_of < list_date:
        return False, "当时未上市"
    if version_as_of is not None and as_of < version_as_of:
        return True, "conditional_research: 历史成员无法重建, 条件性研究 (C12)"
    return True, "declared"
