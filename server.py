#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""
3MF Manager — 本地 3D 打印文件管理器（后端 · 升级版）
零第三方依赖：Python 内置 http.server + sqlite3 + urllib（LLM 用 OpenAI 兼容接口）。

功能：
  1. 上传 3MF → 存入收藏目录；hash 重复自动检测提示
  2. 解析文件提取元数据
  3. 分类：规则分类 + LLM 模型辅助分类；现有分类不满足可【新增分类建议】，用户确认后执行
  4. 确认归档：移动 + 重命名，更新索引
  5. Tag 增删改；分类手动调整（重选分类）
  6. 附件关联：上传非 3MF 文件作为附件挂到 3MF 下
  7. 语义搜索 + 多轮对话检索（LLM 智能体）
  8. Dashboard 资产清单
  9. 缩略图上传
  10. 设置界面：模型能力(地址/key/model) + 本地路径

启动: python server.py [端口]
"""
import os, sys, re, json, hashlib, shutil, sqlite3, time, mimetypes, traceback, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DB_PATH = os.path.join(BASE, "library.db")
THUMB_DIR = os.path.join(BASE, "thumbs")
ATTACH_DIR = os.path.join(BASE, "attachments")
TRASH_DIR = os.path.join(BASE, ".trash")

import llm_client            # 配置 + LLM 客户端
import parse_3mf            # 复用解析器
import subcat               # 复用分类/子分类规则
import mc_subcat

cfg = llm_client.load_config()
LIBRARY_ROOT = cfg["paths"].get("library_root") or os.path.join(os.path.expanduser("~"), "Downloads", "3D模型库")
INBOX = os.path.join(LIBRARY_ROOT, "00_待整理")

# ---------------------------------------------------------------
# 分类器（规则优先；若需 LLM 增强在 _ingest 中调用）
# ---------------------------------------------------------------
SEP = subcat.SEP

def categorize(folder, filename, title, sib_text=""):
    s = (folder + " " + filename + " " + title + " " + sib_text)
    sl = s.lower()
    ftl = (filename + " " + title).lower()
    priority_ip = [("dummy13", "IP·Dummy13"), ("虚拟13", "IP·Dummy13"),
                   ("虚拟模型13", "IP·Dummy13"), ("虚拟人偶13", "IP·Dummy13"),
                   ("虚拟人物13", "IP·Dummy13"), ("13号假人", "IP·Dummy13"),
                   ("13号dummy", "IP·Dummy13"), ("d13tb", "IP·Dummy13"),
                   ("lucky13", "IP·Dummy13"), ("dummy docker", "IP·Dummy13")]
    for kw, label in priority_ip:
        if kw in ftl:
            return subcat.refine(label, folder, filename, title)
    folder_ip = {
        "minecraft": "IP·Minecraft", "哪吒": "IP·哪吒", "dummy13": "IP·Dummy13",
        "gundam": "IP·高达", "pokemon": "IP·宝可梦", "疯狂动物城": "IP·疯狂动物城",
        "harrypotter": "IP·哈利波特", "mario": "IP·马里奥", "labubu": "IP·Labubu",
        "驯龙高手": "IP·驯龙高手", "变形金刚": "IP·变形金刚", "claude": "IP·Claude",
        "奥特曼": "IP·奥特曼",
    }
    for k, v in folder_ip.items():
        if folder.lower() == k or folder.lower().startswith(k + "/") or folder.lower().startswith(k):
            sub = folder[len(k):].strip("/")
            return subcat.refine(v + (SEP + sub if sub else ""), folder, filename, title)
    ip_kw = [
        ("minecraft", "IP·Minecraft"), ("我的世界", "IP·Minecraft"), ("creeper", "IP·Minecraft"),
        ("史蒂夫", "IP·Minecraft"), ("steve", "IP·Minecraft"), ("苦力怕", "IP·Minecraft"),
        ("dummy13", "IP·Dummy13"), ("dummy", "IP·Dummy13"),
        ("哪吒", "IP·哪吒"), ("nezha", "IP·哪吒"),
        ("gundam", "IP·高达"), ("高达", "IP·高达"), ("元祖", "IP·高达"),
        ("pokemon", "IP·宝可梦"), ("宝可梦", "IP·宝可梦"), ("皮卡丘", "IP·宝可梦"),
        ("zootopia", "IP·疯狂动物城"), ("动物城", "IP·疯狂动物城"), ("疯狂动物城", "IP·疯狂动物城"),
        ("harry potter", "IP·哈利波特"), ("哈利", "IP·哈利波特"),
        ("mario", "IP·马里奥"), ("马里奥", "IP·马里奥"),
        ("labubu", "IP·Labubu"),
        ("驯龙高手", "IP·驯龙高手"),
        ("变形金刚", "IP·变形金刚"), ("transformers", "IP·变形金刚"),
        ("claude", "IP·Claude"),
        ("奥特曼", "IP·奥特曼"), ("ultraman", "IP·奥特曼"),
        ("三角洲", "IP·三角洲"), ("delta force", "IP·三角洲"), ("deltaforce", "IP·三角洲"),
    ]
    for kw, label in ip_kw:
        if kw in sl:
            return subcat.refine(label, folder, filename, title)
    extra_ip = [
        ("七龙珠", "IP·七龙珠"), ("龙珠", "IP·七龙珠"), ("dbz", "IP·七龙珠"), ("dragon ball", "IP·七龙珠"),
        ("阿拉蕾", "IP·七龙珠"), ("arale", "IP·七龙珠"),
        ("鬼灭", "IP·鬼灭之刃"), ("鬼灭之刃", "IP·鬼灭之刃"), ("kimetsu", "IP·鬼灭之刃"),
        ("汪汪队", "IP·汪汪队"), ("paw patrol", "IP·汪汪队"),
        ("侏罗纪", "IP·侏罗纪"), ("jurassic", "IP·侏罗纪"),
        ("三丽鸥", "IP·三丽鸥"), ("sanrio", "IP·三丽鸥"), ("美乐蒂", "IP·三丽鸥"),
        ("库洛米", "IP·三丽鸥"), ("玉桂狗", "IP·三丽鸥"),
        ("蛋仔", "IP·蛋仔"), ("蛋仔派对", "IP·蛋仔"),
        ("哆啦", "IP·哆啦A梦"), ("doraemon", "IP·哆啦A梦"),
        ("格里扎", "IP·奥特曼"), ("mr.satan", "IP·七龙珠"), ("撒旦", "IP·七龙珠"),
        ("完美沙鲁", "IP·七龙珠"), ("沙鲁", "IP·七龙珠"), ("完美细胞", "IP·七龙珠"),
        ("柯南", "IP·柯南"), ("conan", "IP·柯南"),
        ("魔女宅急便", "IP·吉卜力"), ("吉卜力", "IP·吉卜力"), ("宫崎骏", "IP·吉卜力"), ("龙猫", "IP·吉卜力"), ("千与千寻", "IP·吉卜力"),
        ("异形", "IP·异形"), ("alien", "IP·异形"),
        ("小智", "IP·宝可梦"), ("精灵宝可梦", "IP·宝可梦"),
        ("小黑", "IP·小黑"),
        ("星球大战", "IP·星球大战"), ("星战", "IP·星球大战"), ("达斯·维达", "IP·星球大战"), ("vader", "IP·星球大战"), ("黑武士", "IP·星球大战"), ("绝地", "IP·星球大战"),
        ("塞尔达", "IP·塞尔达"), ("zelda", "IP·塞尔达"),
        ("黑神话", "IP·黑神话悟空"), ("wukong", "IP·黑神话悟空"), ("悟空", "IP·黑神话悟空"),
    ]
    for kw, label in extra_ip:
        if kw in sl:
            return subcat.refine(label, folder, filename, title)
    content_rules = [
        (["恐龙", "dino"], "手办/恐龙"),
        (["马年", "小白马", "小马", "酷酷马", "哭哭马", "苦苦马", "黑马", "白马", "粉马", "紫马", "黄马", "骏马", "赤马", "马上有钱", "马上开心", "马上系列", "策马", "战马", "关节战马", "八骏", "horsy", "horse", "马年吉祥物", "马年专属", "马年启岁", "马年福", "马年LOGO", "马年摇摇", "马年-双色", "马年冲冲冲"], "手办/马年"),
        (subcat.UNCAT_BY_LABEL["武器/刀剑模型"], "武器/刀剑模型"),
        (subcat.UNCAT_BY_LABEL["载具/车船模型"], "载具/车船模型"),
        (subcat.UNCAT_BY_LABEL["机器人/机甲模型"], "机器人/机甲模型"),
        (subcat.UNCAT_BY_LABEL["解压/指尖玩具"], "解压/指尖玩具"),
        (subcat.UNCAT_BY_LABEL["积木/人偶"], "积木/人偶"),
        (subcat.UNCAT_BY_LABEL["展示架/收纳墙"], "展示架/收纳墙"),
        (subcat.UNCAT_BY_LABEL["动物/生物模型"], "动物/生物模型"),
        (subcat.UNCAT_BY_LABEL["摆件/装饰雕像"], "摆件/装饰雕像"),
        (subcat.UNCAT_BY_LABEL["文具/工具配件"], "文具/工具配件"),
    ]
    for kws, label in content_rules:
        if any(k in sl for k in kws):
            return subcat.refine(label, folder, filename, title)
    ffunc = {
        "gridfinity": "收纳/网格系统", "文具": "文具/办公", "灯具": "灯具/灯饰",
        "容器": "收纳/盒体/容器", "keychain": "钥匙扣/挂件", "toys": "手办/角色/玩具",
    }
    fl = folder.lower()
    for k, v in ffunc.items():
        if fl == k or fl.startswith(k + "/") or fl.startswith(k):
            return subcat.refine(v, folder, filename, title)
    rules = [
        (["笔", "中性笔", "pen", "ballpoint"], "3D打印笔"),
        (["干燥", "药盒", "密封", "收纳", "盒", "box", "kfc", "垃圾桶", "pill", "capsule", "桶", "罐", "网格", "grid", "gridfinity"], "收纳/盒体/容器"),
        (["磁", "magnet", "磁吸"], "磁吸/磁性配件"),
        (["rail", "轨道", "拼接", "b-rail", "bracket"], "拼接/轨道系统"),
        (["圣诞", "解压", "玩具", "fidget", "点击器", "摆件", "手办", "无脸男", "乐高", "石矶", "龙珠", "战车", "龙舟", "子弹", "机娘", "马"], "手办/角色/玩具"),
        (["钥匙", "keychain", "挂件", "挂饰"], "钥匙扣/挂件"),
        (["灯具", "灯", "lamp", "led"], "灯具/灯饰"),
        (["文具", "笔筒", "名片", "回形针"], "文具/办公"),
        (["配件", "支架", "底座", "lid", "盖", "顶", "替换", "联动", "柱体", "锁", "扣", "件", "夹", "夹子", "固定板"], "实用配件/机械件"),
    ]
    for kws, label in rules:
        if any(k in sl for k in kws):
            return subcat.refine(label, folder, filename, title)
    for kws, label in subcat.UNCAT_RULES:
        if any(k in sl for k in kws):
            return label
    for frag, label in subcat.FILENAME_OVERRIDE:
        if frag in filename.lower():
            return subcat.refine(label, folder, filename, title)
    return "其他/未分类"

# ---------------------------------------------------------------
# 目录规划
# ---------------------------------------------------------------
IP_NAME = {
    "Minecraft": "Minecraft", "Dummy13": "Dummy13", "高达": "高达Gundam",
    "疯狂动物城": "疯狂动物城", "Labubu": "Labubu", "宝可梦": "宝可梦Pokemon",
    "哪吒": "哪吒", "马里奥": "马里奥Mario", "哈利波特": "哈利波特",
    "驯龙高手": "驯龙高手", "变形金刚": "变形金刚", "Claude": "Claude",
    "奥特曼": "奥特曼", "石矶": "石矶", "七龙珠": "七龙珠",
    "三角洲": "三角洲DeltaForce", "柯南": "柯南", "吉卜力": "吉卜力StudioGhibli",
    "异形": "异形Alien", "小黑": "小黑", "星球大战": "星球大战StarWars",
    "塞尔达": "塞尔达Zelda", "黑神话悟空": "黑神话悟空",
}
FUNC_MAP = {
    "手办/角色/玩具": ("02_功能实用", "手办角色玩具"), "手办/恐龙": ("02_功能实用", "恐龙"), "手办/马年": ("02_功能实用", "马年"),
    "实用配件/机械件": ("02_功能实用", "实用配件机械件"),
    "钥匙扣/挂件": ("02_功能实用", "钥匙扣挂件"), "3D打印笔": ("02_功能实用", "3D打印笔"),
    "文具/办公": ("02_功能实用", "文具办公"), "灯具/灯饰": ("02_功能实用", "灯具灯饰"),
    "磁吸/磁性配件": ("02_功能实用", "磁吸磁性配件"), "拼接/轨道系统": ("02_功能实用", "拼接轨道系统"),
    "收纳/盒体/容器": ("03_收纳", "盒体容器"), "收纳/网格系统": ("03_收纳", "网格系统Gridfinity"),
    "武器/刀剑模型": ("02_功能实用", "武器刀剑模型"), "载具/车船模型": ("02_功能实用", "载具车船模型"),
    "机器人/机甲模型": ("02_功能实用", "机器人机甲模型"), "解压/指尖玩具": ("02_功能实用", "解压指尖玩具"),
    "积木/人偶": ("02_功能实用", "积木人偶"), "展示架/收纳墙": ("02_功能实用", "展示架收纳墙"),
    "动物/生物模型": ("02_功能实用", "动物生物模型"), "摆件/装饰雕像": ("02_功能实用", "摆件装饰雕像"),
    "文具/工具配件": ("02_功能实用", "文具工具配件"),
    "其他/未分类": ("04_其他未分类", None),
}

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
    return ("04_其他未分类", None, None)

def target_relpath(cat, fn="", title="", folder=""):
    t = target_of(cat, fn, title, folder)
    return "/".join([p for p in t if p])

# ---------------------------------------------------------------
# 别名生成
# ---------------------------------------------------------------
NOISE_WORDS = {
    "无需", "无支撑", "无五金", "免胶水", "免螺丝", "免安装", "一体打印", "分件", "分色", "拆件", "拼装",
    "多色", "双色", "单色", "彩色", "渐变色", "拼色", "分盘", "一盘", "分体", "分层",
    "AMS", "ams", "A1", "A1mini", "P1", "X1", "X1C", "P1P", "P1S",
    "版本", "V1", "V2", "V3", "V4", "v1", "v2", "v3", "v4", "1.0", "2.0", "3.0", "4.0",
    "100%", "200%", "150%", "80%", "50%", "70%", "尺寸", "厘米", "毫米", "打印", "切片",
    "配置文件", "打印配置", "套件", "合集", "全套", "全彩", "定制", "DIY", "diy", "可定制", "参数化",
    "无需AMS", "免支撑", "免", "适配", "通用", "兼容", "适用于", "支持", "自带",
    "无需螺丝", "无需支撑", "快速打印", "一键打印", "无需胶水", "无需上色", "无需五金",
    "即可打印", "打印即用", "即打印", "直接打印", "免安装",
}
_ILLEGAL = '/\\:*?"<>|'

def _sanitize(s):
    for ch in _ILLEGAL:
        s = s.replace(ch, "-")
    s = re.sub(r'\s+', ' ', s).strip()
    return s

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

# ---------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    os.makedirs(LIBRARY_ROOT, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    os.makedirs(ATTACH_DIR, exist_ok=True)
    conn = db_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        abs_path TEXT UNIQUE,
        filename TEXT,
        folder TEXT,
        size_mb REAL,
        title TEXT, designer TEXT, license TEXT, creation_date TEXT,
        design_id TEXT, profile_title TEXT,
        objects INTEGER, vertices INTEGER, triangles INTEGER, plates INTEGER,
        has_slice INTEGER, geom_sig TEXT, sha256 TEXT,
        category TEXT, alias TEXT, target_dir TEXT,
        status TEXT DEFAULT 'pending',
        tags TEXT DEFAULT '', thumb TEXT DEFAULT '',
        plate_imgs TEXT DEFAULT '',
        created_at TEXT, applied_at TEXT
    );
    CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER,
        name TEXT, abs_path TEXT, size_mb REAL,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_files_cat ON files(category);
    CREATE INDEX IF NOT EXISTS idx_files_design ON files(design_id);
    CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
    CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
    """)
    # 迁移：老库补 plate_imgs 列
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(files)")]
    if "plate_imgs" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN plate_imgs TEXT DEFAULT ''")
    # 迁移：老库补 attachments.rel_path 列（相对库根，便于数据迁移）
    acols = [r["name"] for r in conn.execute("PRAGMA table_info(attachments)")]
    if "rel_path" not in acols:
        conn.execute("ALTER TABLE attachments ADD COLUMN rel_path TEXT DEFAULT ''")
    conn.commit()
    conn.close()

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def rel_to_root(path):
    try:
        return os.path.relpath(path, LIBRARY_ROOT)
    except Exception:
        return os.path.basename(path)

def trash_move(src):
    """把 src 移入回收站目录，避免重名覆盖；返回目标路径，失败返回 None。"""
    if not src or not os.path.exists(src):
        return None
    os.makedirs(TRASH_DIR, exist_ok=True)
    base = os.path.basename(src)
    dest = os.path.join(TRASH_DIR, base)
    if os.path.abspath(dest) == os.path.abspath(src):
        return dest
    i = 2
    b, e = os.path.splitext(base)
    while os.path.exists(dest):
        dest = os.path.join(TRASH_DIR, f"{b}_{i}{e}")
        i += 1
    try:
        shutil.move(src, dest)
    except Exception:
        return None
    return dest

def attachment_full_path(row):
    """把附件记录解析为当前库根下的绝对路径（相对路径优先，兼容老库绝对 abs_path）。"""
    rel = row.get("rel_path") if hasattr(row, "get") else row["rel_path"]
    if rel:
        p = os.path.join(LIBRARY_ROOT, rel)
        if os.path.exists(p):
            return p
    abs_p = row.get("abs_path") if hasattr(row, "get") else row["abs_path"]
    if abs_p and os.path.exists(abs_p):
        return abs_p
    # 都不存在时仍返回相对解析结果，便于上层 404 统一处理
    return os.path.join(LIBRARY_ROOT, rel) if rel else (abs_p or "")

# ---------------------------------------------------------------
# LLM 增强功能
# ---------------------------------------------------------------
def llm_classify(info, existing_categories, rule_category):
    """用 LLM 判断：现有分类是否合适；不合适则给出新分类建议。
    返回 {"ok":bool,"category":str,"reason":str,"is_new":bool}
    """
    existing = ", ".join(sorted(set(existing_categories))) or "（无）"
    sys_prompt = (
        "你是 3D 打印模型分类助手。根据给定的模型信息，判断最合适的分类。\n"
        "现有分类如下，用 JSON 严格输出：\n"
        '{"category": "分类名", "is_new": true/false, "reason": "一句话理由"}\n'
        "规则：\n"
        "1. 若能从现有分类中找到合适项，返回该分类名，is_new=false。\n"
        "2. 若现有分类都不合适，返回一个简洁的新分类名（如 '手办/宠物小精灵' 或 'IP·某某'），is_new=true。\n"
        "3. 分类名尽量沿用现有体系风格。"
    )
    user_msg = (
        f"模型信息：\n文件名：{info.get('filename','')}\n"
        f"标题：{info.get('title','')}\n作者：{info.get('designer','')}\n"
        f"设计ID：{info.get('design_id','')}\n顶点/三角面：{info.get('vertices',0)}/{info.get('triangles',0)}\n"
        f"规则分类结果：{rule_category}\n\n"
        f"现有分类：{existing}\n\n"
        "请判断该模型的最佳分类。"
    )
    raw = llm_client.chat([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ], temperature=0.2)
    data = llm_client.extract_json(raw)
    return {
        "category": str(data.get("category", rule_category)).strip(),
        "is_new": bool(data.get("is_new", False)),
        "reason": str(data.get("reason", "")),
    }

def llm_semantic_search(query, files, top_k=8):
    """LLM 语义检索：给模型文件清单，让其选出与 query 最相关的文件。"""
    if not files:
        return []
    # 精简描述列表
    lines = []
    for f in files[:200]:   # 防超长，取前 200
        lines.append(f"{f['id']}. {f['filename']} | {f['title']} | 分类:{f['category']} | tags:{f['tags']}")
    list_str = "\n".join(lines)
    sys_prompt = (
        "你是 3MF 模型库的语义检索智能体。根据用户查询，从提供的文件清单中选出最相关的文件。\n"
        '用 JSON 输出：{"ids": [相关文件的id数字，按相关度降序]，"answer": "给用户的简短自然语言说明"}'
    )
    user_msg = f"用户查询：{query}\n\n文件清单（id. 文件名 | 标题 | 分类 | tags）：\n{list_str}"
    try:
        raw = llm_client.chat([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ], temperature=0.2)
        data = llm_client.extract_json(raw)
        ids = data.get("ids", [])[:top_k]
        # 按 id 映射回文件
        byid = {f["id"]: f for f in files}
        result = [byid[i] for i in ids if i in byid]
        return {"files": result, "answer": data.get("answer", "")}
    except Exception as e:
        return {"files": [], "answer": f"LLM 检索失败：{e}"}

def llm_chat_reply(history, files_summary):
    """多轮对话回复（结合模型库上下文）。"""
    sys_prompt = (
        "你是 3MF 模型库的智能助手。你可以根据模型库文件信息回答用户关于 3D 打印模型的问题。\n"
        "以下是当前模型库的简要清单（id. 文件名 | 标题 | 分类 | tags）。回答用户问题，若涉及具体文件请引用其文件名。\n"
        "若问题超出文件信息范围，可结合通用知识回答。"
    )
    user_context = {"role": "user", "content": "（模型库清单）\n" + files_summary}
    messages = [{"role": "system", "content": sys_prompt}]
    # 插入上下文（不打断多轮）
    messages.append(user_context)
    messages.extend(history)
    return llm_client.chat(messages, temperature=0.4, max_tokens=800)

# ---------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}

    def _read_multipart(self):
        """解析 multipart，返回 {fields, files:[(name,filename,data)]}"""
        ct = self.headers.get("Content-Type", "")
        boundary = ct.split("boundary=", 1)[1].strip().strip('"').encode()
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        parts = body.split(b"--" + boundary)
        fields = {}
        files = []
        for part in parts:
            if b"\r\n\r\n" not in part:
                continue
            head, _, content = part.partition(b"\r\n\r\n")
            hdr = head.decode("utf-8", "ignore")
            content = content.rsplit(b"\r\n", 1)[0]
            nm = re.search(r'name="([^"]*)"', hdr)
            if not nm:
                continue
            fname = re.search(r'filename="([^"]*)"', hdr)
            if fname:
                files.append((nm.group(1), unquote(fname.group(1)), content))
            else:
                fields[nm.group(1)] = content.decode("utf-8", "ignore").strip()
        return fields, files

    def log_message(self, *a):
        pass

    # ---- 路由 ----
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/" or p == "/index.html":
            return self._serve_file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if p.startswith("/static/"):
            return self._serve_file(os.path.join(STATIC, p[len("/static/"):]))
        if p == "/api/stats":
            return self._api_stats()
        if p == "/api/files":
            return self._api_search(parse_qs(u.query))
        if p == "/api/categories":
            return self._api_categories()
        if p == "/api/config":
            return self._api_get_config()
        if p == "/api/attachments":
            return self._api_attachments(parse_qs(u.query))
        if p.startswith("/thumbs/"):
            return self._serve_file(os.path.join(THUMB_DIR, p[len("/thumbs/"):]))
        if p.startswith("/attachments/"):
            aid = p[len("/attachments/"):]
            conn = db_conn()
            r = conn.execute("SELECT abs_path, rel_path FROM attachments WHERE id=?", (aid,)).fetchone()
            conn.close()
            if not r:
                self._send(404, {"error": "not found"}); return
            full = attachment_full_path(r)
            if not full or not os.path.exists(full):
                self._send(404, {"error": "not found"}); return
            return self._serve_file(full)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/upload":
            return self._api_upload()
        if p == "/api/apply":
            return self._api_apply(self._read_json())
        if p == "/api/tags":
            return self._api_set_tags(self._read_json())
        if p == "/api/delete":
            return self._api_delete(self._read_json())
        if p == "/api/thumbnail":
            return self._api_thumbnail()
        if p == "/api/llm-classify":
            return self._api_llm_classify(self._read_json())
        if p == "/api/confirm-new-category":
            return self._api_confirm_new_category(self._read_json())
        if p == "/api/chat":
            return self._api_chat(self._read_json())
        if p == "/api/search-llm":
            return self._api_search_llm(self._read_json())
        if p == "/api/config":
            return self._api_set_config(self._read_json())
        if p == "/api/attach":
            return self._api_attach()
        if p == "/api/attachment-delete":
            return self._api_attachment_delete(self._read_json())
        if p == "/api/recategorize":
            return self._api_recategorize(self._read_json())
        if p == "/api/return-pending":
            return self._api_return_pending(self._read_json())
        if p == "/api/open-folder":
            return self._api_open_folder(self._read_json())
        if p == "/api/set-alias":
            return self._api_set_alias(self._read_json())
        self._send(404, {"error": "not found"})

    def _serve_file(self, fp, fallback="application/octet-stream"):
        if not os.path.isfile(fp):
            self._send(404, {"error": "file not found"})
            return
        ctype = mimetypes.guess_type(fp)[0] or fallback
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- API ----
    def _api_stats(self):
        conn = db_conn()
        total = conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM files WHERE status='pending'").fetchone()["c"]
        applied = conn.execute("SELECT COUNT(*) c FROM files WHERE status='applied'").fetchone()["c"]
        cats = {r["category"]: r["n"] for r in conn.execute(
            "SELECT category, COUNT(*) n FROM files GROUP BY category ORDER BY n DESC")}
        size = conn.execute("SELECT COALESCE(SUM(size_mb),0) s FROM files").fetchone()["s"]
        att = conn.execute("SELECT COUNT(*) c FROM attachments").fetchone()["c"]
        dups = self._dup_groups(conn)
        conn.close()
        self._send(200, {"total": total, "pending": pending, "applied": applied,
                         "categories": cats, "total_mb": round(size, 1),
                         "attachments": att, "dup_groups": len(dups)})

    def _dup_groups(self, conn):
        rows = conn.execute("SELECT sha256, COUNT(*) c, GROUP_CONCAT(filename,' | ') fs FROM files WHERE sha256!='' GROUP BY sha256 HAVING c>1").fetchall()
        return [dict(r) for r in rows]

    def _api_categories(self):
        conn = db_conn()
        uniq = {}
        for r in conn.execute("SELECT DISTINCT category FROM files WHERE category!=''"):
            top = r["category"].split(SEP)[0]
            uniq.setdefault(top, []).append(r["category"])
        conn.close()
        self._send(200, {"ip": list(IP_NAME), "func": list(FUNC_MAP), "in_use": uniq})

    def _api_get_config(self):
        c = llm_client.load_config()
        # 不返回 api_key 明文，只返回是否已配置
        self._send(200, {
            "llm": {"base_url": c["llm"]["base_url"], "model": c["llm"]["model"],
                    "has_key": bool(c["llm"]["api_key"]),
                    "configured": llm_client.llm_configured()},
            "paths": c["paths"],
            "library_root": LIBRARY_ROOT,
        })

    def _api_set_config(self, data):
        c = llm_client.load_config()
        if "llm" in data:
            if "base_url" in data["llm"]:
                c["llm"]["base_url"] = str(data["llm"]["base_url"]).strip()
            if "model" in data["llm"]:
                c["llm"]["model"] = str(data["llm"]["model"]).strip()
            if "api_key" in data["llm"] and data["llm"].get("api_key"):
                c["llm"]["api_key"] = str(data["llm"]["api_key"]).strip()
        if "paths" in data and data["paths"].get("library_root"):
            c["paths"]["library_root"] = str(data["paths"]["library_root"]).strip()
        llm_client.save_config(c)
        # 若路径变了，刷新全局
        global LIBRARY_ROOT, INBOX
        LIBRARY_ROOT = c["paths"]["library_root"]
        INBOX = os.path.join(LIBRARY_ROOT, "00_待整理")
        os.makedirs(LIBRARY_ROOT, exist_ok=True)
        os.makedirs(INBOX, exist_ok=True)
        self._send(200, {"ok": True})

    def _api_search(self, q):
        kw = q.get("q", [""])[0].strip()
        cat = q.get("cat", [""])[0].strip()
        status = q.get("status", [""])[0].strip()
        tag = q.get("tag", [""])[0].strip()
        design = q.get("design_id", [""])[0].strip()
        conn = db_conn()
        sql = "SELECT * FROM files WHERE 1=1"
        args = []
        if kw:
            sql += " AND (filename LIKE ? OR title LIKE ? OR alias LIKE ? OR tags LIKE ? OR design_id LIKE ?)"
            like = f"%{kw}%"
            args += [like, like, like, like, like]
        if cat:
            sql += " AND (category LIKE ? OR target_dir LIKE ?)"; args += [f"%{cat}%", f"%{cat}%"]
        if status:
            sql += " AND status=?"; args.append(status)
        if tag:
            sql += " AND tags LIKE ?"; args.append(f"%{tag}%")
        if design:
            sql += " AND design_id LIKE ?"; args.append(f"%{design}%")
        sql += " ORDER BY created_at DESC, id DESC"
        rows = [dict(r) for r in conn.execute(sql, args)]
        # ---- 重复判定：相同 sha256 视为重复组；组内「最早一份」(created_at ASC,id ASC)
        #      为原始可归档副本(is_earliest)，其余后进副本不可归档，显示红色「重复」状态 ----
        sha_groups = {}
        for r in rows:
            s = (r.get("sha256") or "").strip()
            if s:
                sha_groups.setdefault(s, []).append(r)
        for s, grp in sha_groups.items():
            if len(grp) > 1:
                earliest = min(grp, key=lambda r: (r.get("created_at") or "", r.get("id") or 0))
                for r in grp:
                    r["is_duplicate"] = True
                    r["is_earliest"] = (r is earliest)
        conn.close()
        for r in rows:
            r["path"] = r["abs_path"]
        self._send(200, {"files": rows, "count": len(rows)})

    def _ingest(self, path):
        """解析 + 分类 + 别名 + hash + 提取摆盘图，写入索引。返回记录。"""
        filename = os.path.basename(path)
        meta = parse_3mf.parse_3mf(path)
        size = os.path.getsize(path) / 1024 / 1024
        rel = rel_to_root(path)
        folder = os.path.dirname(rel) if rel else ""
        title = meta["title"]
        category = categorize(folder, filename, title)
        alias = make_alias(filename, title)
        target = target_relpath(category, filename, title, folder)
        h = sha256_file(path)
        # ---- 提取内嵌摆盘图并落盘 ----
        thumb_name = ""
        plate_files = []
        try:
            pv = parse_3mf.extract_previews(path)
            if pv["model"]:
                thumb_name = f"auto_{int(time.time())}_{hashlib.md5(path.encode()).hexdigest()[:8]}.png"
                with open(os.path.join(THUMB_DIR, thumb_name), "wb") as f:
                    f.write(pv["model"]["data"])
            for p in pv["plates"]:
                pf = f"p{int(time.time())}_{p['index']}_{hashlib.md5(path.encode()).hexdigest()[:8]}.png"
                with open(os.path.join(THUMB_DIR, pf), "wb") as f:
                    f.write(p["data"])
                plate_files.append(pf)
        except Exception:
            pass
        plate_imgs = ",".join(plate_files)
        conn = db_conn()
        conn.execute("""
            INSERT OR REPLACE INTO files
            (abs_path, filename, folder, size_mb, title, designer, license, creation_date,
             design_id, profile_title, objects, vertices, triangles, plates, has_slice,
             geom_sig, sha256, category, alias, target_dir, status, tags, thumb, plate_imgs, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (path, filename, folder, round(size, 2), meta["title"], meta["designer"],
              meta["license"], meta["creation_date"], meta["design_id"], meta["profile_title"],
              meta["objects"], meta["vertices"], meta["triangles"], meta["plates"],
              1 if meta["has_slice"] else 0, meta["geom_sig"], h, category, alias, target,
              "pending", "", thumb_name, plate_imgs, time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        rec = dict(conn.execute("SELECT * FROM files WHERE abs_path=?", (path,)).fetchone())
        conn.close()
        return rec

    def _api_upload(self):
        fields, files = self._read_multipart()
        if not files:
            self._send(400, {"error": "缺少文件"})
            return
        results = []
        for _, fname, data in files:
            if not fname.lower().endswith(".3mf"):
                results.append({"name": fname, "ok": False, "error": "仅支持 .3mf"})
                continue
            safe = re.sub(r'[\\/:*?"<>|]', '_', fname)
            dest = os.path.join(INBOX, safe)
            base, ext = os.path.splitext(safe)
            i = 2
            while os.path.exists(dest):
                dest = os.path.join(INBOX, f"{base}_{i}{ext}")
                i += 1
            with open(dest, "wb") as f:
                f.write(data)
            # hash 重复检测
            h = sha256_file(dest)
            conn = db_conn()
            dup = conn.execute("SELECT id, filename, abs_path FROM files WHERE sha256=? AND abs_path!=?", (h, dest)).fetchone()
            conn.close()
            dup_info = None
            if dup:
                dup_info = {"id": dup["id"], "filename": dup["filename"], "path": dup["abs_path"]}
            rec = self._ingest(dest)
            results.append({"name": fname, "ok": True, "file": rec, "is_duplicate": bool(dup_info), "duplicate_of": dup_info})
        self._send(200, {"results": results})

    def _api_apply(self, data):
        ids = data.get("ids", [])
        do_rename = data.get("rename", True)
        if not ids:
            self._send(400, {"error": "no ids"}); return
        conn = db_conn()
        results = []
        for fid in ids:
            row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            if not row or row["status"] == "applied":
                results.append({"id": fid, "ok": False, "error": "不存在或已执行"}); continue
            old = row["abs_path"]
            if not os.path.exists(old):
                results.append({"id": fid, "ok": False, "error": "源文件不存在"}); continue
            tdir = os.path.join(LIBRARY_ROOT, row["target_dir"]) if row["target_dir"] else LIBRARY_ROOT
            os.makedirs(tdir, exist_ok=True)
            new_name = row["filename"]
            if do_rename and row["alias"]:
                new_name = row["alias"] + ".3mf"
            new_path = os.path.join(tdir, new_name)
            if os.path.abspath(new_path) != os.path.abspath(old):
                b, e = os.path.splitext(new_name)
                i = 2
                while os.path.exists(new_path):
                    new_path = os.path.join(tdir, f"{b}_{i}{e}")
                    i += 1
            try:
                if os.path.abspath(new_path) != os.path.abspath(old):
                    shutil.move(old, new_path)
                new_rel = rel_to_root(new_path)
                conn.execute("UPDATE files SET abs_path=?, filename=?, folder=?, status='applied', applied_at=? WHERE id=?",
                             (new_path, os.path.basename(new_path),
                              os.path.dirname(new_rel) if new_rel else "",
                              time.strftime("%Y-%m-%d %H:%M:%S"), fid))
                results.append({"id": fid, "ok": True, "new_path": new_path, "new_name": os.path.basename(new_path)})
            except Exception as e:
                results.append({"id": fid, "ok": False, "error": str(e)})
        conn.commit(); conn.close()
        self._send(200, {"results": results})

    def _api_recategorize(self, data):
        """手动重选分类。data: {id, category}"""
        fid = data.get("id"); cat = data.get("category")
        if not fid or not cat:
            self._send(400, {"error": "need id+category"}); return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        if row["status"] != "pending":
            conn.close(); self._send(400, {"error": "已归档文件不可重新分类"}); return
        target = target_relpath(cat, row["filename"], row["title"], row["folder"])
        conn.execute("UPDATE files SET category=?, target_dir=?, status='pending' WHERE id=?", (cat, target, fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "category": cat, "target_dir": target})

    def _api_return_pending(self, data):
        """退回整理：把索引状态从 applied 推回 pending（仅在数据库中翻转状态，磁盘文件位置不变）。data: {id}"""
        fid = data.get("id")
        if not fid:
            self._send(400, {"error": "need id"}); return
        conn = db_conn()
        row = conn.execute("SELECT id, status FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        if row["status"] == "pending":
            conn.close(); self._send(400, {"error": "已经是待整理状态"}); return
        conn.execute("UPDATE files SET status='pending' WHERE id=?", (fid,))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "id": fid})

    def _api_open_folder(self, data):
        """在系统文件管理器中打开该文件的归档目录。data: {id}"""
        fid = data.get("id")
        if not fid:
            self._send(400, {"error": "need id"}); return
        conn = db_conn()
        row = conn.execute("SELECT target_dir FROM files WHERE id=?", (fid,)).fetchone()
        conn.close()
        if not row:
            self._send(404, {"error": "not found"}); return
        tdir = (row["target_dir"] or "").strip()
        abs_dir = os.path.join(LIBRARY_ROOT, tdir) if tdir else LIBRARY_ROOT
        os.makedirs(abs_dir, exist_ok=True)
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", abs_dir], check=True)
            elif sys.platform == "win32":
                os.startfile(abs_dir)  # noqa: F821 (仅 Windows)
            else:
                subprocess.run(["xdg-open", abs_dir], check=True)
            self._send(200, {"ok": True, "path": abs_dir})
        except Exception as e:
            self._send(500, {"error": f"无法打开目录: {e}"})

    def _api_set_alias(self, data):
        """手动编辑归档名。data: {id, alias}"""
        fid = data.get("id"); alias = data.get("alias")
        if not fid:
            self._send(400, {"error": "need id"}); return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        if row["status"] != "pending":
            conn.close(); self._send(400, {"error": "已归档文件不可修改归档名"}); return
        alias = (alias or "").strip()
        conn.execute("UPDATE files SET alias=? WHERE id=?", (alias, fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "alias": alias})

    def _api_set_tags(self, data):
        fid = data.get("id"); tags = data.get("tags", [])
        if isinstance(tags, str):
            tags = [t for t in tags.split(",") if t.strip()]
        tags = [t.strip() for t in tags if t.strip()]
        conn = db_conn()
        conn.execute("UPDATE files SET tags=? WHERE id=?", (",".join(tags), fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "tags": tags})

    def _api_delete(self, data):
        fid = data.get("id")
        if not fid:
            self._send(400, {"error": "need id"}); return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        # 1) 主文件移入回收站（可恢复）
        moved_main = trash_move(row["abs_path"])
        # 2) 缩略图（可再生成）直接删除
        removed_thumbs = []
        for tname in ([row["thumb"]] if row["thumb"] else []) + \
                       [p for p in (row["plate_imgs"] or "").split(",") if p]:
            if tname:
                tp = os.path.join(THUMB_DIR, tname)
                if os.path.exists(tp):
                    try:
                        os.remove(tp); removed_thumbs.append(tname)
                    except Exception:
                        pass
        # 3) 关联附件：移入回收站 + 删 DB 行
        moved_att = []
        for a in conn.execute("SELECT * FROM attachments WHERE file_id=?", (fid,)).fetchall():
            ap = a["abs_path"] or ""
            if ap:
                d = trash_move(ap)
                if d:
                    moved_att.append(os.path.basename(d))
            conn.execute("DELETE FROM attachments WHERE id=?", (a["id"],))
        # 4) 删除文件索引记录
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "removed_index": fid,
                         "moved_main": os.path.basename(moved_main) if moved_main else None,
                         "removed_thumbs": removed_thumbs,
                         "moved_attachments": moved_att})

    def _api_thumbnail(self):
        fields, files = self._read_multipart()
        if not files:
            self._send(400, {"error": "need image"}); return
        fid = int(fields.get("id", 0))
        _, fname, data = files[0]
        ext = os.path.splitext(fname)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            self._send(400, {"error": "图片格式不支持"}); return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "file not found"}); return
        tname = f"{fid}_{int(time.time())}{ext}"
        tpath = os.path.join(THUMB_DIR, tname)
        with open(tpath, "wb") as f:
            f.write(data)
        conn.execute("UPDATE files SET thumb=? WHERE id=?", (tname, fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "thumb": tname})

    # ---- 附件 ----
    def _api_attach(self):
        fields, files = self._read_multipart()
        fid = int(fields.get("id", 0))
        if not files:
            self._send(400, {"error": "need file"}); return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "file not found"}); return
        # 上传到该模型在模型库的归档目录下（按 target_dir 落盘），再建立关联
        tdir = os.path.join(LIBRARY_ROOT, row["target_dir"]) if row["target_dir"] else LIBRARY_ROOT
        os.makedirs(tdir, exist_ok=True)
        saved = []
        for _, fname, data in files:
            safe = re.sub(r'[\\/:*?"<>|]', '_', fname)
            dest = os.path.join(tdir, f"{int(time.time())}_{safe}")
            with open(dest, "wb") as f:
                f.write(data)
            rel = os.path.relpath(dest, LIBRARY_ROOT)
            conn.execute("INSERT INTO attachments (file_id, name, abs_path, rel_path, size_mb, created_at) VALUES (?,?,?,?,?,?)",
                         (fid, safe, dest, rel, round(len(data)/1024/1024, 2), time.strftime("%Y-%m-%d %H:%M:%S")))
            saved.append(safe)
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "saved": saved})

    def _api_attachment_delete(self, data):
        aid = data.get("id")
        conn = db_conn()
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        full = attachment_full_path(row)
        if full and os.path.exists(full):
            os.remove(full)
        conn.execute("DELETE FROM attachments WHERE id=?", (aid,))
        conn.commit(); conn.close()
        self._send(200, {"ok": True})

    def _api_attachments(self, q):
        fid = q.get("file_id", [""])[0]
        conn = db_conn()
        if fid:
            rows = [dict(r) for r in conn.execute("SELECT * FROM attachments WHERE file_id=? ORDER BY id", (fid,))]
        else:
            rows = [dict(r) for r in conn.execute("SELECT * FROM attachments ORDER BY id DESC LIMIT 200")]
        conn.close()
        self._send(200, {"attachments": rows})

    # ---- LLM ----
    def _api_llm_classify(self, data):
        fid = data.get("id")
        if not llm_client.llm_configured():
            self._send(400, {"error": "LLM 未配置"})
            return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        conn.close()
        if not row:
            self._send(404, {"error": "not found"}); return
        info = dict(row)
        # 现有分类集合
        conn = db_conn()
        existing = [r["category"] for r in conn.execute("SELECT DISTINCT category FROM files WHERE category!=''")]
        conn.close()
        try:
            res = llm_classify(info, existing, info["category"])
            res["id"] = fid
            self._send(200, res)
        except Exception as e:
            self._send(500, {"error": f"LLM 分类失败：{e}"})

    def _api_confirm_new_category(self, data):
        """用户确认 LLM 建议的新分类，落地到规则库并应用到文件。"""
        fid = data.get("id")
        category = data.get("category")
        if not fid or not category:
            self._send(400, {"error": "need id+category"}); return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        if row["status"] != "pending":
            conn.close(); self._send(400, {"error": "已归档文件不可重新分类"}); return
        # 应用分类
        target = target_relpath(category, row["filename"], row["title"], row["folder"])
        conn.execute("UPDATE files SET category=?, target_dir=?, status='pending' WHERE id=?", (category, target, fid))
        conn.commit(); conn.close()
        # 记录新分类到自定义分类（持久化 settings）
        conn = db_conn()
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('custom_categories',?)", ("",))
        row2 = conn.execute("SELECT value FROM settings WHERE key='custom_categories'").fetchone()
        cur = row2["value"] if row2 else ""
        lst = [x for x in cur.split(",") if x] if cur else []
        if category not in lst:
            lst.append(category)
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('custom_categories',?)", (",".join(lst),))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "category": category, "target_dir": target,
                         "message": f"新分类「{category}」已创建并应用"})

    def _api_search_llm(self, data):
        query = data.get("query", "")
        if not llm_client.llm_configured():
            self._send(400, {"error": "LLM 未配置"}); return
        conn = db_conn()
        rows = [dict(r) for r in conn.execute("SELECT * FROM files ORDER BY id")]
        conn.close()
        res = llm_semantic_search(query, rows)
        self._send(200, res)

    def _api_chat(self, data):
        sid = data.get("session_id", "default")
        msg = data.get("message", "")
        if not llm_client.llm_configured():
            self._send(400, {"error": "LLM 未配置"}); return
        conn = db_conn()
        rows = [dict(r) for r in conn.execute("SELECT * FROM files ORDER BY id DESC LIMIT 300")]
        conn.close()
        summary = "\n".join(
            f"{r['id']}. {r['filename']} | {r['title']} | 分类:{r['category']} | tags:{r['tags']} | 状态:{r['status']}"
            for r in rows)
        history = llm_client.session_get(sid)
        llm_client.session_add(sid, "user", msg)
        try:
            reply = llm_chat_reply(llm_client.session_get(sid), summary)
            llm_client.session_add(sid, "assistant", reply)
            self._send(200, {"reply": reply})
        except Exception as e:
            llm_client.session_clear(sid)
            self._send(500, {"error": f"对话失败：{e}"})

# ---------------------------------------------------------------
def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 60)
    print("  3MF Manager v2 — 3D 打印文件管理器")
    print("  " + "-" * 54)
    print(f"  页面   : http://127.0.0.1:{port}")
    print(f"  收藏目录: {LIBRARY_ROOT}")
    print(f"  待整理箱: {INBOX}")
    print(f"  LLM    : {'已配置 ' + llm_client.load_config()['llm']['model'] if llm_client.llm_configured() else '未配置（设置中填写）'}")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")

if __name__ == "__main__":
    main()
