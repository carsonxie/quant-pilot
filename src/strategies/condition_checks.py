"""条件检查规则层 (P11-M4; 研究示例 cond-v0.1-research).

输入: fundamental_data 的 extract (股票报表/估值, ETF 资料/快照) + 已取行情;
输出: ConditionItem 列表 —— 状态词表 spec §8.4.6:
  **未触发 / 关注 / 重点复核 / 无法评估** (+ 不适用: 明确原因或暂缓登记)。

纪律:
  - 条件检查与技术分**单列**, 不混入技术分 (spec §8.4.5);
  - 基期为负/缺失 → 不输出误导增长率 ("无法评估" + 原因);
  - 金融企业不套普通工商偿债门槛 (报表结构识别);
  - 净值为负/为负净资产 → 估值不给"便宜"结论;
  - UI 区分数据问题 (无法评估: 接口/缺失) 与市场/财务问题 (关注/重点复核);
  - 阈值全部来自 config/condition_rules.yaml (研究示例, 非硬编码)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import polars as pl
import yaml

logger = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "condition_rules.yaml"

STATUS_OK = "未触发"
STATUS_WATCH = "关注"
STATUS_REVIEW = "重点复核"
STATUS_UNKNOWN = "无法评估"
STATUS_NA = "不适用"

_STATUS_ORDER = [STATUS_REVIEW, STATUS_WATCH, STATUS_UNKNOWN, STATUS_NA, STATUS_OK]


class ConditionRulesError(ValueError):
    """条件规则卡缺失/不完整."""


@dataclass
class ConditionItem:
    item_id: str
    label: str
    status: str
    detail: str
    value: str | None = None
    period: str | None = None        # 财报期 / 数据截止
    published: str | None = None     # 公告日 (适用时)

    def to_dict(self) -> dict:
        return asdict(self)


def load_condition_rules(path: str | Path | None = None) -> dict:
    rules_path = Path(path) if path else DEFAULT_RULES_PATH
    if not rules_path.exists():
        raise ConditionRulesError(f"条件规则卡不存在: {rules_path}")
    with open(rules_path, encoding="utf-8") as f:
        rules = yaml.safe_load(f) or {}
    for key in ("version", "stock", "etf", "deferred"):
        if key not in rules:
            raise ConditionRulesError(f"条件规则卡缺少 {key}")
    return rules


def summarize_items(items: list[ConditionItem]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    parts = [f"{status} {counts[status]}" for status in _STATUS_ORDER if status in counts]
    return " · ".join(parts) if parts else "无检查项"


# ------------------------------------------------------------------
# 派生工具 (纯函数)
# ------------------------------------------------------------------

def _fmt_yi(value: float | None) -> str:
    return f"{value / 1e8:,.2f} 亿" if value is not None else "缺失"


def _periods_map(periods: list[dict]) -> dict[date, dict]:
    return {p["period"]: p for p in periods if p.get("period")}


def _yoy(periods: list[dict], key: str) -> tuple[float | None, float | None, str | None]:
    """最新期同比 (同月日去年基期). Returns (当期值, 同比, 不可比原因)."""
    if not periods:
        return None, None, "无报告期"
    latest = periods[-1]
    current = latest.get(key)
    if current is None:
        return None, None, "当期值缺失"
    base_date = date(latest["period"].year - 1, latest["period"].month, latest["period"].day)
    base = _periods_map(periods).get(base_date)
    if base is None or base.get(key) is None:
        return current, None, "基期缺失"
    base_value = base[key]
    if base_value <= 0:
        return current, None, "基期为负/近零"
    return current, current / base_value - 1.0, None


_QUARTER_END_DAY = {3: 31, 6: 30, 9: 30, 12: 31}


def _quarter_singles(periods: list[dict], key: str) -> list[tuple[date, float]]:
    """把累计口径报表期转成单季值 (同财年相邻季相减; Q1 即累计)."""
    ordered = [p for p in periods if p.get("period") is not None]
    singles: list[tuple[date, float]] = []
    by_date = _periods_map(ordered)
    for p in ordered:
        value = p.get(key)
        if value is None:
            continue
        period = p["period"]
        if period.month == 3:
            singles.append((period, value))
            continue
        prev_month = period.month - 3
        # 上一季末日期取其季度末日 (09-30 等; 不能沿用本月日期, 如 12-31→09-31 非法)
        prev = by_date.get(
            date(period.year, prev_month, _QUARTER_END_DAY[prev_month])
        )
        if prev is None or prev.get(key) is None:
            continue  # 缺期 → 该单季不可派生 (不硬凑)
        singles.append((period, value - prev[key]))
    return singles


def _ttm(periods: list[dict], key: str) -> float | None:
    """最近 4 个单季之和 (连续且完整才给; 否则 None — 缺期不硬凑)."""
    singles = _quarter_singles(periods, key)
    if len(singles) < 4:
        return None
    last_four = singles[-4:]
    # 必须覆盖连续 4 个季度
    for prev, cur in zip(last_four, last_four[1:], strict=False):
        if (cur[0].year - prev[0].year) * 12 + (cur[0].month - prev[0].month) != 3:
            return None
    return sum(v for _, v in last_four)


def _status_from_yoy(yoy: float, watch: float, review: float) -> str:
    if yoy <= review:
        return STATUS_REVIEW
    if yoy <= watch:
        return STATUS_WATCH
    return STATUS_OK


# ------------------------------------------------------------------
# 股票检查
# ------------------------------------------------------------------

def evaluate_stock_conditions(extract: dict, *, rules: dict) -> list[ConditionItem]:
    cfg = rules["stock"]
    periods = extract.get("periods") or []
    items: list[ConditionItem] = []

    if not periods:
        return [ConditionItem(
            item_id="PL-01", label="盈利", status=STATUS_UNKNOWN,
            detail="无财务报表数据 (接口失败或标的无披露): "
                   + "; ".join(extract.get("errors") or ["未知原因"]),
        )]

    latest = periods[-1]
    period_str = str(latest["period"])
    published_str = str(latest.get("announced")) if latest.get("announced") else None

    # PL-01 营收同比
    revenue, revenue_yoy, reason = _yoy(periods, "revenue")
    p_cfg = cfg["profitability"]
    if revenue is None:
        status, detail = STATUS_UNKNOWN, "当期营收缺失 (报表口径问题)"
    elif revenue_yoy is None:
        status, detail = STATUS_UNKNOWN, f"同比不可评估 ({reason}); 当期营收 {_fmt_yi(revenue)}元级"
    else:
        status = _status_from_yoy(
            revenue_yoy, p_cfg["revenue_yoy_watch"], p_cfg["revenue_yoy_review"],
        )
        watch_pct = p_cfg["revenue_yoy_watch"]
        review_pct = p_cfg["revenue_yoy_review"]
        detail = (
            f"当期营收 {_fmt_yi(revenue)}, 同比 {revenue_yoy:+.1%} "
            f"(研究示例阈值 {watch_pct:.0%}/{review_pct:.0%})"
        )
    items.append(ConditionItem(
        item_id="PL-01", label="营收同比", status=status, detail=detail,
        value=f"{_fmt_yi(revenue)}" if revenue is not None else None,
        period=period_str, published=published_str,
    ))

    # PL-02 归母净利 (+扣非结构提示)
    net_profit, np_yoy, np_reason = _yoy(periods, "net_profit")
    deducted = latest.get("deducted_net_profit")
    if net_profit is None:
        status, detail = STATUS_UNKNOWN, "当期归母净利缺失"
    elif net_profit < 0:
        status = STATUS_REVIEW
        detail = f"当期归母净利为负 ({_fmt_yi(net_profit)}) — 亏损, 不输出增长率与估值结论"
    elif np_yoy is None:
        status = STATUS_UNKNOWN
        detail = f"同比不可评估 ({np_reason}); 当期归母净利 {_fmt_yi(net_profit)}"
    else:
        status = _status_from_yoy(
            np_yoy, p_cfg["net_profit_yoy_watch"], p_cfg["net_profit_yoy_review"],
        )
        detail = f"当期归母净利 {_fmt_yi(net_profit)}, 同比 {np_yoy:+.1%}"
        if deducted is not None and deducted < 0 and p_cfg.get("deducted_negative_watch"):
            if status == STATUS_OK:
                status = STATUS_WATCH
            detail += "; 扣非净利为负 — 利润主要来自非经常性损益 (关注)"
    items.append(ConditionItem(
        item_id="PL-02", label="归母净利同比", status=status, detail=detail,
        value=f"{_fmt_yi(net_profit)}" if net_profit is not None else None,
        period=period_str, published=published_str,
    ))

    # PL-03 TTM (单季合成)
    ttm_np = _ttm(periods, "net_profit")
    ttm_ded = _ttm(periods, "deducted_net_profit")
    if ttm_np is None and ttm_ded is None:
        status, detail = STATUS_UNKNOWN, "TTM 不可派生 (单季缺期, 不做硬凑)"
    else:
        status = STATUS_OK
        detail = f"TTM 归母净利 {_fmt_yi(ttm_np)}" + (
            f"; TTM 扣非 {_fmt_yi(ttm_ded)}" if ttm_ded is not None else "; TTM 扣非缺失"
        )
    items.append(ConditionItem(
        item_id="PL-03", label="TTM 盈利", status=status, detail=detail,
        period=period_str, published=published_str,
    ))

    # CF-01 经营现金流 / 净利 (同期累计)
    cf_cfg = cfg["cashflow"]
    ocf = latest.get("ocf")
    if ocf is None:
        status, detail = STATUS_UNKNOWN, "当期经营现金流缺失 (现金流报表缺期)"
    elif ocf < 0 and cf_cfg.get("ocf_negative_watch"):
        status, detail = STATUS_WATCH, f"当期经营现金流为负 ({_fmt_yi(ocf)})"
    elif net_profit and net_profit > 0:
        ratio = ocf / net_profit
        threshold = cf_cfg["ocf_to_profit_watch"]
        if ratio < threshold:
            status = STATUS_WATCH
            detail = (
                f"经营现金流/归母净利 = {ratio:.2f} < {threshold} "
                f"(利润现金含量偏弱, 单期仅提示)"
            )
        else:
            status = STATUS_OK
            detail = f"经营现金流/归母净利 = {ratio:.2f} (同期累计)"
    else:
        status, detail = STATUS_OK, f"经营现金流 {_fmt_yi(ocf)} (净利非正, 比值不解释)"
    items.append(ConditionItem(
        item_id="CF-01", label="经营现金流质量", status=status, detail=detail,
        value=f"{_fmt_yi(ocf)}" if ocf is not None else None,
        period=period_str, published=published_str,
    ))

    # CF-02 近四个单季 OCF 为负的期数
    singles = _quarter_singles(periods, "ocf")
    recent = singles[-4:]
    if len(recent) < 4:
        status, detail = STATUS_UNKNOWN, "单季经营现金流缺期 (近 4 季不完整, 不硬凑)"
    else:
        negative = sum(1 for _, v in recent if v < 0)
        if negative >= cf_cfg["negative_quarters_watch"]:
            status = STATUS_WATCH
            detail = f"近 4 个单季中经营现金流为负 {negative} 期"
        else:
            status = STATUS_OK
            detail = f"近 4 个单季中经营现金流为负 {negative} 期"
    items.append(ConditionItem(
        item_id="CF-02", label="现金流多期关系", status=status, detail=detail,
        period=period_str,
    ))

    # SV-01 资产负债率
    sv_cfg = cfg["solvency"]
    if extract.get("is_financial") and sv_cfg.get("financial_company_na"):
        items.append(ConditionItem(
            item_id="SV-01", label="资产负债率", status=STATUS_NA,
            detail="金融企业不套普通工商企业门槛 (按报表结构识别: 净利息收入口径)",
            period=period_str,
        ))
        items.append(ConditionItem(
            item_id="SV-02", label="短期偿债覆盖", status=STATUS_NA,
            detail="金融企业不套普通工商企业门槛",
            period=period_str,
        ))
    else:
        assets = latest.get("total_assets")
        liabilities = latest.get("total_liabilities")
        if assets and assets > 0 and liabilities is not None:
            ratio = liabilities / assets
            if ratio >= sv_cfg["debt_ratio_review"]:
                status = STATUS_REVIEW
            elif ratio >= sv_cfg["debt_ratio_watch"]:
                status = STATUS_WATCH
            else:
                status = STATUS_OK
            detail = (
                f"资产负债率 {ratio:.1%} (负债 {_fmt_yi(liabilities)} / 资产 {_fmt_yi(assets)})"
            )
        else:
            status, detail = STATUS_UNKNOWN, "资产/负债口径缺失"
        items.append(ConditionItem(
            item_id="SV-01", label="资产负债率", status=status, detail=detail,
            period=period_str, published=published_str,
        ))

        cash = latest.get("cash")
        short_debt = latest.get("short_debt")
        if short_debt is None:
            status, detail = STATUS_UNKNOWN, "短期有息负债字段缺失 (口径不可用, 不作近似)"
        elif short_debt <= 0:
            status, detail = STATUS_OK, "按可得字段无短期有息负债"
        elif cash is None:
            status, detail = STATUS_UNKNOWN, "货币资金缺失, 覆盖率无法计算"
        else:
            cover = cash / short_debt
            if cover < sv_cfg["short_debt_cover_review"]:
                status = STATUS_REVIEW
            elif cover < sv_cfg["short_debt_cover_watch"]:
                status = STATUS_WATCH
            else:
                status = STATUS_OK
            detail = (
                f"货币资金 {_fmt_yi(cash)} / 短期有息负债 {_fmt_yi(short_debt)} "
                f"= {cover:.2f} (近似口径: 短期借款+一年内到期)"
            )
        items.append(ConditionItem(
            item_id="SV-02", label="短期偿债覆盖", status=status, detail=detail,
            period=period_str, published=published_str,
        ))

    # SV-03 归母净资产
    equity = latest.get("equity_parent")
    if equity is None:
        status, detail = STATUS_UNKNOWN, "归母净资产缺失"
    elif equity <= 0:
        status, detail = STATUS_REVIEW, f"归母净资产为负 ({_fmt_yi(equity)})"
    else:
        status, detail = STATUS_OK, f"归母净资产 {_fmt_yi(equity)}"
    items.append(ConditionItem(
        item_id="SV-03", label="净资产", status=status, detail=detail,
        period=period_str, published=published_str,
    ))

    # VAL-01/02 估值
    val_cfg = cfg["valuation"]
    valuation = extract.get("valuation")
    if not valuation:
        for item_id, label in (("VAL-01", "PE-TTM"), ("VAL-02", "PB")):
            items.append(ConditionItem(
                item_id=item_id, label=label, status=STATUS_UNKNOWN,
                detail="估值数据缺失 (stock_value_em 失败或为空)",
            ))
        return items

    val_date = str(valuation.get("date")) if valuation.get("date") else None
    items.append(_valuation_item(
        "VAL-01", "PE-TTM", valuation.get("pe_ttm"), valuation.get("pe_percentile"),
        val_cfg, val_date, is_pe=True,
    ))
    items.append(_valuation_item(
        "VAL-02", "PB", valuation.get("pb"), valuation.get("pb_percentile"),
        val_cfg, val_date, is_pe=False,
    ))
    return items


def _valuation_item(
    item_id: str, label: str, value: float | None, percentile: float | None,
    val_cfg: dict, val_date: str | None, *, is_pe: bool,
) -> ConditionItem:
    if value is None:
        return ConditionItem(
            item_id=item_id, label=label, status=STATUS_UNKNOWN,
            detail=f"{label} 缺失", period=val_date,
        )
    if value <= 0:
        note = "亏损, 不解释为便宜" if is_pe else "净资产为负, 不解释为便宜"
        return ConditionItem(
            item_id=item_id, label=label, status=STATUS_UNKNOWN,
            detail=f"{label} = {value:.2f} ({note})", value=f"{value:.2f}", period=val_date,
        )
    threshold_key = "pe_percentile_watch" if is_pe else "pb_percentile_watch"
    threshold = val_cfg[threshold_key]
    if percentile is not None and percentile >= threshold:
        status = STATUS_WATCH
        detail = (
            f"{label} = {value:.2f}, 处于自身历史 {percentile:.0%} 分位 "
            f"(≥{threshold:.0%}, 研究示例; 窗口约 5 年)"
        )
    else:
        status = STATUS_OK
        detail = f"{label} = {value:.2f}" + (
            f", 历史分位 {percentile:.0%}" if percentile is not None
            else " (历史分位样本不足, 仅展示数值)"
        )
    return ConditionItem(
        item_id=item_id, label=label, status=status, detail=detail,
        value=f"{value:.2f}", period=val_date,
    )


# ------------------------------------------------------------------
# ETF 检查
# ------------------------------------------------------------------

def evaluate_etf_conditions(
    extract: dict,
    *,
    prices: pl.DataFrame,
    last_completed: date | None,
    market_type: str | None,
    rules: dict,
) -> list[ConditionItem]:
    cfg = rules["etf"]
    deferred = rules["deferred"]
    items: list[ConditionItem] = []
    profile = extract.get("profile")
    errors = extract.get("errors") or []

    # EP-01 身份与资料
    if profile:
        status = STATUS_OK
        detail = (
            f"跟踪标的 {profile.get('track_index') or '未标注'}; "
            f"成立 {profile.get('inception') or '未知'}; 管理人 {profile.get('manager') or '未知'}"
        )
    else:
        status = STATUS_UNKNOWN
        detail = "ETF 资料获取失败: " + ("; ".join(errors) if errors else "未知原因")
    items.append(ConditionItem(
        item_id="EP-01", label="身份/跟踪标的", status=status, detail=detail,
        value=profile.get("track_index") if profile else None,
    ))

    # EP-02 规模 (季度口径)
    scale = (profile or {}).get("scale") or {}
    scale_value = scale.get("value")
    scale_date = scale.get("as_of")
    if scale_value is None:
        status, detail = STATUS_UNKNOWN, "规模字段缺失/未获取"
    else:
        if scale_value < cfg["product"]["scale_yi_review"]:
            status = STATUS_REVIEW
        elif scale_value < cfg["product"]["scale_yi_watch"]:
            status = STATUS_WATCH
        else:
            status = STATUS_OK
        detail = f"净资产规模 {scale_value:.2f} 亿元 (截止 {scale_date}; 季度口径)"
        stale_days = cfg["product"]["scale_stale_days"]
        if scale_date and last_completed and (last_completed - scale_date).days > stale_days:
            detail += f" — 数据已 {(last_completed - scale_date).days} 天未更新"
    items.append(ConditionItem(
        item_id="EP-02", label="规模", status=status, detail=detail,
        value=f"{scale_value:.2f} 亿元" if scale_value is not None else None,
        period=str(scale_date) if scale_date else None,
    ))

    # EP-03 费率
    mgmt = (profile or {}).get("management_fee")
    custody = (profile or {}).get("custody_fee")
    value = None
    if mgmt is None and custody is None:
        status, detail = STATUS_UNKNOWN, "费率字段缺失"
    else:
        total = (mgmt or 0.0) + (custody or 0.0)
        status = (
            STATUS_WATCH if total > cfg["product"]["fee_total_watch"] else STATUS_OK
        )
        if mgmt is not None and custody is not None:
            detail = f"管理费 {mgmt:.2%}/年 + 托管费 {custody:.2%}/年 = 合计 {total:.2%}/年"
        else:
            detail = f"管理费 {mgmt}, 托管费 {custody} (部分缺失; 合计按可得值)"
        value = f"{total:.2%}/年"
    items.append(ConditionItem(
        item_id="EP-03", label="费率", status=status, detail=detail, value=value,
    ))

    # LIQ-01 成交条件 (近 20 日日均成交额)
    amount_20d = None
    if prices is not None and not prices.is_empty() and "amount" in prices.columns:
        recent = prices.sort("date").tail(20)
        if recent.height >= 5:
            amount_20d = float(recent["amount"].mean())
    if amount_20d is None:
        status, detail = STATUS_UNKNOWN, "成交额样本不足 (新上市/停牌)"
    else:
        if amount_20d < cfg["liquidity"]["amount_20d_review"]:
            status = STATUS_REVIEW
        elif amount_20d < cfg["liquidity"]["amount_20d_watch"]:
            status = STATUS_WATCH
        else:
            status = STATUS_OK
        detail = f"近 20 日日均成交额 {_fmt_yi(amount_20d)} (研究示例阈值 3000万/500万)"
    items.append(ConditionItem(
        item_id="LIQ-01", label="成交条件", status=status, detail=detail,
        value=f"{_fmt_yi(amount_20d)}" if amount_20d is not None else None,
    ))

    # LIQ-02 快照买卖价差
    spread = extract.get("spread") or {}
    spread_pct = spread.get("spread_pct")
    if spread_pct is None:
        status, detail = STATUS_UNKNOWN, f"买卖价快照缺失 ({spread.get('note') or '未知'})"
    elif spread_pct > cfg["liquidity"]["spread_watch"]:
        status = STATUS_WATCH
        detail = (
            f"快照买卖价差 {spread_pct:.2%} (买入 {spread.get('bid')} / 卖出 {spread.get('ask')}, "
            f"时点数据, 日线不能伪造价差)"
        )
    else:
        status = STATUS_OK
        detail = f"快照买卖价差 {spread_pct:.2%} (时点)"
    items.append(ConditionItem(
        item_id="LIQ-02", label="买卖价差(快照)", status=status, detail=detail,
        value=f"{spread_pct:.2%}" if spread_pct is not None else None,
    ))

    # PR-01 折溢价 (日期/口径匹配才给精确值)
    premium = extract.get("premium") or {}
    if premium.get("status") != "ok":
        status = STATUS_UNKNOWN
        detail = f"折溢价: {premium.get('note') or '净值缺失/不匹配'}"
    else:
        discount = premium.get("discount_pct")
        status = (
            STATUS_WATCH
            if abs(discount) > cfg["premium"]["watch_abs"]
            else STATUS_OK
        )
        detail = (
            f"净值 {premium.get('nav'):.4f} ({premium.get('nav_date')}) vs 市价 "
            f"{premium.get('price'):.4f}; 折溢价 {discount:+.2%} (正=折价)"
        )
    items.append(ConditionItem(
        item_id="PR-01", label="折溢价", status=status, detail=detail,
        value=None if premium.get("discount_pct") is None else f"{premium['discount_pct']:+.2%}",
        period=str(premium.get("nav_date")) if premium.get("nav_date") else None,
    ))

    # TQ-01 / UV-01 暂缓项
    items.append(ConditionItem(
        item_id="TQ-01", label="跟踪质量", status=STATUS_NA,
        detail=deferred["tracking_quality"],
    ))
    if market_type in ("商品", "债券"):
        uv_detail = f"不适用: {market_type}类 ETF 不套用 PE/PB 口径 (spec §8.4.6)"
    else:
        uv_detail = deferred["underlying_valuation"]
    items.append(ConditionItem(
        item_id="UV-01", label="底层估值", status=STATUS_NA, detail=uv_detail,
    ))
    return items


# ------------------------------------------------------------------
# 序列化 (存入快照 checks_json)
# ------------------------------------------------------------------

def conditions_payload(
    identity_label: str,
    items: list[ConditionItem],
    *,
    version: str,
    notes: list[str] | None = None,
    fetched_at: str | None = None,
) -> dict:
    return {
        "version": version,
        "subject": identity_label,
        "fetched_at": fetched_at,
        "summary": summarize_items(items),
        "items": [item.to_dict() for item in items],
        "notes": notes or [],
    }


def payload_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
