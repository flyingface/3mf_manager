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
wrapper 对象保留其内部装配关系，最后 <build> 里每个源一个 item；摆盘把每个
build item 按世界包围盒逐件货架式排进床面（Bambu 256mm、留边、只动 X/Z），
放不下自动换行。

颜色：Bambu 的加载器不读 3MF 标准材质（basematerials/pid 被忽略），料槽信息
只认自己的 sidecar 配置——对象料槽在 model_settings.config（按对象 id 记
extruder），调色板在 project_settings.config（JSON 的 filament_colour）。
合并时读出各源这两样，压缩到"实际用到的料槽"后拼接成新调色板，wrapper 改为
每个源 build item 一个（对应 Bambu 的对象粒度），并产出这两个配置文件
（对象 id 用重编号后的新 id，料槽号按位移重排）。同时照常合成 3MF 标准
basematerials 并给三角形写 pid/p1，供遵循规范的工具用（Bambu 忽略之）。
尽力而为：无配置的源不上色；对象内焊接的不同部件配色（源导出时已焊成整体
网格）无法恢复，按对象级料槽上色。

限制（有意为之）：不保留 Bambu 切片元数据（板配置/gcode，交给切片器）；
材质只搬运 basematerials/colorgroup，含贴图（texture2d/3d）的源直接报错。
"""
import copy
import io
import json
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
BED_MM = 256        # 摆盘参考床面（Bambu P1/A1 系列 256x256）
MARGIN_MM = 3       # 床面安全边距
USABLE_MM = BED_MM - 2 * MARGIN_MM


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


def _translation(tx, ty, tz):
    return ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [tx, ty, tz])


# ---------------- 源解析 ----------------

class _Source:
    __slots__ = ("path", "name", "unit", "objects", "materials", "items", "main_doc",
                 "palette", "fil_types", "obj_extruder")

    def __init__(self, path):
        self.path = path
        self.name = posixpath.basename(path)
        self.unit = "millimeter"
        self.objects = {}    # (doc, orig_id) -> Element
        self.materials = {}  # (doc, orig_id) -> Element（非 object 资源）
        self.items = []      # [(orig_id, transform_str)]，主文档 build items
        self.main_doc = None
        self.palette = []    # filament_colour 调色板（尽力读取，读不到为空）
        self.fil_types = []  # filament_type，与 palette 对齐
        self.obj_extruder = {}  # 主文档对象 id -> 1 基料槽号（model_settings.config）


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
        _load_color_info(z, src)
    return src


def _load_color_info(z, src):
    """尽力读取 Bambu 工程的颜色信息：调色板（project_settings.config，JSON）
    与对象料槽（model_settings.config，按主文档对象 id）。读不到就保持空，不上色。"""
    try:
        d = json.loads(z.read("Metadata/project_settings.config").decode("utf-8-sig"))
        cols = d.get("filament_colour") or []
        src.palette = [c for c in map(str, cols) if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", c)]
        types = d.get("filament_type") or []
        src.fil_types = [str(t) for t in types][:len(src.palette)]
    except Exception:
        src.palette = []
        src.fil_types = []
    try:
        cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
        for obj in cfg.iter("object"):
            oid = obj.get("id")
            if oid is None or not oid.isdigit():
                continue
            for md in obj.findall("metadata"):
                if md.get("key") == "extruder" and (md.get("value") or "").isdigit():
                    src.obj_extruder[int(oid)] = max(1, int(md.get("value")))
                    break
    except Exception:
        pass


def _resolve_part(names, base, ref, srcname):
    """跨部件引用路径解析：3MF 里 path 相对根部件所在目录，也兼容直接相对包根。"""
    ref = ref.replace("\\", "/").lstrip("/")
    for cand in (ref, posixpath.normpath(posixpath.join(base, ref) if base else ref)):
        hit = names.get(cand.lower())
        if hit:
            return hit
    raise MergeError(f"{srcname}：找不到被引用的部件 {ref}")


def _strip_production_attrs(el):
    """剥掉 production 扩展属性（p:UUID 等）。拍平后的新包不声明 production 语义，
    而同一模型合并两份时 UUID 必然重复，留着会触发切片器的唯一性冲突。"""
    prod_decl = "{%s}" % PROD_NS
    for e in el.iter():
        for k in [k for k in e.attrib if k.startswith(prod_decl)]:
            del e.attrib[k]


# ---------------- 包围盒 ----------------

def _vertex_count(mesh):
    vs = mesh.find(_q("vertices"))
    return len(vs) if vs is not None else 0


def _item_boxes(src):
    """每个 build item 的世界包围盒（含自身 transform，未含摆盘平移）。
    返回 [(lo, hi), ...]（与 src.items 对齐）。"""
    boxes = []
    for oid, ts in src.items:
        m = parse_transform(ts)
        verts = []

        def collect(key, mm):
            obj = src.objects.get(key)
            if obj is None:
                return
            mesh = obj.find(_q("mesh"))
            if mesh is not None:
                vs = mesh.find(_q("vertices"))
                total = len(vs)
                stride = max(1, total // MAX_VERTSample)
                for i, v in enumerate(vs):
                    if i % stride == 0:
                        verts.append(apply_m(mm, (float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))))
            for comp in obj.iter(_q("component")):
                cid = comp.get("objectid")
                if cid:
                    ref = comp.get(f"{{{PROD_NS}}}path") or comp.get("path")
                    collect((ref or key[0], int(cid)), compose(parse_transform(comp.get("transform")), mm))

        collect((src.main_doc, oid), m)
        if verts:
            lo = [min(v[i] for v in verts) for i in range(3)]
            hi = [max(v[i] for v in verts) for i in range(3)]
            boxes.append((lo, hi))
        else:
            boxes.append((m[3][:], m[3][:]))   # 无网格的 item（如负体）按点处理
    return boxes


# ---------------- 合并 ----------------

def merge(paths, title=""):
    """合并多个 3mf -> 新 3mf 的 ZIP 字节。paths 顺序即摆放顺序。"""
    if len(paths) < 2:
        raise MergeError("至少需要 2 个源文件")
    sources = [_load_source(p) for p in paths]
    for s in sources:
        if not any(_vertex_count(o.find(_q("mesh")))
                   for o in s.objects.values() if o.find(_q("mesh")) is not None):
            raise MergeError(f"{s.name}：没有网格几何（顶点数为 0）")
    units = {s.unit for s in sources}
    if len(units) > 1:
        raise MergeError("源文件单位不一致（" + " / ".join(sorted(units)) + "），无法合并")
    unit = units.pop() if units else "millimeter"

    # ---- 拍平拷贝：从每个 build item 出发，按"料槽上下文"复制对象子树 ----
    # 同一网格对象可被多个板（build item）共享而料槽不同（Bambu 多板工程常态），
    # 网格须按上下文分别复制、分别上色。上下文规则：主文档对象自带
    # model_settings 料槽，否则继承父级；无调色板的源全部按 1 处理。
    def _ctx(s, doc, oid, inherited):
        if s.palette and doc == s.main_doc and s.obj_extruder.get(oid):
            return min(max(s.obj_extruder[oid], 1), len(s.palette))
        return inherited

    has_pal = any(s.palette for s in sources)
    pal_group_id = 1 if has_pal else None
    next_id = 2 if has_pal else 1
    id_obj = {}      # (src_idx, doc, orig_id, 料槽) -> 新 id
    flat = {}        # 同键 -> 深拷贝的 Element
    children = {}    # 同键 -> [(component 元素, 子键)]，第二遍改写 objectid 用
    copies = []      # 拷贝顺序（保持源的先后语序）
    for si, s in enumerate(sources):
        for oid, ts in s.items:
            stack = [(s.main_doc, oid, _ctx(s, s.main_doc, oid, 1))]
            while stack:
                doc, oid_, e = stack.pop()
                ck = (si, doc, oid_, e)
                if ck in id_obj:
                    continue
                el = s.objects.get((doc, oid_))
                if el is None:
                    continue
                nid = str(next_id)
                next_id += 1
                id_obj[ck] = nid
                c = copy.deepcopy(el)
                c.set("id", nid)
                flat[ck] = c
                copies.append(ck)
                lst = []
                for comp in c.iter(_q("component")):
                    # 拍平：跨部件引用先按 path 定位目标文档，随即删除 path 并记录子键
                    ref = comp.get(f"{{{PROD_NS}}}path") or comp.get("path")
                    for ak in [k for k in comp.attrib if _local(k) == "path"]:
                        del comp.attrib[ak]
                    cid = comp.get("objectid")
                    if not cid:
                        raise MergeError(f"{s.name}：component 缺少 objectid")
                    cdoc = ref or doc
                    ce = _ctx(s, cdoc, int(cid), e)
                    lst.append((comp, (si, cdoc, int(cid), ce)))
                    stack.append((cdoc, int(cid), ce))
                children[ck] = lst

    # ---- 颜色：把各源"实际用到的料槽"压缩拼接成新调色板（源B 顺延源A 之后）----
    pal_offsets, e2is = [], []
    bases, ftypes = [], []
    for si, s in enumerate(sources):
        pal_offsets.append(len(bases))
        if not s.palette:
            e2is.append({})
            continue
        es = sorted({ck[3] for ck in copies if ck[0] == si
                     and flat[ck].find(_q("mesh")) is not None}) or [1]
        e2is.append({e: i for i, e in enumerate(es)})
        bases.extend(s.palette[e - 1] for e in es)
        ftypes.extend((s.fil_types[e - 1] if e - 1 < len(s.fil_types) else "PLA") for e in es)

    # ---- 重编号改写：组件 objectid、材质引用、按上下文料槽上色 ----
    # 材质资源无料槽上下文，单独编号（对象与材质共用一个 id 空间，不得与
    # basematerials 组号重复）。
    idmap = {}     # (src_idx, doc, orig_id) -> 新 id
    objects_out, materials_out = [], []
    for si, s in enumerate(sources):
        for key in s.materials:
            idmap[(si,) + key] = next_id
            next_id += 1
    for si, s in enumerate(sources):
        for key, el in s.materials.items():
            c = copy.deepcopy(el)
            c.set("id", str(idmap[(si,) + key]))
            _strip_production_attrs(c)
            materials_out.append(c)
    for ck in copies:
        si, doc, oid_, e = ck
        s = sources[si]
        c = flat[ck]
        for comp, ckey in children[ck]:
            nk = id_obj.get(ckey)
            if nk is None:
                raise MergeError(f"{s.name}：component 引用了不存在的对象 {ckey[2]}")
            comp.set("objectid", nk)
        # 材质引用改写（object 与 triangle 上的 pid、object 的 materialid）。
        # 资源通常与对象同文档，但 Bambu 把 basematerials 放在主文档，
        # 因此先查对象所在文档，再回退主文档。
        for owner in [c] + list(c.iter(_q("triangle"))):
            for attr in ("materialid", "pid"):
                v = owner.get(attr)
                if v and v.isdigit():
                    nk = idmap.get((si, doc, int(v)))
                    if nk is None:
                        nk = idmap.get((si, s.main_doc, int(v)))
                    if nk is not None:
                        owner.set(attr, str(nk))
        # 上色：无自带材质的网格按上下文料槽写 pid/p1（供遵循 3MF 规范的工具）；
        # 自带 basematerials 的源不覆盖，保持其原有引用。
        mesh = c.find(_q("mesh"))
        if mesh is not None and s.palette and pal_group_id is not None and e in e2is[si]:
            pi = pal_offsets[si] + e2is[si][e]
            for tr in mesh.iter(_q("triangle")):
                if not s.materials or not tr.get("pid"):
                    tr.set("pid", str(pal_group_id))
                    tr.set("p1", str(pi))
        _strip_production_attrs(c)
        objects_out.append(c)

    # ---- 摆盘 + wrapper：每个源 build item 包一层（对应 Bambu 的一个"对象"，
    # 料槽按对象上色），item 按世界包围盒货架式排进床面 ----
    # 只动 X/Z（3MF 规范 Y 轴朝上），Y 底面对齐到 0（打印床）；本行放不下换一行。
    # 源按顺序依次排，前面的源占前面的格子，保持"先来先摆"的拼盘语序。
    wrappers = []
    cfg_entries = []   # (wrapper_id, 对象名, 全局料槽号 or None)
    cx, cz, row_d = 0.0, 0.0, 0.0
    for si, s in enumerate(sources):
        stem = posixpath.splitext(s.name)[0]
        for k, ((oid, ts), (lo, hi)) in enumerate(zip(s.items, _item_boxes(s)), 1):
            e0 = _ctx(s, s.main_doc, oid, 1)
            nk = id_obj.get((si, s.main_doc, oid, e0))
            if nk is None:
                raise MergeError(f"{s.name}：build 引用了不存在的对象 {oid}")
            wx = hi[0] - lo[0]
            if cx > 0 and cx + wx > USABLE_MM:
                cx, cz, row_d = 0.0, cz + row_d + GAP_MM, 0.0
            tf = compose(parse_transform(ts), _translation(cx - lo[0], -lo[1], cz - lo[2]))
            wid = str(next_id)
            next_id += 1
            w = ET.Element(_q("object"), {"id": wid, "type": "model"})
            comps = ET.SubElement(w, _q("components"))
            ET.SubElement(comps, _q("component"),
                          {"objectid": nk, "transform": fmt_transform(tf)})
            wrappers.append(w)
            g_e = None
            if s.palette and e0 in e2is[si]:
                g_e = pal_offsets[si] + e2is[si][e0] + 1   # Bambu 料槽 1 基
            cfg_entries.append((wid, f"{stem}_{k}", g_e))
            cx += wx + GAP_MM
            row_d = max(row_d, hi[2] - lo[2])

    # ---- 组装输出文档 ----
    model = ET.Element(_q("model"), {"unit": unit})
    meta = ET.SubElement(model, _q("metadata"), {"name": "Title"})
    meta.text = title or f"合并_{len(sources)}个模型"
    for name, text in (("Application", "3MF Manager 合并导出"),
                       ("CreationDate", time.strftime("%Y-%m-%dT%H:%M:%S"))):
        m = ET.SubElement(model, _q("metadata"), {"name": name})
        m.text = text
    res = ET.SubElement(model, _q("resources"))
    if pal_group_id is not None:
        bm = ET.Element(_q("basematerials"), {"id": str(pal_group_id)})
        for i, col in enumerate(bases):
            ET.SubElement(bm, _q("base"), {"name": f"filament{i + 1}", "color": col})
        res.append(bm)
    for el in materials_out + objects_out + wrappers:
        res.append(el)
    build = ET.SubElement(model, _q("build"))
    for w in wrappers:
        ET.SubElement(build, _q("item"), {"objectid": w.get("id")})

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
        # Bambu sidecar 配置：对象名/料槽（model_settings.config）与调色板
        # （project_settings.config，Bambu 存 JSON）。料槽号指向合并后的调色板。
        if cfg_entries and bases:
            cfg = ET.Element("config")
            for wid, nm, g_e in cfg_entries:
                o = ET.SubElement(cfg, "object", {"id": wid})
                ET.SubElement(o, "metadata", {"key": "name", "value": nm})
                if g_e is not None:
                    ET.SubElement(o, "metadata", {"key": "extruder", "value": str(g_e)})
            z.writestr("Metadata/model_settings.config",
                       '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(cfg, encoding="unicode"))
            z.writestr("Metadata/project_settings.config",
                       json.dumps({"filament_colour": bases, "filament_type": ftypes}))
    return buf.getvalue()


def build_export_name(n, ts=None):
    return f"合并_{n}个模型_{ts or time.strftime('%Y%m%d_%H%M%S')}.3mf"

