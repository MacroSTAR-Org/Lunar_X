import logging
import os
import shutil
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Dict, Any, List, Optional

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'lunarx.log')
RUNS_DIR = os.path.join(LOG_DIR, 'runs')

# 每次运行单独归档日志时使用的级别全集（也是默认保存级别）
DEFAULT_SAVE_LEVELS = ['DEBUG', 'INFO', 'SUCCESS', 'WARNING', 'ERROR', 'CRITICAL']


def _archive_previous_run():
    """把上一次运行产生的日志归档到 logs/runs/，实现「每次运行一份日志」。

    主文件 lunarx.log 及其轮转备份（lunarx.log.1/.2/.3）都会按源文件 mtime
    命名的目录归档，命名冲突时自动追加序号，避免覆盖。
    归档完成后，本次运行会从全新的 lunarx.log 开始写。
    """
    if not os.path.exists(LOG_FILE):
        return
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        stamp = datetime.fromtimestamp(os.path.getmtime(LOG_FILE)).strftime('%Y%m%d_%H%M%S')
        dest = os.path.join(RUNS_DIR, f'lunarx_{stamp}.log')
        n = 1
        while os.path.exists(dest):
            dest = os.path.join(RUNS_DIR, f'lunarx_{stamp}_{n}.log')
            n += 1
        shutil.move(LOG_FILE, dest)
        for i in range(1, 10):
            src = f'{LOG_FILE}.{i}'
            if os.path.exists(src):
                shutil.move(src, f'{dest}.{i}')
    except Exception as e:
        print(f"[logger] 归档上次运行日志失败: {e}")

def title() -> str:
    return r'''# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  _                                       __  __
# | |      _   _   _ __     __ _   _ __    \ \/ /
# | |     | | | | | '_ \   / _` | | '__|    \  / 
# | |___  | |_| | | | | | | (_| | | |       /  \ 
# |_____|  \__,_| |_| |_|  \__,_| |_|      /_/\_\ (Beta|MacroSTAR)              
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~'''

class ColorCodes:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"
    SUCCESS = "\033[92m"

SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, 'SUCCESS')

class EmojiFormatter(logging.Formatter):
    _log_level_colors = {
        logging.DEBUG: ColorCodes.BRIGHT_BLUE,
        logging.INFO: ColorCodes.BRIGHT_CYAN,
        logging.WARNING: ColorCodes.BRIGHT_YELLOW,
        logging.ERROR: ColorCodes.BRIGHT_RED,
        logging.CRITICAL: ColorCodes.RED + ColorCodes.BOLD,
        SUCCESS_LEVEL: ColorCodes.BRIGHT_GREEN,
    }

    _log_level_emojis = {
        logging.DEBUG: "🐛",
        logging.INFO: "ℹ️",
        logging.WARNING: "⚠️",
        logging.ERROR: "❌",
        logging.CRITICAL: "🚨",
        SUCCESS_LEVEL: "✅", 
    }

    def format(self, record):
        record.emoji_prefix = self._log_level_emojis.get(record.levelno, "")
        
        color = self._log_level_colors.get(record.levelno, ColorCodes.RESET)
        record.colored_levelname = f"{color}{record.levelname}{ColorCodes.RESET}"
        if record.name == 'LunarBot':
            record.logger_display = ''
        elif record.name == 'LunarPlugins':
            record.logger_display = '[Lunar Plugins System]'
        elif record.name.startswith('Plugins:'):
            record.logger_display = f'[Lunar Plugins System] [{record.name}]'
        else:
            record.logger_display = f'[{record.name}]'
        formatted_message = super().format(record)
        return formatted_message

class PlainFileFormatter(logging.Formatter):
    """文件日志格式：无 ANSI 颜色码，含 emoji 与日志来源"""
    def format(self, record):
        record.emoji_prefix = self._log_level_emojis.get(record.levelno, "")
        if record.name == 'LunarBot':
            record.logger_display = ''
        elif record.name == 'LunarPlugins':
            record.logger_display = '[Lunar Plugins System]'
        elif record.name.startswith('Plugins:'):
            record.logger_display = f'[Lunar Plugins System] [{record.name}]'
        else:
            record.logger_display = f'[{record.name}]'
        return super().format(record)

    _log_level_emojis = {
        logging.DEBUG: "🐛",
        logging.INFO: "ℹ️",
        logging.WARNING: "⚠️",
        logging.ERROR: "❌",
        logging.CRITICAL: "🚨",
        SUCCESS_LEVEL: "✅",
    }

class SaveLevelFilter(logging.Filter):
    """按「保存级别白名单」过滤文件日志；白名单为 None 或空集合时放行所有级别。"""
    def __init__(self, levels: Optional[List[str]] = None):
        super().__init__()
        self.allowed = set(levels) if levels else None

    def filter(self, record: logging.LogRecord) -> bool:
        if self.allowed is None:
            return True
        return record.levelname in self.allowed


class LunarLogger:
    def __init__(self):
        self._loggers = {}
        self._file_filter = None
        self._setup_file_handler()
        self._setup_default_loggers()

    def _setup_file_handler(self):
        """所有 logger 共用的文件输出（供 WebUI 控制台读取）"""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            _archive_previous_run()
            self._file_handler = RotatingFileHandler(
                LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3,
                encoding='utf-8')
            self._file_handler.setFormatter(PlainFileFormatter(
                '[%(asctime)s.%(msecs)03d] %(logger_display)s %(emoji_prefix)s %(levelname)s %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'))
            self._file_handler.setLevel(logging.DEBUG)
            self._file_filter = SaveLevelFilter(DEFAULT_SAVE_LEVELS)
            self._file_handler.addFilter(self._file_filter)
        except Exception as e:
            print(f"[logger] 文件日志初始化失败: {e}")
            self._file_handler = None

    def _attach_file_handler(self, logger: logging.Logger):
        if self._file_handler and self._file_handler not in logger.handlers:
            logger.addHandler(self._file_handler)
    
    def _setup_default_loggers(self):
        self._setup_logger(
            'LunarBot',
            '[%(asctime)s.%(msecs)03d] %(emoji_prefix)s %(colored_levelname)s %(message)s',
            'INFO'
        )
        self._setup_logger(
            'LunarPlugins',
            '[%(asctime)s.%(msecs)03d] %(logger_display)s %(emoji_prefix)s %(colored_levelname)s %(message)s',
            'INFO'
        )
    
    def _setup_logger(self, name: str, format_str: str, level: str):
        logger = logging.getLogger(name)
        
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        log_level = getattr(logging, level.upper(), logging.INFO)
        logger.setLevel(log_level)
        
        handler = logging.StreamHandler(sys.stdout)
        
        handler.setFormatter(EmojiFormatter(
            format_str,
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        
        logger.addHandler(handler)
        self._attach_file_handler(logger)
        logger.propagate = False
        
        self._loggers[name] = logger
    
    def configure_from_config(self, config: Dict[str, Any]):
        log_level = config.get('log_level', 'INFO')

        for logger_name in self._loggers:
            self.set_level(logger_name, log_level)

        save_levels = config.get('log_save_levels')
        self.set_file_save_levels(save_levels)

    def set_file_save_levels(self, levels: Optional[List[str]]):
        """设置写入文件的日志级别白名单。

        传 None / 空列表 / 空数组 = 全部级别都保存；传级别列表则只保存其中的级别。
        """
        if self._file_filter is not None:
            self._file_filter.allowed = set(levels) if levels else None
    
    def get_logger(self, name: str) -> logging.Logger:
        if name not in self._loggers:
            default_format = '[%(asctime)s.%(msecs)03d] %(logger_display)s %(emoji_prefix)s %(colored_levelname)s %(message)s'
            self._setup_logger(name, default_format, 'INFO')
            self._attach_file_handler(self._loggers[name])
        
        return self._loggers[name]
    
    def set_level(self, logger_name: str, level: str):
        if logger_name in self._loggers:
            log_level = getattr(logging, level.upper(), logging.INFO)
            self._loggers[logger_name].setLevel(log_level)
    
    def info(self, message: str, logger_name: str = 'LunarBot'):
        self.get_logger(logger_name).info(message)
    
    def error(self, message: str, logger_name: str = 'LunarBot'):
        self.get_logger(logger_name).error(message)
    
    def warning(self, message: str, logger_name: str = 'LunarBot'):
        self.get_logger(logger_name).warning(message)
    
    def debug(self, message: str, logger_name: str = 'LunarBot'):
        self.get_logger(logger_name).debug(message)
    
    def critical(self, message: str, logger_name: str = 'LunarBot'):
        self.get_logger(logger_name).critical(message)

    def success(self, message: str, logger_name: str = 'LunarBot'): 
        self.get_logger(logger_name).log(SUCCESS_LEVEL, message)

logger = LunarLogger()
