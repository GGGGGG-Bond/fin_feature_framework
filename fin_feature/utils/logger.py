"""日志配置：统一的 logger 工厂。"""

import logging
import sys
from pathlib import Path


def setup_logger(
    name: str = "fin_feature",
    level: str = "INFO",
    log_file: str | None = None,
) -> logging.Logger:
    """创建并配置 logger，输出到控制台和文件（可选）。

    幂等：重复调用不会重复添加 handler。
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 禁止向 root logger 传播，避免重复输出
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台 handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 文件 handler（可选）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger
