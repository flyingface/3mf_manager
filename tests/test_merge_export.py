# -*- coding: utf-8 -*-
"""merge_3mf 合并模块单元测试：拍平重编号、包围盒摆放、异常源。"""
import io
import json
import os
import zipfile
import xml.etree.ElementTree as ET

import pytest

from merge_3mf import MergeError, merge, build_export_name, build_export_title, list_plates, apply_m, compose, parse_transform
from tests.test_api import fetch, _make_3mf_bytes  # client fixture 在 conftest.py

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


PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def _twin_pkg(verts, uid):
    """与另一份 twin 包内部文档名/对象 id 完全相同的源（同模型两个版本的常态），
    主文档与部件都带 p:UUID。verts 区分两份几何，防止内容串源测不出来。"""
    main = f'''<model unit="millimeter" xmlns="{CORE}" xmlns:p="{PROD}">
<resources><object id="1" type="model" p:UUID="aaaa0000-0000-0000-0000-00000000000{uid}"><components>
<component objectid="2" p:UUID="aaaa0001-0000-0000-0000-00000000000{uid}" p:path="3D/Objects/object_1.model"/>
</components></object></resources>
<build><item objectid="1"/></build></model>'''
    part = f'''<model unit="millimeter" xmlns="{CORE}" xmlns:p="{PROD}">
<resources><object id="2" p:UUID="aaaa0002-0000-0000-0000-00000000000{uid}"><mesh><vertices>{''.join(f'<vertex x="{i}" y="0" z="0"/>' for i in range(verts))}</vertices>
<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>
<build></build></model>'''
    return _pkg({"3D/3dmodel.model": main, "3D/Objects/object_1.model": part})


def test_merge_same_structure_sources_no_id_collision(tmp_path):
    """两个源主文档同名（3D/3dmodel.model）且对象 id 撞号时，重编号必须按源隔离：
    不得产出重复 id（会导致 Bambu 报加载失败/无几何），装配树也不得串到对方源。"""
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_twin_pkg(verts=5, uid=1))
    pb.write_bytes(_twin_pkg(verts=7, uid=2))
    data = merge([str(pa), str(pb)], title="撞号回归")

    core = f"{{{CORE}}}"
    root = _parse_merged(data)
    objs = _objs(root)
    ids = [o.get("id") for o in objs]
    assert len(ids) == len(set(ids)), f"重复 id：{ids}"
    # 引用完整性：build item -> wrapper -> component -> mesh 对象全部可解析
    byid = {o.get("id"): o for o in objs}
    for it in root.find(core + "build").findall(core + "item"):
        w = byid[it.get("objectid")]
        for comp in w.iter(core + "component"):
            assert comp.get("objectid") in byid
    # 不串源：沿 build item 子树递归收集 mesh，两个源分别是 5 顶点 / 7 顶点
    def mesh_verts(oid):
        o = byid[oid]
        mesh = o.find(core + "mesh")
        n = [len(mesh.find(core + "vertices"))] if mesh is not None else []
        for comp in o.iter(core + "component"):
            n += mesh_verts(comp.get("objectid"))
        return n

    vert_counts = sorted(sum((mesh_verts(it.get("objectid")) for
                              it in root.find(core + "build").findall(core + "item")), []))
    assert vert_counts == [5, 7]
    # 拍平输出不残留 production 扩展属性
    assert not any(k.startswith("{" + PROD + "}") for o in objs for k in o.attrib)
    assert not any(k.startswith("{" + PROD + "}")
                   for o in objs for e in o.iter() for k in e.attrib)


def _bambu_project_pkg(extruder=2, colors=("#FF0000", "#00FF00"), verts=4):
    """Bambu 工程风格源：颜色在 Metadata 配置里（模型 XML 无颜色）——
    project_settings.config 为 JSON 调色板，model_settings.config 按主文档
    对象 id 记 extruder（1 基）。"""
    main = f'''<model unit="millimeter" xmlns="{CORE}">
<resources><object id="1" type="model"><components>
<component objectid="2" path="3D/Objects/object_1.model"/>
</components></object></resources>
<build><item objectid="1"/></build></model>'''
    part = f'''<model unit="millimeter" xmlns="{CORE}">
<resources><object id="2"><mesh><vertices>{''.join(f'<vertex x="{i}" y="0" z="0"/>' for i in range(verts))}</vertices>
<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>
<build></build></model>'''
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?><config>'
        f'<object id="1"><metadata key="name" value="组合体"/>'
        f'<metadata key="extruder" value="{extruder}"/></object></config>')
    return _pkg({
        "3D/3dmodel.model": main,
        "3D/Objects/object_1.model": part,
        "Metadata/project_settings.config": json.dumps({
            "filament_colour": list(colors),
            "filament_type": ["PLA"] * len(colors),
            "filament_diameter": ["1.75"] * len(colors),
        }),
        "Metadata/model_settings.config": model_settings,
    })


def _plated_pkg(plates, offset=0, colors=("#FF0000", "#00AA00")):
    """带板结构的 Bambu 工程源：plates = [(plater_id, 板名, [对象id])]，
    每个对象一个网格（x 整体平移 offset 以区分源）。颜色配置与预览图可选。"""
    objs, items, cfg_objs = [], [], []
    for n, (_, _, oids) in enumerate(plates):
        for oid in oids:
            main_oid = offset + oid
            objs.append(
                f'<object id="{main_oid}" type="model"><components>'
                f'<component objectid="{main_oid + 100}" path="3D/Objects/object_{oid}.model"/>'
                f'</components></object>')
            items.append(f'<item objectid="{main_oid}" transform="1 0 0 0 1 0 0 0 1 '
                         f'{offset * 10} 0 0"/>')
            cfg_objs.append(f'<object id="{main_oid}">'
                            f'<metadata key="extruder" value="{n + 1}"/></object>')
    main = (f'<model unit="millimeter" xmlns="{CORE}">'
            f'<resources>{"".join(objs)}</resources>'
            f'<build>{"".join(items)}</build></model>')
    parts = {}
    for _, _, oids in plates:
        for oid in oids:
            parts[f"3D/Objects/object_{oid}.model"] = (
                f'<model unit="millimeter" xmlns="{CORE}">'
                f'<resources><object id="{offset + oid + 100}"><mesh><vertices>'
                f'<vertex x="0" y="0" z="0"/><vertex x="{oid}" y="0" z="0"/>'
                f'<vertex x="2" y="0" z="0"/></vertices>'
                f'<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh>'
                f'</object></resources><build></build></model>')
    parts["Metadata/model_settings.config"] = (
        '<?xml version="1.0" encoding="UTF-8"?><config>'
        + "".join(cfg_objs) + "".join(
            f'<plate><metadata key="plater_id" value="{pid}"/>'
            f'<metadata key="plater_name" value="{name}"/>'
            f'<metadata key="thumbnail_file" value="Metadata/plate_{pid}.png"/>'
            + "".join(f'<model_instance><metadata key="object_id" value="{offset + oid}"/></model_instance>'
                      for oid in oids) + '</plate>'
            for pid, name, oids in plates) + '</config>')
    parts["Metadata/project_settings.config"] = json.dumps({
        "filament_colour": list(colors),
        "filament_type": ["PLA"] * len(colors),
        "filament_diameter": ["1.75"] * len(colors),
    })
    parts["Metadata/plate_1.png"] = b"\x89PNG-fake-1"
    parts["Metadata/plate_2.png"] = b"\x89PNG-fake-2"
    parts["3D/3dmodel.model"] = main
    return _pkg(parts)


def test_merge_plates_mode_keeps_positions_and_plates(tmp_path):
    """各自落板：每块源板成为产物一块板，对象位置一毫米不动；
    调色板全集拼接，对象料槽加偏移指向全集；板预览图按输出板号复制。"""
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_plated_pkg([(1, "板一", [10]), (2, "", [11])]))
    pb.write_bytes(_plated_pkg([(1, "五月天", [10])], offset=50))
    data = merge([str(pa), str(pb)], title="落板")

    z = zipfile.ZipFile(io.BytesIO(data))
    root = _parse_merged(data)
    core = f"{{{CORE}}}"
    metas = {m.get("name"): m.text for m in root.findall(core + "metadata")}
    assert metas.get("Application", "").startswith("BambuStudio")
    # 调色板全集：源A 2 色 + 源B 2 色
    bm = root.find(core + "resources").find(core + "basematerials")
    cols = [b.get("color") for b in bm.findall(core + "base")]
    assert cols == ["#FF0000", "#00AA00", "#FF0000", "#00AA00"]
    # 板结构：3 块输出板，名字带源文件名前缀
    cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    plates = cfg.findall("plate")
    assert [p.find("metadata[@key='plater_id']").get("value") for p in plates] == ["1", "2", "3"]
    names = [p.find("metadata[@key='plater_name']").get("value") for p in plates]
    assert names[0] == "a-板一" and names[1].startswith("a-板") and names[2] == "b-五月天"
    # 每板 1 个对象，对象 id 都在 resources 里
    byid = {o.get("id") for o in _objs(root)}
    for p in plates:
        mis = p.findall("model_instance")
        assert len(mis) == 1
        assert mis[0].find("metadata[@key='object_id']").get("value") in byid
    # 位置不动：build item 无平移（源 item 本身也是恒等 transform）
    for it in root.find(core + "build").findall(core + "item"):
        assert it.get("transform") is None
    # 板预览图按输出板号复制
    assert z.read("Metadata/plate_1.png") == b"\x89PNG-fake-1"
    assert z.read("Metadata/plate_3.png") == b"\x89PNG-fake-1"   # 源B 的板1
    # 料槽：源A 板1→1、板2→2；源B 板1→3（全集偏移）
    exts = []
    for o in cfg.findall("object"):
        md = o.find("metadata[@key='extruder']")
        if md is not None:
            exts.append(md.get("value"))
    assert sorted(exts) == ["1", "2", "3"]
    ps = json.loads(z.read("Metadata/project_settings.config").decode())
    assert ps["filament_colour"] == cols
    # 按料槽对齐数组扩到 4 槽
    assert ps["filament_diameter"] == ["1.75"] * 4


def test_merge_plate_filter(tmp_path):
    """人工选板：plate_filter 只保留选中的板。"""
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_plated_pkg([(1, "板一", [10]), (2, "板二", [11])]))
    pb.write_bytes(_plated_pkg([(1, "五月天", [10])], offset=50))
    data = merge([str(pa), str(pb)], title="选板",
                 plate_filter={"0": [2], "1": [1]})

    z = zipfile.ZipFile(io.BytesIO(data))
    cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    plates = cfg.findall("plate")
    assert len(plates) == 2
    names = [p.find("metadata[@key='plater_name']").get("value") for p in plates]
    assert names == ["a-板二", "b-五月天"]


def test_list_plates(tmp_path):
    p = tmp_path / "a.3mf"
    p.write_bytes(_plated_pkg([(1, "板一", [10]), (2, "", [11])]))
    ps = list_plates(str(p))
    assert ps == [{"plater_id": 1, "name": "板一", "objects": 1},
                  {"plater_id": 2, "name": "板2", "objects": 1}]


def test_merge_colors_from_bambu_metadata(tmp_path):
    """Bambu 工程源的颜色搬运：合成 basematerials + 三角形 pid/p1 按对象料槽上色；
    无配置的源不上色。"""
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_bambu_project_pkg(extruder=2, colors=("#FF0000", "#00FF00")))
    pb.write_bytes(_simple_pkg())            # 无 Metadata → 不上色
    data = merge([str(pa), str(pb)], title="上色")

    root = _parse_merged(data)
    res = root.find(f"{{{CORE}}}resources")
    # 全资源（含 basematerials）id 唯一——pid 引用不得有歧义
    all_ids = [el.get("id") for el in res]
    assert len(all_ids) == len(set(all_ids)), f"资源 id 重复：{sorted(all_ids)}"
    bms = res.findall(f"{{{CORE}}}basematerials")
    assert len(bms) == 1 and bms[0].get("id") == "1"
    # 调色板为全集拼接：源A 两色都在，源B 无配置不贡献
    cols = [b.get("color") for b in bms[0].findall(f"{{{CORE}}}base")]
    assert cols == ["#FF0000", "#00FF00"], cols
    byid = {o.get("id"): o for o in _objs(root)}
    # 源A 的网格（4 顶点）：extruder=2 → 压缩后 p1=下标0；源B 的网格（6 顶点）：无 pid
    painted = unpainted = None
    for o in byid.values():
        mesh = o.find(f"{{{CORE}}}mesh")
        if mesh is None:
            continue
        n = len(mesh.find(f"{{{CORE}}}vertices"))
        tr = mesh.find(f"{{{CORE}}}triangles")[0]
        if n == 4:
            painted = tr
        elif n == 6:
            unpainted = tr
    assert painted.get("pid") == "1" and painted.get("p1") == "1"
    assert unpainted.get("pid") is None and unpainted.get("p1") is None
    # Bambu sidecar：料槽配置指向新对象 id 与合并后料槽号；调色板 JSON 与之一致。
    # 无调色板的源也写对象名条目（不写 extruder）。
    z = zipfile.ZipFile(io.BytesIO(data))
    cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    entries = cfg.findall("object")
    assert len(entries) == 2
    with_ext = [e for e in entries
                if any(m.get("key") == "extruder" for m in e.findall("metadata"))]
    assert len(with_ext) == 1 and with_ext[0].get("id") in byid
    md = {m.get("key"): m.get("value") for m in with_ext[0].findall("metadata")}
    assert md.get("extruder") == "2" and md.get("name")   # 全集下指向源A 第2槽
    ps = json.loads(z.read("Metadata/project_settings.config").decode())
    assert ps["filament_colour"] == ["#FF0000", "#00FF00"]
    assert ps["filament_type"] == ["PLA", "PLA"]
    # 基底按料槽对齐数组长度与全集一致（2 槽 → 2 槽，不变）
    assert ps["filament_diameter"] == ["1.75", "1.75"]
    # Application 元数据必须是 BambuStudio 标识，否则 Bambu 不加载料槽表（颜色丢失）
    metas = {m.get("name"): m.text for m in root.findall(f"{{{CORE}}}metadata")}
    assert metas.get("Application", "").startswith("BambuStudio")
    assert metas.get("Description") == "3MF Manager 合并导出"


def test_merge_project_settings_arrays_extended(tmp_path):
    """基底 project_settings 的按料槽数组在合并料槽数变多时向后扩展（沿用末槽设置）。"""
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_bambu_project_pkg(extruder=1, colors=("#FF0000",)))
    # 源B：无 Metadata 的普通源不上色，但源A 的 1 槽基底要扩成 1 槽（不变）——
    # 这里用另一个带 1 色配置的源使合并后为 2 槽，验证数组从 1 扩到 2。
    main = f'''<model unit="millimeter" xmlns="{CORE}">
<resources><object id="1" type="model"><components>
<component objectid="2" path="3D/Objects/object_1.model"/>
</components></object></resources>
<build><item objectid="1"/></build></model>'''
    part = f'''<model unit="millimeter" xmlns="{CORE}">
<resources><object id="2"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="2" y="0" z="0"/></vertices>
<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>
<build></build></model>'''
    pb.write_bytes(_pkg({
        "3D/3dmodel.model": main,
        "3D/Objects/object_1.model": part,
        "Metadata/project_settings.config": json.dumps({
            "filament_colour": ["#00FF00"], "filament_type": ["PLA"]}),
        "Metadata/model_settings.config":
            '<?xml version="1.0" encoding="UTF-8"?><config>'
            '<object id="1"><metadata key="extruder" value="1"/></object></config>',
    }))
    data = merge([str(pa), str(pb)], title="扩数组")

    zz = zipfile.ZipFile(io.BytesIO(data))
    ps = json.loads(zz.read("Metadata/project_settings.config").decode())
    assert ps["filament_colour"] == ["#FF0000", "#00FF00"]
    # 基底（源A）只有 1 槽的 filament_diameter → 扩到 2 槽沿用末槽
    assert ps["filament_diameter"] == ["1.75", "1.75"]
    # 源B 的 filament_type 只有键没有对齐数组时由合并类型覆盖
    assert ps["filament_type"] == ["PLA", "PLA"]
    # 两源的对象料槽分别指向 1 / 2
    cfg = ET.fromstring(zz.read("Metadata/model_settings.config"))
    exts = [{m.get("key"): m.get("value") for m in o.findall("metadata")}
            for o in cfg.findall("object")]
    assert sorted(e["extruder"] for e in exts if "extruder" in e) == ["1", "2"]


def test_merge_shelf_layout_wraps_row(tmp_path):
    """摆盘货架式换行：两件超宽（>250mm 可用宽）模型，第二件换到下一行（z 错开 GAP）。"""
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_simple_pkg(verts=301, item_tf="1 0 0 0 1 0 0 0 1 0 0 0"))   # 宽 300
    pb.write_bytes(_simple_pkg(verts=301, item_tf="1 0 0 0 1 0 0 0 1 0 0 0"))
    data = merge([str(pa), str(pb)], title="换行", mode="single")
    pts = _world_pts(data)
    zs = sorted({p[2] for p in pts})
    # 测试基底无 bed_exclude_area → 起摆 z=边距+间距=8，第二行再错开 GAP=5
    assert zs == [8.0, 13.0], f"应两行摆放，实际 z={zs}"
    assert min(p[0] for p in pts if p[2] == 8.0) == 3
    assert min(p[0] for p in pts if p[2] == 13.0) == 3


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
    data = merge([str(pa), str(pb)], title="测试合并", mode="single")

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
    # build：每个源一个 item（摆盘平移已并入 wrapper 组件的 transform，item 本身不再带）
    items = root.find(f"{{{CORE}}}build").findall(f"{{{CORE}}}item")
    assert len(items) == 2
    byid = {o.get("id"): o for o in objs}
    for it in items:
        w = byid[it.get("objectid")]
        comps = w.find(f"{{{CORE}}}components")
        assert comps is not None and len(comps) == 1
        assert it.get("transform") in (None, "1 0 0 0 1 0 0 0 1 0 0 0")
    # 顶点沿组件链映射到世界坐标：A 归零后 x∈[0,5]，B 挨着排 → x∈[10,14]
    xs = _world_pts(data)
    # 起摆点留床面安全边距 3mm：A x∈[3,8]，B 挨着排 → x∈[13,17]
    assert min(p[0] for p in xs) == 3 and max(p[0] for p in xs) == 17
    assert all(p[0] <= 8 or p[0] >= 13 for p in xs)
    # 逐件摆盘：所有 item 底面都贴床（y 最小值为 0）
    assert min(p[1] for p in xs) == 0


def _world_pts(data):
    """遍历合并产物的 build→component 树，返回所有 mesh 顶点的世界坐标 (x,y,z)。"""
    core = f"{{{CORE}}}"
    z = zipfile.ZipFile(io.BytesIO(data))
    root = ET.fromstring(z.read("3D/3dmodel.model"))
    byid = {o.get("id"): o for o in root.find(core + "resources").findall(core + "object")}
    pts = []

    def walk(oid, m):
        o = byid[str(oid)]
        mesh = o.find(core + "mesh")
        if mesh is not None:
            for v in mesh.find(core + "vertices"):
                p = (float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))
                pts.append(apply_m(m, p))
        for c in o.iter(core + "component"):
            walk(c.get("objectid"), compose(parse_transform(c.get("transform")), m))

    for it in root.find(core + "build").findall(core + "item"):
        walk(it.get("objectid"), parse_transform(it.get("transform")))
    return pts


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
    assert build_export_name(3, "20260915_120000") == "merge_3models_20260915_120000.3mf"
    assert build_export_name(3, "20260915_120000").isascii()
    assert build_export_title(3, "20260915_120000") == "合并_3个模型_20260915_120000"


# ---------------- /api/merge-export 端到端（client fixture 在 conftest.py） ----------------

import json
import urllib.request


def test_merge_export_api_creates_record_and_group(client):
    from tests.test_api import fetch as api

    ra = api(client, "/api/upload", files=[("file", "a.3mf", _make_3mf_bytes("模型A", "CNme1"))])
    rb = api(client, "/api/upload", files=[("file", "b.3mf", _make_3mf_bytes("模型B", "CNme2", verts=11))])
    fa, fb = ra["results"][0]["file"], rb["results"][0]["file"]
    snap = {fa["id"]: open(fa["abs_path"], "rb").read(), fb["id"]: open(fb["abs_path"], "rb").read()}

    r = api(client, "/api/merge-export", data={"ids": [fa["id"], fb["id"]]})
    assert r["ok"], r
    f, g = r["file"], r["group"]
    # 新记录：pending、位于 exports/、几何计数是两源之和
    assert f["status"] == "pending"
    assert f["rel_path"].replace("\\", "/").startswith("exports/")
    assert f["vertices"] == 6 + 11
    # 分组：3 名成员，新记录为主、源为组件，封面=新记录
    assert g["stats"]["total"] == 3 and g["cover_file_id"] == f["id"]
    by_fid = {m["file_id"]: m for m in g["members"]}
    assert by_fid[f["id"]]["is_primary"] is True
    assert by_fid[fa["id"]]["role"] == "component" and by_fid[fb["id"]]["role"] == "component"
    assert not by_fid[fa["id"]]["is_primary"]
    # 核心约束：源文件字节一字未动
    for fid, p in ((fa["id"], fa["abs_path"]), (fb["id"], fb["abs_path"])):
        assert open(p, "rb").read() == snap[fid]
    # 组名用中文显示名；落盘文件名必须全 ASCII（Bambu GUI 对中文路径可能拒开）
    assert g["name"] == f"合并_2个模型_" + f["filename"][len("merge_2models_"):-4]
    stem = f["filename"][:-4]
    assert stem == f"merge_2models_" + g["name"][len("合并_2个模型_"):]
    assert stem.isascii() and f["rel_path"].isascii()


def test_merge_export_api_rejections(client):
    from tests.test_api import fetch as api

    ra = api(client, "/api/upload", files=[("file", "a.3mf", _make_3mf_bytes("模型A", "CNmr1"))])
    fid = ra["results"][0]["file"]["id"]
    # 少于 2 个
    assert "至少" in api(client, "/api/merge-export", data={"ids": [fid]})["error"]
    # 不存在的 id
    assert "不存在" in api(client, "/api/merge-export", data={"ids": [fid, 999999]})["error"]


def _png():
    return b"\x89PNG\r\n\x1a\n" + b"0" * 32  # 魔数合法的伪 PNG（_sniff_image_type 按字节识别）


def test_merge_export_thumb_copied_from_source(client):
    """缩略图选导：从源文件复制（非移动）缩略图给新记录；无图源选导入则保持无缩略图。"""
    import server

    ra = fetch(client, "/api/upload", files=[("file", "a.3mf", _make_3mf_bytes("模型A", "CNmt1"))])
    rb = fetch(client, "/api/upload", files=[("file", "b.3mf", _make_3mf_bytes("模型B", "CNmt2"))])
    fa, fb = ra["results"][0]["file"], rb["results"][0]["file"]
    # 给 A 上传缩略图
    up = fetch(client, "/api/thumbnail", data={"id": str(fa["id"])}, files=[("file", "t.png", _png())])
    assert up["ok"]
    src_thumb = up["thumb"]
    # 合并时选 A 的缩略图
    r = fetch(client, "/api/merge-export", data={"ids": [fa["id"], fb["id"]], "thumb_file_id": fa["id"]})
    assert r["ok"], r
    f = r["file"]
    assert f["thumb"] and f["thumb"] != src_thumb          # 复制出新文件，而非沿用同名
    assert os.path.exists(os.path.join(server.THUMB_DIR, f["thumb"]))
    assert os.path.exists(os.path.join(server.THUMB_DIR, src_thumb))  # 源缩略图原样保留
    # 选无图源（B 无缩略图也无摆盘图）→ 新记录无缩略图
    r2 = fetch(client, "/api/merge-export", data={"ids": [fa["id"], fb["id"]], "thumb_file_id": fb["id"]})
    assert r2["ok"] and not r2["file"]["thumb"]
    # thumb_file_id 不是本次合并的源 → 忽略，不报错也不复制
    r3 = fetch(client, "/api/upload", files=[("file", "c.3mf", _make_3mf_bytes("模型C", "CNmt3"))])
    fc = r3["results"][0]["file"]
    fetch(client, "/api/thumbnail", data={"id": str(fc["id"])}, files=[("file", "t.png", _png())])
    r4 = fetch(client, "/api/merge-export", data={"ids": [fa["id"], fb["id"]], "thumb_file_id": fc["id"]})
    assert r4["ok"] and not r4["file"]["thumb"]

def test_merge_export_api_plates_mode_and_listing(client):
    """API：plates 为默认模式（各自落板）；/api/merge-plates 列出源板结构。"""
    from tests.test_api import fetch as api

    ra = api(client, "/api/upload", files=[("file", "a.3mf", _plated_pkg([(1, "板一", [10]), (2, "板二", [11])]))])
    rb = api(client, "/api/upload", files=[("file", "b.3mf", _plated_pkg([(1, "五月天", [10])], offset=50))])
    fa, fb = ra["results"][0]["file"], rb["results"][0]["file"]

    rl = api(client, f"/api/merge-plates?ids={fa['id']},{fb['id']}")
    by_id = {r["id"]: r for r in rl["results"]}
    assert [p["name"] for p in by_id[fa["id"]]["plates"]] == ["板一", "板二"]
    assert by_id[fb["id"]]["plates"][0]["name"] == "五月天"

    # 选板合并：A 板二 + B 板一 → 2 块板
    r = api(client, "/api/merge-export", data={
        "ids": [fa["id"], fb["id"]], "plates": {"0": [2], "1": [1]}})
    assert r["ok"], r
    z = zipfile.ZipFile(r["file"]["abs_path"])
    cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    plates = cfg.findall("plate")
    names = [p.find("metadata[@key='plater_name']").get("value") for p in plates]
    assert names == ["a-板二", "b-五月天"]
    # mode=single 仍可用：摊平为一块板（产物无 plate 段）
    r2 = api(client, "/api/merge-export", data={"ids": [fa["id"], fb["id"]], "mode": "single"})
    assert r2["ok"], r2
    z2 = zipfile.ZipFile(r2["file"]["abs_path"])
    cfg2 = ET.fromstring(z2.read("Metadata/model_settings.config"))
    assert not cfg2.findall("plate")


def test_merge_fixes_extruder_variant_mismatch(tmp_path):
    """源 project_settings 自带失配（10 料槽但变体/self_index 只有 9 行）时，
    合并产物必须把两表补齐到等长且 ≥ 料槽数——Bambu GUI 的
    PresetBundle::load_config_file_config 校验不过会整包拒载（"Invalid
    configuration file"+ 无几何数据，CLI 切片不校验故只在 GUI 复现）。"""
    pa = tmp_path / "a.3mf"
    pb = tmp_path / "b.3mf"
    pa.write_bytes(_bambu_project_pkg(extruder=1, colors=tuple(f"#{i:06X}" for i in range(10, 20))))
    # 人为制造源内失配：在 A 的配置里把两表截成 9 行（10 料槽）
    za = zipfile.ZipFile(pa)
    ps = json.loads(za.read("Metadata/project_settings.config").decode())
    ps["extruder_variant_list"] = ["Direct Drive Standard,Direct Drive High Flow"]
    ps["filament_extruder_variant"] = ["Direct Drive Standard", "Direct Drive High Flow"] * 4 + ["Direct Drive Standard"]
    ps["filament_self_index"] = [str(i) for i in range(1, 10)]  # 字符串，与源一致
    za.close()
    pa.write_bytes(_pkg({
        "3D/3dmodel.model": zipfile.ZipFile(str(pa)).read("3D/3dmodel.model"),
        "3D/Objects/object_1.model": zipfile.ZipFile(str(pa)).read("3D/Objects/object_1.model"),
        "Metadata/project_settings.config": json.dumps(ps),
        "Metadata/model_settings.config": zipfile.ZipFile(str(pa)).read("Metadata/model_settings.config"),
    }))
    pb.write_bytes(_bambu_project_pkg(extruder=1, colors=("#123456",)))

    data = merge([str(pa), str(pb)], title="失配修复")
    z = zipfile.ZipFile(io.BytesIO(data))
    out = json.loads(z.read("Metadata/project_settings.config").decode())
    n = len(out["filament_colour"])
    assert n == 11  # A 10 色 + B 1 新色
    assert len(out["filament_extruder_variant"]) == len(out["filament_self_index"]) == n
    # 只延长不截断：前 9 行保留原值
    assert out["filament_extruder_variant"][:9] == ["Direct Drive Standard", "Direct Drive High Flow"] * 4 + ["Direct Drive Standard"]
    assert out["filament_self_index"][:9] == [str(i) for i in range(1, 10)]
    # 元素类型与源一致（self_index 保持字符串）
    assert all(isinstance(x, str) for x in out["filament_self_index"])
