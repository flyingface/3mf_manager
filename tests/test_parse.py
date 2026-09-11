# -*- coding: utf-8 -*-
"""parse_3mf 解析器单元测试。"""
import parse_3mf


def test_parse_basic_metadata(make_3mf, tmp_path):
    p = tmp_path / "m.3mf"
    make_3mf(str(p), title="高达元祖", designer="小明", design_id="CNabc123", verts=6, tris=2, has_slice=True)
    rec = parse_3mf.parse_3mf(str(p))
    assert rec["title"] == "高达元祖"
    assert rec["designer"] == "小明"
    assert rec["design_id"] == "CNabc123"
    assert rec["vertices"] == 6
    assert rec["triangles"] == 2
    assert rec["has_slice"] is True
    assert rec["geom_sig"] == "6|2"


def test_parse_counts_geometry_across_parts(make_3mf, tmp_path):
    """几何应跨所有 .model 部件累加。"""
    import zipfile, io
    # 构造含两个 object 部件的 3MF
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", "<model><build></build></model>")
        z.writestr("3D/Objects/object_1.model",
                   '<model><resources><object id="1"><mesh><vertices>'
                   '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
                   '</vertices><triangles><triangle v1="0" v2="1" v3="0"/></triangles>'
                   '</mesh></object></resources></model>')
        z.writestr("3D/Objects/object_2.model",
                   '<model><resources><object id="2"><mesh><vertices>'
                   '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="2" y="0" z="0"/>'
                   '</vertices><triangles><triangle v1="0" v2="1" v3="2"/><triangle v1="1" v2="2" v3="0"/>'
                   '</triangles></mesh></object></resources></model>')
    p = tmp_path / "two.3mf"
    with open(p, "wb") as f:
        f.write(buf.getvalue())
    rec = parse_3mf.parse_3mf(str(p))
    assert rec["vertices"] == 5   # 2 + 3
    assert rec["triangles"] == 3  # 1 + 2
    assert rec["objects"] == 2


def test_parse_invalid_file(tmp_path):
    p = tmp_path / "bad.3mf"
    p.write_bytes(b"not a zip")
    rec = parse_3mf.parse_3mf(str(p))
    # 不崩溃，返回空记录 + error
    assert "error" in rec or rec.get("title") == ""


def test_norm_title():
    assert parse_3mf.norm_title("  Hi  Foo ") == "hifoo"
    assert parse_3mf.norm_title("") == ""


def test_extract_previews(make_3mf, tmp_path):
    """应提取内嵌模型图与各板摆盘图。"""
    import zipfile, io
    # 构造含缩略图 + 2 张摆盘图的 3MF
    buf = io.BytesIO()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40  # 伪 PNG
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", "<model><build></build></model>")
        z.writestr("Auxiliaries/.thumbnails/thumbnail_middle.png", png)
        z.writestr("Metadata/plate_1.png", png)
        z.writestr("Metadata/plate_2.png", png)
        # 加 json 不应算作摆盘图
        z.writestr("Metadata/plate_2.json", "{}")
    p = tmp_path / "pv.3mf"
    with open(p, "wb") as f:
        f.write(buf.getvalue())
    r = parse_3mf.extract_previews(str(p))
    assert r["model"] is not None
    assert r["model"]["name"] == "thumbnail_middle.png"
    assert [x["index"] for x in r["plates"]] == [1, 2]
    # plates 计数不应把 json 重复计
    assert parse_3mf.parse_3mf(str(p))["plates"] == 2


def test_extract_previews_none(tmp_path):
    p = tmp_path / "empty.3mf"
    p.write_bytes(b"not zip")
    r = parse_3mf.extract_previews(str(p))
    assert r["model"] is None and r["plates"] == []
