"""消息推送模块 (T0.8) 单元测试.

覆盖:
  - 消息构建: 每条信号含四要素 (标的/方向/目标仓位/触发原因), 全 HOLD 兜底
  - token 解析: settings 优先 > 环境变量 > 未配置时安全跳过
  - 重试: 失败后指数退避重试, 全部失败不抛异常
  - PushPlus 通道: 请求构造 / 业务错误码 / HTTP 异常 (mock, 不联网)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
import yaml

from orchestration import notifier
from orchestration.daily_pipeline import PipelineResult, Signal
from strategies.macro_timing import MacroScore
from strategies.portfolio_optimizer import PortfolioResult, PortfolioWeight
from strategies.risk_manager import RiskReport

# ------------------------------------------------------------------
# 测试替身
# ------------------------------------------------------------------

class FakeClient:
    """测试用推送通道: 前 fail_times 次调用抛异常."""

    name = "fake"

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.last_title: str | None = None
        self.last_content: str | None = None

    def send(self, title: str, content: str) -> None:
        self.calls += 1
        self.last_title, self.last_content = title, content
        if self.calls <= self.fail_times:
            raise RuntimeError(f"simulated failure #{self.calls}")


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse | None = None,
                 exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls: list[dict] = []
        self.trust_env = True

    def post(self, url: str, json: dict | None = None,
             timeout: float | None = None) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        assert self.response is not None
        return self.response


# ------------------------------------------------------------------
# 合成数据
# ------------------------------------------------------------------

BUY_REASON = "宏观28分(ranging,上限28%); 逆波动率加权27.5%(年化波动18%); 0.0%→27.5%"
HOLD_REASON = "宏观15分(ranging,上限0%); 未入选组合; 变动<0.0%防抖阈值,维持原仓"


def make_settings(**notification_overrides) -> dict:
    notification = {"channel": "pushplus", "pushplus_token": "test-token",
                    "retry_count": 3}
    notification.update(notification_overrides)
    return {
        "universe": {
            "broad_base": [
                {"symbol": "510300", "name": "沪深300ETF",
                 "market_type": "A股", "settlement": "T+1"},
            ],
            "cross_border_commodity": [
                {"symbol": "159920", "name": "恒生ETF",
                 "market_type": "跨境", "settlement": "T+0"},
            ],
        },
        "notification": notification,
    }


def make_result(signals: list[Signal]) -> PipelineResult:
    return PipelineResult(
        run_date="2026-09-11",
        mode="research",
        strategy_version="v0.2",
        macro_scope="a_share",
        macro_score=MacroScore(
            trend_score=27.0, breadth_score=0.0,
            composite_score=15.0, position_cap=0.0, regime="ranging",
        ),
        passed_symbols=["510300", "159920"],
        portfolio=PortfolioResult(
            weights=[
                PortfolioWeight(symbol="159920", annualized_vol=0.18,
                                raw_weight=0.5, final_weight=0.275),
            ],
            position_cap=0.275,
            total_weight=0.275,
            num_symbols_input=2,
            num_symbols_output=1,
        ),
        risk_report=RiskReport(
            stops=[], plunge_detected=False, plunge_score_drop=0.0,
            original_position_cap=0.275, effective_position_cap=0.275,
            forced_exits=[], message="✅ 风控正常: 无回撤触发",
        ),
        signals=signals,
    )


def buy_signal() -> Signal:
    return Signal(symbol="159920", direction="BUY", target_weight=0.275,
                  prev_weight=0.0, trigger_reason=BUY_REASON)


def hold_signal(symbol: str = "510300") -> Signal:
    return Signal(symbol=symbol, direction="HOLD", target_weight=0.0,
                  prev_weight=0.0, trigger_reason=HOLD_REASON)


# ------------------------------------------------------------------
# 消息构建
# ------------------------------------------------------------------

class TestBuildMessage:
    """build_message: 纯函数, 内容可追溯."""

    def test_action_signal_contains_four_elements(self):
        result = make_result([buy_signal(), hold_signal()])
        title, content = notifier.build_message(result, make_settings())

        # 标题: 日期 + 操作信号存在时不标注"无操作"
        assert "2026-09-11" in title
        assert "无操作" not in title

        # 四要素: 标的 (代码 + 名称) / 方向 / 目标仓位 / 触发原因
        assert "159920" in content
        assert "恒生ETF" in content
        assert "BUY" in content
        assert "27.5%" in content
        assert BUY_REASON in content

    def test_hold_signals_in_separate_section(self):
        result = make_result([buy_signal(), hold_signal()])
        _, content = notifier.build_message(result, make_settings())

        ops_section, holds_section = content.split("【维持原仓 · 1 条】")
        assert "159920" in ops_section
        assert "159920" not in holds_section
        assert "510300" in holds_section
        assert HOLD_REASON in holds_section

    def test_all_hold_marks_no_action(self):
        result = make_result([hold_signal("510300"), hold_signal("159920")])
        title, content = notifier.build_message(result, make_settings())

        assert "无操作" in title
        assert "【操作信号 · 0 条】" in content
        assert "建议维持现有仓位" in content
        assert "【维持原仓 · 2 条】" in content

    def test_macro_and_risk_context_included(self):
        result = make_result([buy_signal()])
        _, content = notifier.build_message(result, make_settings())

        assert "15/100" in content
        assert "震荡" in content     # regime 中文化
        assert "上限 0%" in content
        assert "✅ 风控正常" in content
        assert "2 只通过清洗" in content

    def test_symbol_name_falls_back_to_code(self):
        unknown = Signal(symbol="999999", direction="BUY", target_weight=0.1,
                         prev_weight=0.0, trigger_reason="测试原因")
        result = make_result([unknown])
        _, content = notifier.build_message(result, make_settings())
        assert "999999" in content  # 无名称映射时回退为代码本身

    def test_header_has_mode_version_and_data_cutoff(self):
        """A0-1/A0-2: 推送含模式/策略版本/数据截止/无真实账户声明."""
        result = make_result([buy_signal()])
        title, content = notifier.build_message(result, make_settings())

        assert "研究模式" in title
        assert "research" in content
        assert "策略版本 v0.2" in content
        assert "数据截止 2026-09-11" in content
        assert "无真实账户接入" in content
        assert "非成交/持仓记录" in content


# ------------------------------------------------------------------
# token 解析
# ------------------------------------------------------------------

class TestTokenResolution:
    """resolve_pushplus_token: settings > 环境变量 > 空."""

    def test_settings_takes_priority(self, monkeypatch):
        monkeypatch.setenv("PUSHPLUS_TOKEN", "env-token")
        settings = make_settings(pushplus_token="settings-token")
        assert notifier.resolve_pushplus_token(settings) == "settings-token"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("PUSHPLUS_TOKEN", "env-token")
        settings = make_settings(pushplus_token="")
        assert notifier.resolve_pushplus_token(settings) == "env-token"

    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)
        settings = make_settings(pushplus_token="")
        assert notifier.resolve_pushplus_token(settings) == ""


# ------------------------------------------------------------------
# 重试逻辑
# ------------------------------------------------------------------

class TestRetry:
    """_send_with_retry: 指数退避, 上限内重试."""

    def _no_sleep(self, monkeypatch) -> list[float]:
        sleeps: list[float] = []
        monkeypatch.setattr(notifier, "sleep", sleeps.append)
        return sleeps

    def test_success_first_attempt(self, monkeypatch):
        sleeps = self._no_sleep(monkeypatch)
        client = FakeClient()
        ok, attempts, error = notifier._send_with_retry(
            client.send, "t", "c", retry_count=3,
        )
        assert (ok, attempts, error) == (True, 1, None)
        assert client.calls == 1
        assert sleeps == []

    def test_retries_until_success(self, monkeypatch):
        sleeps = self._no_sleep(monkeypatch)
        client = FakeClient(fail_times=2)
        ok, attempts, error = notifier._send_with_retry(
            client.send, "t", "c", retry_count=3,
        )
        assert (ok, attempts, error) == (True, 3, None)
        assert client.calls == 3
        assert sleeps == [1.0, 2.0]  # 指数退避

    def test_all_attempts_fail(self, monkeypatch):
        sleeps = self._no_sleep(monkeypatch)
        client = FakeClient(fail_times=99)
        ok, attempts, error = notifier._send_with_retry(
            client.send, "t", "c", retry_count=3,
        )
        assert ok is False
        assert attempts == 3          # 总尝试次数 = retry_count
        assert client.calls == 3
        assert "simulated failure" in error
        assert len(sleeps) == 2       # 最后一次失败后不再退避


# ------------------------------------------------------------------
# 推送入口
# ------------------------------------------------------------------

class TestPushPipelineResult:
    """push_pipeline_result: 安全边界 + 注入通道."""

    def test_skips_safely_without_token(self, monkeypatch):
        monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)

        def _forbidden(*args, **kwargs):
            raise AssertionError("token 缺失时不应构造 HTTP 客户端")

        monkeypatch.setattr(notifier, "PushPlusClient", _forbidden)
        settings = make_settings(pushplus_token="")
        push_result = notifier.push_pipeline_result(settings, make_result([buy_signal()]))

        assert push_result.ok is False
        assert push_result.attempts == 0
        assert "token" in push_result.error.lower()

    def test_unsupported_channel(self):
        settings = make_settings(channel="feishu")
        push_result = notifier.push_pipeline_result(settings, make_result([buy_signal()]))
        assert push_result.ok is False
        assert "feishu" in push_result.error

    def test_injected_client_receives_message(self, monkeypatch):
        monkeypatch.setattr(notifier, "sleep", lambda s: None)
        client = FakeClient()
        settings = make_settings()
        push_result = notifier.push_pipeline_result(
            settings, make_result([buy_signal()]), client=client,
        )

        assert push_result.ok is True
        assert push_result.attempts == 1
        assert push_result.channel == "fake"
        assert "159920" in client.last_content
        assert BUY_REASON in client.last_content

    def test_exhausted_retry_returns_not_raises(self, monkeypatch):
        monkeypatch.setattr(notifier, "sleep", lambda s: None)
        client = FakeClient(fail_times=99)
        settings = make_settings(retry_count=2)
        push_result = notifier.push_pipeline_result(
            settings, make_result([buy_signal()]), client=client,
        )

        assert push_result.ok is False
        assert push_result.attempts == 2
        assert client.calls == 2   # 永不抛异常, 由调用方决定如何处置

    def test_pushplus_client_constructed_from_settings(self, monkeypatch):
        created: dict = {}

        class StubClient:
            name = "pushplus"

            def __init__(self, token: str, timeout: float = 10.0) -> None:
                created["token"] = token

            def send(self, title: str, content: str) -> None:
                created["sent_title"] = title

        monkeypatch.setattr(notifier, "PushPlusClient", StubClient)
        settings = make_settings(pushplus_token="settings-token")
        push_result = notifier.push_pipeline_result(settings, make_result([buy_signal()]))

        assert push_result.ok is True
        assert created["token"] == "settings-token"
        assert "2026-09-11" in created["sent_title"]


# ------------------------------------------------------------------
# PushPlus 通道 (mock HTTP, 不联网)
# ------------------------------------------------------------------

class TestPushPlusClient:
    """请求构造与错误处理."""

    def test_send_payload_and_success(self):
        client = notifier.PushPlusClient("tok-123")
        session = FakeSession(response=FakeResponse({"code": 200, "msg": "请求成功"}))
        client.session = session  # type: ignore[assignment]

        client.send("标题", "正文")

        assert len(session.calls) == 1
        call = session.calls[0]
        assert call["url"] == notifier.PUSHPLUS_URL
        assert call["json"]["token"] == "tok-123"
        assert call["json"]["title"] == "标题"
        assert call["json"]["content"] == "正文"
        assert call["json"]["template"] == "txt"
        assert call["timeout"] == notifier.DEFAULT_TIMEOUT_SECONDS

    def test_business_error_code_raises(self):
        client = notifier.PushPlusClient("tok-123")
        client.session = FakeSession(  # type: ignore[assignment]
            response=FakeResponse({"code": 999, "msg": "token 无效"})
        )
        with pytest.raises(RuntimeError, match="999"):
            client.send("t", "c")

    def test_http_exception_raises_runtime_error(self):
        client = notifier.PushPlusClient("tok-123")
        client.session = FakeSession(exc=requests.ConnectionError("boom"))  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="PushPlus 请求失败"):
            client.send("t", "c")

    def test_non_json_response_raises(self):
        class BadJSON(FakeResponse):
            def json(self) -> dict:
                raise ValueError("not json")

        client = notifier.PushPlusClient("tok-123")
        client.session = FakeSession(response=BadJSON({"code": 200}))  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="PushPlus 请求失败"):
            client.send("t", "c")

    def test_session_bypasses_system_proxy(self):
        """trust_env=False 绕过不可达系统代理 (EV-004 教训)."""
        client = notifier.PushPlusClient("tok-123")
        assert client.session.trust_env is False


# ------------------------------------------------------------------
# settings.yaml 结构兼容性 (防止配置漂移)
# ------------------------------------------------------------------

class TestSettingsCompatibility:
    def test_real_settings_has_notification_section(self):
        project_root = Path(__file__).resolve().parents[1]
        with open(project_root / "config" / "settings.yaml", encoding="utf-8") as f:
            settings = yaml.safe_load(f)

        notification = settings.get("notification", {})
        assert notification.get("channel") == "pushplus"
        assert "pushplus_token" in notification
        assert notification.get("retry_count", 0) >= 1
