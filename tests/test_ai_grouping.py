# -*- coding: utf-8 -*-
"""批次2 AI 关联判型测试：LLM 解析、降级、建组跳过已分组。"""
import json

import server
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def _up(client, name, title, did):
    r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, did))])
    return r["results"][0]["file"]


def test_suggest_ai_uses_llm(monkeypatch, client):
    a = _up(client, "rx_body.3mf", "高达身体", "CNai1")
    b = _up(client, "rx_weapon.3mf", "高达武器", "CNai2")
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)

    def fake_chat(messages, **kw):
        text = "\n".join(m["content"] for m in messages)
        assert "关联分析助手" in text and f"id={a['id']}" in text
        return json.dumps({"name": "RX78 高达套件", "primary_id": a["id"],
                           "roles": {str(a["id"]): "component", str(b["id"]): "component"}}, ensure_ascii=False)
    monkeypatch.setattr(server.llm_client, "chat", fake_chat)
    sug = fetch(client, "/api/groups/suggest-ai")["suggestions"]
    assert len(sug) == 1 and sug[0]["ai"] is True
    assert sug[0]["name"] == "RX78 高达套件"
    assert sug[0]["primary_id"] == a["id"]
    assert sug[0]["roles"][str(b["id"])] in ("component", None) or sug[0]["roles"].get(b["id"]) == "component"
    # 一键按建议建组
    r = fetch(client, "/api/groups", data={
        "create": {"name": sug[0]["name"], "file_ids": sug[0]["file_ids"],
                   "roles": {str(k): v for k, v in sug[0]["roles"].items()},
                   "primary_id": sug[0]["primary_id"]}})
    assert r["ok"] and r["group"]["name"] == "RX78 高达套件"


def test_suggest_ai_filters_grouped(monkeypatch, client):
    a = _up(client, "g1.3mf", "组一", "CNai3")
    b = _up(client, "g2.3mf", "组二", "CNai3")
    fetch(client, "/api/groups", data={"create": {"name": "已有组", "file_ids": [a["id"], b["id"]]}})
    # 第二对用不同几何，避免 geom_sig 信号把两组合并
    import zipfile, io
    def make(title, did, verts):
        xml = f'''<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
<metadata name="Title">{title}</metadata><metadata name="DesignModelId">{did}</metadata>
<resources><object id="1"><mesh><vertices>{''.join(f'<vertex x="{i}" y="0" z="0"/>' for i in range(verts))}</vertices>
<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources><build><item objectid="1"/></build></model>'''
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("3D/3dmodel.model", xml)
        r = fetch(client, "/api/upload", files=[("file", "x.3mf", buf.getvalue())])
        return r["results"][0]["file"]
    c = make("新组一", "CNai4", 8)
    d = make("新组二", "CNai4", 8)
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(server.llm_client, "chat",
                        lambda m, **kw: json.dumps({"name": "新作品", "primary_id": c["id"], "roles": {}}))
    sug = fetch(client, "/api/groups/suggest-ai")["suggestions"]
    assert len(sug) == 1, "已全部建组的簇不应再出现"
    assert sorted(sug[0]["file_ids"]) == sorted([c["id"], d["id"]])


def test_suggest_ai_fallback_without_llm(monkeypatch, client):
    _up(client, "f1.3mf", "免AI一", "CNai5")
    _up(client, "f2.3mf", "免AI二", "CNai5")
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: False)
    sug = fetch(client, "/api/groups/suggest-ai")["suggestions"]
    assert len(sug) == 1 and sug[0]["ai"] is False
    assert sug[0]["name"], "降级时也必须有命名"
    assert set(sug[0]["roles"].values()) == {"component"}


def test_suggest_ai_bad_llm_json_falls_back(monkeypatch, client):
    _up(client, "bad1.3mf", "坏JSON一", "CNai6")
    _up(client, "bad2.3mf", "坏JSON二", "CNai6")
    monkeypatch.setattr(server.llm_client, "llm_configured", lambda: True)
    monkeypatch.setattr(server.llm_client, "chat", lambda m, **kw: "这不是JSON")
    sug = fetch(client, "/api/groups/suggest-ai")["suggestions"]
    assert len(sug) == 1 and sug[0]["ai"] is False, "LLM 输出异常应降级不抛错"
    assert sug[0]["name"]
