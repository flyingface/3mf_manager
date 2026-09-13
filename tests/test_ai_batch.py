# -*- coding: utf-8 -*-
"""批量 AI 整理测试：SSE 流式分类（规则跳过）、apply-plan 批量归档、路径校验。"""
import json, http.client

import server
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def _sse_post(base, path, data):
    """POST JSON 并按 SSE 解析响应事件列表（服务端 Connection:close，读到 EOF）。"""
    host, port = base.replace("http://", "").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=30)
    try:
        body = json.dumps(data)
        c.request("POST", path, body=body,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
        events = []
        for chunk in raw.split("\n\n"):
            for line in chunk.split("\n"):
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return r.status, events
    finally:
        c.close()


def _fake_llm(monkeypatch, category="手办/恐龙", conf="high", is_new="true"):
    """伪造 LLM 返回，避免测试真实调用；同时放行 llm_configured 闸门（不依赖本机 config.json）。"""
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    def fake_chat(messages, **kw):
        return json.dumps({"category": category, "is_new": is_new,
                           "reason": "恐龙主题", "alias": "恐龙模型",
                           "confidence": conf}, ensure_ascii=False)
    monkeypatch.setattr(server.llm_client, "chat", fake_chat)


def _upload(client, name, title, design_id):
    r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, design_id))])
    assert r["results"][0]["ok"], r
    return r["results"][0]["file"]


def test_batch_sse_skips_rule_matched(monkeypatch, client):
    _fake_llm(monkeypatch)
    ruled = _upload(client, "gundam.3mf", "高达RX78", "CNb1")      # 规则命中 IP·高达
    unknown = _upload(client, "myst.3mf", "神秘模型甲", "CNb2")     # 规则认不出 → 送 LLM
    status, events = _sse_post(client, "/api/llm-batch-classify", {"ids": [ruled["id"], unknown["id"]]})
    assert status == 200
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done"
    assert "skip" in kinds and "progress" in kinds
    skip = next(e for e in events if e["type"] == "skip")
    assert skip["id"] == ruled["id"] and skip["rule_category"] == "IP·高达"
    # 跳过仅指不调 LLM：结果仍带分类/别名/路径供计划全量覆盖
    assert skip["alias"] and skip["target_dir"]
    prog = next(e for e in events if e["type"] == "progress")
    assert prog["id"] == unknown["id"]
    assert prog["category"] == "手办/恐龙" and prog["confidence"] == "high"
    done = events[-1]
    assert done["total"] == 2 and done["skipped"] == 1


def test_batch_sse_llm_failure_yields_error_progress(monkeypatch, client):
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    def broken_chat(messages, **kw):
        raise RuntimeError("超时")
    monkeypatch.setattr(server.llm_client, "chat", broken_chat)
    f = _upload(client, "x.3mf", "神秘模型乙", "CNb3")
    status, events = _sse_post(client, "/api/llm-batch-classify", {"ids": [f["id"]]})
    assert status == 200
    prog = next(e for e in events if e["type"] == "progress")
    assert "error" in prog and "超时" in prog["error"]
    assert events[-1]["type"] == "done"  # 单文件失败不中断整批


def test_apply_plan_archives_and_creates_category(monkeypatch, client):
    _fake_llm(monkeypatch)
    f = _upload(client, "dino.3mf", "神秘模型丙", "CNb4")
    res = fetch(client, "/api/apply-plan", data={
        "items": [{"id": f["id"], "category": "手办/恐龙", "alias": "恐龙模型"}],
        "new_categories": ["手办/恐龙"],
    })
    assert res["ok"] and len(res["applied"]) == 1 and not res["failed"]
    root = server.LIBRARY_ROOT
    import os
    expected = None
    for base, dirs, files in os.walk(root):
        for fn in files:
            if fn == "恐龙模型.3mf":
                expected = os.path.join(base, fn)
    assert expected, "归档文件未落到分类目录"
    cats = fetch(client, "/api/categories")
    assert "手办/恐龙" in cats["custom"]
    row = fetch(client, "/api/files?status=applied")["files"]
    assert any(x["id"] == f["id"] and x["category"] == "手办/恐龙" for x in row)


def test_apply_plan_rejects_invalid_target(monkeypatch, client):
    _fake_llm(monkeypatch)
    f = _upload(client, "bad.3mf", "神秘模型丁", "CNb5")
    res = fetch(client, "/api/apply-plan", data={
        "items": [{"id": f["id"], "category": "手办/恐龙", "target_dir": "../evil"}],
    })
    assert res["ok"] and not res["applied"]
    assert res["failed"] and "不合法" in res["failed"][0]["error"]
    # 文件仍在待整理且未被改坏
    row = fetch(client, "/api/files?status=pending")["files"]
    assert any(x["id"] == f["id"] for x in row)


def test_apply_plan_requires_items(client):
    r = fetch(client, "/api/apply-plan", data={"items": []})
    assert "error" in r
