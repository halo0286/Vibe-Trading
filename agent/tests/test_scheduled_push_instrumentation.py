"""定时简报投递埋点回归（评审 M6）。

已复现缺陷
----------
`src/channels/manager.py` 的注释声称 `_send_with_retry` 是「全部 16 个 IM 渠道
出站消息的唯一漏斗」，但 `src/api/scheduled_routes.py` 的定时简报**直接**调用
`adapter.send_with_receipt()`：

* 它不经过 `_send_with_retry`（那只被 `channel.send` 路径使用）；
* 且 Feishu 等适配器**覆写** `send_with_receipt`，也不会回调 `send`。

结果是场景 10 的**定时** IM 推送完全没有 `channel.push` 事件 ——
"渠道覆盖"结论需要下修。修复：在投递点单独埋 `channel.scheduled_push`。

同时锁定隐私：`chat_id` 落盘为不可逆短哈希、简报正文只记长度。
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import pathlib
import re
import sys
import types

import pytest


def _lines(log_dir) -> list:
    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass
    out = []
    for path in glob.glob(os.path.join(str(log_dir), "*.log")):
        out += [l for l in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def _fields(line: str) -> dict:
    out = {}
    for k, v in re.findall(r'(\w+)=("[^"]*"|\S+)', line):
        out[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v
    return out


def _configure(tmp_path):
    from src.logsystem import LogConfig, configure

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))


def _install_fake_host(adapter):
    class _Manager:
        def get_channel(self, channel):
            return adapter

    host = types.ModuleType("api_server")
    host._channel_manager = _Manager()
    sys.modules["api_server"] = host
    return host


@pytest.fixture()
def fake_host():
    created = []

    def _make(adapter):
        created.append("api_server")
        return _install_fake_host(adapter)

    yield _make
    for name in created:
        sys.modules.pop(name, None)


def test_scheduled_briefing_emits_push_event(tmp_path, fake_host):
    """定时简报必须产生 channel.scheduled_push 事件（此前完全没有）。"""
    from src.channels.bus.events import DeliveryReceipt
    from src.api import scheduled_routes as SR

    _configure(tmp_path)

    class Adapter:
        async def send_with_receipt(self, msg):
            return DeliveryReceipt(status="sent", sent_at=1)

    fake_host(Adapter())
    receipt = asyncio.run(
        SR._send_scheduled_briefing("feishu", "+8613800138000", "今日简报", None)
    )
    assert receipt.status == "sent"

    fields = _fields(_lines(tmp_path)[0])
    assert fields["step"] == "channel.scheduled_push", fields
    assert fields["status"] == "success", fields
    assert fields["channel"] == "feishu", fields
    assert fields["receipt_status"] == "sent", fields


def test_scheduled_briefing_does_not_leak_pii_or_content(tmp_path, fake_host):
    """chat_id 必须哈希、正文只记长度。"""
    from src.channels.bus.events import DeliveryReceipt
    from src.api import scheduled_routes as SR

    _configure(tmp_path)

    class Adapter:
        async def send_with_receipt(self, msg):
            return DeliveryReceipt(status="sent", sent_at=1)

    fake_host(Adapter())
    phone = "+8613800138000"
    body = "今日简报：持仓 +2.3%，密码 hunter2"
    asyncio.run(SR._send_scheduled_briefing("whatsapp", phone, body, None))

    line = _lines(tmp_path)[0]
    assert phone not in line, "chat_id（手机号）明文落盘"
    assert "hunter2" not in line, "简报正文落盘"
    assert "持仓" not in line, "简报正文落盘"

    fields = _fields(line)
    assert fields["chat_id_hash"], fields
    assert fields["chat_id_hash"] != phone
    assert fields["content_chars"] == str(len(body)), fields


def test_scheduled_briefing_failure_is_logged(tmp_path, fake_host):
    """投递异常必须记 failed 并带原因，且原异常照常抛出。"""
    from src.api import scheduled_routes as SR

    _configure(tmp_path)

    class Adapter:
        async def send_with_receipt(self, msg):
            raise RuntimeError("webhook 403")

    fake_host(Adapter())
    with pytest.raises(RuntimeError):
        asyncio.run(SR._send_scheduled_briefing("feishu", "target", "body", None))

    fields = _fields(_lines(tmp_path)[0])
    assert fields["status"] == "failed", fields
    assert fields["level"] == "WARNING", fields
    assert "webhook 403" in (fields.get("error_msg") or ""), fields


def test_missing_manager_does_not_log_success(tmp_path, fake_host):
    """未运行的渠道运行时是**可重试失败**，不得记成功。"""
    from src.api import scheduled_routes as SR

    _configure(tmp_path)
    host = types.ModuleType("api_server")
    host._channel_manager = None
    sys.modules["api_server"] = host

    with pytest.raises(RuntimeError):
        asyncio.run(SR._send_scheduled_briefing("feishu", "t", "b", None))

    # 该分支在埋点之前抛错，因此不应产生任何 success 记录
    assert not [l for l in _lines(tmp_path) if "status=success" in l]
