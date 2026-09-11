# -*- coding: utf-8 -*-
"""安全与正确性回归测试：路径穿越、alias 注入、reset 确认、重复归档拦截、自定义分类闭环。"""
import os, http.client

import server
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def raw_get(base, path):
    """发送不经客户端规范化的原始路径（urllib 会吃掉 ../，需用 http.client 直发）。"""
    host, port = base.replace("http://", "").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _upload(client, name, title, design_id="CNsec1"):
    r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, design_id))])
    assert r["results"][0]["ok"], r
    return r["results"][0]["file"]


class TestPathTraversal:
    def test_static_traversal_blocked(self, client):
        # /static/../ 可越出 static 目录读到源码/数据库，必须 404
        for p in ("/static/../server.py", "/static/../library.db", "/static/../../etc/passwd"):
            status, body = raw_get(client, p)
            assert status == 404, f"{p} -> {status}"

    def test_thumbs_traversal_blocked(self, client):
        for p in ("/thumbs/../server.py", "/thumbs/../../README.md"):
            status, body = raw_get(client, p)
            assert status == 404, f"{p} -> {status}"

    def test_normal_routes_still_work(self, client):
        status, body = raw_get(client, "/")
        assert status == 200
        status, body = raw_get(client, "/api/stats")
        assert status == 200


class TestAliasInjection:
    def test_set_alias_sanitized(self, client):
        f = _upload(client, "inj.3mf", "注入测试")
        r = fetch(client, "/api/set-alias", data={"id": f["id"], "alias": "../../evil<>|name"})
        assert r["ok"]
        # 非法字符与 ../ 都应被清洗掉
        assert "/" not in r["alias"] and "\\" not in r["alias"] and ".." not in r["alias"]
        assert "<" not in r["alias"] and ">" not in r["alias"]

    def test_apply_keeps_file_inside_library(self, client, tmp_path):
        f = _upload(client, "escape.3mf", "穿越测试")
        fetch(client, "/api/set-alias", data={"id": f["id"], "alias": "../../evil"})
        res = fetch(client, "/api/apply", data={"ids": [f["id"]], "rename": True})
        assert res["results"][0]["ok"]
        root = os.path.realpath(str(tmp_path / "libroot"))
        final = os.path.realpath(res["results"][0]["new_path"])
        # 落盘文件必须仍在模型根目录内
        assert os.path.commonpath([final, root]) == root
        assert ".." not in os.path.basename(final)


class TestResetLibraryGuard:
    def test_wrong_path_rejected(self, client):
        r = fetch(client, "/api/reset-library", data={"path": "/tmp/other"})
        assert "error" in r and r.get("ok") is None
        # 数据未被清空
        f = _upload(client, "keep.3mf", "保留")
        files = fetch(client, "/api/files")["files"]
        assert any(x["id"] == f["id"] for x in files)


class TestSetTargetGuard:
    def test_traversal_and_absolute_rejected(self, client):
        f = _upload(client, "tgt.3mf", "路径测试")
        for bad in ("../escape", "a/../../escape", "/etc", "C:\\\\windows"):
            r = fetch(client, "/api/set-target", data={"id": f["id"], "target_dir": bad})
            assert "error" in r, f"{bad} 应被拒绝"
        # 合法相对子路径可用
        ok = fetch(client, "/api/set-target", data={"id": f["id"], "target_dir": "01_IP授权/高达Gundam"})
        assert ok["ok"] and ok["target_dir"] == "01_IP授权/高达Gundam"


class TestDuplicateGuard:
    def test_later_duplicate_cannot_apply(self, client):
        a = _upload(client, "dup_a.3mf", "重复模型", "CNdup9")
        b = _upload(client, "dup_b.3mf", "重复模型", "CNdup9")
        res = fetch(client, "/api/apply", data={"ids": [b["id"]], "rename": True})
        r = res["results"][0]
        assert not r["ok"] and r.get("skipped_duplicate")
        # 最早副本仍可归档
        res2 = fetch(client, "/api/apply", data={"ids": [a["id"]], "rename": True})
        assert res2["results"][0]["ok"]


class TestCustomCategoryFlow:
    def test_confirm_then_rule_reuse(self, client):
        # 1) 先上传一个规则认不出的文件
        f1 = _upload(client, "unknown.3mf", "神秘模型", "CNcc1")
        assert f1["category"] == "其他/未分类"
        # 2) 确认 LLM 式新分类
        r = fetch(client, "/api/confirm-new-category",
                  data={"id": f1["id"], "category": "手办/宠物小精灵", "alias": "神秘手办"})
        assert r["ok"]
        # 3) 出现在分类列表 custom 中
        cats = fetch(client, "/api/categories")
        assert "手办/宠物小精灵" in cats["custom"]
        # 4) 后续上传命中该自定义分类（按最具体末段匹配）
        f2 = _upload(client, "pets.3mf", "宠物小精灵皮卡丘", "CNcc2")
        assert f2["category"] == "手办/宠物小精灵"


class TestThumbReplace:
    def test_old_thumb_deleted_on_replace(self, client):
        f = _upload(client, "thumb.3mf", "缩略图测试")
        old = fetch(client, "/api/thumbnail",
                    data={"id": str(f["id"])},
                    files=[("file", "a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32)])
        assert old["ok"]
        thumb_dir = server.THUMB_DIR
        old_file = os.path.join(thumb_dir, old["thumb"])
        assert os.path.exists(old_file)
        new = fetch(client, "/api/thumbnail",
                    data={"id": str(f["id"])},
                    files=[("file", "b.png", b"\x89PNG\r\n\x1a\n" + b"1" * 32)])
        assert new["ok"] and new["thumb"] != old["thumb"]
        assert not os.path.exists(old_file), "替换缩略图后旧文件应被删除"
        assert os.path.exists(os.path.join(thumb_dir, new["thumb"]))
