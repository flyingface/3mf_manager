# -*- coding: utf-8 -*-
"""merge_3mf 合并模块单元测试：拍平重编号、包围盒摆放、异常源。"""
import io
import zipfile
import xml.etree.ElementTree as ET

import pytest

from merge_3mf import MergeError, merge, build_export_name, apply_m, compose, parse_transform

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def _pkg(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in parts.items():
            z.writestr(n, c)
    return buf.getvalue()


def _simple_pkg(unit="millimeter", oid=7, verts=6, item_tf="1 0 0 0 1 0 0 0 1 100 0 0"):
    """单网格源：oid 号对象，verts 个共线顶点（x 轴 0..verts-1），item 带 transform。"""
    xml = f'''<model unit="{unit}" xmlns="{CORE}">
<resources><object id="{oid}"><mesh><vertices>{''.join(f'<vertex x="{i}" y="0" z="0"/>' for i in range(verts))}</vertices>
<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>
<build><item objectid="{oid}" transform="{item_tf}"/></build></model>'''
    return _pkg({"3D/3dmodel.model": xml})


def _bambu_like_pkg():
    """Bambu 风格源：主文档组件引用拆分的部件网格（带组件 transform）。"""
    main = f'''<model unit="millimeter" xmlns="{CORE}">
<resources><object id="1" type="model"><components>
<component objectid="2" path="3D/Objects/object_2.model" transform="1 0 0 0 1 0 0 0 1 10 20 30"/>
</components></object></resources>
<build><item objectid="1"/></build></model>'''
    part = f'''<model unit="millimeter" xmlns="{CORE}">
<resources><object id="2"><mesh><vertices>{''.join(f'<vertex x="{i}" y="0" z="0"/>' for i in range(5))}</vertices>
<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>
<build></build></model>'''
    return _pkg({"3D/3dmodel.model": main, "3D/Objects/object_2.model": part})


def _parse_merged(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    assert "[Content_Types].xml" in names and "_rels/.rels" in names
    root = ET.fromstring(z.read("3D/3dmodel.model"))
    return root


def _objs(root):
    return root.find(f"{{{CORE}}}resources").findall(f"{{{CORE}}}object")


def test_merge_flatten_renumber_and_layout(tmp_path):
    """拍平跨部件引用、id 全局唯一重编号、build 按包围盒网格平移。"""
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_simple_pkg(verts=6))          # 世界包围盒 x∈[100,105]（item tf +100）
    pb.write_bytes(_bambu_like_pkg())             # 世界包围盒 x∈[10,14]（组件 tf +10）
    data = merge([str(pa), str(pb)], title="测试合并")

    root = _parse_merged(data)
    objs = _objs(root)
    # 源A 1 个 + 源B 2 个（主+部件）+ 2 个 wrapper
    assert len(objs) == 5
    ids = [o.get("id") for o in objs]
    assert len(ids) == len(set(ids)) == 5
    # 拍平后不得残留跨部件 path 引用
    for o in objs:
        for comp in o.iter(f"{{{CORE}}}component"):
            assert not any(k.endswith("path") for k in comp.attrib)
    # build：每个源一个 item，指向纯组件壳 wrapper（每壳 1 个 component）
    items = root.find(f"{{{CORE}}}build").findall(f"{{{CORE}}}item")
    assert len(items) == 2
    byid = {o.get("id"): o for o in objs}
    for it in items:
        w = byid[it.get("objectid")]
        comps = w.find(f"{{{CORE}}}components")
        assert comps is not None and len(comps) == 1
    tfs = [it.get("transform").split() for it in items]
    assert [t[9] for t in sorted(tfs, key=lambda t: float(t[9]))] == ["-100", "0"]
    # 顶点沿组件链映射到世界坐标：A 归零后 x∈[0,5]，B x∈[10,14] —— 全局 [0,14] 且互不重叠
    xs = _world_xs(data)
    assert min(xs) == 0 and max(xs) == 14
    assert all(x <= 5 or x >= 10 for x in xs)


def _world_xs(data):
    """遍历合并产物的 build→component 树，返回所有 mesh 顶点的世界 x 坐标。"""
    core = f"{{{CORE}}}"
    z = zipfile.ZipFile(io.BytesIO(data))
    root = ET.fromstring(z.read("3D/3dmodel.model"))
    byid = {o.get("id"): o for o in root.find(core + "resources").findall(core + "object")}
    xs = []

    def walk(oid, m):
        o = byid[str(oid)]
        mesh = o.find(core + "mesh")
        if mesh is not None:
            for v in mesh.find(core + "vertices"):
                p = (float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))
                xs.append(apply_m(m, p)[0])
        for c in o.iter(core + "component"):
            walk(c.get("objectid"), compose(parse_transform(c.get("transform")), m))

    for it in root.find(core + "build").findall(core + "item"):
        walk(it.get("objectid"), parse_transform(it.get("transform")))
    return xs


def test_merge_title_and_unit(tmp_path):
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_simple_pkg())
    pb.write_bytes(_bambu_like_pkg())
    root = _parse_merged(merge([str(pa), str(pb)], title="我的拼盘"))
    metas = {m.get("name"): m.text for m in root.findall(f"{{{CORE}}}metadata")}
    assert metas["Title"] == "我的拼盘"
    assert root.get("unit") == "millimeter"


def test_merge_unit_mismatch_rejected(tmp_path):
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_simple_pkg(unit="millimeter"))
    pb.write_bytes(_simple_pkg(unit="inch"))
    with pytest.raises(MergeError, match="单位"):
        merge([str(pa), str(pb)])


def test_merge_bad_source_rejected(tmp_path):
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    good = tmp_path / "good.3mf"
    good.write_bytes(_simple_pkg())
    with pytest.raises(MergeError):
        merge([str(good), str(bad)])
    with pytest.raises(MergeError, match="至少"):
        merge([str(good)])


def test_merge_sources_untouched(tmp_path):
    """核心约束：合并只读，源文件字节不变。"""
    pa, pb = tmp_path / "a.3mf", tmp_path / "b.3mf"
    pa.write_bytes(_simple_pkg())
    pb.write_bytes(_bambu_like_pkg())
    before = (pa.read_bytes(), pb.read_bytes())
    merge([str(pa), str(pb)])
    assert (pa.read_bytes(), pb.read_bytes()) == before


def test_export_name():
    assert build_export_name(3, "20260915_120000") == "合并_3个模型_20260915_120000.3mf"
