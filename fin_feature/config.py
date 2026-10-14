"""配置加载：从 config.yaml 读取配置，提供默认值。"""

from pathlib import Path
from typing import Any

import yaml

# 默认配置文件路径（项目根目录）
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# 默认配置（config.yaml 缺失时回退）
_DEFAULTS: dict[str, Any] = {
    "database": {"path": "data/finance.db"},
    "crawler": {
        "base_url": "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "stock_list_url": "https://push2.eastmoney.com/api/qt/clist/get",
        "page_size": 50,
        "timeout": 15,
        "min_interval": 1.5,
        "max_retries": 3,
        "user_agent": "Mozilla/5.0",
        "referer": "https://data.eastmoney.com/",
    },
    "crawl": {"stock_count": 50, "recent_periods": 4},
    "cleaning": {"outlier_thresholds": {}, "missing_strategy": {"default": "keep"}},
    "features": {"groups": ["profitability", "growth", "solvency"]},
    "backtest": {"n_groups": 5, "return_proxy": "next_profit_yoy"},
    "logging": {"level": "INFO", "file": "logs/fin_feature.log"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base，返回新 dict。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """配置对象，支持属性访问。"""

    def __init__(self, data: dict):
        self._data = data

    def get(self, *keys, default: Any = None) -> Any:
        """按路径取值，如 config.get('crawler', 'page_size')。"""
        node = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def db_path(self) -> str:
        return self.get("database", "path", default="data/finance.db")

    @property
    def crawler(self) -> dict:
        return self.get("crawler", default={})

    @property
    def crawl(self) -> dict:
        return self.get("crawl", default={})

    @property
    def cleaning(self) -> dict:
        return self.get("cleaning", default={})

    @property
    def features(self) -> dict:
        return self.get("features", default={})

    @property
    def backtest(self) -> dict:
        return self.get("backtest", default={})

    @property
    def logging(self) -> dict:
        return self.get("logging", default={})


def load_config(config_path: str | Path | None = None) -> Config:
    """加载配置文件，与默认值合并。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    data = dict(_DEFAULTS)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            file_data = yaml.safe_load(f) or {}
        data = _deep_merge(data, file_data)
    return Config(data)
