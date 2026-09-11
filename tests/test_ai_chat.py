# -*- coding: utf-8 -*-
"""对话可操作化测试：结构化输出（reply/file_ids/action）、action 白名单、纯文本降级。"""
import json

import server
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def _upload_pair(client):
    ids = []
    for name, title, did in [("g1.3mf", "高达RX78", "CNc1"), ("g2.3mf", "高达沙扎比", "CNc2")]:
        r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, did))])
        ids.append(r["results"][0]["file"]["id"])
    return ids


def _setup(monkeypatch, client, llm_raw):
    """上传两个文件并伪造 LLM 输出；返回文件 id 列表。"""
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(server.llm_client, "chat", lambda messages, **kw: llm_raw)
    return _upload_pair(client)


def test_chat_structured_output(monkeypatch, client):
    ids = _upload_pair(client)
    raw = json.dumps({
        "reply": "找到 2 个高达模型，建议归档到 01_IP授权/高达Gundam",
        "file_ids": ids,
        "action": {"type": "archive", "ids": ids, "target": "IP·高达"},
    }, ensure_ascii=False)
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(server.llm_client, "chat", lambda messages, **kw: raw)
    res = fetch(client, "/api/chat", data={"session_id": "t1", "message": "帮我找高达"})
    assert "高达" in res["reply"]
    assert sorted(f["id"] for f in res["files"]) == sorted(ids)
    assert res["files"][0]["alias"]
    assert res["action"]["type"] == "archive"
    assert sorted(res["action"]["ids"]) == sorted(ids)
    assert res["action"]["target"] == "IP·高达"


def test_chat_action_whitelist(monkeypatch, client):
    ids = _upload_pair(client)
    raw = json.dumps({
        "reply": "建议删除",
        "file_ids": [ids[0]],
        "action": {"type": "delete", "ids": ids},
    }, ensure_ascii=False)
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(server.llm_client, "chat", lambda messages, **kw: raw)
    res = fetch(client, "/api/chat", data={"session_id": "t2", "message": "删了它们"})
    assert res["action"] is None, "非 archive 动作必须被丢弃"
    assert len(res["files"]) == 1


def test_chat_plain_text_fallback(monkeypatch, client):
    _setup(monkeypatch, client, "这是一个普通的文本回复，不是 JSON。")
    res = fetch(client, "/api/chat", data={"session_id": "t3", "message": "你好"})
    assert res["reply"].startswith("这是一个普通")
    assert res["files"] == [] and res["action"] is None


def test_chat_archive_via_action_executes(monkeypatch, client):
    """前端确认后走的链路：recategorize + apply 真正归档。"""
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(server.llm_client, "chat",
                        lambda m, **kw: '{"reply":"ok","file_ids":[],"action":null}')
    ids = []
    for name, title, did in [("g1.3mf", "高达RX78", "CNc3"), ("g2.3mf", "高达沙扎比", "CNc4")]:
        r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, did))])
        ids.append(r["results"][0]["file"]["id"])
    for fid in ids:
        assert fetch(client, "/api/recategorize", data={"id": fid, "category": "IP·高达"})["ok"]
    res = fetch(client, "/api/apply", data={"ids": ids, "rename": True})
    assert all(x["ok"] for x in res["results"])
    applied = fetch(client, "/api/files?status=applied")["files"]
    assert len(applied) == 2
