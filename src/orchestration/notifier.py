"""消息推送模块 (T0.8) — 将每日信号推送到移动端 (PushPlus).

设计:
  - build_message: 纯函数构建推送标题/正文, 每条信号含四要素
    (标的 / 方向 / 目标仓位 / 触发原因)
  - PushPlusClient: PushPlus 通道封装 (trust_env=False 绕过不可达系统代理, 同 EV-004)
  - push_pipeline_result: 推送入口; 失败重试 notification.retry_count 次 (含首次),
    全部失败只记录日志并返回 PushResult(ok=False) —— 信号已落库, 推送失败不回滚;
    token 未配置时安全跳过 (不抛异常)

Usage:
    uv run daily   # pipeline 完成后自动推送

    # 或代码调用
    from orchestration.notifier import push_pipeline_result
    push_result = push_pipeline_result(settings, result)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from time import sleep
from typing import TYPE_CHECKING, Protocol

import requests

if TYPE_CHECKING:
    from orchestration.daily_pipeline import PipelineResult, Signal

logger = logging.getLogger(__name__)

# ---- 常量 ----
PUSHPLUS_URL = "https://www.pushplus.plus/send"
PUSHPLUS_TEMPLATE = "txt"
ENV_PUSHPLUS_TOKEN = "PUSHPLUS_TOKEN"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_BACKOFF_BASE = 1.0  # 秒; 第 n 次重试退避 = base * 2^(n-1)

_DIRECTION_EMOJI = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}
_REGIME_CN = {"bull": "多头", "bear": "空头", "ranging": "震荡"}


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class PushResult:
    """推送结果."""

    ok: bool
    channel: str
    attempts: int             # 实际尝试次数 (含首次)
    error: str | None = None  # ok=False 时说明原因


class PushChannel(Protocol):
    """推送通道协议: send 失败必须抛异常."""

    name: str

    def send(self, title: str, content: str) -> None: ...


# ------------------------------------------------------------------
# 消息构建 (纯函数)
# ------------------------------------------------------------------

def build_message(result: PipelineResult, settings: dict) -> tuple[str, str]:
    """构建推送标题与正文.

    Returns:
        (title, content); 正文含每条信号的标的/方向/目标仓位/触发原因.
    """
    names = _symbol_names(settings)
    actionable = [s for s in result.signals if s.direction != "HOLD"]
    holds = [s for s in result.signals if s.direction == "HOLD"]

    title = f"Quant-Pilot 每日信号 {result.run_date}"
    if not actionable:
        title += " — 无操作"

    lines: list[str] = []

    macro = result.macro_score
    if macro is not None:
        regime = _REGIME_CN.get(macro.regime, macro.regime)
        lines.append(
            f"🌐 宏观: {macro.composite_score:.0f}/100 · {regime} · "
            f"仓位上限 {macro.position_cap:.0%}"
        )
    if result.risk_report is not None:
        lines.append(f"🛡️ 风控: {result.risk_report.message}")
    lines.append(f"📦 标的池: {len(result.passed_symbols)} 只通过清洗")
    if result.portfolio is not None:
        p = result.portfolio
        lines.append(
            f"📈 组合: {p.num_symbols_output} 只入选, 总仓位 {p.total_weight:.1%} "
            f"(上限 {p.position_cap:.0%})"
        )

    lines.append("")
    lines.append(f"【操作信号 · {len(actionable)} 条】")
    if actionable:
        lines.extend(_format_signal(s, names) for s in actionable)
    else:
        lines.append("(无 — 建议维持现有仓位)")
    lines.append("")
    lines.append(f"【维持原仓 · {len(holds)} 条】")
    if holds:
        lines.extend(_format_signal(s, names) for s in holds)
    else:
        lines.append("(无)")

    return title, "\n".join(lines)


def _format_signal(sig: Signal, names: dict[str, str]) -> str:
    """单条信号两行式: [emoji 代码 名称 · 方向 仓位] + [原因]."""
    emoji = _DIRECTION_EMOJI.get(sig.direction, "")
    name = names.get(sig.symbol, sig.symbol)
    if sig.direction == "HOLD":
        head = f"{emoji} {sig.symbol} {name} · HOLD 维持 {sig.target_weight:.1%}"
    else:
        head = (
            f"{emoji} {sig.symbol} {name} · {sig.direction} "
            f"{sig.prev_weight:.1%} → {sig.target_weight:.1%}"
        )
    return f"{head}\n   原因: {sig.trigger_reason}"


def _symbol_names(settings: dict) -> dict[str, str]:
    """从 settings.universe 构建 symbol → 名称映射."""
    names: dict[str, str] = {}
    for category in settings.get("universe", {}).values():
        if isinstance(category, list):
            for etf in category:
                names[etf["symbol"]] = etf.get("name", etf["symbol"])
    return names


# ------------------------------------------------------------------
# token 解析
# ------------------------------------------------------------------

def resolve_pushplus_token(settings: dict) -> str:
    """解析 PushPlus token: settings 优先, 环境变量兜底."""
    notification = settings.get("notification", {})
    token = str(notification.get("pushplus_token") or "").strip()
    if token:
        return token
    return os.environ.get(ENV_PUSHPLUS_TOKEN, "").strip()


# ------------------------------------------------------------------
# PushPlus 通道
# ------------------------------------------------------------------

class PushPlusClient:
    """PushPlus 通道 (https://www.pushplus.plus)."""

    name = "pushplus"

    def __init__(self, token: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False  # 绕过 Windows 不可达系统代理 (EV-004)

    def send(self, title: str, content: str) -> None:
        """发送一条消息; 网络异常或业务码非 200 抛 RuntimeError."""
        payload = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": PUSHPLUS_TEMPLATE,
        }
        try:
            resp = self.session.post(PUSHPLUS_URL, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise RuntimeError(f"PushPlus 请求失败: {e}") from e

        if data.get("code") != 200:
            raise RuntimeError(
                f"PushPlus 业务错误: code={data.get('code')} msg={data.get('msg')}"
            )


# ------------------------------------------------------------------
# 推送入口
# ------------------------------------------------------------------

def push_pipeline_result(
    settings: dict,
    result: PipelineResult,
    *,
    client: PushChannel | None = None,
) -> PushResult:
    """构建并推送每日信号; 永不抛异常 (失败返回 ok=False).

    Args:
        settings: settings.yaml 字典 (读取 notification 节)
        result: run_daily_pipeline 的返回值
        client: 测试注入用通道; None 时按 settings.notification.channel 构建

    Returns:
        PushResult — ok=False 时 error 说明原因 (token 未配置 / 通道不支持 / 推送失败)
    """
    notification = settings.get("notification", {})
    channel = str(notification.get("channel", "pushplus"))
    retry_count = max(1, int(notification.get("retry_count", 3)))

    if client is None:
        if channel != "pushplus":
            return PushResult(
                ok=False, channel=channel, attempts=0,
                error=f"暂不支持的推送通道: {channel}",
            )
        token = resolve_pushplus_token(settings)
        if not token:
            return PushResult(
                ok=False, channel=channel, attempts=0,
                error=(
                    "PushPlus token 未配置 "
                    "(settings.notification.pushplus_token 或环境变量 PUSHPLUS_TOKEN)"
                ),
            )
        client = PushPlusClient(token)

    channel = getattr(client, "name", channel)
    title, content = build_message(result, settings)
    ok, attempts, error = _send_with_retry(client.send, title, content, retry_count)
    if ok:
        logger.info(
            "信号推送成功 (channel=%s, 尝试 %d/%d): %s",
            channel, attempts, retry_count, title,
        )
    else:
        logger.error(
            "信号推送失败 (channel=%s, 尝试 %d/%d): %s",
            channel, attempts, retry_count, error,
        )
    return PushResult(ok=ok, channel=channel, attempts=attempts, error=error)


def _send_with_retry(
    send_fn: Callable[[str, str], None],
    title: str,
    content: str,
    retry_count: int,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
) -> tuple[bool, int, str | None]:
    """调用 send_fn 并在失败后指数退避重试.

    Returns:
        (ok, attempts, last_error); attempts = 实际尝试次数 (含首次).
    """
    attempts = 0
    last_error: str | None = None
    for attempt in range(1, retry_count + 1):
        attempts = attempt
        try:
            send_fn(title, content)
            return True, attempts, None
        except Exception as e:  # 通道异常 (网络/业务码) 统一重试
            last_error = f"{type(e).__name__}: {e}"
            if attempt < retry_count:
                backoff = backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "推送尝试 %d/%d 失败: %s. %.0fs 后重试",
                    attempt, retry_count, last_error, backoff,
                )
                sleep(backoff)
    return False, attempts, last_error
