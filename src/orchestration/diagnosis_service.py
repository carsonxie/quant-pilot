"""统一诊断服务 (Phase 1.1, P11-M3-05/06).

单入口 `run_diagnosis` (供持仓/观察/探索三页复用):
  身份解析 → 统一取数 (新鲜度/受控刷新) → 权益事件检查 (暂停/调整)
  → 指标计算 (必要时用已核验事件调整序列) → 计分与解释
  → 状态机 (连续触发/恢复 + 版本变化区分) → 快照落库 → 返回结果。

约定:
  - 幂等: 快照 ID = symbol|as_of|rules_version|price_version (确定性);
    同一版本同一输入重复运行不新增行 (upsert); 同日重跑与"更早运行日"比较,
    不与自己比较 (trigger 稳定)。
  - 只保存实际运行过的诊断日; `run_kind="rebuild"` 标记历史重建结果。
  - 状态机: 升级需连续 N 个运行日 (rules.scoring.hysteresis.upgrade_confirmations)
    满足更高原始档; 恢复 1 日确认; 规则/价格版本变化触发 "版本变化", 不冒充市场变化;
    首次可评估标 "初始"; 数据无效标 "数据失效"; 必需指标缺失标 "不可评估"。
  - 无交易副作用: 只写 diagnostic_snapshots 与行情缓存 (经 DuckDBManager)。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

import polars as pl

from data_pipeline.diag_data import (
    EquityEventCheck,
    ResearchPrices,
    TradingCalendar,
    check_equity_events,
    ensure_research_prices,
)
from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.securities import (
    SecurityIdentity,
    SecurityResolver,
    normalize_symbol,
    resolver_from_settings,
)
from factor_engineering.technical import compute_indicators
from strategies.condition_checks import (
    ConditionItem,
    conditions_payload,
    evaluate_etf_conditions,
    evaluate_stock_conditions,
    load_condition_rules,
)
from strategies.diagnosis import (
    DiagnosisAssessment,
    assess_row,
    load_diagnostics_rules,
)

logger = logging.getLogger(__name__)

_STATUS_RANK = {"正常": 0, "警戒": 1, "防御": 2}

# 触发词表 (快照 trigger 列)
TRIGGER_INITIAL = "初始"
TRIGGER_PENDING_UPGRADE = "升级待确认"
TRIGGER_NEW_WATCH = "新增警戒"
TRIGGER_NEW_DEFENSE = "新增防御"
TRIGGER_RECOVERED = "恢复"
TRIGGER_MAINTAINED = "维持"
TRIGGER_VERSION_CHANGE = "版本变化"
TRIGGER_DATA_INVALID = "数据失效"
TRIGGER_NOT_EVALUABLE = "不可评估"


@dataclass
class DiagnosisOutcome:
    """一次诊断运行的完整结果 (UI 直接消费)."""

    symbol: str
    identity: SecurityIdentity
    prices: ResearchPrices
    equity: EquityEventCheck
    assessment: DiagnosisAssessment
    status: str | None           # 确认档位 (状态机输出)
    trigger: str
    as_of: date | None
    price_version: str
    snapshot: dict | None
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# 状态机 (连续触发 / 恢复 / 版本变化)
# ------------------------------------------------------------------

def _previous_run_snapshots(
    db: DuckDBManager, symbol: str, current_as_of: date | None,
) -> list[dict]:
    """更早运行日的快照 (最新在前; 同日重跑不与自己比较)."""
    df = db.get_diagnostic_snapshots(symbol, limit=20)
    if df.is_empty():
        return []
    rows = df.to_dicts()
    if current_as_of is None:
        return rows
    return [r for r in rows if r["as_of"] != current_as_of]


def resolve_status_transition(
    prev_snapshots: list[dict],
    assessment: DiagnosisAssessment,
    *,
    rules_version: str,
    price_version: str,
    upgrade_confirmations: int = 2,
) -> tuple[str | None, str]:
    """由历史快照 + 本次原始档位推导 (确认档位, trigger)."""
    raw_status = assessment.raw_status  # None → 不可评估

    if assessment.score is None:
        # 数据无效由调用方先行判定 (数据失效); 此处为"指标不足/事件暂停"
        return None, TRIGGER_NOT_EVALUABLE

    if not prev_snapshots:
        return raw_status, TRIGGER_INITIAL

    prev = prev_snapshots[0]
    if prev.get("rules_version") != rules_version or prev.get("price_version") != price_version:
        return raw_status, TRIGGER_VERSION_CHANGE

    prev_confirmed = prev.get("status")
    if prev_confirmed is None:
        return raw_status, TRIGGER_INITIAL

    raw_rank = _STATUS_RANK[raw_status]
    prev_rank = _STATUS_RANK[prev_confirmed]

    if raw_rank > prev_rank:
        prev_raw = _raw_status_of(prev)
        if prev_raw is not None and _STATUS_RANK.get(prev_raw) == raw_rank:
            trigger = TRIGGER_NEW_DEFENSE if raw_status == "防御" else TRIGGER_NEW_WATCH
            return raw_status, trigger
        # 首次达到更高档 → 待确认 (确认档位保持)
        if upgrade_confirmations <= 1:
            trigger = TRIGGER_NEW_DEFENSE if raw_status == "防御" else TRIGGER_NEW_WATCH
            return raw_status, trigger
        return prev_confirmed, TRIGGER_PENDING_UPGRADE

    if raw_rank < prev_rank:
        return raw_status, TRIGGER_RECOVERED
    return raw_status, TRIGGER_MAINTAINED


def _raw_status_of(snapshot: dict) -> str | None:
    """从快照 metrics_json 读取当时的原始档位 (状态机比较用)."""
    try:
        payload = json.loads(snapshot.get("metrics_json") or "{}")
    except (TypeError, ValueError):
        return None
    return payload.get("raw_status")


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------

def run_diagnosis(
    db: DuckDBManager,
    symbol: str,
    *,
    refresh: str = "auto",
    settings: dict | None = None,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
    fetchers: dict[str, Callable] | None = None,
    resolver: SecurityResolver | None = None,
    rules: dict | None = None,
    run_kind: str = "live",
    condition_fetchers: dict[str, Callable] | None = None,
    condition_rules: dict | None = None,
) -> DiagnosisOutcome:
    """单标的诊断全链路 (无交易副作用; 幂等).

    condition_fetchers: 条件检查数据注入 {"stock": fn(symbol)->extract, "etf": fn(symbol)->extract}
    (测试用; 默认走 fundamental_data 真实接口)。
    """
    rules = rules or load_diagnostics_rules()
    resolver = resolver or resolver_from_settings(db, settings)
    identity = resolver.resolve(symbol)

    prices = ensure_research_prices(
        db, identity, refresh=refresh, settings=settings, now=now,
        calendar=calendar, fetchers=fetchers,
    )

    notes = list(prices.notes)
    conditions = _build_conditions(
        identity, prices, condition_fetchers=condition_fetchers,
        condition_rules=condition_rules,
    )
    # ---- 数据无效: 不进入指标/计分, 记录"数据失效"快照 ----
    if prices.prices.is_empty():
        equity = check_equity_events(db, identity, prices.prices, settings=settings)
        assessment = assess_row({}, rules, as_of=prices.expected_last,
                                source_note=prices.price_version)
        as_of = prices.expected_last or prices.last_date
        snapshot = _persist_snapshot(
            db, identity, prices, assessment, status=None,
            trigger=TRIGGER_DATA_INVALID, rules=rules, run_kind=run_kind,
            extra_notes=notes, as_of=as_of, conditions=conditions,
        )
        return DiagnosisOutcome(
            symbol=identity.symbol, identity=identity, prices=prices, equity=equity,
            assessment=assessment, status=None, trigger=TRIGGER_DATA_INVALID,
            as_of=as_of, price_version=prices.price_version, snapshot=snapshot,
            notes=notes,
        )

    equity = check_equity_events(db, identity, prices.prices, settings=settings)
    notes.append(equity.note)

    # ---- 指标输入: 已核验事件 → 调整序列; 未核验/跳变 → 暂停跨事件指标 ----
    unavailable: dict[str, str] = {}
    if equity.adjusted_prices is not None:
        indicator_input = equity.adjusted_prices
        notes.append("指标基于已核验权益事件的调整序列 (量额保持原始口径)")
    else:
        indicator_input = prices.prices
    if equity.status in ("paused", "data_issue"):
        reason = (
            "窗口含未核验权益事件 → 该项暂停" if equity.status == "paused"
            else "窗口含未登记跳变 → 相关指标不可信"
        )
        unavailable = {col: reason for col in rules["event_sensitive_columns"]}

    indicators = compute_indicators(indicator_input, rules["indicators"])
    latest_row = indicators.to_dicts()[-1] if not indicators.is_empty() else {}

    as_of = prices.expected_last or prices.last_date
    assessment = assess_row(
        latest_row, rules,
        as_of=as_of,
        source_note=prices.price_version,
        unavailable=unavailable,
    )
    if prices.status in ("stale", "failed", "blocked"):
        notes.append(
            f"数据时效: {prices.status} (截止 {prices.last_date}, "
            f"最近已完成交易日 {prices.expected_last}); 结果按现有数据给出"
        )

    # ---- 状态机 ----
    prev_snapshots = _previous_run_snapshots(db, identity.symbol, as_of)
    hysteresis = rules["scoring"].get("hysteresis", {}) or {}
    status, trigger = resolve_status_transition(
        prev_snapshots, assessment,
        rules_version=rules["rules_version"],
        price_version=prices.price_version,
        upgrade_confirmations=int(hysteresis.get("upgrade_confirmations", 2)),
    )
    assessment.status = status

    snapshot = _persist_snapshot(
        db, identity, prices, assessment, status=status, trigger=trigger,
        rules=rules, run_kind=run_kind, extra_notes=notes, as_of=as_of,
        conditions=conditions,
    )

    return DiagnosisOutcome(
        symbol=identity.symbol, identity=identity, prices=prices, equity=equity,
        assessment=assessment, status=status, trigger=trigger, as_of=as_of,
        price_version=prices.price_version, snapshot=snapshot, notes=notes,
    )


def _persist_snapshot(
    db: DuckDBManager,
    identity: SecurityIdentity,
    prices: ResearchPrices,
    assessment: DiagnosisAssessment,
    *,
    status: str | None,
    trigger: str,
    rules: dict,
    run_kind: str,
    extra_notes: list[str],
    as_of: date | None,
    conditions: dict | None = None,
) -> dict | None:
    if as_of is None:
        logger.warning("%s: 无可用诊断时点 (日历/数据), 不写快照", identity.symbol)
        return None

    metrics = {
        "raw_status": assessment.raw_status,
        "raw_score": assessment.raw_score,
        "group_points": assessment.group_points,
        "display": assessment.display,
        "hints": assessment.hints,
        "data_status": prices.status,
        "data_last_date": prices.last_date.isoformat() if prices.last_date else None,
        "history_days": prices.history_days,
    }
    contributions = {
        item.item_id: {
            "column": item.column, "value": item.value, "points": item.points,
            "max_points": item.max_points, "triggered": item.triggered,
            "note": item.note,
        }
        for item in assessment.items
    }
    checks = {
        "missing_required": assessment.missing_required,
        "unavailable": assessment.unavailable,
        "completeness": assessment.completeness,
        "notes": extra_notes,
        "explanations_version": rules.get("explanation_version"),
        "explanations": assessment.explanations,  # 模板行留档, UI 免重算展示
        "conditions": conditions,                 # 条件检查 (与技术分单列, P11-M4)
    }
    snapshot_id = DuckDBManager.diagnostic_snapshot_id(
        identity.symbol, as_of, rules["rules_version"], prices.price_version,
    )
    row = {
        "snapshot_id": snapshot_id,
        "symbol": identity.symbol,
        "as_of": as_of,
        "rules_version": rules["rules_version"],
        "price_version": prices.price_version,
        "run_kind": run_kind,
        "metrics_json": json.dumps(metrics, ensure_ascii=False, default=str),
        "contributions_json": json.dumps(contributions, ensure_ascii=False, default=str),
        "checks_json": json.dumps(checks, ensure_ascii=False, default=str),
        "score": assessment.score,
        "status": status,
        "completeness": assessment.completeness,
        "trigger": trigger,
    }
    db.upsert_diagnostic_snapshot(row)
    return row


# ------------------------------------------------------------------
# 条件检查装配 (P11-M4; 接口失败/数据缺失 → 无法评估, 不阻塞技术诊断)
# ------------------------------------------------------------------

def _build_conditions(
    identity: SecurityIdentity,
    prices: ResearchPrices,
    *,
    condition_fetchers: dict[str, Callable] | None = None,
    condition_rules: dict | None = None,
) -> dict | None:
    """组装条件检查 payload (存入快照 checks_json.conditions)."""
    try:
        rules = condition_rules or load_condition_rules()
    except Exception as exc:
        return {
            "version": None, "subject": identity.symbol, "fetched_at": None,
            "summary": "无法评估", "items": [],
            "notes": [f"条件规则卡不可用: {type(exc).__name__}: {exc}"],
        }

    fetched_at = datetime.now().isoformat(timespec="seconds")
    subject = f"{identity.symbol} {identity.name or ''}".strip()
    try:
        items: list[ConditionItem]
        if identity.sec_type == "stock":
            fetch_fn = (condition_fetchers or {}).get("stock")
            if fetch_fn is None:
                from data_pipeline.fundamental_data import load_stock_conditions_input

                val_cfg = rules["stock"]["valuation"]
                fetch_fn = lambda symbol: load_stock_conditions_input(  # noqa: E731
                    symbol,
                    pe_window=int(val_cfg["percentile_window"]),
                    pe_min_samples=int(val_cfg["percentile_min_samples"]),
                )
            extract = fetch_fn(identity.symbol)
            items = evaluate_stock_conditions(extract, rules=rules)
        else:
            fetch_fn = (condition_fetchers or {}).get("etf")
            if fetch_fn is None:
                from data_pipeline.fundamental_data import load_etf_conditions_input

                fetch_fn = load_etf_conditions_input
            extract = fetch_fn(identity.symbol)
            items = evaluate_etf_conditions(
                extract, prices=prices.prices, last_completed=prices.expected_last,
                market_type=identity.market_type, rules=rules,
            )
        return conditions_payload(
            subject, items, version=rules["version"],
            notes=list(extract.get("errors") or []), fetched_at=fetched_at,
        )
    except Exception as exc:  # 条件数据失败不阻断技术诊断
        logger.warning("条件检查失败 (%s): %s", identity.symbol, exc)
        return {
            "version": rules["version"], "subject": subject, "fetched_at": fetched_at,
            "summary": "无法评估", "items": [],
            "notes": [f"条件数据获取失败: {type(exc).__name__}: {exc}"],
        }


# ------------------------------------------------------------------
# 读取接口 (三入口复用; 不刷新、不写库)
# ------------------------------------------------------------------

def get_snapshot_payload(db: DuckDBManager, symbol: str) -> dict | None:
    """最近一次诊断快照 (解析 JSON 字段; 无 → None)."""
    row = db.get_latest_diagnostic_snapshot(symbol)
    if row is None:
        return None
    return _parse_snapshot_row(row)


def get_snapshot_history(db: DuckDBManager, symbol: str, limit: int = 60) -> list[dict]:
    """历史快照 (最新在前, JSON 解析)."""
    df = db.get_diagnostic_snapshots(symbol, limit=limit)
    if df.is_empty():
        return []
    return [_parse_snapshot_row(row) for row in df.to_dicts()]


def _parse_snapshot_row(row: dict) -> dict:
    parsed = dict(row)
    for source, target in (
        ("metrics_json", "metrics"),
        ("contributions_json", "contributions"),
        ("checks_json", "checks"),
    ):
        try:
            parsed[target] = json.loads(row.get(source) or "{}")
        except (TypeError, ValueError):
            parsed[target] = {}
    return parsed


def snapshot_display_frame(db: DuckDBManager, symbols: list[str] | None = None) -> pl.DataFrame:
    """多标的最近快照 (列表视图用; symbols=None → 全部有快照标的)."""
    df = db.get_latest_snapshot_per_symbol()
    if df.is_empty():
        return df
    if symbols:
        df = df.filter(pl.col("symbol").is_in(symbols))
    return df


# ------------------------------------------------------------------
# 观察名单服务 (P11-M5-01; 受控写入口, 幂等, 不写策略池)
# ------------------------------------------------------------------

def watchlist_add(
    db: DuckDBManager, symbol: str, *, note: str | None = None,
    resolver: SecurityResolver | None = None, settings: dict | None = None,
) -> SecurityIdentity:
    """加入观察名单 (先确认身份, 再快照身份字段; 重复添加幂等)."""
    identity = (resolver or resolver_from_settings(db, settings)).resolve(symbol)
    db.add_watchlist(
        identity.symbol, name=identity.name, exchange=identity.exchange,
        sec_type=identity.sec_type, note=note,
    )
    return identity


def watchlist_remove(db: DuckDBManager, symbol: str, *, reason: str | None = None) -> None:
    """移出观察名单 (软删除; 不删除真实持仓/行情/诊断历史)."""
    db.remove_watchlist(normalize_symbol(symbol), reason=reason)


def watchlist_update_note(db: DuckDBManager, symbol: str, note: str) -> None:
    """更新观察名单备注 (记录变更)."""
    db.update_watchlist_note(normalize_symbol(symbol), note)
