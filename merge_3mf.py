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
BED_MM = 256        # 摆盘参考床面（Bambu P1/A1 系列 256x256）
MARGIN_MM = 3       # 床面安全边距
USABLE_MM = BED_MM - 2 * MARGIN_MM
# Bambu 的多板在内部坐标系里按网格并排（GUI reload_all_objects 按实例包围盒
# 与各板矩形相交决定归属），板间距 = 板宽 × 1/5（PartPlate 的
# LOGICAL_PART_PLATE_GAP）。产物必须把第 k 块板的对象平移到该板的虚拟床区，
# 否则全部对象会被并进第一块板。
PLATE_STRIDE_MM = BED_MM * 1.2


def _bambu_plate_cols(n):
    """照抄 PartPlateList::compute_colum_count 的列数公式。"""
    v = math.sqrt(n)
    r = round(v)
    return int(r + 1) if v > r else int(r)
# Bambu 只在模型文档的 Application 元数据带 BambuStudio 标识时才按自家工程
# 加载（否则丢弃 project_settings 的料槽表并提示"仅加载几何数据"）。
# 取值与用户常用版本一致，避免触发"由旧版本生成"之类的版本审查。
BAMBU_APP_TAG = "BambuStudio-02.08.02.61"


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
                 "palette", "fil_types", "obj_extruder", "project_settings", "plates")

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
        self.project_settings = None  # 完整的 project_settings JSON（dict），作产物基底
        self.plates = []     # [{"plater_id","name","objects":[oid], "imgs":{key:zip路径}}]，无板为空


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
        _load_plates(z, src)
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
        src.project_settings = d if isinstance(d, dict) and d else None
    except Exception:
        src.palette = []
        src.fil_types = []
        src.project_settings = None
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


PLATE_IMG_KEYS = ("thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file")


def _load_plates(z, src):
    """解析 model_settings.config 的 <plate> 段：板号、板名、板上的对象与预览图引用。
    无板段（普通 3mf）保持空列表，合并时整包视为一块板。"""
    try:
        cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    except Exception:
        return
    names = {n.lower(): n for n in z.namelist()}
    for idx, pl in enumerate(cfg.iter("plate"), 1):
        pid, name, objs, imgs = None, None, [], {}
        for md in pl.findall("metadata"):
            k, v = md.get("key"), md.get("value") or ""
            if k == "plater_id" and v.isdigit():
                pid = int(v)
            elif k == "plater_name" and v.strip():
                name = v.strip()
            elif k in PLATE_IMG_KEYS and v.strip():
                hit = names.get(v.strip().replace("\\", "/").lstrip("/").lower())
                if hit:
                    imgs[k] = hit
        for mi in pl.findall("model_instance"):
            for md in mi.findall("metadata"):
                if md.get("key") == "object_id" and (md.get("value") or "").isdigit():
                    objs.append(int(md.get("value")))
        if objs:
            src.plates.append({"plater_id": pid or idx, "name": name,
                               "objects": objs, "imgs": imgs})


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

def merge(paths, title="", mode="plates", plate_filter=None):
    """合并多个 3mf -> 新 3mf 的 ZIP 字节。paths 顺序即板的先后语序。

    mode="plates"（默认，各自落板）：保留每个源的板结构——每块源板成为产物的
    一块板，板内对象位置一毫米不动；无板段的源整包视为一块板。板选可经
    plate_filter（{源序号字符串: [plater_id, ...]}）人工筛选，缺省全选。
    mode="single"（拼一盘）：所有 build item 摊平到一块板，货架式重摆。
    两种模式的调色板都是各源调色板的全集拼接（源 B 顺延源 A 之后），对象料槽
    加偏移指向全集。"""
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

    # ---- 颜色：调色板全集拼接（源 B 顺延源 A），对象料槽加偏移指向全集 ----
    pal_offsets = []
    bases, ftypes = [], []
    for s in sources:
        pal_offsets.append(len(bases))
        bases.extend(s.palette)
        ftypes.extend((s.fil_types[i] if i < len(s.fil_types) else "PLA")
                      for i in range(len(s.palette)))

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
        if mesh is not None and s.palette and pal_group_id is not None:
            pi = pal_offsets[si] + e - 1
            for tr in mesh.iter(_q("triangle")):
                if not s.materials or not tr.get("pid"):
                    tr.set("pid", str(pal_group_id))
                    tr.set("p1", str(pi))
        _strip_production_attrs(c)
        objects_out.append(c)

    # ---- wrapper：每个源 build item 包一层（对应 Bambu 的一个"对象"，
    # 料槽按对象上色）。plates 模式对象位置一毫米不动并保留板结构；
    # single 模式按世界包围盒货架式排进一块床面（跳过 bed_exclude_area）。
    base_ps = next((s.project_settings for s in sources if s.project_settings), None)

    def source_plan(s, si):
        """plates 模式的输出板计划：真实板（可按 plate_filter 筛选）+ 不属于
        任何真实板的 item 兜底板；无板段的源整包一块板。被筛选掉的板其对象
        一并排除（人工选板即明确不要）。src_origin 是该板在源文件网格里的
        原点（Bambu 保存时把每板对象平移进自己的虚拟床区，重排时要先扣掉）。"""
        sel = None
        if plate_filter:
            sel = {int(x) for x in (plate_filter.get(str(si)) or [])}
        src_cols = _bambu_plate_cols(max(len(s.plates), 1))
        plan = []
        in_any_real = set()
        for p in s.plates:
            in_any_real.update(p["objects"])
            if sel is not None and p["plater_id"] not in sel:
                continue
            its = [(oid, ts) for (oid, ts) in s.items if oid in p["objects"]]
            srow, scol = divmod(p["plater_id"] - 1, src_cols)
            plan.append({"name": p["name"] or f"板{p['plater_id']}",
                         "imgs": p["imgs"], "items": its,
                         "src_origin": (scol * PLATE_STRIDE_MM, -srow * PLATE_STRIDE_MM)})
        rest = [(oid, ts) for (oid, ts) in s.items if oid not in in_any_real]
        if rest:
            plan.append({"name": f"板{len(plan) + 1}", "imgs": {}, "items": rest,
                         "src_origin": (0.0, 0.0)})
        return plan

    def walk_refs(s, key, mm, parts):
        """展开对象引用树：每个网格引用一条 <part>（列主序 4x4 矩阵与面数），
        缺 part 会被 Bambu 判配置无效。"""
        obj = s.objects.get(key)
        if obj is None:
            return
        mesh = obj.find(_q("mesh"))
        if mesh is not None:
            trs = mesh.find(_q("triangles"))
            r0, r1, r2, t = mm
            mat16 = " ".join(f"{x:.17g}" for x in (
                r0[0], r1[0], r2[0], t[0],
                r0[1], r1[1], r2[1], t[1],
                r0[2], r1[2], r2[2], t[2],
                0.0, 0.0, 0.0, 1.0))
            parts.append((mat16, len(trs) if trs is not None else 0))
            return
        comps_el = obj.find(_q("components"))
        for comp in (comps_el if comps_el is not None else []):
            cid = comp.get("objectid")
            if not cid or not cid.isdigit():
                continue
            ref = comp.get(f"{{{PROD_NS}}}path") or comp.get("path")
            walk_refs(s, (ref or key[0], int(cid)),
                      compose(parse_transform(comp.get("transform") or ""), mm), parts)

    wrappers = []
    cfg_entries = []   # (wrapper_id, 对象名, 全局料槽号 or None, [(矩阵16, 面数), ...], 板序号 0=无板)

    def make_wrapper(s, si, stem, j, oid, placement, plate_no):
        """包一层 wrapper、登记 cfg 条目，返回 wrapper id。part 矩阵相对物体
        （不含 wrapper 的板位 transform），与 Bambu 原生语义一致。"""
        nonlocal next_id
        e0 = _ctx(s, s.main_doc, oid, 1)
        nk = id_obj.get((si, s.main_doc, oid, e0))
        if nk is None:
            raise MergeError(f"{s.name}：build 引用了不存在的对象 {oid}")
        wid = str(next_id)
        next_id += 1
        w = ET.Element(_q("object"), {"id": wid, "type": "model"})
        comps = ET.SubElement(w, _q("components"))
        ET.SubElement(comps, _q("component"),
                      {"objectid": nk, "transform": fmt_transform(placement)})
        wrappers.append(w)
        parts = []
        walk_refs(s, (s.main_doc, oid), parse_transform(""), parts)
        g_e = pal_offsets[si] + e0 if s.palette else None
        cfg_entries.append((wid, f"{stem}_{j}", g_e, parts, plate_no))
        return wid

    plates_out = []    # plates 模式：[(板名, [wrapper_id...], {img key: 输出路径})]
    img_copies = []    # (输出路径, 源序号, 源路径)
    if mode == "plates":
        # 先收集全部输出板，再按 Bambu 的板网格公式平移：板 k(0 基) 位于
        # (col*stride, -row*stride)，stride=板宽×1.2；col/row 按列优先序展开。
        flat_plates = []
        for si, s in enumerate(sources):
            stem = posixpath.splitext(s.name)[0]
            for plt in source_plan(s, si):
                flat_plates.append((si, s, stem, plt))
        cols = _bambu_plate_cols(len(flat_plates))
        for k, (si, s, stem, plt) in enumerate(flat_plates):
            row, col = divmod(k, cols)
            src_ox, src_oy = plt["src_origin"]
            # Bambu 的板网格在 3mf 坐标的 x（列）与 y（行，负方向）上展开，
            # z 是高度不动；delta = 新板原点 - 源板原点
            dx = col * PLATE_STRIDE_MM - src_ox
            dy = -row * PLATE_STRIDE_MM - src_oy
            pname = f"{stem}-{plt['name']}"
            imgs = {}
            for key, zipname in plt["imgs"].items():
                ext = posixpath.splitext(zipname)[1] or ".png"
                prefix = {"thumbnail_file": f"plate_{k + 1}",
                          "thumbnail_no_light_file": f"plate_no_light_{k + 1}",
                          "top_file": f"top_{k + 1}",
                          "pick_file": f"pick_{k + 1}"}[key]
                out = f"Metadata/{prefix}{ext}"
                imgs[key] = out
                img_copies.append((out, si, zipname))
            shift = _translation(dx, dy, 0.0)
            wids = [make_wrapper(s, si, stem, j, oid,
                                 compose(parse_transform(ts), shift), k + 1)
                    for j, (oid, ts) in enumerate(plt["items"], 1)]
            plates_out.append((pname, wids, imgs))
    else:
        excl_top = 0.0
        if base_ps:
            pts = []
            for r in base_ps.get("bed_exclude_area") or []:
                mm_ = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", str(r))
                if mm_:
                    pts.append((float(mm_.group(1)), float(mm_.group(2))))
            excl_top = max((y for _, y in pts), default=0.0)
        cx, cz, row_d = MARGIN_MM, excl_top + GAP_MM + MARGIN_MM, 0.0
        for si, s in enumerate(sources):
            stem = posixpath.splitext(s.name)[0]
            for k, ((oid, ts), (lo, hi)) in enumerate(zip(s.items, _item_boxes(s)), 1):
                wx = hi[0] - lo[0]
                if cx > MARGIN_MM and cx + wx > USABLE_MM:
                    cx, cz, row_d = MARGIN_MM, cz + row_d + GAP_MM, 0.0
                placement = compose(parse_transform(ts), _translation(cx - lo[0], -lo[1], cz - lo[2]))
                make_wrapper(s, si, stem, k, oid, placement, 0)
                cx += wx + GAP_MM
                row_d = max(row_d, hi[2] - lo[2])

    # ---- 组装输出文档 ----
    model = ET.Element(_q("model"), {"unit": unit})
    meta = ET.SubElement(model, _q("metadata"), {"name": "Title"})
    meta.text = title or f"合并_{len(sources)}个模型"
    for name, text in (("Application", BAMBU_APP_TAG),
                       ("Description", "3MF Manager 合并导出"),
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
        ct = ('<?xml version="1.0" encoding="utf-8"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>')
        if img_copies:
            ct += '<Default Extension="png" ContentType="image/png"/>'
        ct += "</Types>"
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="utf-8"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rel0" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" '
                   'Target="/3D/3dmodel.model"/>'
                   "</Relationships>")
        z.writestr("3D/3dmodel.model", doc)
        # 板预览图：从各源包复制并按输出板号重命名（缺图省略引用）。
        for out, si, zipname in img_copies:
            try:
                with zipfile.ZipFile(sources[si].path) as zs:
                    zn = {n.lower(): n for n in zs.namelist()}
                    hit = zn.get(zipname.lower())
                    if hit:
                        z.writestr(out, zs.read(hit))
            except Exception:
                pass
        # Bambu sidecar 配置：对象名/料槽（model_settings.config）、板结构
        # 与项目设置（project_settings.config）。料槽号指向合并后的全集调色板。
        if cfg_entries and bases:
            cfg = ET.Element("config")
            for wid, nm, g_e, parts, plate_no in cfg_entries:
                o = ET.SubElement(cfg, "object", {"id": wid})
                ET.SubElement(o, "metadata", {"key": "name", "value": nm})
                if g_e is not None:
                    ET.SubElement(o, "metadata", {"key": "extruder", "value": str(g_e)})
                for i, (mat16, fc) in enumerate(parts, 1):
                    pe = ET.SubElement(o, "part", {"id": str(i), "subtype": "normal_part"})
                    ET.SubElement(pe, "metadata", {"key": "name", "value": f"{nm}_{i}"})
                    ET.SubElement(pe, "metadata", {"key": "matrix", "value": mat16})
                    ET.SubElement(pe, "mesh_stat", {"face_count": str(fc), "edges_fixed": "0",
                                                    "degenerate_facets": "0", "facets_removed": "0",
                                                    "facets_reversed": "0", "backwards_edges": "0"})
            if plates_out:
                identify = 0
                for k, (pname, wids, imgs) in enumerate(plates_out, 1):
                    pl = ET.SubElement(cfg, "plate")
                    for key, val in (("plater_id", str(k)), ("plater_name", pname),
                                     ("locked", "false"),
                                     ("filament_map_mode", "Auto For Flush"),
                                     ("filament_maps", " ".join(["1"] * len(bases))),
                                     ("filament_volume_maps", " ".join(["0"] * len(bases))),
                                     ("gcode_file", "")):
                        ET.SubElement(pl, "metadata", {"key": key, "value": val})
                    for key, out in imgs.items():
                        ET.SubElement(pl, "metadata", {"key": key, "value": out})
                    for wid in wids:
                        identify += 1
                        mi = ET.SubElement(pl, "model_instance")
                        ET.SubElement(mi, "metadata", {"key": "object_id", "value": wid})
                        # instance_id 是对象内的实例下标（每个 wrapper 只有一个
                        # build item，恒为 0）；identify_id 才是全局唯一标识
                        ET.SubElement(mi, "metadata", {"key": "instance_id", "value": "0"})
                        ET.SubElement(mi, "metadata", {"key": "identify_id", "value": str(identify + 200)})
            z.writestr("Metadata/model_settings.config",
                       '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(cfg, encoding="unicode"))
            # project_settings 必须是 Bambu 认可的完整结构（含 filament_settings_id
            # 等几十个按料槽对齐的数组），只写颜色会被判"配置无效"而整包丢弃。
            # 因此以第一个带配置的源为基底，把按料槽对齐的数组扩/截到合并后的
            # 料槽数。数组三种形态，扩容规则各异（弄错会被 Bambu 整包判无效）：
            #   len==old_n          直排（filament_colour 等）：扩到 new_n，沿用末槽；
            #   len==old_n²         方阵（flush_volumes_matrix 的 N×N）：每行扩到
            #                       new_n 后再补 new_n-old_n 行；
            #   len==k*old_n (1<k<old_n)  每料槽 k 行的组表（filament_extruder_variant
            #                       的料槽×变体表等）：保留全表，为新料槽追加末槽的组。
            base = base_ps
            if base is not None:
                ps = copy.deepcopy(base)
                old_n = len(ps.get("filament_colour") or [])
                new_n = len(bases)
                if old_n and new_n != old_n:
                    for v in ps.values():
                        if not isinstance(v, list) or not v or len(v) % old_n:
                            continue
                        L = len(v)
                        if L == old_n:
                            v[:] = (v + [v[-1]] * (new_n - old_n)) if new_n > old_n else v[:new_n]
                        elif L == old_n * old_n:
                            rows = [v[i:i + old_n] for i in range(0, L, old_n)]
                            rows = [(r + [r[-1]] * (new_n - old_n))[:new_n] for r in rows][:new_n]
                            rows += [rows[-1][:]] * (new_n - len(rows))
                            v[:] = [x for r in rows for x in r]
                        else:
                            k = L // old_n
                            v[:] = v + v[-k:] * (new_n - old_n)
                    # filament_self_index 是"料槽 id × 变体"行表里的料槽编号，
                    # 扩容后新行的编号必须是新料槽号（9..new_n），不能复制末槽；
                    # Bambu 以字符串存储（JSON 数组元素类型须与源一致）
                    fsi = ps.get("filament_self_index")
                    if isinstance(fsi, list) and new_n and len(fsi) % new_n == 0:
                        per = len(fsi) // new_n
                        as_str = bool(fsi) and isinstance(fsi[0], str)
                        ps["filament_self_index"] = [
                            str(i // per + 1) if as_str else (i // per + 1)
                            for i in range(len(fsi))]
            else:
                ps = {}
            ps["filament_colour"] = list(bases)
            ps["filament_type"] = list(ftypes)
            # Bambu GUI 校验（PresetBundle::load_config_file_config）：配置含 extruder_variant_list
            # 时，filament_extruder_variant 与 filament_self_index 必须等长且数量 ≥ 料槽数，
            # 否则整包判 "Invalid configuration file" 并放弃加载模型（随后报"无几何数据"）。
            # CLI 切片不校验，故只在 GUI 复现。源文件自带的失配（料槽数 ≠ 变体行数）在此
            # 统一补齐：只延长不截断，沿用末行值，保持元素类型与源一致（self_index 为字符串）。
            fev = ps.get("filament_extruder_variant")
            fsi = ps.get("filament_self_index")
            if isinstance(fev, list) and isinstance(fsi, list) and fev and fsi:
                target = max(len(fev), len(fsi), len(bases))
                if len(fev) != target:
                    ps["filament_extruder_variant"] = fev + [fev[-1]] * (target - len(fev))
                if len(fsi) != target:
                    ps["filament_self_index"] = fsi + [fsi[-1]] * (target - len(fsi))
            z.writestr("Metadata/project_settings.config",
                       json.dumps(ps, ensure_ascii=False))
    return buf.getvalue()


def build_export_name(n, ts=None):
    """导出文件的落盘名。必须保持 ASCII：Bambu Studio GUI 对含中文的文件路径
    可能报「Invalid configuration file / 此文件不包含任何几何数据」而拒开
    （同样的文件改 ASCII 路径后 CLI 三条加载路径全部通过，见 2.8.1 排查记录）。"""
    return f"merge_{n}models_{ts or time.strftime('%Y%m%d_%H%M%S')}.3mf"


def build_export_title(n, ts=None):
    """导出记录的显示名（XML Title 元数据 / 别名 / 分组名），保留中文。"""
    return f"合并_{n}个模型_{ts or time.strftime('%Y%m%d_%H%M%S')}"


def list_plates(path):
    """列出一个 3mf 的板结构（选板 UI 用）：[{plater_id, name, objects}]。
    无板段返回空列表。只读。"""
    src = _load_source(path)
    return [{"plater_id": p["plater_id"],
             "name": p["name"] or f"板{p['plater_id']}",
             "objects": len(p["objects"])} for p in src.plates]

