#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""
把多个 3mf 拼盘合并为一个新 3mf（只读源文件，输出全新 ZIP 字节）。

原理：3MF = ZIP 包 + 3D/*.model (XML)。合并 = 把各源的 mesh/component 对象拍平
进同一个 model 文档（对象 id 统一重编号、去掉跨部件 path 引用），每个源包一层
wrapper 对象保留其内部装配关系，最后 <build> 里每个源一个 item，按包围盒网格
平移摆放（3MF 规范 Y 轴朝上，只动 X/Z，保证互不重叠）。

限制（有意为之）：不保留 Bambu 切片元数据（板配置/gcode，交给切片器）；
材质只搬运 basematerials/colorgroup，含贴图（texture2d/3d）的源直接报错。
"""
import copy
import io
import math
import posixpath
import re
import time
import zipfile
import xml.etree.ElementTree as ET

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
BBL_NS = "http://schemas.bambulab.com/package/2021"
MAT_NS = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"

# 常见命名空间注册，避免复制带扩展属性的元素时序列化成 ns0:/ns1:
ET.register_namespace("", CORE_NS)
ET.register_namespace("p", PROD_NS)
ET.register_namespace("bbl", BBL_NS)
ET.register_namespace("m", MAT_NS)

GAP_MM = 5          # 摆放间距
MAX_VERTSample = 100_000   # 包围盒顶点抽样上限


class MergeError(Exception):
    """可预期的合并失败（源缺失/损坏/不支持），消息可直接展示给用户。"""


def _q(tag):
    return f"{{{CORE_NS}}}{tag}"


def _local(tag):
    """{ns}local -> local"""
    return tag.rsplit("}", 1)[-1]


# ---------------- 变换矩阵（3MF 行向量约定：p' = [x y z 1]·M，M 为 4x3） ----------------

_IDENTITY = ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 0.0])


def parse_transform(s):
    """'m00 m01 ... m32' 12 个数 -> (3x3 行, 平移)。空/None -> 单位阵。"""
    if not s or not s.strip():
        return _IDENTITY
    parts = s.split()
    if len(parts) != 12:
        raise MergeError(f"非法 transform：{s[:60]}")
    v = [float(x) for x in parts]
    return (v[0:3], v[3:6], v[6:9], v[9:12])


def compose(a, b):
    """先 a 后 b（world = p·a·b）。按 4x4（末行 [0,0,0,1] 隐含）做 3x3+平移 乘法。"""
    (a00, a01, a02), (a10, a11, a12), (a20, a21, a22), (ax, ay, az) = a
    (b00, b01, b02), (b10, b11, b12), (b20, b21, b22), (bx, by, bz) = b
    return (
        [a00 * b00 + a01 * b10 + a02 * b20, a00 * b01 + a01 * b11 + a02 * b21, a00 * b02 + a01 * b12 + a02 * b22],
        [a10 * b00 + a11 * b10 + a12 * b20, a10 * b01 + a11 * b11 + a12 * b21, a10 * b02 + a11 * b12 + a12 * b22],
        [a20 * b00 + a21 * b10 + a22 * b20, a20 * b01 + a21 * b11 + a22 * b21, a20 * b02 + a21 * b12 + a22 * b22],
        [ax * b00 + ay * b10 + az * b20 + bx, ax * b01 + ay * b11 + az * b21 + by, ax * b02 + ay * b12 + az * b22 + bz],
    )


def apply_m(m, p):
    (r0, r1, r2, t) = m
    x, y, z = p
    return (x * r0[0] + y * r1[0] + z * r2[0] + t[0],
            x * r0[1] + y * r1[1] + z * r2[1] + t[1],
            x * r0[2] + y * r1[2] + z * r2[2] + t[2])


def fmt_transform(m):
    (r0, r1, r2, t) = m
    return " ".join(f"{v:.6g}" for v in (*r0, *r1, *r2, *t))


# ---------------- 源解析 ----------------

class _Source:
    __slots__ = ("path", "name", "unit", "objects", "materials", "items", "main_doc")

    def __init__(self, path):
        self.path = path
        self.name = posixpath.basename(path)
        self.unit = "millimeter"
        self.objects = {}    # (doc, orig_id) -> Element
        self.materials = {}  # (doc, orig_id) -> Element（非 object 资源）
        self.items = []      # [(orig_id, transform_str)]，主文档 build items
        self.main_doc = None


def _load_source(path):
    src = _Source(path)
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        raise MergeError(f"{src.name}：不是有效的 3MF（ZIP）文件")
    with z:
        names = {n.lower(): n for n in z.namelist()}
        main = names.get("3d/3dmodel.model")
        if not main:
            models = sorted(n for n in names.values() if n.lower().endswith(".model"))
            if not models:
                raise MergeError(f"{src.name}：包内没有 3D 模型部件")
            main = models[0]
        docs = {}

        def load_doc(docname):
            if docname in docs:
                return
            try:
                root = ET.fromstring(z.read(docname))
            except Exception:
                raise MergeError(f"{src.name}：模型部件 {docname} 解析失败")
            docs[docname] = root
            # 发现跨部件引用，递归加载（Bambu 把网格拆在 3D/Objects/*.model）；
            # 同时把 path 值改写为解析后的实际部件名，供后续重编号定位目标文档
            base = posixpath.dirname(docname)
            for comp in root.iter(_q("component")):
                ref = comp.get(f"{{{PROD_NS}}}path") or comp.get("path")
                if ref:
                    comp.set(f"{{{PROD_NS}}}path", _resolve_part(names, base, ref, src.name))
                    load_doc(comp.get(f"{{{PROD_NS}}}path"))

        load_doc(main)
        src.main_doc = main
        src.unit = docs[main].get("unit") or "millimeter"
        for docname, root in docs.items():
            res = root.find(_q("resources"))
            if res is None:
                continue
            for el in res:
                rid = el.get("id")
                if rid is None:
                    continue
                if el.tag == _q("object"):
                    src.objects[(docname, int(rid))] = el
                else:
                    if _local(el.tag).startswith("texture"):
                        raise MergeError(f"{src.name}：暂不支持含贴图材质的模型")
                    src.materials[(docname, int(rid))] = el
        build = docs[main].find(_q("build"))
        if build is not None:
            for it in build.findall(_q("item")):
                oid = it.get("objectid")
                if oid is None:
                    raise MergeError(f"{src.name}：build item 缺少 objectid")
                src.items.append((int(oid), it.get("transform") or ""))
        if not src.items:
            raise MergeError(f"{src.name}：没有可导出的构建对象（build 为空）")
    return src


def _resolve_part(names, base, ref, srcname):
    """跨部件引用路径解析：3MF 里 path 相对根部件所在目录，也兼容直接相对包根。"""
    ref = ref.replace("\\", "/").lstrip("/")
    for cand in (ref, posixpath.normpath(posixpath.join(base, ref) if base else ref)):
        hit = names.get(cand.lower())
        if hit:
            return hit
    raise MergeError(f"{srcname}：找不到被引用的部件 {ref}")


# ---------------- 包围盒 ----------------

def _vertex_count(mesh):
    vs = mesh.find(_q("vertices"))
    return len(vs) if vs is not None else 0


def _source_bbox(src):
    """组合 transform 后的世界包围盒（不含布局平移）。返回 ((minx,...),(maxx,...), stride)。"""
    total = sum(_vertex_count(o.find(_q("mesh")))
                for o in src.objects.values() if o.find(_q("mesh")) is not None)
    if total == 0:
        raise MergeError(f"{src.name}：没有网格几何（顶点数为 0）")
    stride = max(1, total // MAX_VERTSample)
    lo, hi = None, None

    def put(p):
        nonlocal lo, hi
        lo = [min(a, b) for a, b in zip(lo, p)] if lo else list(p)
        hi = [max(a, b) for a, b in zip(hi, p)] if hi else list(p)

    def walk(key, m):
        obj = src.objects.get(key)
        if obj is None:
            return
        mesh = obj.find(_q("mesh"))
        if mesh is not None:
            vs = mesh.find(_q("vertices"))
            for i, v in enumerate(vs):
                if i % stride == 0:
                    put(apply_m(m, (float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))))
        for comp in obj.iter(_q("component")):
            cid = comp.get("objectid")
            if cid:
                ref = comp.get(f"{{{PROD_NS}}}path") or comp.get("path")
                walk((ref or key[0], int(cid)), compose(parse_transform(comp.get("transform")), m))

    for oid, ts in src.items:
        walk((src.main_doc, oid), parse_transform(ts))

    if lo is None:
        raise MergeError(f"{src.name}：构建对象里没有网格几何")
    return lo, hi, stride


# ---------------- 合并 ----------------

def merge(paths, title=""):
    """合并多个 3mf -> 新 3mf 的 ZIP 字节。paths 顺序即摆放顺序。"""
    if len(paths) < 2:
        raise MergeError("至少需要 2 个源文件")
    sources = [_load_source(p) for p in paths]
    units = {s.unit for s in sources}
    if len(units) > 1:
        raise MergeError("源文件单位不一致（" + " / ".join(sorted(units)) + "），无法合并")
    unit = units.pop() if units else "millimeter"

    # ---- 统一重编号（对象与材质共用一个 id 空间），收集输出资源元素 ----
    idmap = {}     # (doc, orig_id) -> new_id
    objects_out, materials_out = [], []
    next_id = 1

    for s in sources:
        for key in list(s.materials.keys()) + list(s.objects.keys()):
            idmap[key] = next_id
            next_id += 1
    for s in sources:
        for key, el in s.materials.items():
            c = copy.deepcopy(el)
            c.set("id", str(idmap[key]))
            materials_out.append(c)
        for key, el in s.objects.items():
            c = copy.deepcopy(el)
            c.set("id", str(idmap[key]))
            for comp in c.iter(_q("component")):
                    # 拍平：跨部件引用先按 path 定位目标文档，再删除 path、改写 objectid
                    ref = comp.get(f"{{{PROD_NS}}}path") or comp.get("path")
                    for ak in [k for k in comp.attrib if _local(k) == "path"]:
                        del comp.attrib[ak]
                    cid = comp.get("objectid")
                    if not cid:
                        raise MergeError(f"{s.name}：component 缺少 objectid")
                    nk = idmap.get((ref or key[0], int(cid)))
                    if nk is None:
                        raise MergeError(f"{s.name}：component 引用了不存在的对象 {cid}")
                    comp.set("objectid", str(nk))
            # 材质引用改写（object 与 triangle 上的 pid、object 的 materialid）
            for owner in [c] + list(c.iter(_q("triangle"))):
                for attr in ("materialid", "pid"):
                    v = owner.get(attr)
                    if v and v.isdigit():
                        nk = idmap.get((key[0], int(v)))
                        if nk is not None:
                            owner.set(attr, str(nk))
            objects_out.append(c)

    # ---- 每个源包一个 wrapper，保留其 build item 的原始 transform ----
    wrappers = []
    for s in sources:
        w = ET.Element(_q("object"), {"id": str(next_id), "type": "model"})
        next_id += 1
        comps = ET.SubElement(w, _q("components"))
        for oid, ts in s.items:
            nk = idmap.get((s.main_doc, oid))
            if nk is None:
                raise MergeError(f"{s.name}：build 引用了不存在的对象 {oid}")
            attrs = {"objectid": str(nk)}
            if ts:
                parse_transform(ts)  # 先校验合法性
                attrs["transform"] = ts
            ET.SubElement(comps, _q("component"), attrs)
        wrappers.append(w)

    # ---- 包围盒 + 网格摆放（只动 X/Z；Y 是 3MF 的上方向，保持不动） ----
    boxes = []
    for s in sources:
        lo, hi, _ = _source_bbox(s)
        boxes.append((lo, hi))
    cols = max(1, math.ceil(math.sqrt(len(boxes))))
    item_tf = []
    cx, cz, row_dz = 0.0, 0.0, 0.0
    for i, (lo, hi) in enumerate(boxes):
        tx, ty, tz = cx - lo[0], -lo[1], cz - lo[2]   # ty：底面对齐到 Y=0（打印床上）
        item_tf.append(fmt_transform(parse_transform(f"1 0 0 0 1 0 0 0 1 {tx:.6g} {ty:.6g} {tz:.6g}")))
        cx += (hi[0] - lo[0]) + GAP_MM
        row_dz = max(row_dz, hi[2] - lo[2])
        if (i + 1) % cols == 0:
            cx, cz, row_dz = 0.0, cz + row_dz + GAP_MM, 0.0

    # ---- 组装输出文档 ----
    model = ET.Element(_q("model"), {"unit": unit})
    meta = ET.SubElement(model, _q("metadata"), {"name": "Title"})
    meta.text = title or f"合并_{len(sources)}个模型"
    for name, text in (("Application", "3MF Manager 合并导出"),
                       ("CreationDate", time.strftime("%Y-%m-%dT%H:%M:%S"))):
        m = ET.SubElement(model, _q("metadata"), {"name": name})
        m.text = text
    res = ET.SubElement(model, _q("resources"))
    for el in materials_out + objects_out + wrappers:
        res.append(el)
    build = ET.SubElement(model, _q("build"))
    for w, tf in zip(wrappers, item_tf):
        ET.SubElement(build, _q("item"), {"objectid": w.get("id"), "transform": tf})

    doc = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(model, encoding="unicode").encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="utf-8"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="utf-8"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rel0" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" '
                   'Target="/3D/3dmodel.model"/>'
                   "</Relationships>")
        z.writestr("3D/3dmodel.model", doc)
    return buf.getvalue()


def build_export_name(n, ts=None):
    return f"合并_{n}个模型_{ts or time.strftime('%Y%m%d_%H%M%S')}.3mf"

