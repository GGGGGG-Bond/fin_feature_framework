"""特征基类与上下文。

FeatureContext 封装计算特征所需的三时点数据：
- current: 当期三表合并数据
- previous: 上一报告期（环比基准）
- year_ago: 去年同期（同比基准）

Feature 基类提供 safe_divide / pct_change 等工具方法，子类只需实现 calculate()。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class FeatureContext:
    """特征计算上下文，包含三时点的合并数据。

    current/previous/year_ago: 标准化后的累计值（供同比特征使用）
    current_sq/previous_sq: 标准化后的单季值（供环比特征使用），向后兼容默认 None
    diagnostics: 结构化诊断列表，按 feature_name 索引由引擎收集
    """

    secucode: str
    report_date: str
    current: dict[str, Any]            # 当期三表合并数据（标准化累计值）
    previous: dict[str, Any] | None    # 上一报告期（环比基准，标准化累计值）
    year_ago: dict[str, Any] | None    # 去年同期（同比基准，标准化累计值）
    current_sq: dict[str, Any] | None = None   # 当期单季值（标准化）
    previous_sq: dict[str, Any] | None = None  # 上一报告期单季值（标准化）
    diagnostics: list[dict] | None = None     # 结构化诊断


class Feature(ABC):
    """特征抽象基类。子类需设置 name/group/description 并实现 calculate()。"""

    name: str = ""
    group: str = ""
    description: str = ""

    @abstractmethod
    def calculate(self, ctx: FeatureContext) -> float | None:
        """计算特征值。返回 None 表示无法计算（数据不足）。"""

    @staticmethod
    def safe_divide(numerator: float | None, denominator: float | None) -> float | None:
        """安全除法，除零或 None 时返回 None。"""
        if numerator is None or denominator is None:
            return None
        if denominator == 0:
            return None
        return numerator / denominator

    @staticmethod
    def pct_change(current: float | None, base: float | None) -> float | None:
        """计算百分比变化率：(current - base) / base。

        stage1-v1: 比较基数为零或负数时返回 None（non_positive_comparison_base）。
        不使用绝对分母。当期为负、基数为正时可计算负增长（如 10→-5 = -1.5）。
        结果为小数（如 0.2 表示 20%）。
        """
        if current is None or base is None:
            return None
        if base <= 0:
            return None
        return (current - base) / base

    @staticmethod
    def difference(current: float | None, base: float | None) -> float | None:
        """计算差值：current - base。"""
        if current is None or base is None:
            return None
        return current - base
