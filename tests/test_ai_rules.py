# -*- coding: utf-8 -*-
"""教学闭环测试：学习规则匹配优先级、/api/rules CRUD、摄入命中计数。"""
import server
import classify
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def test_learned_rule_beats_builtin(monkeypatch):
    monkeypatch.setattr(server, "load_learned_rules", lambda: [
        {"keyword": "折扇", "category": "明星周边", "hits": 0}])
    # 「折扇」无内置规则，但验证规则优先于其他匹配（无命中内置时直接用规则）
    assert server.categorize("", "邓紫棋折扇.3mf", "浮雕折扇",
                             learned_rules=server.load_learned_rules()) == "明星周边"


def test_learned_rule_priority_over_custom(monkeypatch):
    monkeypatch.setattr(server, "load_learned_rules", lambda: [])
    rules = [{"keyword": "高达模型", "category": "手办/机甲收藏", "hits": 0}]
    # 显式关键词规则优先于内置 IP 关键词
    got = classify.categorize("", "我的高达模型.3mf", "", learned_rules=rules)
    assert got == "手办/机甲收藏"


def test_rules_crud(client):
    # 列表为空
    assert fetch(client, "/api/rules")["rules"] == []
    # 新增
    r = fetch(client, "/api/rules", data={"keyword": "折扇", "category": "明星周边"})
    assert r["ok"] and r["rule"]["keyword"] == "折扇"
    # 重复新增不重复存储，但保留 hits
    fetch(client, "/api/rules", data={"keyword": "折扇", "category": "明星周边", "hits_keep": 1})
    assert len(fetch(client, "/api/rules")["rules"]) == 1
    # 关键词过短拒绝
    assert "error" in fetch(client, "/api/rules", data={"keyword": "扇", "category": "x"})
    # 删除
    r = fetch(client, "/api/rules", data={"delete": True, "keyword": "折扇", "category": "明星周边"})
    assert r["ok"] and fetch(client, "/api/rules")["rules"] == []


def test_ingest_counts_rule_hits(client):
    # 教一条规则：关键词「神秘模型」→ 手办/恐龙
    fetch(client, "/api/rules", data={"keyword": "神秘模型", "category": "手办/恐龙"})
    r = fetch(client, "/api/upload", files=[("file", "hit.3mf", _make_3mf_bytes("神秘模型一号", "CNe9"))])
    assert r["results"][0]["ok"]
    assert r["results"][0]["file"]["category"] == "手办/恐龙"
    rules = fetch(client, "/api/rules")["rules"]
    assert rules[0]["hits"] == 1
