#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""
解析 Downloads 目录下所有 .3mf 文件，提取元数据与几何信息。
3MF = ZIP 包，内含多个 3D/*.model (XML)。Bambu Studio 把真实网格拆到
3D/Objects/object_X.model 等部件里，主 3D/3dmodel.model 只做装配引用。
优化：几何用字节级子串计数（极快），元数据用正则提取（避免逐顶点 XML 解析）。
"""
import os, sys, re, json, zipfile

ROOT = "/Users/barney/Downloads"

def parse_3mf(path):
    rec = {
        "title": "", "designer": "", "license": "", "creation_date": "",
        "design_id": "", "profile_title": "", "objects": 0,
        "vertices": 0, "triangles": 0,
        "plates": 0, "has_slice": False, "geom_sig": "",
    }
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            rec["has_slice"] = any(n.lower().endswith("slice_info.config") for n in names)
            # 摆盘数 = 不同的 plate_N 编号（.png 或 .json），避免重复计数
            plate_nums = set()
            for n in names:
                m = re.search(r"Metadata/plate_(\d+)\.(?:png|json)", n)
                if m:
                    plate_nums.add(int(m.group(1)))
            rec["plates"] = len(plate_nums)
            model_entries = [n for n in names if n.lower().endswith(".model")]
            if not model_entries:
                return rec
            # ---- 几何：字节级子串计数（跨所有 model 部件求和）----
            v = t = obj = 0
            for entry in model_entries:
                c = z.read(entry)
                v += c.count(b"<vertex ")
                t += c.count(b"<triangle ")
                obj += c.count(b"<object ")
            rec["vertices"] = v
            rec["triangles"] = t
            rec["objects"] = obj
            rec["geom_sig"] = f"{v}|{t}"
            # ---- 元数据：正则提取主部件 ----
            main = "3D/3dmodel.model" if "3D/3dmodel.model" in model_entries else model_entries[0]
            mc = z.read(main).decode("utf-8", "ignore")
            def meta(name):
                m = re.search(r'name="' + re.escape(name) + r'">([^<]*)<', mc)
                return m.group(1).strip() if m else ""
            rec["title"] = meta("Title")
            rec["designer"] = meta("Designer")
            rec["license"] = meta("License")
            rec["creation_date"] = meta("CreationDate")
            rec["design_id"] = meta("DesignModelId")
            rec["profile_title"] = meta("ProfileTitle")
    except Exception as e:
        rec["error"] = str(e)
    return rec


def extract_previews(path):
    """从 3MF 内提取内嵌预览图（Bambu Studio 打包）。

    返回:
      {
        "model": {name, data} | None,        # 模型主预览图（thumbnail_middle 优先）
        "small": {name, data} | None,        # 模型小图
        "plates": [ {index, data}, ... ]     # 各打印板摆盘图（plate_N.png，按 N 排序）
      }
    若无任何内嵌图返回 {"model": None, "small": None, "plates": []}。
    """
    out = {"model": None, "small": None, "plates": []}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            # 模型缩略图：优先 thumbnail_middle，其次 thumbnail_3mf
            for cand in ("Auxiliaries/.thumbnails/thumbnail_middle.png",
                         "Auxiliaries/.thumbnails/thumbnail_3mf.png",
                         "Auxiliaries/.thumbnails/thumbnail_small.png"):
                if cand in names:
                    out["model"] = {"name": os.path.basename(cand), "data": z.read(cand)}
                    break
            # 摆盘图 plate_N.png
            plate_items = []
            for n in names:
                m = re.search(r"Metadata/plate_(\d+)\.png$", n)
                if m:
                    plate_items.append((int(m.group(1)), n))
            for idx, n in sorted(plate_items, key=lambda x: x[0]):
                out["plates"].append({"index": idx, "data": z.read(n)})
    except Exception:
        pass
    return out


def norm_title(t):
    t = (t or "").strip().lower()
    t = re.sub(r"[\s_\-()（）\[\]【】]+", "", t)
    return t

def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    files = []
    for dp, dn, fn in os.walk(ROOT):
        for f in fn:
            if f.lower().endswith(".3mf"):
                files.append(os.path.join(dp, f))
    files.sort()
    if limit:
        files = files[:limit]
    rows = []
    for p in files:
        rec = parse_3mf(p)
        rel = os.path.relpath(p, ROOT)
        folder = os.path.dirname(rel)
        size = os.path.getsize(p)
        rows.append({
            "path": p, "rel": rel, "filename": os.path.basename(p),
            "folder": folder, "size_mb": round(size/1024/1024, 2),
            "title": rec["title"], "designer": rec["designer"], "license": rec["license"],
            "creation_date": rec["creation_date"], "design_id": rec["design_id"],
            "profile_title": rec["profile_title"], "objects": rec["objects"],
            "vertices": rec["vertices"], "triangles": rec["triangles"],
            "plates": rec["plates"], "has_slice": rec["has_slice"],
            "geom_sig": rec["geom_sig"], "norm_title": norm_title(rec["title"]),
            "error": rec.get("error", ""),
        })
    print(json.dumps({"count": len(rows), "rows": rows}, ensure_ascii=False))

if __name__ == "__main__":
    main()
