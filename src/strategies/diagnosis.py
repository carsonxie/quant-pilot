"""诊断计分引擎与解释模板 (Phase 1.1, P11-M3-03/04).

输入: 指标行 (factor_engineering.technical.compute_indicators 的最后一行) +
      规则卡 (config/diagnostics.yaml, 研究示例, 冻结版本)。
输出: 分项贡献 / 组上限 / 归一化技术风险分 / 三态(原始档) / 提示 / 解释模板行。

原则 (spec §8.4.5):
  - 计分项全部为必需: 任一缺失 → 总分为空、不出三态 (缺失不视为低风险);
  - 组上限与组合上限显式 (趋势+动量相关 → 合计折减), 同一信息不重复计票;
  - 展示项 (RSI/布林位置/量额比) 只给"关注"提示, 不强行合成方向预测;
  - 分数只是规则下的关注程度, 不是涨跌概率; 研究示例参数不宣称已验证;
  - 解释模板 (tmpl-v0.1): 指标含义→当前值→比较对象→触发规则→结论标签→数据日期/来源,
    只用确定规则与模板, 不调用 LLM。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from factor_engineering.technical import REQUIRED_PARAM_KEYS

logger = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "diagnostics.yaml"

STATUS_NORMAL = "正常"
STATUS_WATCH = "警戒"
STATUS_DEFENSE = "防御"


class RulesConfigError(ValueError):
    """规则卡缺失/不一致 (不静默使用默认值)."""


@dataclass
class ItemScore:
    """单计分项结果."""

    item_id: str
    group: str
    column: str
    value: float | None
    points: float | None          # 贡献 (缺失 → None)
    max_points: float
    triggered: bool
    note: str = ""


@dataclass
class DiagnosisAssessment:
    """单日诊断评估结果 (未含状态机确认; status 由快照层写回)."""

    rules_version: str
    as_of: date | None
    raw_score: float | None           # 折减后原始贡献合计 (0–max_raw)
    score: float | None               # 归一化 0–100
    raw_status: str | None            # 原始档位 (正常/警戒/防御)
    status: str | None                # 确认档位 (初始=raw_status; 状态机覆盖)
    completeness: str                 # 计分项 "n/N"
    items: list[ItemScore] = field(default_factory=list)
    group_points: dict[str, float] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)
    hints: list[str] = field(default_factory=list)
    display: dict[str, float | None] = field(default_factory=dict)
    explanations: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# 规则卡加载与校验
# ------------------------------------------------------------------

def load_diagnostics_rules(path: str | Path | None = None) -> dict:
    """加载并校验规则卡 (缺关键段/参数不齐 → RulesConfigError)."""
    rules_path = Path(path) if path else DEFAULT_RULES_PATH
    if not rules_path.exists():
        raise RulesConfigError(f"规则卡不存在: {rules_path}")
    with open(rules_path, encoding="utf-8") as f:
        rules = yaml.safe_load(f) or {}

    if not rules.get("rules_version"):
        raise RulesConfigError("规则卡缺少 rules_version")
    indicators = rules.get("indicators", {})
    missing = [k for k in REQUIRED_PARAM_KEYS if k not in indicators]
    if missing:
        raise RulesConfigError(f"规则卡 indicators 缺参数: {missing}")
    scoring = rules.get("scoring", {})
    items = scoring.get("items", {})
    if not items:
        raise RulesConfigError("规则卡 scoring.items 为空")
    for item_id, item in items.items():
        for key in ("column", "direction", "max_points", "group", "required"):
            if key not in item:
                raise RulesConfigError(f"计分项 {item_id} 缺字段 {key}")
    if scoring.get("normalization", {}).get("max_raw") in (None, 0):
        raise RulesConfigError("规则卡缺 normalization.max_raw")
    return rules


# ------------------------------------------------------------------
# 分项计分
# ------------------------------------------------------------------

def _item_points(value: float, item: dict) -> float:
    """按方向计分 (0 ≤ points ≤ max_points); 负向风险越深分越高."""
    direction = item["direction"]
    full_scale = float(item["full_scale"]) if "full_scale" in item else None
    if direction in ("below_ma", "negative_slope", "negative_value"):
        risk = max(0.0, -float(value))
        if full_scale is None or full_scale <= 0:
            raise RulesConfigError(f"计分项方向 {direction} 需要 full_scale")
    elif direction == "above_pivot":
        pivot = float(item.get("pivot", 0.0))
        risk = max(0.0, float(value) - pivot)
        if full_scale is None or full_scale <= 0:
            raise RulesConfigError("above_pivot 需要 full_scale")
    else:
        raise RulesConfigError(f"未知计分方向: {direction}")
    ratio = min(1.0, risk / full_scale)
    return ratio * float(item["max_points"])


def _status_from_score(score: float | None, bands: dict) -> str | None:
    if score is None:
        return None
    if score < float(bands["normal_max"]):
        return STATUS_NORMAL
    if score < float(bands["watch_max"]):
        return STATUS_WATCH
    return STATUS_DEFENSE


def _item_explanation(
    item_id: str, item: ItemScore, item_cfg: dict, as_of: date | None,
    source_note: str | None,
) -> str:
    """单计分项解释 (tmpl-v0.1): 含义→当前值→比较→规则→结论→数据日期/来源."""
    direction = item_cfg["direction"]
    full_scale = item_cfg.get("full_scale")
    pivot = item_cfg.get("pivot")
    if direction == "below_ma":
        rule_text = f"低于均线按深度计分 ({float(full_scale):.0%} 深度=满分)"
    elif direction == "negative_slope":
        rule_text = f"均线斜率转负按幅度计分 ({float(full_scale):.0%}=满分)"
    elif direction == "negative_value":
        rule_text = f"负值按幅度计分 (≤-{float(full_scale):.0%}=满分)"
    else:
        rule_text = f"高于 {float(pivot):g} 起计分 (高出 {float(full_scale):g}=满分)"
    if item.value is None:
        reason = "该项缺失/不可评估 → 总分为空 (缺失不视为低风险)"
        return f"{item_id}: {reason}"
    date_text = f"数据日期 {as_of.isoformat()}" if as_of else "数据日期未知"
    if source_note:
        date_text += f" ({source_note})"
    return (
        f"{item_id}: 当前值 {item.value:.4g} (比较对象: {rule_text}); "
        f"结论: {'触发' if item.triggered else '未触发'}, "
        f"贡献 {item.points:.1f}/{item.max_points:g}; {date_text}。"
    )


def _display_hints(display: dict[str, float | None], rules: dict) -> list[str]:
    """展示项提示 (关注语义, 非方向判断)."""
    hints: list[str] = []
    cfg = rules.get("display_items", {}) or {}
    rsi_cfg = cfg.get("rsi14", {})
    rsi_val = display.get("rsi14")
    if rsi_val is not None:
        if rsi_val >= float(rsi_cfg.get("overbought", 70)):
            hints.append(
                f"RSI14={rsi_val:.1f} ≥ {rsi_cfg.get('overbought', 70)}: "
                "超买区提示 (关注, 非卖出建议)"
            )
        elif rsi_val <= float(rsi_cfg.get("oversold", 30)):
            hints.append(
                f"RSI14={rsi_val:.1f} ≤ {rsi_cfg.get('oversold', 30)}: "
                "超卖区提示 (关注, 非买入建议)"
            )
    boll_cfg = cfg.get("boll_pos", {})
    pos = display.get("boll_pos")
    if pos is not None:
        if pos > float(boll_cfg.get("upper_hint", 1.0)):
            hints.append(f"价格在上轨之上 (布林位置 {pos:.2f}): 短期过热提示")
        elif pos < float(boll_cfg.get("lower_hint", 0.0)):
            hints.append(f"价格在下轨之下 (布林位置 {pos:.2f}): 短期走弱提示")
    amt_cfg = cfg.get("amt_ratio", {})
    amt = display.get("amt_ratio")
    if amt is not None:
        if amt >= float(amt_cfg.get("high_hint", 2.0)):
            hints.append(
                f"量额比 {amt:.2f} ≥ {amt_cfg.get('high_hint', 2.0)}: "
                "异常放量 (方向未知, 仅提示)"
            )
        elif amt <= float(amt_cfg.get("low_hint", 0.5)):
            hints.append(f"量额比 {amt:.2f} ≤ {amt_cfg.get('low_hint', 0.5)}: 显著缩量")
    return hints


# ------------------------------------------------------------------
# 主评估入口
# ------------------------------------------------------------------

def assess_row(
    row: dict,
    rules: dict,
    *,
    as_of: date | None = None,
    source_note: str | None = None,
    unavailable: dict[str, str] | None = None,
) -> DiagnosisAssessment:
    """对指标行 (dict) 执行计分与解释 (无状态; 状态机在快照层)."""
    unavailable = unavailable or {}
    scoring = rules["scoring"]
    bands = scoring["bands"]
    items_cfg: dict = scoring["items"]

    items: list[ItemScore] = []
    missing_required: list[str] = []
    per_item_points: dict[str, float] = {}

    for item_id, item_cfg in items_cfg.items():
        column = item_cfg["column"]
        reason = unavailable.get(column)
        value = row.get(column)
        if reason is not None or value is None:
            note = reason or "指标缺失"
            item = ItemScore(
                item_id=item_id, group=item_cfg["group"], column=column,
                value=None, points=None, max_points=float(item_cfg["max_points"]),
                triggered=False, note=note,
            )
            items.append(item)
            if item_cfg.get("required", True):
                missing_required.append(f"{column}: {note}")
            continue
        points = _item_points(float(value), item_cfg)
        per_item_points[item_id] = points
        items.append(ItemScore(
            item_id=item_id, group=item_cfg["group"], column=column,
            value=float(value), points=points,
            max_points=float(item_cfg["max_points"]),
            triggered=points > 0,
        ))

    # ---- 组贡献 (组上限) ----
    group_points: dict[str, float] = {}
    for item in items:
        if item.points is None:
            continue
        group_points[item.group] = group_points.get(item.group, 0.0) + item.points
    group_caps = scoring.get("group_caps", {}) or {}
    for group, cap in group_caps.items():
        if group in group_points:
            group_points[group] = min(group_points[group], float(cap))

    # ---- 组合上限 (比例折减, 保持项级可追溯) ----
    combined_notes: list[str] = []
    for combined in scoring.get("combined_caps", []) or []:
        groups = list(combined["groups"])
        cap = float(combined["cap"])
        total = sum(group_points.get(g, 0.0) for g in groups)
        if total > cap and total > 0:
            scale = cap / total
            for g in groups:
                if g in group_points:
                    group_points[g] *= scale
            combined_notes.append(
                f"组合上限折减: {'+'.join(groups)} 合计 {total:.1f} → {cap:g} (×{scale:.2f})"
            )

    raw_score = sum(group_points.values())
    max_raw = float(scoring["normalization"]["max_raw"])
    complete = not missing_required
    if complete:
        score = round(min(raw_score, max_raw) / max_raw * 100.0, 1)
        raw_status = _status_from_score(score, bands)
    else:
        score = None
        raw_status = None

    counted = sum(1 for i in items if i.points is not None)
    completeness = f"{counted}/{len(items)}"

    # ---- 展示项与提示 (展示项与计分完整性无关, 始终给出) ----
    display_cfg = rules.get("display_items", {}) or {}
    display = {}
    for column in display_cfg:
        raw = row.get(column)
        display[column] = (
            float(raw) if raw is not None and column not in unavailable else None
        )
    hints = _display_hints(display, rules)

    # ---- 解释模板 ----
    explanations: list[str] = []
    for item in items:
        explanations.append(
            _item_explanation(
                item.item_id, item, items_cfg[item.item_id], as_of, source_note,
            )
        )
    explanations.extend(combined_notes)
    if not complete:
        explanations.append(
            "结论: 必需计分项缺失 → 总分为空, 不出三态 (数据完整后用同一版本重算)。"
        )
    else:
        explanations.append(
            f"结论: 技术风险分 {score:.1f}/100 → 原始档位 {raw_status} "
            f"(分档为研究示例; 确认档位由状态机按连续条件给出)。"
        )

    return DiagnosisAssessment(
        rules_version=rules["rules_version"],
        as_of=as_of,
        raw_score=round(raw_score, 2) if complete else None,
        score=score,
        raw_status=raw_status,
        status=raw_status,           # 初始=原始档; 服务层状态机覆盖
        completeness=completeness,
        items=items,
        group_points={k: round(v, 2) for k, v in group_points.items()},
        missing_required=missing_required,
        unavailable=dict(unavailable),
        hints=hints,
        display=display,
        explanations=explanations,
    )
