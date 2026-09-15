"""账户账本 (M2) — 四类记录之 AccountState: 事件回放与派生状态.

契约 (plan §M2 / spec §3):
  - 事件是唯一事实: 账户状态由事件回放生成; 快照/批次是可重建的派生缓存.
  - 真实账户的成交/出入金只来自用户录入 (手动/标准 CSV, source=manual|csv|broker);
    策略计算、通知、用户"已查看"不产生真实成交 (spec §3.1).
  - event_time (发生) 与 import_time (导入) 分开; 更正通过 correction 事件
    关联原事件 (原记录保留, 回放时替换), 不直接覆盖事实.
  - 重复导入不增加第二笔事件: event_id 为稳定去重标识 (外部编号或确定性哈希).
  - 现金口径: cash = 可交易现金 (卖出款当日可用);
    cash_withdrawable = 可取现金 (卖出/分红到账 T+1 转可取).
  - 应收权益 (receivable) 到账后转入现金, 不重复计资产.

事件类型:
  initial            — 初始状态 (amount=现金;
                       payload.positions=[{symbol,shares,cost_basis,sellable_date}])
  deposit            — 入金 (amount; 现金与可取同时增加)
  withdrawal         — 出金 (amount; 不可超过可取现金)
  trade              — 成交 (side=BUY/SELL, symbol, shares, price, fee)
  fee                — 单独费用 (amount)
  dividend_accrual   — 分红应收 (amount)
  dividend_payment   — 分红到账 (冲减应收并转现金; 未记应收时按直接到账)
  share_change       — 份额变动 (payload.delta_shares; 拆分/折算按比例调整, 成本不变)
  correction         — 更正 (correction_of=原事件; 提供字段为完整替代值)

CLI (标准 CSV 导入, 模板见 docs/account_events_template.csv):
    uv run account-import --account REAL-1 --name "项目账户" --mode real --csv events.csv
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from data_pipeline.duckdb_manager import DuckDBManager

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "initial", "deposit", "withdrawal", "trade", "fee",
    "dividend_accrual", "dividend_payment", "share_change", "correction",
)
ACCOUNT_MODES = ("real", "paper", "backtest")


class AccountError(ValueError):
    """账本规则错误 (卖超/现金不足/未知事件/无效更正等)."""


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class Lot:
    """买入批次 (批次级可卖时间, C05)."""

    lot_id: str
    symbol: str
    shares: float
    cost_basis: float          # 剩余成本 (含费)
    open_date: date
    sellable_date: date        # 可卖起始日
    open_event_id: str


@dataclass
class AccountEvent:
    """账户事件 (与 account_events 表列一一对应)."""

    event_id: str
    account_id: str
    event_type: str
    event_time: datetime
    symbol: str | None = None
    side: str | None = None
    shares: float | None = None
    price: float | None = None
    amount: float | None = None
    fee: float | None = None
    source: str = "manual"
    reference: str | None = None
    correction_of: str | None = None
    payload: dict | None = None
    note: str | None = None


@dataclass
class AccountState:
    """由事件回放得到的账户状态 (权威派生结果)."""

    account_id: str
    cash: float = 0.0
    cash_withdrawable: float = 0.0
    pending_withdrawable: float = 0.0
    cash_reserved: float = 0.0
    receivable: float = 0.0
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    last_event_time: datetime | None = None
    last_event_id: str | None = None

    def shares_of(self, symbol: str) -> float:
        """持仓份额合计."""
        return sum(lot.shares for lot in self.lots.get(symbol, []))

    def positions_summary(self) -> dict[str, float]:
        """symbol → 持仓份额."""
        return {s: self.shares_of(s) for s in sorted(self.lots) if self.lots[s]}


@dataclass
class ImportResult:
    """导入结果 (inserted/skipped + 回放后状态)."""

    inserted: int
    skipped: int
    state: AccountState


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------

def next_business_day(d: date) -> date:
    """次一工作日 (简化交易日历; 实际日历在 M3/M4 接入)."""
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:
        nd += timedelta(days=1)
    return nd


def make_event_id(
    account_id: str,
    event_type: str,
    event_time: datetime,
    *,
    symbol: str | None = None,
    side: str | None = None,
    shares: float | None = None,
    price: float | None = None,
    amount: float | None = None,
    reference: str | None = None,
) -> str:
    """确定性事件 ID: 同一记录重复导入 → 同一 ID → 自动去重."""
    parts = [
        account_id, event_type, event_time.isoformat(sep=" "),
        symbol or "", side or "",
        "" if shares is None else f"{shares:.6f}",
        "" if price is None else f"{price:.6f}",
        "" if amount is None else f"{amount:.6f}",
        reference or "",
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{account_id}-{digest[:20]}"


def _parse_time(value: Any) -> datetime:
    """解析事件时间 (接受 datetime/date/多种字符串格式)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise AccountError(f"无法解析事件时间: {value!r}")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise AccountError(f"无法解析数值: {value!r}") from exc


# ------------------------------------------------------------------
# 事件应用 (纯逻辑)
# ------------------------------------------------------------------

def apply_event(state: AccountState, event: AccountEvent) -> None:
    """将单个事件应用于状态 (原地修改). 无效规则抛 AccountError."""
    if event.event_type not in EVENT_TYPES:
        raise AccountError(f"未知事件类型: {event.event_type}")
    if event.event_type == "correction":
        raise AccountError("correction 事件应在 resolve_corrections 中替换, 不应直接应用")

    # 跨日: 前日卖出/到账资金 T+1 转可取
    if (
        state.last_event_time is not None
        and event.event_time.date() > state.last_event_time.date()
    ):
        state.cash_withdrawable += state.pending_withdrawable
        state.pending_withdrawable = 0.0

    fee = float(event.fee or 0.0)
    amount = float(event.amount or 0.0)

    if event.event_type == "initial":
        state.cash += amount
        state.cash_withdrawable += amount
        for pos in (event.payload or {}).get("positions", []):
            shares = float(pos["shares"])
            if shares <= 0:
                continue
            sym = str(pos["symbol"])
            sellable = pos.get("sellable_date")
            state.lots.setdefault(sym, []).append(Lot(
                lot_id=f"{event.event_id}:{sym}",
                symbol=sym,
                shares=shares,
                cost_basis=float(pos.get("cost_basis", 0.0)),
                open_date=(
                    date.fromisoformat(str(pos["open_date"]))
                    if pos.get("open_date") else event.event_time.date()
                ),
                sellable_date=(
                    date.fromisoformat(str(sellable))
                    if sellable else event.event_time.date()
                ),
                open_event_id=event.event_id,
            ))

    elif event.event_type == "deposit":
        if amount <= 0:
            raise AccountError("deposit 金额必须为正")
        state.cash += amount
        state.cash_withdrawable += amount

    elif event.event_type == "withdrawal":
        if amount <= 0:
            raise AccountError("withdrawal 金额必须为正")
        if amount > state.cash + 1e-9 or amount > state.cash_withdrawable + 1e-9:
            raise AccountError(
                f"出金超过可取现金: amount={amount}, cash={state.cash}, "
                f"withdrawable={state.cash_withdrawable}"
            )
        state.cash -= amount
        state.cash_withdrawable -= amount

    elif event.event_type == "trade":
        if event.side not in ("BUY", "SELL") or not event.symbol:
            raise AccountError(f"trade 事件字段不完整: {event}")
        if not event.shares or event.shares <= 0 or not event.price or event.price <= 0:
            raise AccountError(f"trade 份额/价格无效: {event}")
        shares = float(event.shares)
        price = float(event.price)
        gross = shares * price
        if event.side == "BUY":
            cost = gross + fee
            if cost > state.cash + 1e-9:
                raise AccountError(
                    f"买入现金不足: 需要 {cost:.2f}, 可用 {state.cash:.2f}"
                )
            state.cash -= cost
            state.cash_withdrawable = max(0.0, min(state.cash_withdrawable, state.cash))
            sellable = (event.payload or {}).get("sellable_date")
            sellable_date = (
                date.fromisoformat(str(sellable))
                if sellable else next_business_day(event.event_time.date())
            )
            state.lots.setdefault(event.symbol, []).append(Lot(
                lot_id=event.event_id,
                symbol=event.symbol,
                shares=shares,
                cost_basis=cost,
                open_date=event.event_time.date(),
                sellable_date=sellable_date,
                open_event_id=event.event_id,
            ))
        else:
            _consume_lots(state, event.symbol, shares)
            proceeds = gross - fee
            state.cash += proceeds
            state.pending_withdrawable += proceeds  # 可取 T+1

    elif event.event_type == "fee":
        if amount <= 0:
            raise AccountError("fee 金额必须为正")
        state.cash -= amount
        state.cash_withdrawable = max(0.0, min(state.cash_withdrawable, state.cash))

    elif event.event_type == "dividend_accrual":
        if amount <= 0:
            raise AccountError("dividend_accrual 金额必须为正")
        state.receivable += amount

    elif event.event_type == "dividend_payment":
        if amount <= 0:
            raise AccountError("dividend_payment 金额必须为正")
        if state.receivable + 1e-9 >= amount:
            state.receivable -= amount
        else:
            # 未记录应收: 视为直接到账的现金分红 (不倒挂应收)
            logger.warning(
                "dividend_payment 超过应收 (%.2f > %.2f), 按直接到账处理",
                amount, state.receivable,
            )
            state.receivable = 0.0
        state.cash += amount
        state.pending_withdrawable += amount

    elif event.event_type == "share_change":
        _apply_share_change(state, event)

    state.last_event_time = event.event_time
    state.last_event_id = event.event_id


def _consume_lots(state: AccountState, symbol: str, shares: float) -> None:
    """FIFO 扣减批次 (实际成交不回看可卖日; 可卖约束在建议层由 M4 执行)."""
    lots = state.lots.get(symbol, [])
    total = sum(lot.shares for lot in lots)
    if shares > total + 1e-9:
        raise AccountError(f"卖出超过持仓: {symbol} 卖 {shares}, 持有 {total}")
    remaining = shares
    for lot in sorted(lots, key=lambda item: (item.open_date, item.lot_id)):
        if remaining <= 1e-9:
            break
        take = min(lot.shares, remaining)
        ratio = take / lot.shares if lot.shares > 0 else 1.0
        lot.cost_basis *= (1.0 - ratio)
        lot.shares -= take
        remaining -= take
    state.lots[symbol] = [lot for lot in lots if lot.shares > 1e-9]


def _apply_share_change(state: AccountState, event: AccountEvent) -> None:
    """份额变动 (拆分/折算): 按比例调整批次, 成本不变."""
    payload = event.payload or {}
    symbol = event.symbol or payload.get("symbol")
    delta = payload.get("delta_shares")
    if symbol is None or delta is None:
        raise AccountError("share_change 需要 symbol 与 payload.delta_shares")
    lots = state.lots.get(str(symbol), [])
    old_total = sum(lot.shares for lot in lots)
    if old_total <= 0:
        raise AccountError(f"share_change 无持仓: {symbol}")
    new_total = old_total + float(delta)
    if new_total <= 0:
        raise AccountError("share_change 导致持仓归零或为负; 请用更正/卖出处理")
    ratio = new_total / old_total
    for lot in lots:
        lot.shares *= ratio
    state.lots[str(symbol)] = lots


def resolve_corrections(
    events: list[AccountEvent],
) -> tuple[list[AccountEvent], list[str]]:
    """将 correction 事件替换为被更正事件的修订版 (原事件保留于日志).

    更正事件字段为完整替代值; 未提供 (None) 的字段沿用原事件.
    更正不改变原事件的发生时间 (event_time 保留原值; 更正自身时间见事件行).
    Returns:
        (生效事件列表, 引用了不存在原事件的更正 ID 列表)
    """
    by_id = {event.event_id: event for event in events}
    corrections = {
        event.correction_of: event
        for event in events
        if event.event_type == "correction" and event.correction_of
    }
    orphan = [orig for orig in corrections if orig not in by_id]
    effective: list[AccountEvent] = []
    for event in events:
        if event.event_type == "correction":
            continue
        replacement = corrections.get(event.event_id)
        if replacement is None:
            effective.append(event)
            continue
        data = asdict(event)
        for key, value in asdict(replacement).items():
            if key in (
                "event_id", "account_id", "event_type",
                "correction_of", "event_time",
            ):
                continue
            if value is not None:
                data[key] = value
        effective.append(AccountEvent(**data))
    return effective, orphan


def replay_account(events: list[AccountEvent]) -> AccountState:
    """按时间顺序回放事件 (含更正解析) 生成账户状态."""
    ordered = sorted(events, key=lambda event: (event.event_time, event.event_id))
    effective, orphan = resolve_corrections(ordered)
    if orphan:
        raise AccountError(f"更正事件引用了不存在的事件: {orphan}")
    state = AccountState(account_id=ordered[0].account_id if ordered else "")
    for event in effective:
        apply_event(state, event)
    return state


# ------------------------------------------------------------------
# DataFrame 转换
# ------------------------------------------------------------------

_EVENTS_SCHEMA = {
    "event_id": pl.Utf8, "account_id": pl.Utf8, "event_type": pl.Utf8,
    "event_time": pl.Datetime("us"), "symbol": pl.Utf8, "side": pl.Utf8,
    "shares": pl.Float64, "price": pl.Float64, "amount": pl.Float64,
    "fee": pl.Float64, "source": pl.Utf8, "reference": pl.Utf8,
    "correction_of": pl.Utf8, "payload_json": pl.Utf8, "note": pl.Utf8,
}

_LOTS_SCHEMA = {
    "lot_id": pl.Utf8, "account_id": pl.Utf8, "symbol": pl.Utf8,
    "open_event_id": pl.Utf8, "shares": pl.Float64, "cost_basis": pl.Float64,
    "open_date": pl.Date, "sellable_date": pl.Date, "closed": pl.Boolean,
}


def events_to_dataframe(events: list[AccountEvent]) -> pl.DataFrame:
    """事件列表 → DataFrame (与 account_events 表列对应)."""
    rows = [
        {
            "event_id": event.event_id,
            "account_id": event.account_id,
            "event_type": event.event_type,
            "event_time": event.event_time,
            "symbol": event.symbol,
            "side": event.side,
            "shares": event.shares,
            "price": event.price,
            "amount": event.amount,
            "fee": event.fee,
            "source": event.source,
            "reference": event.reference,
            "correction_of": event.correction_of,
            "payload_json": (
                json.dumps(event.payload, ensure_ascii=False)
                if event.payload else None
            ),
            "note": event.note,
        }
        for event in events
    ]
    return pl.DataFrame(rows, schema=_EVENTS_SCHEMA)


def events_from_dataframe(df: pl.DataFrame) -> list[AccountEvent]:
    """account_events 表 → 事件列表."""
    events: list[AccountEvent] = []
    for row in df.iter_rows(named=True):
        payload = None
        if row.get("payload_json"):
            payload = json.loads(row["payload_json"])
        events.append(AccountEvent(
            event_id=row["event_id"],
            account_id=row["account_id"],
            event_type=row["event_type"],
            event_time=_parse_time(row["event_time"]),
            symbol=row.get("symbol"),
            side=row.get("side"),
            shares=row.get("shares"),
            price=row.get("price"),
            amount=row.get("amount"),
            fee=row.get("fee"),
            source=row.get("source") or "manual",
            reference=row.get("reference"),
            correction_of=row.get("correction_of"),
            payload=payload,
            note=row.get("note"),
        ))
    return events


def lots_to_dataframe(account_id: str, state: AccountState) -> pl.DataFrame:
    """账户状态批次 → position_lots DataFrame (派生缓存)."""
    rows = [
        {
            "lot_id": lot.lot_id,
            "account_id": account_id,
            "symbol": symbol,
            "open_event_id": lot.open_event_id,
            "shares": lot.shares,
            "cost_basis": lot.cost_basis,
            "open_date": lot.open_date,
            "sellable_date": lot.sellable_date,
            "closed": False,
        }
        for symbol, lots in state.lots.items()
        for lot in lots
    ]
    return pl.DataFrame(rows, schema=_LOTS_SCHEMA)


def snapshot_dataframe(state: AccountState, note: str | None = None) -> pl.DataFrame:
    """账户状态 → account_snapshots DataFrame (as_of = 最后事件时间)."""
    positions = {
        symbol: [
            {
                "lot_id": lot.lot_id,
                "shares": lot.shares,
                "cost_basis": lot.cost_basis,
                "sellable_date": lot.sellable_date.isoformat(),
            }
            for lot in lots
        ]
        for symbol, lots in state.lots.items()
        if lots
    }
    return pl.DataFrame([{
        "account_id": state.account_id,
        "as_of": state.last_event_time or datetime.now(),
        "cash": state.cash,
        "cash_withdrawable": state.cash_withdrawable,
        "cash_reserved": state.cash_reserved,
        "receivable": state.receivable,
        "positions_json": json.dumps(positions, ensure_ascii=False),
        "event_cutoff": state.last_event_id,
        "reconciled": False,
        "note": note,
    }])


# ------------------------------------------------------------------
# 标准 CSV 导入 (用户录入通道)
# ------------------------------------------------------------------

def read_events_csv(
    path: str | Path,
    account_id: str,
    default_source: str = "csv",
) -> list[AccountEvent]:
    """读取标准事件 CSV (模板见 docs/account_events_template.csv).

    必需列: event_time, event_type; 其余可选。
    未提供 event_id 时, 以 reference (外部编号) 或行内容哈希生成确定性 ID,
    因此同一文件重复导入会自动去重。
    """
    df = pl.read_csv(path, infer_schema_length=0)  # 全字符串, 自行解析
    if df.is_empty():
        return []
    missing = {"event_time", "event_type"} - set(df.columns)
    if missing:
        raise AccountError(f"事件 CSV 缺少必需列: {sorted(missing)}")

    events: list[AccountEvent] = []
    for row in df.iter_rows(named=True):
        event_type = (_clean(row.get("event_type")) or "").lower()
        if event_type not in EVENT_TYPES:
            raise AccountError(f"未知 event_type: {event_type!r} (支持: {EVENT_TYPES})")
        event_time = _parse_time(row["event_time"])
        symbol = _clean(row.get("symbol"))
        side = (_clean(row.get("side")) or "").upper() or None
        shares = _num(row.get("shares"))
        price = _num(row.get("price"))
        amount = _num(row.get("amount"))
        fee = _num(row.get("fee"))
        reference = _clean(row.get("reference"))
        correction_of = _clean(row.get("correction_of"))
        payload = None
        if row.get("payload_json"):
            payload = json.loads(row["payload_json"])
        event_id = _clean(row.get("event_id"))
        if not event_id:
            event_id = reference or make_event_id(
                account_id, event_type, event_time,
                symbol=symbol, side=side, shares=shares, price=price,
                amount=amount, reference=reference,
            )
        events.append(AccountEvent(
            event_id=event_id,
            account_id=account_id,
            event_type=event_type,
            event_time=event_time,
            symbol=symbol,
            side=side,
            shares=shares,
            price=price,
            amount=amount,
            fee=fee,
            source=_clean(row.get("source")) or default_source,
            reference=reference,
            correction_of=correction_of,
            payload=payload,
            note=_clean(row.get("note")),
        ))
    return events


# ------------------------------------------------------------------
# 服务: 统一写入口 (事件→回放→派生缓存/快照, 同一事务)
# ------------------------------------------------------------------

class AccountService:
    """账户账本服务 (plan §M2: 统一写入口与事务; 重复导入幂等)."""

    def __init__(self, db: DuckDBManager) -> None:
        self.db = db

    def ensure_account(
        self,
        account_id: str,
        name: str = "",
        mode: str = "real",
        note: str | None = None,
    ) -> None:
        """创建/更新账户 (mode: real / paper / backtest)."""
        if mode not in ACCOUNT_MODES:
            raise AccountError(f"未知 mode: {mode!r} (支持: {ACCOUNT_MODES})")
        self.db.upsert_account(pl.DataFrame([{
            "account_id": account_id,
            "name": name,
            "mode": mode,
            "base_currency": "CNY",
            "note": note,
        }]))

    def import_events(
        self,
        events: list[AccountEvent],
        manage_transaction: bool = True,
    ) -> ImportResult:
        """导入事件 (幂等) 并在同一事务内重建批次缓存与快照.

        Args:
            events: 同一账户的事件列表
            manage_transaction: True 时自行开启事务; 调用方已在事务中时传 False
                (组合事务, 如执行反馈联动入账)

        Raises:
            AccountError: 事件无效 (卖超/现金不足/无效更正等) → 事务回滚, 不留半笔.
        """
        if not events:
            raise AccountError("空事件列表")
        account_ids = {event.account_id for event in events}
        if len(account_ids) != 1:
            raise AccountError("单次导入仅允许同一账户的事件")
        account_id = account_ids.pop()

        ordered = sorted(events, key=lambda event: (event.event_time, event.event_id))
        df = events_to_dataframe(ordered)

        if manage_transaction:
            with self.db.transaction():
                result = self._import_locked(account_id, df)
        else:
            result = self._import_locked(account_id, df)
        logger.info(
            "账户 %s 事件导入: 新增 %d, 去重跳过 %d, 现金 %.2f, 应收 %.2f",
            account_id, result.inserted, result.skipped,
            result.state.cash, result.state.receivable,
        )
        return result

    def _import_locked(self, account_id: str, df: pl.DataFrame) -> ImportResult:
        """事务内实现: 插入事件 → 回放 → 重建批次/快照."""
        inserted, skipped = self.db.insert_account_events(df)
        state = self._replay(account_id)
        self.db.replace_position_lots(account_id, lots_to_dataframe(account_id, state))
        self.db.upsert_account_snapshot(snapshot_dataframe(state))
        return ImportResult(inserted=inserted, skipped=skipped, state=state)

    def _replay(self, account_id: str) -> AccountState:
        df = self.db.get_account_events(account_id)
        if df.is_empty():
            return AccountState(account_id=account_id)
        return replay_account(events_from_dataframe(df))

    def current_state(self, account_id: str) -> AccountState:
        """回放库内全部事件得到当前状态 (事实来源)."""
        return self._replay(account_id)

    def latest_snapshot(self, account_id: str) -> dict | None:
        """最近快照 (派生缓存; None=从未导入)."""
        return self.db.get_latest_account_snapshot(account_id)


# ------------------------------------------------------------------
# CLI (uv run account-import)
# ------------------------------------------------------------------

def main() -> None:
    """CLI: 导入账户事件 CSV (标准模板 docs/account_events_template.csv)."""
    import argparse
    import sys

    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Quant-Pilot 账户事件导入 (标准 CSV; 重复导入自动去重)",
    )
    parser.add_argument("--account", required=True, help="账户 ID (如 REAL-1)")
    parser.add_argument("--name", default="", help="账户名称 (创建/更新时)")
    parser.add_argument(
        "--mode", default="real", choices=ACCOUNT_MODES,
        help="账户模式 (默认 real; 研究示例用 paper)",
    )
    parser.add_argument("--csv", required=True, help="事件 CSV 路径")
    parser.add_argument("--db", default=None, help="DuckDB 路径 (缺省读 settings)")
    args = parser.parse_args()

    if args.db:
        db_path = args.db
    else:
        project_root = Path(__file__).resolve().parents[2]
        with open(project_root / "config" / "settings.yaml", encoding="utf-8") as f:
            settings = yaml.safe_load(f)
        db_path = str(project_root / settings["storage"]["db_path"])

    db = DuckDBManager(db_path)
    db.init_database()
    try:
        service = AccountService(db)
        service.ensure_account(args.account, args.name, args.mode)
        events = read_events_csv(args.csv, account_id=args.account)
        result = service.import_events(events)
        print(f"导入完成: 新增 {result.inserted} 条, 去重跳过 {result.skipped} 条")
        state = result.state
        print(
            f"现金 {state.cash:,.2f} | 可取 {state.cash_withdrawable:,.2f} "
            f"| 应收 {state.receivable:,.2f}"
        )
        for symbol, shares in state.positions_summary().items():
            print(f"  持仓 {symbol}: {shares:,.0f} 份")
    finally:
        db.close()


if __name__ == "__main__":
    main()
