# -*- coding: utf-8 -*-
"""流式对话测试：chat_stream SSE 解析、META 行拆分、/api/chat/stream 事件序列。"""
import json

import server
import llm_client
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def test_chat_stream_parses_sse(monkeypatch):
    class FakeResp:
        def __init__(self, lines):
            self._lines = lines

        def __iter__(self):
            return iter(self._lines)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    lines = [
        b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n',
        b': keep-alive\n\n',
        b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n\n',
        b'data: {"choices":[{"delta":{}}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    monkeypatch.setattr(llm_client.urllib.request, "urlopen", lambda req, timeout=None: FakeResp(lines))
    got = list(server.llm_client.chat_stream([{"role": "user", "content": "hi"}]))
    assert got == ["你", "好"]


def test_split_chat_meta_variants():
    # 标准：正文 + META 行
    reply, meta = server._split_chat_meta('找到 2 个模型。\nMETA:{"file_ids":[1,2],"action":null}')
    assert reply == "找到 2 个模型。"
    assert json.loads(meta)["file_ids"] == [1, 2]
    # 无 META：整体是正文
    reply, meta = server._split_chat_meta("只是普通回复")
    assert reply == "只是普通回复" and meta is None
    # META 在行首
    reply, meta = server._split_chat_meta('META:{"file_ids":[]}')
    assert reply == "" and json.loads(meta)["file_ids"] == []


def test_chat_stream_endpoint_events(monkeypatch, client):
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    f = fetch(client, "/api/upload", files=[("file", "d.3mf", _make_3mf_bytes("高达模型", "CNd1"))])
    fid = f["results"][0]["file"]["id"]

    def fake_stream(messages, temperature=0.3, max_tokens=800):
        yield "找到 1 个高达模型。"
        yield "\n"
        yield 'META:{"file_ids":[' + str(fid) + '],"action":{"type":"archive","ids":[' + str(fid) + '],"target":"IP·高达"}}'

    monkeypatch.setattr(server.llm_client, "chat_stream", fake_stream)

    # SSE 读取（Connection:close）
    import http.client
    host, port = client.replace("http://", "").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=15)
    try:
        c.request("POST", "/api/chat/stream", body=json.dumps({"session_id": "ts", "message": "找高达"}),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
    finally:
        c.close()
    events = [json.loads(line[6:]) for chunk in raw.split("\n\n") for line in chunk.split("\n") if line.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "final"
    deltas = "".join(e["delta"] for e in events if e["type"] == "delta")
    assert "META" not in deltas, "元数据行不能泄漏到可见回复"
    assert "高达" in deltas
    final = events[-1]
    assert final["reply"] == "找到 1 个高达模型。"
    assert final["files"][0]["id"] == fid
    assert final["action"]["target"] == "IP·高达"


def test_chat_nonstream_uses_meta_protocol(monkeypatch, client):
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    f = fetch(client, "/api/upload", files=[("file", "e.3mf", _make_3mf_bytes("高达模型E", "CNe1"))])
    fid = f["results"][0]["file"]["id"]

    def fake_chat(messages, **kw):
        return '回复正文。\nMETA:{"file_ids":[' + str(fid) + '],"action":null}'

    monkeypatch.setattr(server.llm_client, "chat", fake_chat)
    res = fetch(client, "/api/chat", data={"session_id": "tn", "message": "找高达"})
    assert res["reply"] == "回复正文。"
    assert res["files"][0]["id"] == fid and res["action"] is None


def test_config_exposes_latency(monkeypatch, client):
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    server.llm_client.last_latency["ms"] = 123
    cfg = fetch(client, "/api/config")
    assert cfg["last_ai_latency_ms"] == 123


def test_split_chat_meta_bare_no_newline():
    """回归：模型漏掉换行直接输出 META: 时同样截断，不把元数据留给可见回复。"""
    reply, meta = server._split_chat_meta('你好META:{"file_ids":[1]}')
    assert reply == "你好"
    assert json.loads(meta)["file_ids"] == [1]


def test_chat_stream_no_newline_meta_not_leaked(monkeypatch, client):
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)

    def fake_stream(messages, temperature=0.3, max_tokens=800):
        yield "直接回答。"
        yield 'META:{"file_ids":[]}'

    monkeypatch.setattr(server.llm_client, "chat_stream", fake_stream)
    import http.client
    host, port = client.replace("http://", "").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=15)
    try:
        c.request("POST", "/api/chat/stream", body=json.dumps({"session_id": "tnl", "message": "问"}),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
    finally:
        c.close()
    events = [json.loads(line[6:]) for chunk in raw.split("\n\n") for line in chunk.split("\n") if line.startswith("data: ")]
    deltas = "".join(e["delta"] for e in events if e["type"] == "delta")
    assert "META" not in deltas, "无换行的 META 也必须截留"
    assert events[-1]["type"] == "final" and events[-1]["reply"] == "直接回答。"
