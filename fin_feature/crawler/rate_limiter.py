"""请求频率控制器：令牌桶模式，控制请求最小间隔，反爬策略核心组件。"""

import threading
import time


class RateLimiter:
    """线程安全的请求频率控制器。

    通过记录上次请求时间，强制每次请求间隔不小于 min_interval 秒。
    """

    def __init__(self, min_interval: float = 1.5):
        self.min_interval = min_interval
        self._last_time = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """阻塞等待，直到满足最小间隔要求。"""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_time = time.monotonic()

    def reset(self) -> None:
        """重置计时器（用于测试）。"""
        with self._lock:
            self._last_time = 0.0
