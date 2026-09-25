# -*- coding: utf-8 -*-
"""打印标记测试：files.printed 标记/取消 + 按打印状态筛选（v4 迁移）。"""
from tests.test_api import fetch, _make_3mf_bytes


def _upload(client, name, title, design_id="CNpr"):
    r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, design_id))])
    assert r["results"][0]["ok"], r
    return r["results"][0]["file"]


def test_new_file_defaults_unprinted(client):
    f = _upload(client, "m1.3mf", "打印标记模型", "CNpr1")
    assert f["printed"] == 0


def test_set_printed_toggle_and_filter(client):
    a = _upload(client, "a.3mf", "已打印模型", "CNprA")
    b = _upload(client, "b.3mf", "未打印模型", "CNprB")
    # 标记 a 已打印
    r = fetch(client, "/api/set-printed", data={"id": a["id"], "printed": True})
    assert r["ok"] and r["printed"] is True
    rows = {x["filename"]: x for x in fetch(client, "/api/files")["files"]}
    assert rows["a.3mf"]["printed"] == 1
    assert rows["b.3mf"]["printed"] == 0
    # 筛选：已打印 / 未打印
    yes = fetch(client, "/api/files?printed=1")["files"]
    assert [x["filename"] for x in yes] == ["a.3mf"]
    no = fetch(client, "/api/files?printed=0")["files"]
    assert [x["filename"] for x in no] == ["b.3mf"]
    # 取消标记
    r2 = fetch(client, "/api/set-printed", data={"id": a["id"], "printed": False})
    assert r2["ok"] and r2["printed"] is False
    assert fetch(client, "/api/files?printed=1")["files"] == []
    assert len(fetch(client, "/api/files?printed=0")["files"]) == 2


def test_set_printed_missing_and_unknown_id(client):
    r = fetch(client, "/api/set-printed", data={"printed": True})
    assert "error" in r
    r2 = fetch(client, "/api/set-printed", data={"id": 999999, "printed": True})
    assert r2.get("error") == "not found"


def test_printed_filter_combines_with_other_filters(client):
    a = _upload(client, "c.3mf", "组合筛选", "CNprC")
    fetch(client, "/api/set-printed", data={"id": a["id"], "printed": True})
    # 与状态筛选组合：待整理里应能找到；已归档里没有
    r = fetch(client, "/api/files?printed=1&status=pending")
    assert [x["id"] for x in r["files"]] == [a["id"]]
    r2 = fetch(client, "/api/files?printed=1&status=applied")
    assert r2["files"] == []
