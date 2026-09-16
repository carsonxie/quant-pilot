"""调仓建议运行编排 (M4) — 账户 → 状态化风控 → 规划 → 建议落库.

流程 (daily_pipeline Step 10; 账户不存在时整段跳过, 保持 M3 研究输出):
  1. 读取最新账户状态 (事件回放, 事实来源)
  2. 更新持有期状态 (高水位/ATR/棘轮止损/冷却; M4-06)
  3. 目标 (含现金权重) vs 账户实际 → 规划建议 (rebalance_planner, 纯函数)
  4. 建议落库 (proposal_ledger; 确定性 ID 幂等, 新目标替代旧开放建议)
  5. 状态与决策留痕 (runs 运行记录由 daily 层写入)

纪律:
  - 账户事件是唯一事实; 本模块不写账户事件, 不自动执行建议.
  - 旧建议不被无痕覆盖: 新目标生效时旧开放建议 → replaced (spec §3.2).
  - 无账户/空账户 → 返回 None, 不做任何写入 (研究模式不受影响).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from data_pipeline.duckdb_manager import DuckDBManager
from orchestration.proposal_ledger import ProposalLedger
from strategies import rebalance_planner, strategy_states
from strategies.rebalance_planner import PlannerConfig, PlanResult

logger = logging.getLogger(__name__)


@dataclass
class RebalanceRun:
    """调仓运行结果 (daily 摘要与运行记录使用)."""

    account_id: str
    plan: PlanResult
    stop_exits: list[str]                       # 状态化止损触发
    cooldown_symbols: dict[str, str]            # 冷却中标的 → 说明
    proposals_inserted: int = 0
    proposals_skipped: int = 0
    replaced_open: int = 0

    @property
    def summary_ok(self) -> bool:
        return not self.plan.paused


def run_rebalance(
    db: DuckDBManager,
    settings: dict,
    ohlcv: pl.DataFrame,
    target_weights: dict[str, float],
    *,
    as_of: date,
    account_id: str | None = None,
    target_id: str | None = None,
    pool_can_buy: set[str] | None = None,
    pool_can_sell: set[str] | None = None,
    stale_symbols: set[str] | None = None,
    ref_time: datetime | None = None,
) -> RebalanceRun | None:
    """执行一次建议规划 (账户存在时); 否则返回 None (不写入).

    Args:
        db: 数据库 (建议/状态写入入口)
        settings: settings.yaml (rebalancing / trading_cost 节)
        ohlcv: 行情 (估值与参考价, 截至 as_of)
        target_weights: 目标完整权重 (含现金 `__CASH__`)
        as_of: 决策日 (T 日盘后)
        account_id: 账户 ID; None 时读 settings.rebalancing.account
        target_id: 目标记录 ID (daily 生成, 保证建议引用一致)
        pool_can_buy / pool_can_sell: 四层池许可 (M3)
        stale_symbols: 停牌/无当日价格 (不能买卖)
        ref_time: 决策时间戳 (建议参考时间; 缺省 as_of 09:00)
    """
    rebal_cfg = settings.get("rebalancing", {})
    account_id = account_id or rebal_cfg.get("account")
    if not account_id:
        return None

    accounts = db.get_accounts()
    if accounts.is_empty() or account_id not in set(accounts["account_id"].to_list()):
        logger.info("调仓规划跳过: 账户 %s 不存在 (等待 account-import 录入)", account_id)
        return None

    from data_pipeline.accounting import AccountService

    accounts_svc = AccountService(db)
    account_state = accounts_svc.current_state(str(account_id))
    has_events = bool(
        account_state.lots or account_state.last_event_time is not None
        or account_state.cash > 0 or account_state.receivable > 0
    )
    if not has_events:
        logger.info("调仓规划跳过: 账户 %s 无事件 (无可执行建议)", account_id)
        return None

    # ---- 1. 持有期状态更新 (M4-06) ----
    rm = _risk_manager(settings)
    cooldown_days = int(rebal_cfg.get("cooldown_days", 5))
    states = strategy_states.load_position_states(db)
    held = set(account_state.positions_summary())
    entry_dates = db.get_position_entry_dates(str(account_id))
    states, stop_exits, cooldown = strategy_states.update_position_states(
        states, rm, ohlcv, held_symbols=held, entry_dates=entry_dates,
        as_of=as_of, cooldown_days=cooldown_days,
    )
    strategy_states.persist_position_states(db, states)
    if stop_exits:
        logger.warning("状态化止损触发 (账户 %s): %s", account_id, stop_exits)

    risk_exits = {
        sym: (states[sym].exit_reason or "ATR止损触发")
        for sym in stop_exits
        if sym in states
    }

    # ---- 2. 规划 ----
    plan_config: PlannerConfig = PlannerConfig.from_settings(settings)
    plan = rebalance_planner.plan_rebalance(
        account_state, ohlcv, target_weights,
        as_of=as_of, ref_time=ref_time, account_id=str(account_id),
        target_id=target_id,
        pool_can_buy=pool_can_buy, pool_can_sell=pool_can_sell,
        stale_symbols=stale_symbols, cooldown_symbols=cooldown,
        risk_exit_symbols=risk_exits, settings=settings, config=plan_config,
    )
    logger.info("调仓规划 (账户 %s):\n%s", account_id, plan.summary())

    # ---- 3. 建议落库 (幂等 + 替代旧开放建议) ----
    inserted = skipped = replaced = 0
    if plan.proposals:
        ledger = ProposalLedger(db)
        proposals = rebalance_planner.plan_to_ledger_proposals(
            plan, target_id=target_id, account_id=str(account_id),
            account_as_of=account_state.last_event_time,
        )
        inserted, skipped = ledger.create_proposals(proposals)
        # 新计划生效 → 更早的开放建议一律 replaced (spec §3.2: 不继续累计待买;
        # 本次新建的建议不受影响, 因 ID 带当前目标/时点)
        replaced = ledger.mark_stale_replaced(target_id)
        if replaced:
            logger.info("新计划生效: %d 条旧开放建议标记 replaced", replaced)

    return RebalanceRun(
        account_id=str(account_id), plan=plan, stop_exits=list(stop_exits),
        cooldown_symbols=dict(cooldown),
        proposals_inserted=inserted, proposals_skipped=skipped,
        replaced_open=replaced,
    )


def _risk_manager(settings: dict):
    from strategies import risk_manager

    return risk_manager.from_settings(settings)
