"""建议与执行反馈账本 (M2b) — 四类记录之 TradeProposal / ExecutionFeedback.

契约 (spec §3.2):
  - 建议引用 target 与账户状态; 一条建议可关联多笔反馈 (部分成交).
  - 建议被新目标替代或过期后不继续累计为待买数量 (status: replaced / expired).
  - 部分成交 / 未执行 / 拒绝 / 延迟均可记录并保留原因; 重复导入按 feedback_id 去重.
  - 成交类反馈 (filled / partial) 可联动账户账本: 以确定性 event_id 生成 trade 事件,
    同一反馈不会重复入账 (apply_to_ledger=True, 默认).
    ⚠️ 若成交已通过 account-import 直接入账, 必须使用 apply_to_ledger=False 以免重复计账.
  - 超额或非系统交易保留真实事实并告警, 不强行截成建议量.
  - 用户"已查看"不是成交: 只有 filled/partial 反馈才可能触发账本更新.

状态机 (trade_proposals.status):
  proposed → partially_filled → filled
  proposed / partially_filled → replaced (新目标生效) / expired (超过有效期) / rejected
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

import polars as pl

from data_pipeline.accounting import AccountError, AccountEvent, AccountService
from data_pipeline.duckdb_manager import DuckDBManager

PROPOSAL_STATUSES = (
    "proposed", "partially_filled", "filled", "expired", "replaced", "rejected",
)
FEEDBACK_STATUSES = ("filled", "partial", "rejected", "delayed", "not_executed")


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class Proposal:
    """交易建议 (引用 target 与账户状态; 建议价非成交价)."""

    proposal_id: str
    symbol: str
    side: str                       # BUY / SELL
    shares: float
    target_id: str | None = None
    account_id: str | None = None
    account_as_of: datetime | None = None
    ref_price: float | None = None
    ref_time: datetime | None = None
    est_amount: float | None = None
    est_fee: float | None = None
    target_weight: float | None = None
    actual_weight: float | None = None
    valid_until: datetime | None = None
    priority: int = 0
    depends_on: str | None = None
    status: str = "proposed"
    reason: str | None = None


@dataclass
class Feedback:
    """执行反馈 (成交/部分/拒绝/延迟/未执行; 允许标记非系统交易)."""

    feedback_id: str
    feedback_time: datetime
    status: str                     # filled / partial / rejected / delayed / not_executed
    proposal_id: str | None = None  # None = 非系统交易
    account_id: str | None = None
    trade_id: str | None = None     # 成交编号 (去重/对账)
    shares: float | None = None
    price: float | None = None
    fee: float | None = None
    source: str = "manual"
    reason: str | None = None
    symbol: str | None = None       # 非系统交易入账时使用
    side: str | None = None


@dataclass
class FeedbackResult:
    """反馈记录结果 (inserted=False 表示重复反馈已忽略)."""

    feedback_id: str
    inserted: bool
    proposal_status: str | None = None
    account_event_id: str | None = None
    warnings: list[str] = field(default_factory=list)


def make_proposal_id(
    target_id: str | None, symbol: str, side: str, seq: int = 0,
) -> str:
    """确定性建议 ID (同一目标同一标的同一方向不重复生成)."""
    digest = hashlib.sha1(
        f"{target_id}|{symbol}|{side}|{seq}".encode(),
    ).hexdigest()
    return f"P-{digest[:16]}"


_PROPOSAL_SCHEMA = {
    "proposal_id": pl.Utf8, "target_id": pl.Utf8, "account_id": pl.Utf8,
    "account_as_of": pl.Datetime("us"), "symbol": pl.Utf8, "side": pl.Utf8,
    "shares": pl.Float64, "ref_price": pl.Float64, "ref_time": pl.Datetime("us"),
    "est_amount": pl.Float64, "est_fee": pl.Float64,
    "target_weight": pl.Float64, "actual_weight": pl.Float64,
    "valid_until": pl.Datetime("us"), "priority": pl.Int64,
    "depends_on": pl.Utf8, "status": pl.Utf8, "reason": pl.Utf8,
}

_FEEDBACK_SCHEMA = {
    "feedback_id": pl.Utf8, "proposal_id": pl.Utf8, "account_id": pl.Utf8,
    "trade_id": pl.Utf8, "feedback_time": pl.Datetime("us"),
    "status": pl.Utf8, "shares": pl.Float64, "price": pl.Float64,
    "fee": pl.Float64, "source": pl.Utf8, "reason": pl.Utf8,
}


def proposals_to_dataframe(proposals: list[Proposal]) -> pl.DataFrame:
    """建议列表 → DataFrame (与 trade_proposals 表列对应)."""
    rows = [
        {
            "proposal_id": p.proposal_id, "target_id": p.target_id,
            "account_id": p.account_id, "account_as_of": p.account_as_of,
            "symbol": p.symbol, "side": p.side, "shares": p.shares,
            "ref_price": p.ref_price, "ref_time": p.ref_time,
            "est_amount": p.est_amount, "est_fee": p.est_fee,
            "target_weight": p.target_weight, "actual_weight": p.actual_weight,
            "valid_until": p.valid_until, "priority": p.priority,
            "depends_on": p.depends_on, "status": p.status, "reason": p.reason,
        }
        for p in proposals
    ]
    return pl.DataFrame(rows, schema=_PROPOSAL_SCHEMA)


def feedback_to_dataframe(feedbacks: list[Feedback]) -> pl.DataFrame:
    """反馈列表 → DataFrame (与 execution_feedback 表列对应)."""
    rows = [
        {
            "feedback_id": fb.feedback_id, "proposal_id": fb.proposal_id,
            "account_id": fb.account_id, "trade_id": fb.trade_id,
            "feedback_time": fb.feedback_time, "status": fb.status,
            "shares": fb.shares, "price": fb.price, "fee": fb.fee,
            "source": fb.source, "reason": fb.reason,
        }
        for fb in feedbacks
    ]
    return pl.DataFrame(rows, schema=_FEEDBACK_SCHEMA)


# ------------------------------------------------------------------
# 服务
# ------------------------------------------------------------------

class ProposalLedger:
    """建议与反馈账本 (write path: 建议落库 → 反馈 → 状态机 → 账户联动)."""

    def __init__(self, db: DuckDBManager) -> None:
        self.db = db
        self.accounts = AccountService(db)

    # ---- 建议 ----

    def create_proposals(self, proposals: list[Proposal]) -> tuple[int, int]:
        """写入建议 (proposal_id 去重). Returns: (inserted, skipped)."""
        for p in proposals:
            if p.status not in PROPOSAL_STATUSES:
                raise AccountError(f"未知建议状态: {p.status!r}")
            if p.side not in ("BUY", "SELL"):
                raise AccountError(f"建议方向必须为 BUY/SELL: {p.side!r}")
            if p.shares <= 0:
                raise AccountError("建议份额必须为正")
        df = proposals_to_dataframe(proposals)
        with self.db.transaction():
            return self.db.insert_proposals(df)

    def mark_target_replaced(self, new_target_id: str) -> int:
        """新目标生效: 引用旧目标且开放的建议 → replaced (不继续累计待买)."""
        with self.db.transaction():
            return self.db.mark_targets_replaced(new_target_id)

    def expire_open_proposals(self, now: datetime) -> int:
        """超过 valid_until 的开放建议 → expired."""
        df = self.db.get_open_proposals()
        if df.is_empty():
            return 0
        expired = 0
        for row in df.iter_rows(named=True):
            valid_until = row.get("valid_until")
            if valid_until is not None and valid_until < now:
                self.db.update_proposal_status(row["proposal_id"], "expired")
                expired += 1
        return expired

    # ---- 反馈 ----

    def record_feedback(
        self,
        fb: Feedback,
        apply_to_ledger: bool = True,
    ) -> FeedbackResult:
        """记录执行反馈 (幂等) 并联动建议状态与账户账本.

        Args:
            fb: 反馈记录 (feedback_id 为稳定去重标识)
            apply_to_ledger: True 时, filled/partial 且带价格/标的/账户的反馈
                会生成账户 trade 事件入账; 若成交已由 account-import 直接录入,
                必须传 False 以免重复计账.

        Raises:
            AccountError: 反馈字段非法 (未知状态等); 重复反馈返回 inserted=False.
        """
        if fb.status not in FEEDBACK_STATUSES:
            raise AccountError(f"未知反馈状态: {fb.status!r} (支持: {FEEDBACK_STATUSES})")

        warnings: list[str] = []
        with self.db.transaction():
            inserted, _skipped = self.db.insert_feedback(
                feedback_to_dataframe([fb]),
            )
        if inserted == 0:
            return FeedbackResult(
                feedback_id=fb.feedback_id,
                inserted=False,
                warnings=["重复反馈 (feedback_id 已存在), 已忽略"],
            )

        proposal = self.db.get_proposal(fb.proposal_id) if fb.proposal_id else None
        if fb.proposal_id and proposal is None:
            warnings.append(f"建议 {fb.proposal_id} 不存在; 反馈已保留")

        # 账户账本联动 (成交类反馈)
        account_event_id = None
        if (
            apply_to_ledger
            and fb.status in ("filled", "partial")
            and (fb.shares or 0) > 0
        ):
            try:
                account_event_id = self._apply_to_ledger(fb, proposal, warnings)
            except AccountError as exc:
                # 反馈是真实事实: 保留反馈, 账本未入账仅告警 (不强行截断/改写)
                warnings.append(f"账本未入账: {exc}")

        # 建议状态推进
        proposal_status = None
        if proposal is not None:
            proposal_status = self._advance_status(proposal, fb, warnings)
            if proposal_status and proposal_status != proposal["status"]:
                with self.db.transaction():
                    self.db.update_proposal_status(fb.proposal_id, proposal_status)

        return FeedbackResult(
            feedback_id=fb.feedback_id,
            inserted=True,
            proposal_status=proposal_status,
            account_event_id=account_event_id,
            warnings=warnings,
        )

    def _apply_to_ledger(
        self,
        fb: Feedback,
        proposal: dict | None,
        warnings: list[str],
    ) -> str | None:
        """成交类反馈 → 账户 trade 事件 (确定性 event_id, 不重复入账)."""
        symbol = fb.symbol or (proposal or {}).get("symbol")
        side = fb.side or (proposal or {}).get("side")
        account_id = fb.account_id or (proposal or {}).get("account_id")
        if not (symbol and side and account_id):
            warnings.append("缺少 symbol/side/account, 未入账 (仅记录反馈)")
            return None
        if not fb.price or fb.price <= 0:
            warnings.append("缺少成交价格, 未入账 (仅记录反馈)")
            return None

        event = AccountEvent(
            event_id=f"{fb.feedback_id}:trade",
            account_id=account_id,
            event_type="trade",
            event_time=fb.feedback_time,
            symbol=str(symbol),
            side=str(side),
            shares=float(fb.shares),
            price=float(fb.price),
            fee=float(fb.fee or 0.0),
            source="feedback",
            reference=fb.trade_id or fb.feedback_id,
            note=f"执行反馈 {fb.feedback_id}",
        )
        with self.db.transaction():
            self.accounts.import_events([event], manage_transaction=False)
        return event.event_id

    def _advance_status(
        self,
        proposal: dict,
        fb: Feedback,
        warnings: list[str],
    ) -> str | None:
        """按反馈推进建议状态 (超额保留事实并告警)."""
        current = proposal["status"]
        if fb.status == "rejected":
            return "rejected"
        if fb.status in ("delayed", "not_executed"):
            return current  # 未执行/延迟不累计成交, 状态保持
        if fb.status in ("filled", "partial"):
            filled = self._filled_shares(str(fb.proposal_id))
            proposed = float(proposal["shares"] or 0)
            if proposed > 0 and filled > proposed + 1e-9:
                warnings.append(
                    f"超额成交: 累计 {filled:.0f} > 建议 {proposed:.0f}, "
                    f"保留真实事实不截断",
                )
            if proposed > 0 and filled + 1e-9 >= proposed:
                return "filled"
            return "partially_filled"
        return current

    def _filled_shares(self, proposal_id: str) -> float:
        """某建议累计成交份额 (filled + partial)."""
        df = self.db.get_feedback_for_proposal(proposal_id)
        if df.is_empty():
            return 0.0
        total = 0.0
        for row in df.iter_rows(named=True):
            if row["status"] in ("filled", "partial"):
                total += float(row["shares"] or 0.0)
        return total
