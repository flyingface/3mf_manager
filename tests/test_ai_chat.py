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


def test_chat_history_passed_to_llm(monkeypatch, client):
    """回归：多轮对话必须把 session 历史送进 LLM（批次D 重构曾整体丢失上下文）。"""
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    seen = []

    def fake_chat(messages, **kw):
        seen.append(messages)
        return json.dumps({"reply": "第一条回复", "file_ids": []}, ensure_ascii=False)

    monkeypatch.setattr(server.llm_client, "chat", fake_chat)
    try:
        fetch(client, "/api/chat", data={"session_id": "th", "message": "第一问"})
        fetch(client, "/api/chat", data={"session_id": "th", "message": "第二问"})
        assert len(seen) == 2
        roles = [(m["role"], m["content"]) for m in seen[1]]
        assert any(r == "user" and c == "第一问" for r, c in roles), "第 2 次调用必须带上第 1 轮提问"
        assert any(r == "assistant" and "第一条回复" in c for r, c in roles), "第 2 次调用必须带上第 1 轮回复"
        assert any(r == "user" and c == "第二问" for r, c in roles)
    finally:
        server.llm_client.session_clear("th")


def test_chat_failed_turn_not_in_history(monkeypatch, client):
    """失败的本轮提问只回退自身，不丢历史；重试不从残骸开始。"""
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    calls = {"n": 0}

    def flaky(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("临时超时")
        return json.dumps({"reply": f"回复{calls['n']}", "file_ids": []}, ensure_ascii=False)

    monkeypatch.setattr(server.llm_client, "chat", flaky)
    try:
        fetch(client, "/api/chat", data={"session_id": "tf", "message": "第一问"})
        fetch(client, "/api/chat", data={"session_id": "tf", "message": "会失败的一问"})
        res = fetch(client, "/api/chat", data={"session_id": "tf", "message": "第二问"})
        assert res["reply"] == "回复3"
        sess = server.llm_client.session_get("tf")
        contents = [m["content"] for m in sess]
        contents = [m["content"] for m in sess]
        assert "会失败的一问" not in contents, "失败提问不能留在 session"
        assert "第一问" in contents and "回复1" in contents
    finally:
        server.llm_client.session_clear("tf")
