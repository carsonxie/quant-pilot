"""诊断规则卡 / 计分引擎 / 解释模板测试 (P11-M3-02/03/04).

全部期望值手工推导 (spec §9.1: 不以实现输出自证)。
"""

import importlib
from pathlib import Path

import pytest
import yaml

from factor_engineering.technical import REQUIRED_PARAM_KEYS, indicator_columns
from strategies.diagnosis import (
    STATUS_DEFENSE,
    STATUS_NORMAL,
    STATUS_WATCH,
    RulesConfigError,
    assess_row,
    load_diagnostics_rules,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = PROJECT_ROOT / "config" / "diagnostics.yaml"
REGISTRY_PATH = PROJECT_ROOT / "config" / "factor_registry.yaml"


@pytest.fixture(scope="module")
def rules():
    return load_diagnostics_rules(RULES_PATH)


def _row(**overrides) -> dict:
    base = {
        "ma200_dev": 0.05, "ma200_slope": 0.01, "momentum_12_1": 0.10,
        "vol_pctile": 0.30, "atr_pct_ratio": 0.80,
        "rsi14": 50.0, "boll_pos": 0.50, "boll_bw": 0.10,
        "amt_ratio": 1.0, "atr_pct": 0.02,
        "ma20": 10.0, "ma60": 10.0, "ma200": 9.5,
        "boll_mid": 10.0, "boll_upper": 10.5, "boll_lower": 9.5,
    }
    base.update(overrides)
    return base


class TestRulesConfig:
    def test_load_frozen_rules(self, rules):
        assert rules["rules_version"] == "diag-v0.1-research"
        assert rules["frozen_at"] == "2026-09-28"
        assert rules["explanation_version"] == "tmpl-v0.1"

    def test_indicators_cover_required_params(self, rules):
        assert set(REQUIRED_PARAM_KEYS) <= set(rules["indicators"])

    def test_columns_exist_in_indicator_library(self, rules):
        columns = set(indicator_columns(rules["indicators"]))
        for item_id, item in rules["scoring"]["items"].items():
            assert item["column"] in columns, item_id
        for column in rules["display_items"]:
            assert column in columns, column
        for column in rules["event_sensitive_columns"]:
            assert column in columns, column

    def test_caps_are_consistent(self, rules):
        scoring = rules["scoring"]
        # 组上限 ≥ 组内项满分之和
        group_max: dict[str, float] = {}
        for item in scoring["items"].values():
            group_max[item["group"]] = group_max.get(item["group"], 0.0) + item["max_points"]
        for group, cap in scoring["group_caps"].items():
            assert group_max[group] <= cap, group
        # 归一化上限 = 组合上限 + 未组合组上限 (显式公式的一致性)
        combined = sum(c["cap"] for c in scoring["combined_caps"])
        combined_groups = {g for c in scoring["combined_caps"] for g in c["groups"]}
        others = sum(
            cap for group, cap in scoring["group_caps"].items()
            if group not in combined_groups
        )
        assert scoring["normalization"]["max_raw"] == combined + others

    def test_band_ordering(self, rules):
        bands = rules["scoring"]["bands"]
        assert 0 < bands["normal_max"] < bands["watch_max"] < 100

    def test_missing_rules_file_raises(self, tmp_path):
        with pytest.raises(RulesConfigError):
            load_diagnostics_rules(tmp_path / "nope.yaml")

    def test_reject_incomplete_rules(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            yaml.safe_dump({"rules_version": "x", "indicators": {}, "scoring": {}}),
            encoding="utf-8",
        )
        with pytest.raises(RulesConfigError):
            load_diagnostics_rules(bad)

    def test_reject_item_missing_field(self, tmp_path):
        good = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
        del good["scoring"]["items"]["ma200_dev"]["max_points"]
        bad = tmp_path / "bad2.yaml"
        bad.write_text(yaml.safe_dump(good, allow_unicode=True), encoding="utf-8")
        with pytest.raises(RulesConfigError, match="ma200_dev"):
            load_diagnostics_rules(bad)


class TestRegistryConsistency:
    def test_diagnostic_indicators_registered_and_importable(self):
        registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
        indicators = registry["diagnostic_indicators"]
        assert len(indicators) >= 12
        required_fields = {
            "name", "version", "formula", "inputs", "units", "window",
            "min_samples", "missing_policy", "applicable_types", "usage", "code_path",
        }
        for key, entry in indicators.items():
            missing = required_fields - set(entry)
            assert not missing, f"{key} 缺字段: {missing}"
            module_name, func_name = entry["code_path"].split("::")
            module = importlib.import_module(module_name)
            assert callable(getattr(module, func_name)), key

    def test_factors_section_untouched(self):
        registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
        assert registry["factors"] == {}
        assert "2026-09-28" in registry["metadata"]["last_updated"]


class TestScoringEngine:
    def test_zero_risk_row(self, rules):
        result = assess_row(_row(), rules, as_of=None)
        assert result.raw_score == 0.0
        assert result.score == 0.0
        assert result.raw_status == STATUS_NORMAL
        assert result.completeness == "5/5"
        assert result.missing_required == []

    def test_full_risk_row_with_combined_cap(self, rules):
        result = assess_row(
            _row(
                ma200_dev=-0.15,       # 20/20
                ma200_slope=-0.06,     # 10/10 → trend 30 (cap 30)
                momentum_12_1=-0.30,   # 10/10
                vol_pctile=1.0,        # 15/15
                atr_pct_ratio=1.6,     # 10/10
            ),
            rules,
        )
        # 组合上限: trend+momentum = 40 → 35, 折减 ×0.875
        assert result.group_points["trend"] == pytest.approx(26.25)
        assert result.group_points["momentum"] == pytest.approx(8.75)
        assert result.group_points["volatility"] == pytest.approx(25.0)
        assert result.raw_score == pytest.approx(60.0)
        assert result.score == pytest.approx(100.0)
        assert result.raw_status == STATUS_DEFENSE
        assert any("组合上限折减" in line for line in result.explanations)

    def test_mid_case_hand_calc(self, rules):
        result = assess_row(
            _row(
                ma200_dev=-0.05,       # 10.0 (0.5×20)
                ma200_slope=-0.015,    # 5.0  (0.5×10) → trend 15
                momentum_12_1=-0.10,   # 5.0  (0.5×10) → 合计 20 ≤ 35, 不折减
                vol_pctile=0.75,       # 7.5  ((0.75-0.5)/0.5×15)
                atr_pct_ratio=1.25,    # 5.0  ((0.25)/0.5×10) → vol 12.5
            ),
            rules,
        )
        assert result.raw_score == pytest.approx(32.5)
        assert result.score == pytest.approx(round(32.5 / 60 * 100, 1))  # 54.2
        assert result.raw_status == STATUS_WATCH

    def test_band_boundaries(self, rules):
        # raw=18 → score 30.0 → 警戒 (≥30)
        r1 = assess_row(_row(ma200_dev=-0.09, ma200_slope=0.0, momentum_12_1=0.0,
                             vol_pctile=0.5, atr_pct_ratio=1.0), rules)
        assert r1.raw_score == pytest.approx(18.0)
        assert r1.score == pytest.approx(30.0)
        assert r1.raw_status == STATUS_WATCH
        # raw=36 → score 60.0 → 防御 (≥60)
        # 20 (ma200_dev 满分) + 0 + 0 + 15 (vol_pctile 满分) + 1 (atr_ratio=1.05 → 0.05/0.5×10)
        r2 = assess_row(_row(ma200_dev=-0.10, ma200_slope=0.0, momentum_12_1=0.0,
                             vol_pctile=1.0, atr_pct_ratio=1.05), rules)
        assert r2.raw_score == pytest.approx(36.0)
        assert r2.score == pytest.approx(60.0)
        assert r2.raw_status == STATUS_DEFENSE

    def test_negative_risk_floors_at_zero(self, rules):
        # 价格高于均线/正动量/低波动 → 全部 0 分, 不出现负贡献
        result = assess_row(
            _row(ma200_dev=0.20, ma200_slope=0.10, momentum_12_1=0.50,
                 vol_pctile=0.0, atr_pct_ratio=0.5),
            rules,
        )
        assert result.raw_score == 0.0
        assert all(item.points in (0.0, None) for item in result.items)

    def test_missing_required_yields_empty_total(self, rules):
        result = assess_row(_row(ma200_dev=None), rules)
        assert result.score is None
        assert result.raw_score is None
        assert result.raw_status is None
        assert len(result.missing_required) == 1
        assert any("必需计分项缺失" in line for line in result.explanations)

    def test_unavailable_reason_is_recorded(self, rules):
        result = assess_row(
            _row(), rules,
            unavailable={"vol_pctile": "窗口含未核验权益事件"},
        )
        assert result.score is None
        assert any("未核验权益事件" in m for m in result.missing_required)

    def test_completeness_counts_scored_items(self, rules):
        result = assess_row(_row(ma200_slope=None), rules)
        assert result.completeness == "4/5"

    def test_hints_and_display(self, rules):
        result = assess_row(
            _row(rsi14=75.0, boll_pos=1.2, amt_ratio=2.5), rules,
        )
        assert any("超买区提示" in h for h in result.hints)
        assert any("上轨之上" in h for h in result.hints)
        assert any("异常放量" in h for h in result.hints)
        assert result.display["rsi14"] == pytest.approx(75.0)

        result2 = assess_row(_row(rsi14=25.0, amt_ratio=0.3), rules)
        assert any("超卖区提示" in h for h in result2.hints)
        assert any("缩量" in h for h in result2.hints)

    def test_hints_available_even_when_score_missing(self, rules):
        result = assess_row(_row(ma200_dev=None, rsi14=80.0), rules)
        assert result.score is None
        assert any("超买区提示" in h for h in result.hints)

    def test_explanations_structure(self, rules):
        result = assess_row(_row(), rules, as_of=None, source_note="test|none|d")
        for item in result.items:
            line = next(x for x in result.explanations if x.startswith(item.item_id))
            if item.points is None:
                continue
            assert "当前值" in line
            assert "比较对象" in line or "起计分" in line
            assert "贡献" in line
        assert any("技术风险分" in line for line in result.explanations)

    def test_explanation_has_data_date_and_source(self, rules):
        from datetime import date

        result = assess_row(
            _row(), rules, as_of=date(2026, 9, 25), source_note="ohlcv_daily|none",
        )
        scored_lines = [x for x in result.explanations if "当前值" in x]
        assert scored_lines
        assert all("2026-09-25" in x for x in scored_lines)

    def test_display_only_items_do_not_affect_score(self, rules):
        baseline = assess_row(_row(), rules)
        extreme_display = assess_row(
            _row(rsi14=100.0, boll_pos=5.0, amt_ratio=10.0, atr_pct=0.5), rules,
        )
        assert baseline.score == extreme_display.score == 0.0
