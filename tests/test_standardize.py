"""标准化入口测试：单位转换、防二次转换、未知单位、覆盖机制。

对应契约 T13、T17、T27、T28。
"""

from fin_feature.features.standardize import (
    standardize,
    standardize_record,
    set_unit_override,
    clear_unit_overrides,
)


class TestStandardize:
    """标准化函数测试。"""

    def test_standardize_percent_roe(self):
        """T17: WEIGHTAVG_ROE 原百分值 16.75 → 标准小数 0.1675。"""
        val, reason = standardize("weightavg_roe", 16.75)
        assert val == 0.1675
        assert reason is None

    def test_standardize_percent_gross_margin(self):
        """T13: XSMLL 原百分值 30.0 → 标准小数 0.3。"""
        val, reason = standardize("gross_margin", 30.0)
        assert val == 0.3
        assert reason is None

    def test_standardize_amount_no_conversion(self):
        """金额字段不转换。"""
        val, reason = standardize("total_operate_income", 1000.0)
        assert val == 1000.0
        assert reason is None

    def test_standardize_unknown_unit_current_ratio(self):
        """T28: CURRENT_RATIO 单位未知 → None + unknown_unit。"""
        val, reason = standardize("current_ratio", 558.9543)
        assert val is None
        assert reason == "unknown_unit"

    def test_standardize_percent_ratio_override(self):
        """T27: current_ratio 单位明确为 percent_ratio 时 → 250 → 2.5。"""
        set_unit_override("current_ratio", "percent_ratio")
        try:
            val, reason = standardize("current_ratio", 250)
            assert val == 2.5
            assert reason is None
        finally:
            clear_unit_overrides()

    def test_standardize_times_override(self):
        """T27: current_ratio 单位为 times 时原样使用。"""
        set_unit_override("current_ratio", "times")
        try:
            val, reason = standardize("current_ratio", 2.5)
            assert val == 2.5
            assert reason is None
        finally:
            clear_unit_overrides()

    def test_standardize_none_value(self):
        """None 值 → (None, missing_input)。"""
        val, reason = standardize("weightavg_roe", None)
        assert val is None
        assert reason == "missing_input"

    def test_standardize_nan(self):
        """T29: NaN → (None, invalid_value)。"""
        val, reason = standardize("weightavg_roe", float("nan"))
        assert val is None
        assert reason == "invalid_value"

    def test_standardize_inf(self):
        """T29: Inf → (None, invalid_value)。"""
        val, reason = standardize("weightavg_roe", float("inf"))
        assert val is None
        assert reason == "invalid_value"

    def test_standardize_boolean(self):
        """T29: 布尔值不作为数值。"""
        val, reason = standardize("total_operate_income", True)
        assert val is None
        assert reason == "invalid_value"

    def test_no_double_conversion(self):
        """T17: 标准化只转换一次，不会二次除以 100。

        原值 30 → 0.3（第一次）；不会变成 0.003。
        """
        val1, _ = standardize("gross_margin", 30.0)
        assert val1 == 0.3
        # 再次标准化标准值 0.3（作为 amount 字段不会转换）
        val2, _ = standardize("total_operate_income", 0.3)
        assert val2 == 0.3  # 不会变成 0.003

    def test_standardize_record(self):
        """standardize_record 批量标准化。"""
        record = {
            "weightavg_roe": 16.75,
            "gross_margin": 30.0,
            "total_operate_income": 1000.0,
            "current_ratio": 558.95,
        }
        std, diags = standardize_record(record)
        assert std["weightavg_roe"] == 0.1675
        assert std["gross_margin"] == 0.3
        assert std["total_operate_income"] == 1000.0
        assert std["current_ratio"] is None
        # current_ratio 应有 unknown_unit 诊断
        unknown_diags = [d for d in diags if d["reason_code"] == "unknown_unit"]
        assert len(unknown_diags) == 1
        assert unknown_diags[0]["field"] == "current_ratio"
