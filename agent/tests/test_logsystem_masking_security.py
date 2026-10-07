"""脱敏与日志完整性安全回归（评审 C-1 / C-2 / C-5）。

覆盖三个已复现缺陷
------------------
- **C-2** 结构化字段走**精确键匹配**，而 ``constants.SENSITIVE_KEYS_DEFAULT``
  文档写的是「小写子串匹配」→ ``user_token`` / ``my_password`` /
  ``session_id`` / ``api_key_v2`` / ``X-Api-Key`` 全部**明文落盘**。
- **C-1** ``_format_file`` 只在值含空格/Tab 时加引号，**不转义换行** →
  含 ``\\n`` 的值（异常堆栈、第三方返回体）会把一条记录拆成多行，并可
  ``\\nbusiness_id=victim cost_ms=999999 status=failed`` **伪造**日志记录，
  污染分析器统计（审计可信性）。
- **C-5** ``error_msg`` / ``stack_trace`` 是「非敏感键名承载的自由文本」，
  键级脱敏对它们无效 → 异常里的 token/密码明文进文件；且 ``Formatter`` 把
  **未脱敏的** ``record.exc_text`` 打到 stdout，而文件里反而没有堆栈。

同时锁定**不能误伤**：``business_id`` / ``order_id`` / ``monkey`` 必须保留。
"""

from __future__ import annotations

import glob
import logging
import os

import pytest


@pytest.fixture()
def logdir(tmp_path):
    from src.logsystem import LogConfig, configure

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))
    yield str(tmp_path)
    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _read(logdir: str) -> str:
    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass
    return "".join(
        open(p, encoding="utf-8").read()
        for p in glob.glob(os.path.join(logdir, "*.log"))
    )


def _lines(logdir: str):
    return [l for l in _read(logdir).splitlines() if l.strip()]


# --------------------------------------------------------------------------- #
# C-2：词元匹配 —— 复合键名必须脱敏，同时不能误伤链路字段
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "key",
    ["user_token", "my_password", "session_id", "api_key_v2",
     "X-Api-Key", "apikey_v2", "access_token_id", "refresh_token",
     "password", "token", "secret"],
)
def test_composite_sensitive_keys_are_masked(logdir, key):
    from src.logsystem import log_event

    log_event(logging.INFO, "probe", extra={key: "TOPSECRET-VALUE"})
    assert "TOPSECRET-VALUE" not in _read(logdir), f"{key} 未脱敏"


@pytest.mark.parametrize("key", ["order_id", "trade_id", "tenant_id", "monkey", "keyword", "bookmark"])
def test_non_sensitive_keys_are_not_masked(logdir, key):
    """不能误伤：含 ``id`` / ``key`` 子串但非敏感的字段名必须保留。

    ``id_card`` 是敏感键；若把敏感键也切词得到 ``id``，就会连 ``order_id`` /
    ``trade_id`` 一起脱敏；同理裸子串匹配会误伤 ``monkey`` / ``keyword`` /
    ``bookmark``。两者都会破坏日志可用性。
    """
    from src.logsystem import log_event

    log_event(logging.INFO, "probe", extra={key: "KEEP-ME"})
    assert "KEEP-ME" in _read(logdir), f"{key} 被误脱敏"


def test_canonical_business_id_cannot_be_overridden_by_extra(logdir):
    """``business_id`` 是**规范字段**，extra 不得覆盖它（C-4 的直接体现）。

    注意：本文件的「非敏感键不被脱敏」不能用 ``business_id`` 表达 —— 它会被
    规范值覆盖，那是**预期**行为（保证链路键可信），与脱敏无关。
    """
    from src.logsystem import log_event, trace_scope

    with trace_scope(business_id="REAL-BID"):
        log_event(logging.INFO, "probe", extra={"business_id": "FORGED"})

    disk = _read(logdir)
    assert "REAL-BID" in disk, "规范 business_id 丢失"
    assert "FORGED" not in disk, "extra 覆盖了规范 business_id"


def test_text_path_and_key_path_agree(logdir):
    """文本路径与结构化路径的匹配语义必须一致（此前一套子串一套精确）。"""
    from src.logsystem import log_event, mask

    text = mask("user_token=ABC123 my_password=DEF456", ("token", "password"))
    assert "ABC123" not in text and "DEF456" not in text

    log_event(logging.INFO, "probe", extra={"user_token": "ABC123", "my_password": "DEF456"})
    disk = _read(logdir)
    assert "ABC123" not in disk and "DEF456" not in disk


# --------------------------------------------------------------------------- #
# C-1：换行/引号注入不得拆行，不得伪造记录
# --------------------------------------------------------------------------- #

def test_newline_in_value_does_not_split_records(logdir):
    from src.logsystem import log_event

    log_event(logging.INFO, "one", step="order")
    log_event(logging.INFO, "inject",
              extra={"error_msg": "upstream:\nbusiness_id=victim cost_ms=999999"})
    log_event(logging.INFO, "three", step="order")

    assert len(_lines(logdir)) == 3, "换行未转义 → 记录被拆成多行"


def test_newline_injection_cannot_forge_analyzer_stats(logdir):
    """核心断言：伪造载荷不得影响分析器统计。"""
    from src.logsystem import log_event
    from src.logsystem.analyzer import LogAnalyzer

    log_event(logging.INFO, "normal", step="order", status="success")
    log_event(logging.INFO, "inject", extra={
        "error_msg": "upstream:\nbusiness_id=victim cost_ms=999999 level=ERROR status=failed",
    })

    report = LogAnalyzer(logdir).analyze()
    assert report["total_logs"] == 2, f"出现伪造记录: {report}"
    assert not report.get("error_count"), f"伪造出错误计数: {report}"
    assert (report.get("max_ms") or 0) < 999999, f"伪造出耗时: {report}"


def test_quotes_and_backslashes_are_escaped(logdir):
    from src.logsystem import log_event

    log_event(logging.INFO, "quote", extra={"error_msg": 'he said "hi" C:\\tmp\\x'})
    lines = _lines(logdir)
    assert len(lines) == 1
    assert '\\"hi\\"' in lines[0], lines[0]


# --------------------------------------------------------------------------- #
# C-5：异常文本与自由文本字段必须脱敏（文件 + 控制台）
# --------------------------------------------------------------------------- #

def test_exception_message_is_masked_in_file(logdir):
    # 触发异常日志路径（logsystem 已由 fixture 配置）

    logger = logging.getLogger("logsystem")
    try:
        raise ValueError("auth failed api_key=SK-EXC-LEAK password=hunter2")
    except ValueError:
        logger.error("handler blew up", exc_info=True)

    disk = _read(logdir)
    assert "SK-EXC-LEAK" not in disk, "异常消息明文落盘"
    assert "hunter2" not in disk, "异常消息明文落盘"


def test_traceback_present_in_file_after_masking(logdir):
    """脱敏不能把堆栈弄丢（此前 `_record_fields` 里根本没有 exc_text）。"""
    logger = logging.getLogger("logsystem")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.error("handler blew up", exc_info=True)

    disk = _read(logdir)
    assert "Traceback" in disk, "堆栈在文件中丢失"


def test_console_exception_text_is_masked(logdir, capsys):
    """stdout 是独立泄露汇聚点（journald / Docker / CI）。"""
    logger = logging.getLogger("logsystem")
    try:
        raise ValueError("auth failed api_key=SK-CONSOLE-LEAK")
    except ValueError:
        logger.error("handler blew up", exc_info=True)

    captured = capsys.readouterr()
    both = captured.out + captured.err
    assert "SK-CONSOLE-LEAK" not in both, "异常消息明文进控制台"


@pytest.mark.parametrize("field", ["error_msg", "stack_trace", "reason", "result"])
def test_freetext_extra_fields_are_masked(logdir, field):
    from src.logsystem import log_event

    log_event(logging.INFO, "probe", extra={field: "token=PLAINTOKEN password=PLAINPW"})
    disk = _read(logdir)
    assert "PLAINTOKEN" not in disk, f"{field} 未做值级脱敏"
    assert "PLAINPW" not in disk, f"{field} 未做值级脱敏"


def test_non_string_message_is_masked(logdir):
    """非 str 的 msg（dict / list）此前完全绕过掩码。"""
    from src.logsystem import log_event

    log_event(logging.INFO, {"password": "DICT-MSG-PW"})
    assert "DICT-MSG-PW" not in _read(logdir)
