"""特征注册表：装饰器机制，零侵入注册特征类。

用法：
    @FeatureRegistry.register
    class MyFeature(Feature):
        name = "my_feature"
        ...

注册后可通过 get_all() / get_by_group() / get_by_name() 查询。
新增特征只需定义类 + 装饰器 + 在 engine.py 中 import 触发注册。
"""

from fin_feature.features.base import Feature


class FeatureRegistry:
    """特征注册表，维护所有已注册的特征类。"""

    _features: dict[str, type[Feature]] = {}

    @classmethod
    def register(cls, feature_cls: type[Feature]) -> type[Feature]:
        """注册特征类的装饰器。"""
        if not feature_cls.name:
            raise ValueError(f"特征类 {feature_cls.__name__} 未设置 name 属性")
        cls._features[feature_cls.name] = feature_cls
        return feature_cls

    @classmethod
    def get_all(cls) -> list[type[Feature]]:
        """返回所有已注册的特征类。"""
        return list(cls._features.values())

    @classmethod
    def get_by_group(cls, group: str) -> list[type[Feature]]:
        """按特征组查询。"""
        return [f for f in cls._features.values() if f.group == group]

    @classmethod
    def get_by_name(cls, name: str) -> type[Feature] | None:
        """按特征名查询。"""
        return cls._features.get(name)

    @classmethod
    def clear(cls) -> None:
        """清空注册表（用于测试）。"""
        cls._features.clear()

    @classmethod
    def count(cls) -> int:
        return len(cls._features)
