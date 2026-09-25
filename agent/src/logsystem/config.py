"""config.py - 日志系统配置。全部字段可配置，提供 dataclass 默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .constants import SENSITIVE_KEYS_DEFAULT


@dataclass
class LogConfig:
    """日志系统总配置。

    Attributes:
        log_dir: 日志文件目录。
        rotation_bytes: 单文件滚动阈值（字节），默认 100MB。
        level: 全局日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）。
        console_level: 终端输出级别；None 时跟随 level。
        console_format: 终端格式模板（human-readable）。
        file_format: 文件格式模板。默认 JSON Lines 风格 key=value；设为 "json" 输出标准 JSON 对象（每行一个）。
        retention_days: 日志保留天数；0 表示不清理。
        max_files_per_day: 同一进程同一天最多文件数（安全上限，0 表示不限制）。
        sensitive_keys: 额外敏感键（默认敏感键见 constants.SENSITIVE_KEYS_DEFAULT）。
        sensitive_whitelist: 敏感键白名单（命中则该键不做脱敏）。
        mask_placeholder: 脱敏替换字符串。
        sample_rate: 高频日志采样率（0.0~1.0，1.0 表示全量）；配合 rate_limit_* 使用。
        rate_limit_window_seconds: 限流时间窗（秒）。
        rate_limit_max: 时间窗内最多条数，超出则按 sample_rate 抽样/丢弃。
        max_value_length: 单个字段值记录的最大字符数（防大对象）。
        max_args_length: 函数入参序列化最大字符数。
        enable_async_analysis: 业务完成后是否自动发起分析任务。
        analysis_scope: 分析范围（business_id 或 trace_id）。
        business_id_field: business_id 来源字段名（供 analyzer 读取）。
        extra_fields: 额外注入到每条日志的固定字段。
    """

    log_dir: str = "./logs"
    rotation_bytes: int = 100 * 1024 * 1024
    level: str = "INFO"
    console_level: Optional[str] = None
    console_format: str = (
        "{asctime} [{levelname}] {business_id} {message}"
    )
    file_format: str = "kv"  # "kv" 或 "json"
    retention_days: int = 7
    max_files_per_day: int = 0
    sensitive_keys: Sequence[str] = field(default_factory=tuple)
    sensitive_whitelist: Sequence[str] = field(default_factory=tuple)
    mask_placeholder: str = "***"
    sample_rate: float = 1.0
    rate_limit_window_seconds: float = 1.0
    rate_limit_max: int = 0  # 0 表示不限流
    max_value_length: int = 512
    max_args_length: int = 2048
    enable_async_analysis: bool = True
    analysis_scope: str = "business_id"  # "business_id" | "trace_id"
    business_id_field: str = "business_id"
    extra_fields: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.level = (self.level or "INFO").upper()
        if self.console_level:
            self.console_level = self.console_level.upper()
        if not (0.0 <= self.sample_rate <= 1.0):
            raise ValueError("sample_rate 必须在 [0,1] 区间")

    def effective_sensitive_keys(self) -> List[str]:
        """合并默认敏感键与自定义敏感键，并排除白名单。"""
        merged = list(SENSITIVE_KEYS_DEFAULT) + list(self.sensitive_keys)
        wl = {w.lower() for w in self.sensitive_whitelist}
        result = [k for k in merged if k.lower() not in wl]
        # 去重保序
        seen = set()
        uniq = []
        for k in result:
            if k.lower() not in seen:
                seen.add(k.lower())
                uniq.append(k)
        return uniq

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_env(cls) -> "LogConfig":
        """从环境变量构建配置（便于无代码配置）。"""
        cfg = cls()
        if os.getenv("LOG_DIR"):
            cfg.log_dir = os.environ["LOG_DIR"]
        if os.getenv("LOG_ROTATION_BYTES"):
            cfg.rotation_bytes = int(os.environ["LOG_ROTATION_BYTES"])
        if os.getenv("LOG_LEVEL"):
            cfg.level = os.environ["LOG_LEVEL"]
        if os.getenv("LOG_RETENTION_DAYS"):
            cfg.retention_days = int(os.environ["LOG_RETENTION_DAYS"])
        return cfg

    def ensure_dir(self) -> Path:
        p = Path(self.log_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p