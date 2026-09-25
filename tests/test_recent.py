# -*- coding: utf-8 -*-
"""最近打开测试：/api/track-view 记录、/api/recent 列表与 v5 迁移（files.last_viewed_at）。"""
import sqlite3

import db as dbm
from tests.test_api import fetch, _make_3mf_bytes


def _upload(client, name, title, design_id="CNrv"):
    r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, design_id))])
    assert r["results"][0]["ok"], r
    return r["results"][0]["file"]


def _seed_view(db_path, fid, ts):
    """直接写入固定的 last_viewed_at，避免真实时间戳同秒竞态。"""
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE files SET last_viewed_at=? WHERE id=?", (ts, fid))
    conn.commit(); conn.close()


def test_track_view_sets_timestamp(client):
    a = _upload(client, "a.3mf", "模型甲", "CNrv1")
    r = fetch(client, "/api/track-view", data={"id": a["id"]})
    assert r["ok"] is True and r["last_viewed_at"]
    rows = {x["filename"]: x for x in fetch(client, "/api/files")["files"]}
    assert rows["a.3mf"]["last_viewed_at"] == r["last_viewed_at"]
    rec = fetch(client, "/api/recent")["files"]
    assert [x["id"] for x in rec] == [a["id"]]
    assert rec[0]["filename"] == "a.3mf"


def test_track_view_validation_and_empty_recent(client):
    assert "error" in fetch(client, "/api/track-view", data={})
    assert fetch(client, "/api/track-view", data={"id": 999999}).get("error") == "not found"
    _upload(client, "b.3mf", "没打开过", "CNrv2")
    r = fetch(client, "/api/recent")
    assert r["files"] == [] and r["count"] == 0


def test_recent_ordering_and_limit(client):
    import server
    a = _upload(client, "a.3mf", "模型甲", "CNrv3")
    b = _upload(client, "b.3mf", "模型乙", "CNrv4")
    c = _upload(client, "c.3mf", "模型丙", "CNrv5")
    _seed_view(server.DB_PATH, a["id"], "2020-01-01 10:00:00")
    _seed_view(server.DB_PATH, b["id"], "2020-01-01 11:00:00")
    _seed_view(server.DB_PATH, c["id"], "2020-01-01 12:00:00")
    rec = fetch(client, "/api/recent")["files"]
    assert [x["id"] for x in rec] == [c["id"], b["id"], a["id"]]
    assert [x["id"] for x in fetch(client, "/api/recent?limit=2")["files"]] == [c["id"], b["id"]]
    # 再次打开 a（真实时间戳必然晚于 2020 年）→ 置顶
    fetch(client, "/api/track-view", data={"id": a["id"]})
    rec = fetch(client, "/api/recent")["files"]
    assert rec[0]["id"] == a["id"] and len(rec) == 3


def test_reingest_same_path_preserves_last_viewed(client, tmp_path):
    """同路径重复入库（INSERT OR REPLACE）重写记录时保留 last_viewed_at，与 printed 同款防护。"""
    import server
    dest = str(tmp_path / "m.3mf")
    with open(dest, "wb") as f:
        f.write(_make_3mf_bytes("保留时间", "CNrv6"))
    h = object.__new__(server.Handler)  # _ingest 只用模块级全局，不依赖请求上下文
    rec1 = h._ingest(dest)
    fetch(client, "/api/track-view", data={"id": rec1["id"]})
    before = fetch(client, "/api/files")["files"][0]["last_viewed_at"]
    assert before
    rec2 = h._ingest(dest)
    # OR REPLACE 会换 rowid（id 变化），但同 abs_path 记录的 last_viewed_at 应被回写保留
    assert rec2["abs_path"] == rec1["abs_path"]
    assert rec2["last_viewed_at"] == before


def test_v4_to_v5_migration_preserves_rows(tmp_path):
    """v4 旧库升级：只加列加索引，既有记录不动，新列默认空串。"""
    p = str(tmp_path / "old.db")
    conn = dbm.db_conn(p)
    for ver in range(1, 4 + 1):
        dbm._MIGRATIONS[ver](conn)
    conn.execute("PRAGMA user_version = 4")
    conn.execute("INSERT INTO files (filename, status, printed) VALUES ('legacy.3mf','applied',1)")
    conn.commit(); conn.close()
    dbm.init_db(p, str(tmp_path / "root"), str(tmp_path / "inbox"),
                str(tmp_path / "thumbs"), str(tmp_path / "attach"))
    conn = dbm.db_conn(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    row = conn.execute("SELECT filename, status, printed, last_viewed_at FROM files").fetchone()
    assert row["filename"] == "legacy.3mf"
    assert row["status"] == "applied" and row["printed"] == 1
    assert row["last_viewed_at"] == ""
    idxs = [r["name"] for r in conn.execute("PRAGMA index_list(files)")]
    assert "idx_files_viewed" in idxs
    conn.close()
