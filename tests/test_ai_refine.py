# -*- coding: utf-8 -*-
"""对话式纠正测试：llm_classify 多轮 history 注入提示词、API 透传。"""
import json

import server
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def _capture_llm(monkeypatch, reply=None):
    captured = {}

    def fake_chat(messages, **kw):
        captured["messages"] = messages
        return reply or json.dumps({"category": "手办/恐龙", "is_new": "true",
                                    "reason": "结合你的纠正", "alias": "恐龙模型",
                                    "confidence": "high"}, ensure_ascii=False)
    monkeypatch.setattr(server.llm_client, "chat", fake_chat)
    return captured


def test_history_injected_into_prompt(monkeypatch):
    captured = _capture_llm(monkeypatch)
    history = [{"category": "浮雕画", "reason": "含浮雕画关键词", "feedback": "这是侏罗纪公园的周边"}]
    res = server.llm_classify({"filename": "x.3mf", "title": "神秘模型"}, [], "其他/未分类",
                              hint="侏罗纪周边", history=history)
    text = "\n".join(m["content"] for m in captured["messages"])
    assert "多轮纠正对话" in text
    assert "第 1 轮结论：分类「浮雕画」" in text
    assert "这是侏罗纪公园的周边" in text
    assert "不要重复已被否定的结论" in text
    assert res["category"] == "手办/恐龙" and res["confidence"] == "high"


def test_history_empty_no_round_section(monkeypatch):
    captured = _capture_llm(monkeypatch)
    server.llm_classify({"filename": "x.3mf"}, [], "其他/未分类")
    text = "\n".join(m["content"] for m in captured["messages"])
    assert "多轮纠正对话" not in text


def test_history_truncated_to_five_rounds(monkeypatch):
    captured = _capture_llm(monkeypatch)
    history = [{"category": f"c{i}", "reason": "", "feedback": f"纠正{i}"} for i in range(9)]
    server.llm_classify({"filename": "x.3mf"}, [], "其他/未分类", history=history)
    text = "\n".join(m["content"] for m in captured["messages"])
    assert "第 5 轮" in text and "第 6 轮" not in text


def test_api_llm_classify_passes_history(monkeypatch, client):
    captured = _capture_llm(monkeypatch)
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    f = fetch(client, "/api/upload", files=[("file", "h.3mf", _make_3mf_bytes("纠正测试", "CNr1"))])
    fid = f["results"][0]["file"]["id"]
    res = fetch(client, "/api/llm-classify", data={
        "id": fid, "hint": "电影周边",
        "history": [{"category": "浮雕画", "reason": "r", "feedback": "fb"}, "bad-row", {"nope": 1}],
    })
    assert res.get("category") == "手办/恐龙", res
    # API 层只保留合法 dict 且字段被截断清洗
    hist = None
    for m in captured["messages"]:
        if m["role"] == "user" and "多轮纠正对话" in m.get("content", ""):
            hist = m["content"]
    assert hist and "fb" in hist and "bad-row" not in hist
