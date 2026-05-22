"""
Headless Agent V3 — 统一结构化日志配置
- 按日期轮转文件
- 控制台 + 文件双输出
- 可配置的日志级别
"""

import logging
import logging.handlers
import sys
from pathlib import Path

try:
    from utils.config_loader import cfg_str, cfg_int
except ImportError:
    from config_loader import cfg_str, cfg_int

# 日志根目录（与 data/logs 对齐）
LOG_ROOT = Path(__file__).resolve().parents[1] / "data" / "logs"
LOG_ROOT.mkdir(parents=True, exist_ok=True)

class ColoredFormatter(logging.Formatter):
    """控制台彩色输出（仅当输出到 tty 时）"""

    COLORS = {
        "DEBUG": "\033[36m",      # cyan
        "INFO": "\033[32m",       # green
        "WARNING": "\033[33m",    # yellow
        "ERROR": "\033[31m",      # red
        "CRITICAL": "\033[35m",   # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        if sys.stdout.isatty():
            color = self.COLORS.get(record.levelname, "")
            record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging(name: str = "agent", level: str | None = None) -> logging.Logger:
    """
    为指定模块名配置 logger。
    每个模块获得独立的按大小轮转文件 + 共享控制台输出。
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # 已初始化，避免重复添加 handler

    if level is None:
        level = cfg_str("logging.level", "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))

    max_bytes = cfg_int("logging.max_bytes", 10_000_000)  # 10MB
    backup_count = cfg_int("logging.backup_count", 5)

    # 统一格式
    file_fmt = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_fmt = ColoredFormatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # 文件 Handler：按大小轮转
    log_file = LOG_ROOT / f"{name}.log"
    fh = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(file_fmt)
    logger.addHandler(fh)

    # 控制台 Handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(console_fmt)
    logger.addHandler(ch)

    return logger


def get_logger(name: str) -> logging.Logger:
    """获取已配置的 logger（若未初始化则自动 setup）"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logging(name)
    return logger
