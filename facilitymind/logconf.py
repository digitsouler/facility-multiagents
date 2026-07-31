"""集中日志配置：供引擎与评估使用。

用法：在程序入口调用一次 setup_logging() 即可。
默认输出到控制台，可选落盘到 logs/pipeline.log 便于事后追查流程。
"""

import logging
import os

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, log_file: str | None = None) -> None:
    """配置 facilitymind 根 logger；幂等，重复调用安全。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("facilitymind")
    root.setLevel(level)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            root.warning("日志文件不可写，仅输出到控制台：%s", log_file)

    _CONFIGURED = True
