# -*- coding: utf-8 -*-
"""备份与恢复：全量打包 / 还原回环 / 安全校验 / HTTP 任务接口。

隔离边界（严格不碰真实数据）：
- 单元测试只传 tmp_path 下的显式路径，不读 server 全局；
- HTTP 测试在 client 基础上再把 DATA_DIR 与 llm_client.CONFIG_PATH 一起
  指向临时目录（否则备份/恢复会读写仓库根的真实 config.json）。
"""
import json
import os
import time
import urllib.parse
import urllib.request
import zipfile

import pytest

import backup as backupm
import db as dbm
import llm_client
import server


def fetch(base, path, data=None):
    """与 test_api.fetch 同款：JSON 请求，HTTP 错误转成 {"error": ...}。"""
    if data is not None:
        req = urllib.request.Request(base + path, data=json.dumps(data).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read().decode()).get("error", "http" + str(e.code)), "_code": e.code}


def _make_env(tmp_path):
    """隔离的库环境：db + 配置 + 模型/缩略图/附件/回收站各一。"""
    lib = tmp_path / "lib"
    (lib / "分类A").mkdir(parents=True)
    inbox = lib / "00_待整理"
    inbox.mkdir()
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    attach = tmp_path / "attach"
    attach.mkdir()
    trash = tmp_path / ".trash"
    trash.mkdir()
    dbp = tmp_path / "library.db"
    dbm.init_db(str(dbp), str(lib), str(inbox), str(thumbs), str(attach))
    conn = dbm.db_conn(str(dbp))
    conn.execute(
        "INSERT INTO files(abs_path, rel_path, filename, folder, status, created_at) "
        "VALUES(?,?,?,?, 'pending','2026-01-01T00:00:00')",
        (str(lib / "分类A" / "a.3mf"), "分类A/a.3mf", "a.3mf", "分类A"))
    conn.commit()
    conn.close()
    (lib / "分类A" / "a.3mf").write_bytes(b"MODEL-A")
    (thumbs / "a.png").write_bytes(b"THUMB-A")
    (attach / "note.txt").write_text("hello", encoding="utf-8")
    (trash / "old.3mf").write_bytes(b"OLD")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"llm": {"base_url": "http://x/v1", "api_key": "k", "model": "m"},
                               "paths": {"library_root": str(lib)}}), encoding="utf-8")
    return {"db_path": str(dbp), "config_path": str(cfg),
            "library_root": str(lib), "thumb_dir": str(thumbs),
            "attach_dir": str(attach), "trash_dir": str(trash),
            "data_dir": str(tmp_path)}


def _db_rows(db_path, sql="SELECT COUNT(*) FROM files"):
    conn = dbm.db_conn(db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------
# 单元：打包 / 还原回环
# ---------------------------------------------------------------
def test_backup_roundtrip(tmp_path):
    env = _make_env(tmp_path)
    dest = tmp_path / "nas"
    dest.mkdir()
    out = backupm.create_backup(dest_dir=str(dest), **env)
    assert os.path.exists(out["path"])
    with zipfile.ZipFile(out["path"]) as z:
        names = set(z.namelist())
    assert {"meta.json", "library.db", "config.json"} <= names
    assert "library_root/分类A/a.3mf" in names
    assert "thumbs/a.png" in names
    assert "attachments/note.txt" in names
    assert ".trash/old.3mf" in names
    assert out["meta"]["kind"] == backupm.BACKUP_KIND
    assert out["meta"]["counts"]["files"] == 1

    # 破坏现场：删模型、清索引、加新文件、删缩略图
    os.remove(os.path.join(env["library_root"], "分类A", "a.3mf"))
    os.remove(os.path.join(env["thumb_dir"], "a.png"))
    with open(os.path.join(env["library_root"], "new.3mf"), "wb") as f:
        f.write(b"NEW")
    conn = dbm.db_conn(env["db_path"])
    conn.execute("DELETE FROM files")
    conn.commit()
    conn.close()

    result = backupm.restore_backup(zip_path=out["path"], **env)
    assert result["library_root"] == env["library_root"]
    # 原文件回来了，恢复期间的新增文件被替换掉
    with open(os.path.join(env["library_root"], "分类A", "a.3mf"), "rb") as f:
        assert f.read() == b"MODEL-A"
    assert not os.path.exists(os.path.join(env["library_root"], "new.3mf"))
    with open(os.path.join(env["thumb_dir"], "a.png"), "rb") as f:
        assert f.read() == b"THUMB-A"
    with open(os.path.join(env["attach_dir"], "note.txt"), encoding="utf-8") as f:
        assert f.read() == "hello"
    # 索引恢复且 abs_path 指向当前库根
    conn = dbm.db_conn(env["db_path"])
    row = conn.execute("SELECT abs_path, rel_path FROM files").fetchone()
    conn.close()
    assert row["rel_path"] == "分类A/a.3mf"
    assert os.path.normpath(row["abs_path"]) == \
        os.path.normpath(os.path.join(env["library_root"], "分类A", "a.3mf"))
    # config.json 一并还原，但库根目录保持当前设置
    with open(env["config_path"], encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["llm"]["model"] == "m"
    assert os.path.normpath(cfg["paths"]["library_root"]) == os.path.normpath(env["library_root"])


def test_restore_creates_pre_restore_snapshot(tmp_path):
    env = _make_env(tmp_path)
    dest = tmp_path / "nas"
    dest.mkdir()
    out = backupm.create_backup(dest_dir=str(dest), **env)
    # 恢复前往库里再加一条：快照应捕获 2 条，恢复后主库回到 1 条
    conn = dbm.db_conn(env["db_path"])
    conn.execute("INSERT INTO files(abs_path, rel_path, filename, folder, status) "
                 "VALUES('x', 'b.3mf', 'b.3mf', '', 'pending')")
    conn.commit()
    conn.close()
    result = backupm.restore_backup(zip_path=out["path"], **env)
    assert result["pre_restore_backup"] and os.path.exists(result["pre_restore_backup"])
    assert os.path.dirname(result["pre_restore_backup"]) == os.path.join(env["data_dir"], "backups")
    with zipfile.ZipFile(result["pre_restore_backup"]) as z:
        meta = json.loads(z.read("meta.json").decode())
    assert meta["counts"]["files"] == 2
    assert _db_rows(env["db_path"]) == 1  # 主库已回到备份时点


def test_restore_rebases_abs_path_to_current_root(tmp_path):
    env1 = _make_env(tmp_path / "e1")
    dest = tmp_path / "nas"
    dest.mkdir()
    out = backupm.create_backup(dest_dir=str(dest), **env1)
    env2 = _make_env(tmp_path / "e2")  # 另一个库根（模拟换机/换盘恢复）
    backupm.restore_backup(zip_path=out["path"], **env2)
    conn = dbm.db_conn(env2["db_path"])
    row = conn.execute("SELECT abs_path FROM files").fetchone()
    conn.close()
    assert os.path.normpath(row["abs_path"]) == \
        os.path.normpath(os.path.join(env2["library_root"], "分类A", "a.3mf"))
    assert os.path.exists(row["abs_path"])


def test_list_backups_newest_first_and_restore_from_dir(tmp_path):
    env = _make_env(tmp_path)
    dest = tmp_path / "nas"
    dest.mkdir()
    first = backupm.create_backup(dest_dir=str(dest), **env)
    # 又归档了一个模型后再备一次
    conn = dbm.db_conn(env["db_path"])
    conn.execute("INSERT INTO files(abs_path, rel_path, filename, folder, status) "
                 "VALUES('x', 'b.3mf', 'b.3mf', '', 'pending')")
    conn.commit()
    conn.close()
    with open(os.path.join(env["library_root"], "b.3mf"), "wb") as f:
        f.write(b"MODEL-B")
    time.sleep(1.1)  # 保证备份文件名时间戳不同
    second = backupm.create_backup(dest_dir=str(dest), **env)
    assert first["path"] != second["path"]
    entries = backupm.list_backups(str(dest))
    assert [e["name"] for e in entries[:2]] == \
        [os.path.basename(second["path"]), os.path.basename(first["path"])]
    assert all(e["valid"] for e in entries)
    # 指定目录恢复 → 自动取最新包（2 条记录而非 1 条）
    backupm.restore_backup(zip_path=str(dest), **env)
    assert _db_rows(env["db_path"]) == 2
    assert os.path.exists(os.path.join(env["library_root"], "b.3mf"))


def test_backup_rejects_missing_dest(tmp_path):
    env = _make_env(tmp_path)
    with pytest.raises(backupm.BackupError):
        backupm.create_backup(dest_dir=str(tmp_path / "nope"), **env)


def test_restore_rejects_non_backup_zip(tmp_path):
    env = _make_env(tmp_path)
    zpath = tmp_path / "random.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(backupm.BackupError):
        backupm.restore_backup(zip_path=str(zpath), **env)


def test_restore_rejects_missing_path(tmp_path):
    env = _make_env(tmp_path)
    with pytest.raises(backupm.BackupError):
        backupm.restore_backup(zip_path=str(tmp_path / "gone.zip"), **env)


def test_restore_rejects_newer_schema(tmp_path):
    env = _make_env(tmp_path)
    dest = tmp_path / "nas"
    dest.mkdir()
    out = backupm.create_backup(dest_dir=str(dest), **env)
    with zipfile.ZipFile(out["path"]) as z:
        data = {n: z.read(n) for n in z.namelist()}
    meta = json.loads(data["meta.json"].decode())
    meta["schema_version"] = dbm.SCHEMA_VERSION + 50
    data["meta.json"] = json.dumps(meta, ensure_ascii=False).encode()
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(forged, "w") as z:
        for n, b in data.items():
            z.writestr(n, b)
    with pytest.raises(backupm.BackupError):
        backupm.restore_backup(zip_path=str(forged), **env)


def test_restore_blocks_zip_slip(tmp_path):
    """备份包里混入 ../ 越界条目：不得写出暂存目录之外。"""
    env = _make_env(tmp_path)
    dest = tmp_path / "nas"
    dest.mkdir()
    out = backupm.create_backup(dest_dir=str(dest), **env)
    with zipfile.ZipFile(out["path"]) as z:
        data = {n: z.read(n) for n in z.namelist()}
    data["../evil.txt"] = b"EVIL"
    data["thumbs/../../evil2.txt"] = b"EVIL2"
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(forged, "w") as z:
        for n, b in data.items():
            z.writestr(n, b)
    backupm.restore_backup(zip_path=str(forged), **env)
    assert not os.path.exists(tmp_path / "evil.txt")
    assert not os.path.exists(tmp_path / "evil2.txt")
    assert not os.path.exists(os.path.join(env["library_root"], "evil.txt"))
    assert _db_rows(env["db_path"]) == 1  # 正常条目照常恢复


def test_backup_inside_library_root_survives_restore(tmp_path):
    """备份包放在库根目录内：恢复时先复制出来，不得自毁用户的备份包。"""
    env = _make_env(tmp_path)
    dest = os.path.join(env["library_root"], "bak")
    os.makedirs(dest)
    out = backupm.create_backup(dest_dir=dest, **env)
    # 破坏现场再恢复
    os.remove(os.path.join(env["library_root"], "分类A", "a.3mf"))
    backupm.restore_backup(zip_path=out["path"], **env)
    assert os.path.exists(out["path"])  # 备份包本身还在
    assert os.path.exists(os.path.join(env["library_root"], "分类A", "a.3mf"))


# ---------------------------------------------------------------
# HTTP 任务接口
# ---------------------------------------------------------------
@pytest.fixture()
def api(tmp_path, monkeypatch, client):
    """在 client 基础上把 DATA_DIR 与 CONFIG_PATH 也隔离到临时目录。

    client 已隔离 LIBRARY_ROOT/DB_PATH/THUMB_DIR/ATTACH_DIR；备份/恢复还会
    读写 config.json（llm_client.CONFIG_PATH 缺省指向仓库根真实配置）、
    TRASH_DIR（缺省指向仓库根真实 .trash/，恢复时会整体搬移——绝不允许）与
    DATA_DIR（恢复前快照、暂存目录的落点），必须一并指向 tmp。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir(exist_ok=True)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"base_url": "http://127.0.0.1:9/v1", "api_key": "test", "model": "test-model"},
        "paths": {"library_root": str(tmp_path / "libroot")},  # 与 client fixture 的库根一致
    }), encoding="utf-8")
    monkeypatch.setattr(server, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(server, "TRASH_DIR", str(trash_dir))
    monkeypatch.setattr(llm_client, "CONFIG_PATH", str(cfg_path))
    return {"base": client, "tmp": tmp_path}


def _wait_job(base, kind, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = fetch(base, f"/api/{kind}/status").get("job")
        if j and j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("任务超时未结束")


def test_backup_api_flow(api):
    base, tmp = api["base"], api["tmp"]
    # 准备一点数据（走真实上传，索引/文件/缩略图都齐）
    import io
    xml = ('<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
           '<metadata name="Title">备份测试</metadata><resources><object id="1"><mesh>'
           '<vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>'
           '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
           '<build><item objectid="1"/></build></model>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", xml)
    boundary = "----tb"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="bk.3mf"\r\n\r\n'
            .encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(base + "/api/upload", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    urllib.request.urlopen(req, timeout=20).read()

    dest = str(tmp / "nas")
    os.makedirs(dest)
    # 目录不存在 → 400
    r = fetch(base, "/api/backup/start", {"path": str(tmp / "nope")})
    assert r.get("error")
    # 正常启动 → 轮询到完成
    r = fetch(base, "/api/backup/start", {"path": dest})
    assert r.get("ok") and r["job"]["state"] == "running"
    job = _wait_job(base, "backup")
    assert job["state"] == "done" and job["result"]["meta"]["counts"]["files"] == 1
    # 列表可扫描到
    q = urllib.parse.quote(dest)
    entries = fetch(base, f"/api/backup/list?path={q}")["entries"]
    assert len(entries) == 1 and entries[0]["valid"]


def test_restore_api_flow(api):
    base, tmp = api["base"], api["tmp"]
    dest = str(tmp / "nas")
    os.makedirs(dest)
    # 先备份（当前为空库）
    fetch(base, "/api/backup/start", {"path": dest})
    assert _wait_job(base, "backup")["state"] == "done"
    # 破坏现场：清空索引
    conn = dbm.db_conn(server.DB_PATH)
    conn.execute("DELETE FROM files")
    conn.commit()
    conn.close()
    # 从目录恢复（自动取最新包）→ 等待完成 → 库表可用
    r = fetch(base, "/api/restore/start", {"path": dest})
    assert r.get("ok")
    job = _wait_job(base, "restore")
    assert job["state"] == "done", job.get("error")
    assert fetch(base, "/api/stats")["total"] >= 0  # 恢复后接口正常响应
    assert job["result"]["pre_restore_backup"]
    # 不存在的路径 → 400
    r = fetch(base, "/api/restore/start", {"path": str(tmp / "nope.zip")})
    assert r.get("error")


def test_backup_busy_rejected():
    """已有任务在跑时再次启动 → BusyError。用 Event 卡住第一个任务，保证互斥窗口存在。"""
    import threading
    release = threading.Event()

    def slow(prog):
        release.wait(10)
        return {"ok": True}

    started = backupm.start_job("backup", "x", slow)
    try:
        with pytest.raises(backupm.BusyError):
            backupm.start_job("restore", "y", lambda prog: {"ok": True})
    finally:
        release.set()
    deadline = time.time() + 10
    while backupm.job_snapshot("backup", started["id"])["state"] == "running":
        if time.time() > deadline:
            raise AssertionError("任务超时未结束")
        time.sleep(0.05)
    assert backupm.job_snapshot("backup", started["id"])["state"] == "done"
    # 结束后可以再启动
    j2 = backupm.start_job("backup", "z", lambda prog: {"ok": True})
    deadline = time.time() + 10
    while backupm.job_snapshot("backup", j2["id"])["state"] == "running":
        if time.time() > deadline:
            raise AssertionError("任务超时未结束")
        time.sleep(0.05)
    assert backupm.job_snapshot("backup")["id"] == j2["id"]
