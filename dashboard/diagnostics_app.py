"""Phase 1.1 单标的诊断渲染层 (P11-M5-02/03/04/05/06).

同一 Streamlit 入口的"单标的诊断"模块; 渲染与数据分离:
  - 读取/装配: `dashboard.diagnostics_data` (只读连接, 可单测)
  - 运行/名单: `orchestration.diagnosis_service` (受控写入口)

页面: 我的持仓 / 观察名单 / 探索 → 详情 (评分卡 / 问题清单 / 六个月日K / 指标 / 解释)。
刷新: 打开详情先读快照; 过期或缺失时受控自动更新 (会话内冷却去抖);
手动刷新走 force; 失败保留旧结果并显示原因 (休市与接口失败区分)。
边界: 不生成买入/卖出/候选入场结论; 风险状态仅为 正常/警戒/防御。
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from dashboard.data_loader import _read_only, load_symbol_names
from dashboard.diagnostics_data import (
    DISPLAY_MONTHS,
    build_kline_frame,
    build_list_rows,
    load_holdings,
    load_snapshot_details,
    load_watchlist_rows,
    read_symbol_prices,
)
from data_pipeline.diag_data import last_completed_trading_day, load_trading_calendar
from data_pipeline.duckdb_manager import DuckDBManager
from data_pipeline.securities import UnknownSymbolError, resolver_from_settings
from orchestration.diagnosis_service import (
    get_snapshot_payload,
    run_diagnosis,
    watchlist_add,
    watchlist_remove,
    watchlist_update_note,
)

AUTO_RUN_COOLDOWN_SECONDS = 300  # 同一标的自动更新最小间隔 (会话内去抖)

MODE_CN = {"real": "真实账户", "paper": "纸上账户", "backtest": "模拟/回测"}


# ------------------------------------------------------------------
# 小工具 (读写入口 / 快照读取)
# ------------------------------------------------------------------

def _rw_db(db_path: str) -> DuckDBManager:
    """受控写入口 (短生命周期; 与 daily 等冲突时由 DuckDB 显式报错)."""
    return DuckDBManager(db_path)


def _read_payload(db_path: str, symbol: str) -> dict | None:
    if not Path(db_path).exists():
        return None
    db = _read_only(db_path)
    try:
        return get_snapshot_payload(db, symbol)
    finally:
        db.close()


def _expected_last_trading_day(db_path: str) -> date | None:
    """最近已完成交易日 (日历进程内缓存; 失败回退池内日期并集)."""
    db = None
    try:
        if Path(db_path).exists():
            db = _read_only(db_path)
        calendar = load_trading_calendar(db=db)
        return last_completed_trading_day(calendar)
    except Exception:  # 日历不可用不阻断页面 (新鲜度仅作提示)
        return None
    finally:
        if db is not None:
            db.close()


def _run_and_handle(
    db_path: str, symbol: str, *, refresh: str, settings: dict,
) -> tuple[bool, str | None]:
    """执行诊断 (受控写); 失败返回 (False, 原因), 不抛出到 UI."""
    db = _rw_db(db_path)
    try:
        run_diagnosis(db, symbol, refresh=refresh, settings=settings)
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        db.close()


def _auto_run_if_needed(db_path: str, symbol: str, payload: dict | None, settings: dict) -> None:
    """过期/缺失时自动受控更新 (会话内冷却, 避免重绘重复抓取)."""
    expected = _expected_last_trading_day(db_path)
    needs_update = payload is None or (
        expected is not None and payload.get("as_of") is not None
        and payload["as_of"] < expected
    )
    if not needs_update:
        return
    last_run = st.session_state.get("diag_auto_run", {}).get(symbol, 0.0)
    if time.time() - last_run < AUTO_RUN_COOLDOWN_SECONDS:
        return
    with st.spinner("正在计算/更新诊断 (受控更新, 数据将写入诊断缓存)..."):
        ok, error = _run_and_handle(db_path, symbol, refresh="auto", settings=settings)
    st.session_state.setdefault("diag_auto_run", {})[symbol] = time.time()
    if not ok:
        st.warning(f"自动更新失败 (保留上次结果): {error}")
    else:
        st.rerun()


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------

def render_diagnostics(settings: dict, db_path: str) -> None:
    st.subheader("🔍 单标的诊断（研究规则 · 非交易建议）")
    st.caption(
        "指标与评分为版本化研究规则（diag-v0.1-research）下的关注程度，"
        "不是涨跌概率，也不构成买入/卖出建议；状态仅为 正常/警戒/防御。"
    )
    view = st.radio(
        "视角", ["我的持仓", "观察名单", "探索"], horizontal=True, key="diag_view",
    )
    names = load_symbol_names(settings)
    selected: str | None = None
    if view == "我的持仓":
        selected = _render_holdings(settings, db_path, names)
    elif view == "观察名单":
        selected = _render_watchlist(settings, db_path, names)
    else:
        selected = _render_explore(settings, db_path)
    if selected:
        _render_detail(settings, db_path, selected)


# ------------------------------------------------------------------
# 我的持仓 (P11-M5-02)
# ------------------------------------------------------------------

def _render_holdings(settings: dict, db_path: str, names: dict[str, str]) -> str | None:
    account_id = str((settings.get("rebalancing", {}) or {}).get("account") or "")
    if not account_id:
        st.info(
            "未配置账户（settings.rebalancing.account 为空）。"
            "录入账户后可在此查看持仓诊断：`uv run account-import --account REAL-1 "
            "--name \"项目账户\" --mode real --csv events.csv`。"
        )
        return None
    holdings = load_holdings(db_path, account_id, settings)
    if not holdings["ok"]:
        st.info(f"账户 {account_id}: {holdings['reason']}")
        return None

    mode_label = MODE_CN.get(holdings["mode"], holdings["mode"])
    st.caption(
        f"账户 {account_id}（{mode_label}）· 持仓估值合计 {holdings['total']:,.0f} 元 "
        f"（含现金 {holdings['cash']:,.0f} 元）· 实际权重按最近可用收盘价估值"
    )
    if not holdings["rows"]:
        st.info("账户当前无 ETF 持仓（现金状态）。")
        return None
    frame = pd.DataFrame([
        {
            "代码": row["symbol"],
            "名称": row["name"],
            "份额": f"{row['shares']:,.0f}",
            "实际权重": f"{row['weight']:.1%}" if row["weight"] is not None else "-",
            "估值日": row["price_date"] or "无价格",
        }
        for row in holdings["rows"]
    ])
    st.dataframe(frame, width="stretch", hide_index=True)
    options = [row["symbol"] for row in holdings["rows"]]
    chosen = st.selectbox(
        "选择标的查看诊断", options, key="diag_holdings_pick",
        format_func=lambda s: f"{s} {names.get(s, '')}",
    )
    return chosen


# ------------------------------------------------------------------
# 观察名单 (P11-M5-01 的 UI 侧)
# ------------------------------------------------------------------

def _render_watchlist(settings: dict, db_path: str, names: dict[str, str]) -> str | None:
    watchlist = load_watchlist_rows(db_path)
    if watchlist.is_empty():
        st.info("观察名单为空。在「探索」中查询证券后可加入观察名单（加入不改变策略池）。")
        return None

    symbols = watchlist["symbol"].to_list()
    details = load_snapshot_details(db_path, symbols)
    entries = [
        {"symbol": row["symbol"], "name": row.get("name") or names.get(row["symbol"], ""),
         "source": "观察"}
        for row in watchlist.iter_rows(named=True)
    ]
    rows = build_list_rows(entries, details)
    if not rows.is_empty():
        st.dataframe(
            rows.to_pandas(), width="stretch", hide_index=True,
            column_config={
                "分数": st.column_config.NumberColumn("分数", format="%.1f"),
            },
        )

    symbol_options = [row["symbol"] for row in watchlist.iter_rows(named=True)]
    chosen = st.selectbox(
        "选择标的查看诊断", symbol_options, key="diag_watch_pick",
        format_func=lambda s: f"{s} {names.get(s, '')}",
    )

    with st.expander("管理（备注 / 移除）"):
        target = st.selectbox(
            "标的", symbol_options, key="diag_watch_manage_target",
            format_func=lambda s: f"{s} {names.get(s, '')}",
        )
        current_note = watchlist.filter(
            watchlist["symbol"] == target
        )["note"][0] if "note" in watchlist.columns else None
        note = st.text_input("备注", value=current_note or "", key="diag_watch_note")
        col_save, col_remove = st.columns(2)
        if col_save.button("保存备注", key="diag_watch_save"):
            db = _rw_db(db_path)
            try:
                watchlist_update_note(db, target, note)
                st.success("备注已保存")
                st.rerun()
            except Exception as exc:
                st.error(f"保存失败: {exc}")
            finally:
                db.close()
        if col_remove.button("移出观察名单", key="diag_watch_remove"):
            db = _rw_db(db_path)
            try:
                watchlist_remove(db, target, reason="用户在面板移除")
                st.success(f"{target} 已移出（不删除真实持仓/行情/诊断历史）")
                st.rerun()
            except Exception as exc:
                st.error(f"移除失败: {exc}")
            finally:
                db.close()
    return chosen


# ------------------------------------------------------------------
# 探索 (P11-M5-03)
# ------------------------------------------------------------------

def _resolve_candidates(settings: dict, db_path: str, query: str) -> tuple[list, str | None]:
    """解析查询 → (候选身份列表, 错误信息). 不猜测身份."""
    db = _rw_db(db_path)  # 身份解析会缓存名录/身份 (受控写)
    try:
        resolver = resolver_from_settings(db, settings)
        text = query.strip()
        if text.isdigit() and len(text) <= 6:
            try:
                return [resolver.resolve(text)], None
            except UnknownSymbolError as exc:
                return [], str(exc)
        candidates = resolver.search(text, limit=8)
        if not candidates:
            return [], f"未找到名称包含「{text}」的证券（可在名称/代码间切换重试）"
        return candidates, None
    except ValueError as exc:
        return [], str(exc)
    except Exception as exc:
        return [], f"查询失败: {type(exc).__name__}: {exc}"
    finally:
        db.close()


def _render_explore(settings: dict, db_path: str) -> str | None:
    with st.form("diag_explore_form", clear_on_submit=False):
        query = st.text_input(
            "输入证券代码或名称",
            placeholder="如 510300 / 沪深300 / 600519（提交后查询, 非逐键请求）",
        )
        submitted = st.form_submit_button("查询")
    if submitted:
        st.session_state["diag_explore_query"] = query.strip()
    active_query = st.session_state.get("diag_explore_query", "")
    if not active_query:
        st.caption("支持境内上市 A 股与 ETF；不自动加入观察名单。")
        return None

    candidates, error = _resolve_candidates(settings, db_path, active_query)
    if error:
        st.error(error)
        return None

    st.caption("已确认证券身份（详情不自动加入观察名单）:")
    if len(candidates) == 1:
        identity = candidates[0]
    else:
        picked = st.radio(
            "多个匹配，请选择:", candidates, key="diag_explore_pick",
            format_func=lambda c: f"{c.symbol} {c.name}（{c.sec_type}）",
        )
        identity = picked
    st.markdown(
        f"**{identity.name or identity.symbol}** `{identity.symbol}` · "
        f"{identity.exchange} · {'ETF' if identity.sec_type == 'etf' else '股票'} · "
        f"价格口径 {identity.adjustment}"
    )
    if st.button("加入观察名单", key=f"diag_add_watch_{identity.symbol}"):
        db = _rw_db(db_path)
        try:
            watchlist_add(db, identity.symbol, settings=settings)
            st.success(f"{identity.symbol} 已加入观察名单（不改变策略可交易池）")
        except Exception as exc:
            st.error(f"加入失败: {exc}")
        finally:
            db.close()
    return identity.symbol


# ------------------------------------------------------------------
# 详情 (P11-M5-04/05/06)
# ------------------------------------------------------------------

def _render_detail(settings: dict, db_path: str, symbol: str) -> None:
    st.divider()
    payload = _read_payload(db_path, symbol)

    # 打开先读快照 → 缺失/过期时受控自动更新 (冷却去抖)
    _auto_run_if_needed(db_path, symbol, payload, settings)
    payload = _read_payload(db_path, symbol)

    header_left, header_right = st.columns([5, 1])
    with header_left:
        st.markdown(f"### {symbol}")
    with header_right:
        if st.button("手动刷新", key=f"diag_manual_refresh_{symbol}"):
            with st.spinner("强制刷新中（重新取数并计分）..."):
                ok, error = _run_and_handle(
                    db_path, symbol, refresh="force", settings=settings,
                )
            if ok:
                st.rerun()
            else:
                st.error(f"刷新失败 (保留上次结果): {error}")

    if payload is None:
        st.error(
            "暂无诊断结果：行情接口不可用或该标的无数据。"
            "可在数据恢复后点击上方「手动刷新」重试；休市日通常无新增数据属正常。"
        )
        return

    _render_status_card(payload)
    _render_issue_list(payload)
    _render_kline(settings, db_path, symbol, payload)
    _render_indicator_tables(payload)
    _render_conditions(payload)
    with st.expander("解释（模板生成，无 LLM）"):
        explanations = (payload.get("checks") or {}).get("explanations") or []
        for line in explanations:
            st.markdown(f"- {line}")
    checks = payload.get("checks") or {}
    metrics = payload.get("metrics") or {}
    data_cutoff = metrics.get("data_last_date") or payload.get("as_of")
    st.caption(
        f"规则版本 {payload.get('rules_version')} · 价格口径版本 {payload.get('price_version')} · "
        f"数据状态 {metrics.get('data_status')} · 数据截止 {data_cutoff} · "
        f"运行类型 {payload.get('run_kind')}"
    )
    for note in checks.get("notes") or []:
        st.caption(f"• {note}")


def _render_status_card(payload: dict) -> None:
    status = payload.get("status")
    score = payload.get("score")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("技术风险分 (0-100)", f"{score:.1f}" if score is not None else "—")
    c2.metric("风险状态", status or "不可评估")
    c3.metric("数据完整度", payload.get("completeness") or "—")
    c4.metric("触发", payload.get("trigger") or "—")
    if score is None:
        st.warning(
            "必需指标缺失或事件暂停 → 总分为空, 不出三态（缺失不视为低风险）。"
        )
    if payload.get("status") in ("警戒", "防御"):
        st.markdown(
            "**警惕**: 风险状态仅为规则下的关注程度，不代表卖出建议；复核后自行决策。"
        )


def _render_issue_list(payload: dict) -> None:
    checks = payload.get("checks") or {}
    metrics = payload.get("metrics") or {}
    missing = checks.get("missing_required") or []
    unavailable = checks.get("unavailable") or {}
    hints = metrics.get("hints") or []
    if not (missing or unavailable or hints):
        if payload.get("status") == "正常":
            st.success("检查清单: 当前可评估范围内未触发风险规则（正常 ≠ 安全或值得买入）。")
        return
    if missing:
        for item in missing:
            st.error(f"不可评估: {item}")
    if unavailable:
        for column, reason in unavailable.items():
            st.warning(f"{column}: {reason}")
    for hint in hints:
        st.info(hint)


def _render_kline(settings: dict, db_path: str, symbol: str, payload: dict) -> None:
    st.markdown(f"**日K线（近 {DISPLAY_MONTHS} 个自然月；计算使用更长历史）**")
    prices = read_symbol_prices(db_path, symbol)
    frame = build_kline_frame(prices)
    if frame.is_empty():
        st.info("无可用行情数据（新上市/停牌/缓存未建立）。")
        return
    c1, c2, c3, c4 = st.columns(4)
    show_ma20 = c1.checkbox("MA20", value=True, key=f"k_ma20_{symbol}")
    show_ma60 = c2.checkbox("MA60", value=True, key=f"k_ma60_{symbol}")
    show_ma200 = c3.checkbox("MA200", value=True, key=f"k_ma200_{symbol}")
    show_boll = c4.checkbox("布林带", value=False, key=f"k_boll_{symbol}")
    vol_mode = st.radio(
        "副图", ["成交量", "成交额"], horizontal=True, key=f"k_vol_{symbol}",
    )
    fig = _make_kline_figure(
        frame.to_pandas(), show={"ma20": show_ma20, "ma60": show_ma60,
                                 "ma200": show_ma200, "boll": show_boll},
        vol_mode=vol_mode,
    )
    st.plotly_chart(fig, width="stretch")
    available_from = frame["date"].min()
    st.caption(
        f"显示范围 {available_from} → {frame['date'].max()} · 口径 {payload.get('price_version')} "
        f"（ETF 不复权 / 股票后复权，均为已声明研究口径）· 未上市/停牌区间不补造K线"
    )


def _make_kline_figure(df: pd.DataFrame, *, show: dict, vol_mode: str) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25],
        vertical_spacing=0.02,
    )
    fig.add_trace(
        go.Candlestick(
            x=df["date"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name="日K",
            increasing_line_color="#d62728", decreasing_line_color="#2ca02c",
            increasing_fillcolor="#d62728", decreasing_fillcolor="#2ca02c",
        ),
        row=1, col=1,
    )
    for key, column, color in (
        ("ma20", "ma20", "#ff7f0e"), ("ma60", "ma60", "#1f77b4"),
        ("ma200", "ma200", "#9467bd"),
    ):
        if show.get(key) and column in df.columns:
            fig.add_trace(
                go.Scatter(x=df["date"], y=df[column], mode="lines",
                           name=column.upper(), line=dict(color=color, width=1.2)),
                row=1, col=1,
            )
    if show.get("boll"):
        for column, dash in (("boll_upper", "dot"), ("boll_mid", "dash"), ("boll_lower", "dot")):
            if column in df.columns:
                fig.add_trace(
                    go.Scatter(x=df["date"], y=df[column], mode="lines",
                               name=column.replace("boll_", "BOLL "),
                               line=dict(color="#7f7f7f", width=1, dash=dash)),
                    row=1, col=1,
                )
    vol_series = df["amount"] if vol_mode == "成交额" else df["volume"]
    colors = [
        "#d62728" if c >= o else "#2ca02c"
        for o, c in zip(df["open"], df["close"], strict=True)
    ]
    fig.add_trace(
        go.Bar(x=df["date"], y=vol_series, marker_color=colors, name=vol_mode),
        row=2, col=1,
    )
    fig.update_layout(
        height=560, margin=dict(l=10, r=10, t=30, b=10),
        xaxis_rangeslider_visible=False, hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig


def _render_indicator_tables(payload: dict) -> None:
    metrics = payload.get("metrics") or {}
    display = metrics.get("display") or {}
    contributions = payload.get("contributions") or {}
    if display:
        display_rows = [
            {"指标": key, "当前值": (f"{value:.4g}" if isinstance(value, (int, float)) else "—")}
            for key, value in display.items()
        ]
        st.dataframe(pd.DataFrame(display_rows), width="stretch", hide_index=True)
    if contributions:
        rows = []
        for item_id, info in contributions.items():
            value = info.get("value")
            points = info.get("points")
            rows.append({
                "计分项": item_id,
                "原值": f"{value:.4g}" if isinstance(value, (int, float)) else "—",
                "触发": "是" if info.get("triggered") else "否",
                "贡献": f"{points:.1f}" if isinstance(points, (int, float)) else "—",
                "满分": info.get("max_points"),
                "说明": info.get("note") or "",
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_conditions(payload: dict) -> None:
    """条件检查 (基本面 / ETF 产品) —— 与技术分单列, 不混算 (P11-M4)."""
    st.markdown("**条件检查（基本面 / ETF 产品；单列展示，不混入技术分）**")
    conditions = (payload.get("checks") or {}).get("conditions")
    if not conditions:
        st.info("本快照未包含条件检查（旧版本快照）；点击「手动刷新」可生成。")
        return
    summary = conditions.get("summary") or "—"
    version = conditions.get("version") or "—"
    st.caption(f"规则版本 {version} · 结果汇总：{summary}")
    items = conditions.get("items") or []
    if items:
        table = pd.DataFrame([
            {
                "检查项": item.get("label"),
                "状态": item.get("status"),
                "数值": item.get("value") or "—",
                "报告期/数据截止": item.get("period") or "—",
                "公告/披露": item.get("published") or "—",
                "说明": item.get("detail") or "",
            }
            for item in items
        ])
        st.dataframe(table, width="stretch", hide_index=True)
    for note in conditions.get("notes") or []:
        st.caption(f"• {note}")
