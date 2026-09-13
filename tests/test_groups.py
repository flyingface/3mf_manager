# -*- coding: utf-8 -*-
"""批次1 确定性关联测试：聚类信号、组 CRUD API、成员操作、孤儿清理。"""
import relate
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py


def _row(fid, fn, design_id="", sha="", geom=""):
    return {"id": fid, "filename": fn, "design_id": design_id,
            "sha256": sha, "geom_sig": geom, "created_at": "2026-01-01 00:00:00", "thumb": ""}


class TestCluster:
    def test_design_id_cluster_high(self):
        rows = [_row(1, "a.3mf", design_id="CN1"), _row(2, "b.3mf", design_id="CN1"), _row(3, "c.3mf", design_id="CN2")]
        out = relate.find_clusters(rows)
        assert len(out) == 1
        assert sorted(f["id"] for f in out[0]["files"]) == [1, 2]
        assert out[0]["confidence"] == "high"

    def test_sha256_duplicate(self):
        rows = [_row(1, "a.3mf", sha="S"), _row(2, "b.3mf", sha="S"), _row(3, "c.3mf", sha="T")]
        out = relate.find_clusters(rows)
        assert len(out) == 1 and out[0]["confidence"] == "high"

    def test_geom_sig_variant(self):
        rows = [_row(1, "a.3mf", sha="S1", geom="100|50"), _row(2, "b_150.3mf", sha="S2", geom="100|50")]
        out = relate.find_clusters(rows)
        assert len(out) == 1
        assert any("几何指纹" in s for s in out[0]["signals"])

    def test_stem_cluster_low_with_noise_diff(self):
        # 词干相同 + 差异词均为噪音（v2/150%）→ low 簇
        rows = [_row(1, "高达模型_v2.3mf"), _row(2, "高达模型_150%.3mf"), _row(3, "无关文件.3mf")]
        out = relate.find_clusters(rows)
        assert len(out) == 1 and out[0]["confidence"] == "low"

    def test_stem_cluster_rejects_non_noise_diff(self):
        # 差异词是实义词 → 不聚类
        rows = [_row(1, "高达身体.3mf"), _row(2, "高达武器.3mf")]
        assert relate.find_clusters(rows) == []

    def test_transitive_cluster(self):
        # a-b 同 design，b-c 同 sha → 传递成簇
        rows = [_row(1, "a.3mf", design_id="X", sha="SA"),
                _row(2, "b.3mf", design_id="X", sha="SA"),
                _row(3, "c.3mf", design_id="Y", sha="SA")]
        out = relate.find_clusters(rows)
        assert len(out) == 1 and len(out[0]["files"]) == 3


class TestGroupAPI:
    def _upload(self, client, name, title, did):
        r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, did))])
        return r["results"][0]["file"]

    def test_create_and_get(self, client):
        f1 = self._upload(client, "body.3mf", "高达身体", "CNg1")
        f2 = self._upload(client, "weapon.3mf", "高达武器", "CNg2")
        r = fetch(client, "/api/groups", data={
            "create": {"name": "高达套件", "file_ids": [f1["id"], f2["id"]],
                       "roles": {str(f1["id"]): "component", str(f2["id"]): "accessory"},
                       "primary_id": f1["id"]}})
        assert r["ok"]
        g = r["group"]
        assert g["name"] == "高达套件" and len(g["members"]) == 2
        assert g["stats"]["total"] == 2
        prim = [m for m in g["members"] if m["is_primary"]]
        assert len(prim) == 1 and prim[0]["file_id"] == f1["id"]
        # 详情
        d = fetch(client, "/api/groups/get", data={"id": g["id"]})
        assert d["group"]["name"] == "高达套件"
        # 列表
        lst = fetch(client, "/api/groups")["groups"]
        assert any(x["id"] == g["id"] for x in lst)

    def test_member_ops(self, client):
        f1 = self._upload(client, "m1.3mf", "成员一", "CNg3")
        f2 = self._upload(client, "m2.3mf", "成员二", "CNg4")
        g = fetch(client, "/api/groups", data={"create": {"name": "测试组", "file_ids": [f1["id"]]}})["group"]
        gid = g["id"]
        # 加成员
        r = fetch(client, "/api/groups", data={"member": {"group_id": gid, "file_id": f2["id"], "add": True, "role": "variant"}})
        assert len(r["group"]["members"]) == 2
        # 改角色 + 打印状态
        r = fetch(client, "/api/groups", data={"member": {"group_id": gid, "file_id": f2["id"], "role": "accessory", "printed": True}})
        m2 = next(m for m in r["group"]["members"] if m["file_id"] == f2["id"])
        assert m2["role"] == "accessory" and m2["printed"]
        assert r["group"]["stats"]["printed"] == 1
        # 设主文件
        r = fetch(client, "/api/groups", data={"member": {"group_id": gid, "file_id": f2["id"], "is_primary": True}})
        prim = [m for m in r["group"]["members"] if m["is_primary"]]
        assert len(prim) == 1 and prim[0]["file_id"] == f2["id"]
        # 移出
        r = fetch(client, "/api/groups", data={"member": {"group_id": gid, "file_id": f2["id"], "remove": True}})
        assert len(r["group"]["members"]) == 1

    def test_delete_file_prunes_group(self, client):
        f1 = self._upload(client, "x1.3mf", "独苗", "CNg5")
        f2 = self._upload(client, "x2.3mf", "陪衬", "CNg6")
        g = fetch(client, "/api/groups", data={"create": {"name": "孤儿组", "file_ids": [f1["id"], f2["id"]],
                                                          "primary_id": f1["id"]}})["group"]
        # 删除主文件 → primary 转移；再删到空 → 组自动消失
        fetch(client, "/api/delete", data={"id": f1["id"]})
        g2 = fetch(client, "/api/groups/get", data={"id": g["id"]})["group"]
        assert len(g2["members"]) == 1 and g2["members"][0]["is_primary"]
        fetch(client, "/api/delete", data={"id": f2["id"]})
        assert "error" in fetch(client, "/api/groups/get", data={"id": g["id"]})

    def test_update_name_and_cover(self, client):
        f1 = self._upload(client, "u1.3mf", "改组一", "CNg7")
        f2 = self._upload(client, "u2.3mf", "改组二", "CNg8")
        g = fetch(client, "/api/groups", data={"create": {"name": "旧名", "file_ids": [f1["id"], f2["id"]]}})["group"]
        r = fetch(client, "/api/groups", data={"update": {"group_id": g["id"], "name": "新名", "cover_file_id": f2["id"]}})
        assert r["group"]["name"] == "新名" and r["group"]["cover_file_id"] == f2["id"]


class TestSuggestions:
    def test_suggestions_detect_same_design(self, client):
        # 同 design_id 的两个上传 → 建议；建组后 already_grouped 生效
        a = self.__class__._up(client, "s1.3mf", "建议一", "CNs")
        b = self.__class__._up(client, "s2.3mf", "建议二", "CNs")
        sug = fetch(client, "/api/groups/suggestions")["suggestions"]
        assert len(sug) == 1 and sug[0]["confidence"] == "high"
        assert sorted(sug[0]["file_ids"]) == sorted([a["id"], b["id"]])
        fetch(client, "/api/groups", data={"create": {"name": "已建组", "file_ids": [a["id"], b["id"]]}})
        sug2 = fetch(client, "/api/groups/suggestions")["suggestions"]
        assert sug2[0]["already_grouped"] == 2

    @staticmethod
    def _up(client, name, title, did):
        r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, did))])
        return r["results"][0]["file"]

    @staticmethod
    def _up(client, name, title, did):
        r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, did))])
        return r["results"][0]["file"]


class TestRolesAndDissolve:
    """批次4：other 角色、非法角色回退、主文件唯一性、解散分组不影响文件。"""

    def _up(self, client, name, title, did):
        r = fetch(client, "/api/upload", files=[("file", name, _make_3mf_bytes(title, did))])
        return r["results"][0]["file"]

    def test_role_other_roundtrip(self, client):
        f1 = self._up(client, "ro1.3mf", "角色甲", "CNr1")
        f2 = self._up(client, "ro2.3mf", "角色乙", "CNr2")
        r = fetch(client, "/api/groups", data={
            "create": {"name": "其他角色组", "file_ids": [f1["id"], f2["id"]],
                       "roles": {str(f1["id"]): "component", str(f2["id"]): "other"},
                       "primary_id": f1["id"]}})
        m2 = next(m for m in r["group"]["members"] if m["file_id"] == f2["id"])
        assert m2["role"] == "other"
        # 改回常规角色也正常
        r = fetch(client, "/api/groups", data={"member": {"group_id": r["group"]["id"], "file_id": f2["id"], "role": "variant"}})
        m2 = next(m for m in r["group"]["members"] if m["file_id"] == f2["id"])
        assert m2["role"] == "variant"

    def test_invalid_role_falls_back_to_component(self, client):
        f1 = self._up(client, "ir1.3mf", "非法甲", "CNi1")
        f2 = self._up(client, "ir2.3mf", "非法乙", "CNi2")
        r = fetch(client, "/api/groups", data={
            "create": {"name": "回退组", "file_ids": [f1["id"], f2["id"]],
                       "roles": {str(f2["id"]): "bogus"}}})
        m2 = next(m for m in r["group"]["members"] if m["file_id"] == f2["id"])
        assert m2["role"] == "component"
        # member 接口设置非法角色同样回退
        r = fetch(client, "/api/groups", data={"member": {"group_id": r["group"]["id"], "file_id": f2["id"], "role": "nope"}})
        m2 = next(m for m in r["group"]["members"] if m["file_id"] == f2["id"])
        assert m2["role"] == "component"

    def test_primary_transfer_keeps_single_primary(self, client):
        f1 = self._up(client, "pt1.3mf", "转移甲", "CNp1")
        f2 = self._up(client, "pt2.3mf", "转移乙", "CNp2")
        g = fetch(client, "/api/groups", data={"create": {"name": "转移组", "file_ids": [f1["id"], f2["id"]],
                                                          "primary_id": f1["id"]}})["group"]
        r = fetch(client, "/api/groups", data={"member": {"group_id": g["id"], "file_id": f2["id"], "is_primary": True}})
        prims = [m for m in r["group"]["members"] if m["is_primary"]]
        assert len(prims) == 1 and prims[0]["file_id"] == f2["id"]
        old = next(m for m in r["group"]["members"] if m["file_id"] == f1["id"])
        assert not old["is_primary"]

    def test_dissolve_group_keeps_files(self, client):
        f1 = self._up(client, "dg1.3mf", "解散甲", "CNd1")
        f2 = self._up(client, "dg2.3mf", "解散乙", "CNd2")
        g = fetch(client, "/api/groups", data={"create": {"name": "待解散", "file_ids": [f1["id"], f2["id"]],
                                                          "primary_id": f1["id"]}})["group"]
        r = fetch(client, "/api/groups", data={"delete": {"group_id": g["id"]}})
        assert r["ok"]
        # 组已消失
        assert "error" in fetch(client, "/api/groups/get", data={"id": g["id"]})
        assert fetch(client, "/api/groups")["groups"] == []
        # 文件原封不动（状态、文件名等属性不受影响）
        files = fetch(client, "/api/files?status=pending")["files"]
        by_id = {f["id"]: f for f in files}
        assert f1["id"] in by_id and f2["id"] in by_id
        assert by_id[f1["id"]]["filename"] == f1["filename"]


class TestGroupDetailGet:
    def test_detail_via_get_query(self, client):
        """回归：GET /api/groups/get?id=N 收到 parse_qs（值为列表），不得 500。"""
        r = fetch(client, "/api/upload", files=[("file", "gq.3mf", _make_3mf_bytes("查询甲", "CNq1"))])
        fid = r["results"][0]["file"]["id"]
        fetch(client, "/api/groups", data={"create": {"name": "查询组", "file_ids": [fid]}})
        res = fetch(client, f"/api/groups/get?id={fid}")
        assert res["ok"] and res["group"]["name"] == "查询组"

    def test_detail_via_post(self, client):
        r = fetch(client, "/api/upload", files=[("file", "gp.3mf", _make_3mf_bytes("查询乙", "CNq2"))])
        fid = r["results"][0]["file"]["id"]
        fetch(client, "/api/groups", data={"create": {"name": "查询组2", "file_ids": [fid]}})
        res = fetch(client, "/api/groups/get", data={"id": fid})
        assert res["ok"] and res["group"]["name"] == "查询组2"
