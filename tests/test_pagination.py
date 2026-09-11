# -*- coding: utf-8 -*-
"""分页与聚合测试：limit/offset/total、跨页重复标记、附件内嵌（消 N+1）。"""
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def _upload(client, name, title, design_id="CNpg1"):
    r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, design_id))])
    assert r["results"][0]["ok"], r
    return r["results"][0]["file"]


def test_pagination_total_and_offset(client):
    for i in range(3):
        _upload(client, f"p{i}.3mf", f"分页模型{i}", "CNpg%d" % i)
    r = fetch(client, "/api/files?limit=2&offset=0")
    assert r["total"] == 3
    assert len(r["files"]) == 2
    r2 = fetch(client, "/api/files?limit=2&offset=2")
    assert r2["total"] == 3
    assert len(r2["files"]) == 1


def test_duplicate_flag_across_pages(client):
    # 同内容两份 + 第三份不同内容；分页可能把重复组切到不同页
    _upload(client, "a.3mf", "重复内容", "CNdup")
    _upload(client, "a2.3mf", "重复内容", "CNdup")
    _upload(client, "b.3mf", "不同内容", "CNother")
    seen_dup, seen_earliest = [], []
    for off in (0, 2):
        page = fetch(client, f"/api/files?limit=2&offset={off}")["files"]
        for f in page:
            if f.get("is_duplicate"):
                (seen_earliest if f.get("is_earliest") else seen_dup).append(f["filename"])
    assert seen_dup == ["a2.3mf"], "后进副本应标记重复且不可归档"
    assert seen_earliest == ["a.3mf"], "最早副本应标记为可归档"


def test_attachments_embedded_in_files(client):
    f = _upload(client, "withatt.3mf", "带附件模型", "CNatt")
    res = fetch(client, "/api/attach", data={"id": str(f["id"])}, files=[("file", "notes.txt", b"howto")])
    assert res["ok"]
    rows = fetch(client, "/api/files")["files"]
    row = next(x for x in rows if x["id"] == f["id"])
    assert row["attachments"] and row["attachments"][0]["name"] == "notes.txt"
