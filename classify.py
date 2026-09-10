#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""分类与归档规划纯逻辑层：规则分类、目标路径计算、别名生成。

不访问数据库与文件系统（rules.json 仅按模块目录只读加载），
server.py 作为组合根负责注入运行时配置与自定义分类。

规则数据（关键词/映射/噪音词）外置在 rules.json，可直接编辑扩充；
子分类语义（subcat.UNCAT_RULES / FUNC_SUB / dummy / minecraft）仍在
subcat.py 与 mc_subcat.py，rules.json 的 label_keywords 按分类名引用。
"""
import json
import os
import re

import subcat
import mc_subcat  # noqa: F401  (由 subcat.refine 间接使用，保持导入一致)

SEP = " · "
_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")


def load_rules(path=None):
    """加载规则库 JSON；文件缺失时给出可操作的报错（正常仓库/uv 运行不会发生）。"""
    p = path or _RULES_PATH
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"分类规则文件缺失：{p}。请在项目根目录保留 rules.json 后重试。")


RULES = load_rules()

IP_NAME = dict(RULES["ip_name"])
FUNC_MAP = {k: tuple(v) for k, v in RULES["func_map"].items()}
NOISE_WORDS = set(RULES["noise_words"])


def _build_content_rules():
    """content_rules 展开：keywords 直接用；label_keywords 按分类名从 subcat 引用关键词表。"""
    out = []
    for r in RULES.get("content_rules", []):
        if "keywords" in r:
            out.append((r["keywords"], r["category"]))
        for label in r.get("label_keywords", []):
            kws = subcat.UNCAT_BY_LABEL.get(label)
            if kws:
                out.append((kws, label))
    return out


CONTENT_RULES = _build_content_rules()
PRIORITY_IP = RULES["priority_ip"]
FOLDER_IP = RULES["folder_ip"]
IP_KW = RULES["ip_kw"]
EXTRA_IP = RULES["extra_ip"]
FFUNC = RULES["ffunc"]
RULES_FINAL = [tuple(x) for x in RULES["rules"]]


def custom_cat_keyword(cat):
    """自定义分类的匹配关键词：取分类名最具体的末段。
    如 '手办/宠物小精灵' → '宠物小精灵'，'IP·初音未来' → '初音未来'。
    只取末段是为了避免 '手办' 这类宽泛前缀误吞无关文件。"""
    tail = cat[3:] if cat.startswith("IP·") else cat
    parts = [p.strip() for p in re.split(r'/|·', tail) if p.strip()]
    if not parts:
        return ""
    kw = max(parts, key=len)
    return kw if len(kw) >= 2 else ""


def categorize(folder, filename, title, sib_text="", custom_cats=None):
    s = (folder + " " + filename + " " + title + " " + sib_text)
    sl = s.lower()
    ftl = (filename + " " + title).lower()
    # 用户确认过的自定义分类优先命中（用户明确教过系统的分类，应最优先）
    for cat in (custom_cats or []):
        kw = custom_cat_keyword(cat)
        if kw and kw in sl:
            return subcat.refine(cat, folder, filename, title)
    for kw, label in PRIORITY_IP:
        if kw in ftl:
            return subcat.refine(label, folder, filename, title)
    for k, v in FOLDER_IP.items():
        if folder.lower() == k or folder.lower().startswith(k + "/") or folder.lower().startswith(k):
            sub = folder[len(k):].strip("/")
            return subcat.refine(v + (SEP + sub if sub else ""), folder, filename, title)
    for kw, label in IP_KW:
        if kw in sl:
            return subcat.refine(label, folder, filename, title)
    for kw, label in EXTRA_IP:
        if kw in sl:
            return subcat.refine(label, folder, filename, title)
    for kws, label in CONTENT_RULES:
        if any(k in sl for k in kws):
            return subcat.refine(label, folder, filename, title)
    fl = folder.lower()
    for k, v in FFUNC.items():
        if fl == k or fl.startswith(k + "/") or fl.startswith(k):
            return subcat.refine(v, folder, filename, title)
    for kws, label in RULES_FINAL:
        if any(k in sl for k in kws):
            return subcat.refine(label, folder, filename, title)
    for kws, label in subcat.UNCAT_RULES:
        if any(k in sl for k in kws):
            return label
    for frag, label in subcat.FILENAME_OVERRIDE:
        if frag in filename.lower():
            return subcat.refine(label, folder, filename, title)
    return "其他/未分类"


def target_of(cat, fn="", title="", folder=""):
    if cat.startswith("IP·"):
        rest = cat[3:]
        name, sub = rest.split(SEP, 1) if SEP in rest else (rest, None)
        l2 = IP_NAME.get(name.strip(), name.strip())
        if l2 == "Minecraft":
            sub = mc_subcat.minecraft_subcat(fn, title, folder)
        elif l2 == "Dummy13":
            sub = subcat.dummy_subcat(fn, title, folder)
        return ("01_IP授权", l2, sub)
    if SEP in cat:
        parent, sub = cat.split(SEP, 1)
    else:
        parent, sub = cat, None
    if parent in FUNC_MAP:
        l1, l2 = FUNC_MAP[parent]
        return (l1, l2, sub)
    # 不在映射表的分类：用分类名本身按 "/" 拆成多级目录，让归档路径反映分类（过滤 ".." 防穿越）
    parts = [p for p in cat.split("/") if p and p != ".."]
    return tuple(parts) if parts else ("04_其他未分类", None, None)


def target_relpath(cat, fn="", title="", folder=""):
    t = target_of(cat, fn, title, folder)
    return "/".join([p for p in t if p])


# ---------------------------------------------------------------
# 别名生成
# ---------------------------------------------------------------
_ILLEGAL = '/\\:*?"<>|'


def _sanitize(s):
    for ch in _ILLEGAL:
        s = s.replace(ch, "-")
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def sanitize_alias(alias):
    """清洗用户/LLM 提供的归档名：去非法字符、压连字符、去首尾点/横杠。

    归档时会以 alias 作为文件名落盘，任何含 / \\ 等的输入都可能把文件
    写出目标目录，因此入库与落盘两侧都必须过这里。
    """
    a = _sanitize(str(alias or ""))
    a = re.sub(r'-{2,}', '-', a)
    a = a.strip().strip('.-')
    return a.strip()


def _strip_date(fn):
    m = re.match(r'^(\d{4})', fn)
    if m and 1 <= int(m.group(1)[:2]) <= 12:
        return fn[m.end():]
    return fn


def _split_tokens(s):
    return [p for p in re.split(r'[\s,，;；、/\\|:：·+_\-—～~]+', s) if p]


def _clean_desc(s):
    s = re.sub(r'[（(].*?[)）]', ' ', s)
    s = re.sub(r'[\[\]【】]', ' ', s)
    keep = []
    for t in _split_tokens(s):
        if t in NOISE_WORDS:
            continue
        if re.fullmatch(r'\d{1,3}|V\d+\.?\d*|\d+%', t):
            continue
        keep.append(t)
    out = ' '.join(keep)
    out = re.sub(r'无需支撑打印[！!]?', ' ', out)
    out = re.sub(r'无需支撑|无需AMS|免支撑', ' ', out)
    out = re.sub(r'分色分件|分件分色|分色打印|分件打印|一键打印|直接打印', ' ', out)
    out = re.sub(r'[！!]+', '', out)
    out = re.sub(r'\s+', ' ', out).strip()
    return out


def make_alias(filename, title=""):
    base = filename[:-4] if filename.lower().endswith(".3mf") else filename
    if title and title != base:
        raw = title
    else:
        raw = _strip_date(base)
    d = _clean_desc(raw)
    if not d:
        d = re.sub(r'\s+', ' ', raw).strip()
    d = _sanitize(d)
    d = re.sub(r'^[\s_\-]+', '', d).strip()
    d = d[:26]
    d = re.sub(r'[\s_\-]*$', '', d)
    return d or base[:26]
