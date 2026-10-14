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
    """特征计算上下文，包含三时点的合并数据。"""

    secucode: str
    report_date: str
    current: dict[str, Any]            # 当期三表合并数据
    previous: dict[str, Any] | None    # 上一报告期（环比）
    year_ago: dict[str, Any] | None    # 去年同期（同比）


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
        """计算百分比变化率：(current - base) / |base|。

        base 为 0 或 None 时返回 None。结果为小数（如 0.2 表示 20%）。
        """
        if current is None or base is None:
            return None
        if base == 0:
            return None
        return (current - base) / abs(base)

    @staticmethod
    def difference(current: float | None, base: float | None) -> float | None:
        """计算差值：current - base。"""
        if current is None or base is None:
            return None
        return current - base
