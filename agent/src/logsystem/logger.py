"""logger.py - 核心日志器：终端 + 文件双输出、`{pid}_{YYYY-MM-DD}_{序号}.log`、100MB 滚动。

多进程安全策略：
- 每个进程 pid 不同 -> 文件名不同 -> 天然避免跨进程文件竞争。
- 同进程内使用独立 handler；旋转基于字节数阈值，在 emit 时检查并在
  达到阈值时由该进程自行创建下一个序号文件（同进程单线程 + 锁保护）。
- 线程安全由 logging 内置 Lock 保证。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import LogConfig
from .masking import MaskFilter
from .trace import get_business_id, get_span_id, get_trace_id


# JSON 可序列化兜底（datetime 等）
class _JsonSafeEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, (datetime,)):
            return o.isoformat()
        if isinstance(o, (bytes, bytearray)):
            return f"<bytes len={len(o)}>"
        try:
            return str(o)
        except Exception:
            return f"<{type(o).__name__}>"


class _RotatingPidFileHandler(logging.Handler):
    """按 `{pid}_{YYYY-MM-DD}_{序号}.log` 命名、按字节数滚动的文件 handler。"""

    def __init__(self, config: LogConfig) -> None:
        super().__init__()
        self.config = config
        self.pid = os.getpid()
        self._date: Optional[str] = None
        self._seq: int = 0
        self._stream = None
        self._lock = threading.RLock()
        self._path: Optional[Path] = None
        self._today_paths: int = 0
        # P-1 fix: track bytes written in-memory to avoid per-emit stat syscall
        self._bytes_written: int = 0

    def _build_path(self, seq: int) -> Path:
        date_str = datetime.now().strftime("%Y-%m-%d")
        return Path(self.config.log_dir) / f"{self.pid}_{date_str}_{seq}.log"

    def _open_next(self) -> None:
        """关闭当前流，打开 (序列号+1) 的新文件。"""
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        date_str = datetime.now().strftime("%Y-%m-%d")
        # 跨天重置序号
        if self._date != date_str:
            self._date = date_str
            self._seq = 0
            self._today_paths = 0
        self._seq += 1
        self._today_paths += 1
        self._path = self._build_path(self._seq)
        self._stream = open(self._path, "a", encoding="utf-8")
        # P-1: initialize byte counter; seed with existing file size on first open
        try:
            self._bytes_written = os.path.getsize(self._path)
        except OSError:
            self._bytes_written = 0

    def _ensure_stream(self) -> None:
        if self._stream is None:
            self._open_next()
            return
        # 跨天检查
        date_str = datetime.now().strftime("%Y-%m-%d")
        if self._date != date_str:
            self._open_next()
            return
        # P-1 fix: use in-memory byte counter instead of per-emit stat syscall.
        # Only falls back to stat if counter is somehow reset (shouldn't happen).
        max_files = self.config.max_files_per_day
        if self._bytes_written >= self.config.rotation_bytes or (
            max_files > 0 and self._today_paths >= max_files
        ):
            self._open_next()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._lock:
                self._ensure_stream()
                line = self._format_file(record)
                data = line + "\n"
                self._stream.write(data)
                self._stream.flush()
                # P-1: accumulate bytes written to avoid per-emit stat
                self._bytes_written += len(data.encode("utf-8"))
        except Exception:
            self.handleError(record)

    def _format_file(self, record: logging.LogRecord) -> str:
        fields = self._record_fields(record)
        if self.config.file_format == "json":
            # standard JSON object per line
            return json.dumps(fields, ensure_ascii=False, cls=_JsonSafeEncoder)
        # key=value（稳定、易 grep/解析）
        parts = []
        for k, v in fields.items():
            s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
            # 含空格的值用引号包裹
            if any(ch in s for ch in (" ", "\t")):
                s = '"' + s.replace('"', '\\"') + '"'
            parts.append(f"{k}={s}")
        return " ".join(parts)

    def _record_fields(self, record: logging.LogRecord) -> Dict[str, Any]:
        ts = datetime.now(timezone.utc).isoformat()
        fields: Dict[str, Any] = {
            "timestamp": ts,
            "pid": self.pid,
            "level": record.levelname,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        for k in ("business_id", "trace_id", "span_id", "step", "status",
                  "cost_ms", "error_code", "error_msg", "args", "result",
                  "exception_type", "stack_trace", "business_done"):
            if hasattr(record, k):
                fields[k] = getattr(record, k)
        # extra_fields 配置注入
        for k, v in self.config.extra_fields.items():
            fields.setdefault(k, v)
        # 用户 extra 的其它字段
        extra = getattr(record, "_logsystem_extra", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                fields.setdefault(k, v)
        fields["message"] = record.getMessage()
        return fields

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                finally:
                    self._stream = None
        super().close()


class _BusinessFormatter(logging.Formatter):
    """终端 human-readable 格式化，自动注入 business_id/trace_id/span_id 摘要。"""

    def __init__(self, fmt: str) -> None:
        super().__init__(fmt, style="{")

    def format(self, record: logging.LogRecord) -> str:
        bid = get_business_id()
        tid = get_trace_id()
        sid = get_span_id()
        # 允许记录时显式覆盖（extra 注入优先）
        record.business_id = getattr(record, "business_id", None) or bid or "-"
        record.trace_id = getattr(record, "trace_id", None) or tid or ""
        record.span_id = getattr(record, "span_id", None) or sid or ""
        return super().format(record)


_configured: Optional["GlobalLogger"] = None
_lock = threading.Lock()


class _RateLimiter:
    """按 (定位信息) 维度做高频日志限流：窗口内超过阈值则按采样率抽样/丢弃。

    使用进程内共享状态（多线程安全）；多进程各自维护自己的限流器，
    与 pid 隔离的文件名一致，天然按进程隔离。
    """

    def __init__(self, config: LogConfig) -> None:
        self.config = config
        self._buckets: Dict[Any, list] = {}
        self._lock = threading.Lock()

    def allow(self, key: Any) -> bool:
        cfg = self.config
        if cfg.rate_limit_max <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            bucket = [t for t in self._buckets.get(key, []) if now - t < cfg.rate_limit_window_seconds]
            self._buckets[key] = bucket
            if len(bucket) >= cfg.rate_limit_max:
                # 超出阈值：按采样率放行
                import random

                if random.random() < cfg.sample_rate:
                    bucket.append(now)
                    return True
                return False
            bucket.append(now)
            return True


class GlobalLogger:
    """全局日志系统单例，负责 handler 组装与配置。"""

    def __init__(self, config: LogConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("logsystem")
        self.logger.setLevel(config.level)
        self.logger.propagate = False
        # 清空已有 handler（幂等重配置）
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        cfg = self.config
        # 终端
        console = logging.StreamHandler(stream=sys.stdout)
        console.setLevel(cfg.console_level or cfg.level)
        console.setFormatter(_BusinessFormatter(cfg.console_format))
        # 文件
        file_handler = _RotatingPidFileHandler(cfg)
        file_handler.setLevel(cfg.level)
        # 脱敏过滤器同时挂到两个 handler
        mf = MaskFilter(cfg.effective_sensitive_keys(), cfg.mask_placeholder)
        console.addFilter(mf)
        file_handler.addFilter(mf)
        self.logger.addHandler(console)
        self.logger.addHandler(file_handler)
        self.file_handler = file_handler
        self.console = console
        self.rate_limiter = _RateLimiter(cfg)
        # 清理过期日志
        if cfg.retention_days > 0:
            self._cleanup_old_logs()

    def _cleanup_old_logs(self) -> None:
        import time as _t

        root = Path(self.config.log_dir)
        if not root.exists():
            return
        cutoff = _t.time() - self.config.retention_days * 86400
        for f in root.glob("*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def get_logger(self, name: Optional[str] = None) -> logging.Logger:
        if name:
            return self.logger.getChild(name)
        return self.logger

    def shutdown(self) -> None:
        self.file_handler.close()
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)


def configure(config: Optional[LogConfig] = None) -> GlobalLogger:
    """配置（或重配置）全局日志系统。"""
    global _configured
    cfg = config or LogConfig()
    cfg.ensure_dir()
    with _lock:
        _configured = GlobalLogger(cfg)
    return _configured


def get_logger(name: Optional[str] = None, config: Optional[LogConfig] = None) -> logging.Logger:
    """获取日志器；未配置时使用默认配置自动初始化。"""
    global _configured
    if _configured is None:
        configure(config or LogConfig())
    if config is not None:
        configure(config)
    return _configured.get_logger(name)


def log_business_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    step: Optional[str] = None,
    status: Optional[str] = None,
    error_code: Optional[str] = None,
    error_msg: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """记录一条携带业务上下文的日志。"""
    ctx = {}
    bid = get_business_id()
    if bid is not None:
        ctx["business_id"] = bid
    tid = get_trace_id()
    sid = get_span_id()
    if tid:
        ctx["trace_id"] = tid
    if sid:
        ctx["span_id"] = sid
    if step is not None:
        ctx["step"] = step
    if status is not None:
        ctx["status"] = status
    if error_code is not None:
        ctx["error_code"] = error_code
    if error_msg is not None:
        ctx["error_msg"] = error_msg
    if extra:
        ctx.update(extra)
    # 高频限流：按 step 维度限流
    global _configured
    rl = getattr(_configured, "rate_limiter", None) if _configured else None
    if rl is not None:
        key = step or message
        if not rl.allow(key):
            return  # 被限流丢弃
    merged_extra = {"_logsystem_extra": ctx}
    for k, v in ctx.items():
        merged_extra[k] = v
    logger.log(level, message, extra=merged_extra)