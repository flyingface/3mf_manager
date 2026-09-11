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
  1. 上传 3MF → 存入模型根目录；hash 重复自动检测提示
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
import os, sys, re, json, hashlib, shutil, sqlite3, time, mimetypes, traceback, subprocess, logging
from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DB_PATH = os.path.join(BASE, "library.db")
THUMB_DIR = os.path.join(BASE, "thumbs")
ATTACH_DIR = os.path.join(BASE, "attachments")
TRASH_DIR = os.path.join(BASE, ".trash")
LOG_DIR = os.path.join(BASE, "logs")

import llm_client            # 配置 + LLM 客户端
import parse_3mf            # 复用解析器
import db as dbm            # 存储层（schema/迁移/路径工具，显式传参）
import classify             # 分类/别名/目录规划纯逻辑层

# ---- 组合根：把 classify/db 的纯逻辑与运行时配置绑定并对外复用（兼容旧 API）----
SEP = classify.SEP
IP_NAME = classify.IP_NAME
FUNC_MAP = classify.FUNC_MAP
categorize = classify.categorize
custom_cat_keyword = classify.custom_cat_keyword
target_of = classify.target_of
target_relpath = classify.target_relpath
make_alias = classify.make_alias
sanitize_alias = classify.sanitize_alias

cfg = llm_client.load_config()
LIBRARY_ROOT = cfg["paths"].get("library_root") or os.path.join(os.path.expanduser("~"), "Downloads", "3mf_data")
INBOX = os.path.join(LIBRARY_ROOT, "00_待整理")

# ---- 日志：logs/mfmanager.log 轮转 + 控制台；替换静默 except，出错可回溯 ----
def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(os.path.join(LOG_DIR, "mfmanager.log"),
                             maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)

LOG = logging.getLogger("mfmanager")

def load_custom_categories():
    """读取用户确认过的自定义分类（settings.custom_categories，逗号分隔）。"""
    try:
        conn = db_conn()
        row = conn.execute("SELECT value FROM settings WHERE key='custom_categories'").fetchone()
        conn.close()
    except sqlite3.Error:
        return []
    return [x for x in (row["value"].split(",") if row and row["value"] else []) if x]

def validate_rel_target(raw):
    """校验用户提供的相对归档子路径：合法返回归一化路径（/ 分隔），非法返回 None。

    拒绝：父级穿越(..)、当前目录(.)、盘符/冒号（Windows 绝对路径会逃逸库根）。
    """
    raw = (raw or "").strip().strip("/\\")
    if not raw:
        return None
    parts = [p for p in re.split(r"[\\/]+", raw) if p]
    if not parts or any(p in ("..", ".") for p in parts) or any(":" in p for p in parts):
        return None
    return "/".join(parts)

# ---------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------
def _sniff_image_type(data):
    """按文件内容识别图片格式（不依赖扩展名）。返回标准扩展名或 None。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] in (b"GIF8",):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return ".heic"
    return None


def _convert_to_jpeg(data, ext):
    """用系统工具把不支持的图片（HEIC/BMP/TIFF 等）转成 JPEG（macOS sips / Linux ImageMagick）。
    返回 (jpeg_bytes, ".jpg")；无法转换返回 None。零第三方依赖。"""
    import tempfile, subprocess, shutil
    if ext not in (".heic", ".heif", ".bmp", ".tif", ".tiff"):
        return None
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in" + ext)
        dst = os.path.join(td, "out.jpg")
        with open(src, "wb") as f:
            f.write(data)
        cmds = []
        if shutil.which("sips"):
            cmds.append(["sips", "-s", "format", "jpeg", src, "--out", dst])
        if shutil.which("magick") or shutil.which("convert"):
            cmds.append([shutil.which("magick") or "convert", src, dst])
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=30)
                if r.returncode == 0 and os.path.exists(dst):
                    with open(dst, "rb") as f:
                        return f.read(), ".jpg"
            except Exception:
                LOG.debug("图片转换工具失败: %s", " ".join(cmd))
    return None


def db_conn():
    return dbm.db_conn(DB_PATH)

def init_db():
    dbm.init_db(DB_PATH, LIBRARY_ROOT, INBOX, THUMB_DIR, ATTACH_DIR)

def sha256_file(path):
    return dbm.sha256_file(path)

def rel_to_root(path):
    return dbm.rel_to_root(path, LIBRARY_ROOT)

def trash_move(src):
    return dbm.trash_move(src, TRASH_DIR)

def attachment_full_path(row):
    return dbm.attachment_full_path(row, LIBRARY_ROOT)

def file_full_path(row):
    """把文件记录解析为当前库根下的绝对路径（rel_path 优先，兼容老记录）。"""
    return dbm.file_full_path(row, LIBRARY_ROOT)

# ---------------------------------------------------------------
# LLM 增强功能
# ---------------------------------------------------------------
def llm_classify(info, existing_categories, rule_category, hint=""):
    """用 LLM 判断：现有分类是否合适；不合适则给出新分类建议。
    hint: 用户补充分类提示（可选）。非空时模型优先参考该提示分类；留空走默认逻辑。
    返回 {"category":str,"is_new":bool,"reason":str,"alias":str,"confidence":"high/medium/low"}
    """
    hint = (hint or "").strip()
    existing = ", ".join(sorted(set(existing_categories))) or "（无）"
    sys_prompt = (
        "你是 3D 打印模型分类助手。根据给定的模型信息，判断最合适的分类与归档名。\n"
        "现有分类如下，用 JSON 严格输出：\n"
        '{"category": "分类名", "is_new": true/false, "reason": "一句话理由", "alias": "简短归档名", "confidence": "high/medium/low"}\n'
        "规则：\n"
        "1. 若能从现有分类中找到合适项，返回该分类名，is_new=false。\n"
        "2. 若现有分类都不合适，返回一个简洁的新分类名（如 '手办/宠物小精灵' 或 'IP·某某'），is_new=true。\n"
        "3. 分类名尽量沿用现有体系风格。\n"
        "4. alias 是用于归档的简短可读文件名（不含扩展名，≤26字），基于标题/内容提炼，去除版本号/尺寸/打印参数等噪音词（如 V2、150%、免支撑、AMS、多色等），如标题为「哪吒之魔童降世 手办 V2 150%」则 alias 给「哪吒手办」。\n"
        "5. confidence 是你对本次分类的置信度自评：证据充分（标题/文件名明确指向）给 high，仅部分线索或依赖推断给 medium，基本靠猜测给 low。\n"
        "6. 若模型信息中的专有名词、角色、IP、作品名等你不理解，可通过网络搜索确认其类别与常见归类后再判断，不要凭猜测分类。"
    )
    user_msg = (
        f"模型信息：\n文件名：{info.get('filename','')}\n"
        f"标题：{info.get('title','')}\n作者：{info.get('designer','')}\n"
        f"设计ID：{info.get('design_id','')}\n顶点/三角面：{info.get('vertices',0)}/{info.get('triangles',0)}\n"
        f"规则分类结果：{rule_category}\n\n"
        f"现有分类：{existing}\n\n"
    )
    if hint:
        user_msg += (
            f"用户的分类提示：{hint}\n"
            "请优先结合上述用户提示进行判断；若提示已明确指定目标分类，应优先采纳该分类。\n\n"
        )
    user_msg += "请判断该模型的最佳分类。"
    raw = llm_client.chat([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ], temperature=0.2)
    data = llm_client.extract_json(raw)
    conf = str(data.get("confidence", "medium")).strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "medium"
    return {
        "category": str(data.get("category", rule_category)).strip(),
        "is_new": bool(data.get("is_new", False)),
        "reason": str(data.get("reason", "")),
        "alias": str(data.get("alias", "")).strip(),
        "confidence": conf,
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

def prefilter_files(query, limit=80, fallback=120):
    """按查询词 LIKE 预筛文件清单，避免把全库塞进 LLM 上下文。
    提取查询中长度 >=2 的词做 OR 匹配；无命中回退到最近的 fallback 条。"""
    tokens = [t for t in re.split(r'[\s,，。？！?!.;；:：、]+', (query or "")) if len(t) >= 2][:6]
    conn = db_conn()
    rows = []
    try:
        if tokens:
            conds, args = [], []
            for t in tokens:
                like = f"%{t}%"
                conds.append("(filename LIKE ? OR title LIKE ? OR alias LIKE ? OR tags LIKE ? OR category LIKE ?)")
                args += [like] * 5
            sql = "SELECT * FROM files WHERE " + " OR ".join(conds) + " ORDER BY id DESC LIMIT ?"
            rows = [dict(r) for r in conn.execute(sql, args + [limit])]
        if not rows:
            rows = [dict(r) for r in conn.execute("SELECT * FROM files ORDER BY id DESC LIMIT ?", (fallback,))]
    finally:
        conn.close()
    return rows

# ---------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------
UPLOAD_SPOOL_THRESHOLD = 64 << 20  # 请求体超过 64MB 落盘解析，避免整包驻留内存


class _Part:
    """multipart 文件 part：在底层缓冲（bytes 或 mmap）区间上的只读流。

    不把内容整体拷进内存；底层为 mmap 时由 GC 在处理结束后释放。
    """

    def __init__(self, name, filename, buf, start, end):
        self.name = name
        self.filename = filename
        self._buf = buf
        self._pos = start
        self._end = end
        self.size = end - start

    def read(self, n=-1):
        if n is None or n < 0:
            n = self._end - self._pos
        data = self._buf[self._pos:min(self._pos + n, self._end)]
        self._pos += len(data)
        return data


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

    def _read_body(self):
        """读取请求体。小于阈值返回 bytes；超过则 spool 到临时文件返回 mmap（低内存）。"""
        import mmap as _mmap
        import tempfile
        cl = self.headers.get("Content-Length")
        if cl is not None:
            n = int(cl)
            if n <= UPLOAD_SPOOL_THRESHOLD:
                return self.rfile.read(n)

            def chunks():
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            return self._spool(_mmap, tempfile, chunks())
        if "chunked" in (self.headers.get("Transfer-Encoding", "") or "").lower():
            def gen():
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    try:
                        size = int(line.strip().split(b";")[0], 16)
                    except ValueError:
                        return
                    if size == 0:
                        while True:
                            t = self.rfile.readline()
                            if t in (b"\r\n", b"\n", b""):
                                break
                        return
                    yield self.rfile.read(size)
                    self.rfile.readline()
            return self._spool(_mmap, tempfile, gen())
        return b""

    @staticmethod
    def _spool(_mmap, tempfile, chunk_iter):
        spool = tempfile.TemporaryFile()
        for chunk in chunk_iter:
            spool.write(chunk)
        spool.flush()
        spool.seek(0)
        return _mmap.mmap(spool.fileno(), 0, access=_mmap.ACCESS_READ)

    def _read_multipart(self):
        """解析 multipart，返回 ({fields}, [_Part|bytes 兼容元组文件列表])。

        兼容性：返回的 files 元素同时提供 (name, filename, data) 元组解包
        （小文件，data 为 bytes）与只读流接口 _Part（大文件走 mmap 区间）。
        """
        ct = self.headers.get("Content-Type", "")
        boundary = ct.split("boundary=", 1)[1].strip().strip('"').encode()
        buf = self._read_body()
        sep = b"--" + boundary
        fields = {}
        files = []
        # 用 find 循环切分（mmap/bytes 通吃，避免整体 split 拷贝）
        pos = buf.find(sep)
        while pos != -1:
            seg_start = pos + len(sep)
            nxt = buf.find(sep, seg_start)
            seg_end = nxt if nxt != -1 else len(buf)
            pos = nxt
            hidx = buf.find(b"\r\n\r\n", seg_start, seg_end)
            if hidx == -1:
                continue
            hdr = bytes(buf[seg_start:hidx]).decode("utf-8", "ignore")
            cstart = hidx + 4
            cend = seg_end
            # 只剥掉 boundary 前的一个尾部 \r\n（不能 rsplit，会截断内部含 \r\n 的二进制）
            if cend - 2 >= cstart and bytes(buf[cend - 2:cend]) == b"\r\n":
                cend -= 2
            # name 兼容带引号/无引号
            nm = re.search(r'name="([^"]*)"', hdr) or re.search(r'name=([^;\r\n]+)', hdr)
            if not nm:
                continue
            name = nm.group(1)
            # filename 兼容 filename="x" / filename=x / filename*=UTF-8''x （Apple 图库等会用到后两种）
            fm = (re.search(r'filename\*=(?:UTF-8\'\')?([^;\r\n]+)', hdr, re.I)
                  or re.search(r'filename="([^"]*)"', hdr, re.I)
                  or re.search(r'filename=([^;\r\n]+)', hdr, re.I))
            if fm:
                files.append(_Part(name, unquote(fm.group(1)).strip(), buf, cstart, cend))
            elif name == "file":
                # 兜底：name=file 但无 filename（部分浏览器从照片图库选图时不带 filename），按文件处理
                files.append(_Part(name, "", buf, cstart, cend))
            else:
                fields[name] = bytes(buf[cstart:cend]).decode("utf-8", "ignore").strip()
        return fields, files

    def log_message(self, *a):
        pass

    # ---- 路由 ----
    def _safe_under(self, base, rel):
        """拼接 base 与 rel 并校验 realpath 仍在 base 内，防 ../ 路径穿越。
        安全返回绝对路径；越界返回 None。"""
        fp = os.path.join(base, rel)
        real = os.path.realpath(fp)
        base_real = os.path.realpath(base)
        if real == base_real or real.startswith(base_real + os.sep):
            return real
        return None

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        """统一入口：全局异常兜底，避免未捕获异常直接断连且无迹可循。"""
        try:
            if method == "GET":
                self._do_get()
            else:
                self._do_post()
        except json.JSONDecodeError:
            self._send(400, {"error": "请求体不是合法 JSON"})
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端已断开，无需回包
        except Exception:
            LOG.exception("请求处理失败: %s %s", method, self.path)
            try:
                self._send(500, {"error": "服务器内部错误（详情见服务日志）"})
            except Exception:
                pass

    def _do_get(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/" or p == "/index.html":
            return self._serve_file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if p.startswith("/static/"):
            fp = self._safe_under(STATIC, p[len("/static/"):])
            if not fp:
                return self._send(404, {"error": "not found"})
            if fp.endswith("index.html") or fp.endswith(".html"):
                return self._serve_file(fp, cache="no-store")
            return self._serve_file(fp, cache="public, max-age=300")
        handler = GET_ROUTES.get(p)
        if handler:
            return handler(self, parse_qs(u.query))
        if p.startswith("/thumbs/"):
            fp = self._safe_under(THUMB_DIR, p[len("/thumbs/"):])
            if not fp:
                return self._send(404, {"error": "not found"})
            # 缩略图文件名含随机后缀，内容不可变，可长缓存
            return self._serve_file(fp, cache="public, max-age=31536000, immutable")
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
            return self._serve_file(full, cache="public, max-age=86400")
        self._send(404, {"error": "not found"})

    def _do_post(self):
        u = urlparse(self.path)
        p = u.path
        handler = POST_ROUTES.get(p)
        if handler:
            # multipart 端点自行读 body，不做 JSON 预读
            payload = None if p in MULTIPART_ROUTES else self._read_json()
            return handler(self, payload)
        self._send(404, {"error": "not found"})

    def _serve_file(self, fp, fallback="application/octet-stream", cache="no-store"):
        if not os.path.isfile(fp):
            self._send(404, {"error": "file not found"})
            return
        ctype = mimetypes.guess_type(fp)[0] or fallback
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    # ---- API ----
    def _api_stats(self, _=None):
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

    def _api_categories(self, _=None):
        conn = db_conn()
        uniq = {}
        for r in conn.execute("SELECT DISTINCT category FROM files WHERE category!=''"):
            top = r["category"].split(SEP)[0]
            uniq.setdefault(top, []).append(r["category"])
        conn.close()
        self._send(200, {"ip": list(IP_NAME), "func": list(FUNC_MAP),
                         "custom": load_custom_categories(), "in_use": uniq})

    def _api_get_config(self, _=None):
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
        # 若路径变了，刷新全局并按 rel_path 重定位已有索引（附件同理）
        global LIBRARY_ROOT, INBOX
        old_root = LIBRARY_ROOT
        LIBRARY_ROOT = c["paths"]["library_root"]
        INBOX = os.path.join(LIBRARY_ROOT, "00_待整理")
        os.makedirs(LIBRARY_ROOT, exist_ok=True)
        os.makedirs(INBOX, exist_ok=True)
        if os.path.abspath(LIBRARY_ROOT) != os.path.abspath(old_root):
            dbm.rebase_paths(DB_PATH, LIBRARY_ROOT)
        self._send(200, {"ok": True})

    def _api_reset_library(self, data):
        """初始化模型根目录：清空目录下全部数据并重置索引。

        安全设计：必须由前端二次确认，并传入与当前模型根目录逐字一致的路径
        做比对，否则拒绝执行（避免误清空其它目录）。
        """
        path = (data.get("path") or "").strip()
        if not path:
            self._send(400, {"error": "需要在输入框中完整填写模型根目录路径以确认"}); return
        root_abs = os.path.abspath(LIBRARY_ROOT)
        given_abs = os.path.abspath(os.path.expanduser(path))
        if given_abs != root_abs:
            self._send(400, {"error": "路径与当前模型根目录不一致，已取消操作", "expected": LIBRARY_ROOT}); return
        # 清空目录下所有内容（保留目录本身）
        removed = 0
        os.makedirs(root_abs, exist_ok=True)
        for name in os.listdir(root_abs):
            p = os.path.join(root_abs, name)
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                removed += 1
            except OSError:
                pass  # 跳过无法删除的项（如被占用的文件）
        os.makedirs(INBOX, exist_ok=True)
        # 重置索引（文件已清空，索引必须同步清空保持一致）
        conn = db_conn()
        conn.execute("DELETE FROM attachments")
        conn.execute("DELETE FROM files")
        # 重置自增 id 计数，让后续上传的 id 从 1 重新编号（完全恢复新装状态）
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('files','attachments')")
        except sqlite3.OperationalError:
            pass  # 表尚无自增记录时该表不存在，忽略
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "removed": removed, "root": LIBRARY_ROOT})

    def _api_about(self, _=None):
        """返回面向普通用户的「关于」说明（Markdown 文本）。"""
        p = os.path.join(BASE, "ABOUT.md")
        if not os.path.exists(p):
            self._send(404, {"error": "about not found"}); return
        with open(p, encoding="utf-8") as f:
            self._send(200, {"markdown": f.read()})

    def _api_search(self, q):
        kw = q.get("q", [""])[0].strip()
        cat = q.get("cat", [""])[0].strip()
        status = q.get("status", [""])[0].strip()
        tag = q.get("tag", [""])[0].strip()
        design = q.get("design_id", [""])[0].strip()
        try:
            limit = max(0, int(q.get("limit", ["0"])[0] or 0))
            offset = max(0, int(q.get("offset", ["0"])[0] or 0))
        except ValueError:
            limit, offset = 0, 0
        where, args = [], []
        if kw:
            where.append("(filename LIKE ? OR title LIKE ? OR alias LIKE ? OR tags LIKE ? OR design_id LIKE ?)")
            args += [f"%{kw}%"] * 5
        if cat:
            where.append("(category LIKE ? OR target_dir LIKE ?)"); args += [f"%{cat}%", f"%{cat}%"]
        if status:
            where.append("status=?"); args.append(status)
        if tag:
            where.append("tags LIKE ?"); args.append(f"%{tag}%")
        if design:
            where.append("design_id LIKE ?"); args.append(f"%{design}%")
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        conn = db_conn()
        # ---- 重复判定在全量过滤集上计算（分页可能把同组切到不同页）----
        dup_ids, earliest_ids = set(), set()
        idsha = conn.execute(f"SELECT id, sha256, created_at FROM files{wsql}", args).fetchall()
        sha_groups = {}
        for r in idsha:
            s = (r["sha256"] or "").strip()
            if s:
                sha_groups.setdefault(s, []).append(r)
        for grp in sha_groups.values():
            if len(grp) > 1:
                earliest = min(grp, key=lambda r: (r["created_at"] or "", r["id"]))
                for r in grp:
                    dup_ids.add(r["id"])
                earliest_ids.add(earliest["id"])
        total = len(idsha)
        # ---- 分页取整行；limit=0 表示全量（兼容旧调用）----
        sql = f"SELECT * FROM files{wsql} ORDER BY created_at DESC, id DESC"
        qargs = list(args)
        if limit:
            sql += " LIMIT ? OFFSET ?"; qargs += [limit, offset]
        rows = [dict(r) for r in conn.execute(sql, qargs)]
        # ---- 附件聚合返回（消除前端逐卡片请求的 N+1）----
        attmap = {}
        if rows:
            ids = [r["id"] for r in rows]
            ph = ",".join("?" * len(ids))
            for a in conn.execute(f"SELECT id, file_id, name, rel_path, abs_path, size_mb FROM attachments WHERE file_id IN ({ph}) ORDER BY id", ids):
                attmap.setdefault(a["file_id"], []).append(dict(a))
        conn.close()
        for r in rows:
            r["path"] = r["abs_path"]
            if r["id"] in dup_ids:
                r["is_duplicate"] = True
                r["is_earliest"] = r["id"] in earliest_ids
            r["attachments"] = attmap.get(r["id"], [])
        self._send(200, {"files": rows, "count": len(rows), "total": total})

    def _ingest(self, path, sha256_hex=None):
        """解析 + 分类 + 别名 + hash + 提取摆盘图，写入索引。返回记录。
        sha256_hex: 调用方已算好的内容哈希（如上传时做过重复检测），避免重复读盘。"""
        filename = os.path.basename(path)
        meta = parse_3mf.parse_3mf(path)
        size = os.path.getsize(path) / 1024 / 1024
        rel = rel_to_root(path)
        folder = os.path.dirname(rel) if rel else ""
        title = meta["title"]
        category = categorize(folder, filename, title, custom_cats=load_custom_categories())
        alias = make_alias(filename, title)
        target = target_relpath(category, filename, title, folder)
        h = sha256_hex or sha256_file(path)
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
            LOG.warning("提取摆盘图失败 %s: %s", path, traceback.format_exc(limit=1))
        plate_imgs = ",".join(plate_files)
        conn = db_conn()
        conn.execute("""
            INSERT OR REPLACE INTO files
            (abs_path, filename, folder, size_mb, title, designer, license, creation_date,
             design_id, profile_title, objects, vertices, triangles, plates, has_slice,
             geom_sig, sha256, category, alias, target_dir, status, tags, thumb, plate_imgs, rel_path, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (path, filename, folder, round(size, 2), meta["title"], meta["designer"],
              meta["license"], meta["creation_date"], meta["design_id"], meta["profile_title"],
              meta["objects"], meta["vertices"], meta["triangles"], meta["plates"],
              1 if meta["has_slice"] else 0, meta["geom_sig"], h, category, alias, target,
              "pending", "", thumb_name, plate_imgs, rel, time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        rec = dict(conn.execute("SELECT * FROM files WHERE abs_path=?", (path,)).fetchone())
        conn.close()
        return rec

    def _api_upload(self, _=None):
        fields, files = self._read_multipart()
        if not files:
            self._send(400, {"error": "缺少文件"})
            return
        results = []
        for part in files:
            fname = part.filename
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
                shutil.copyfileobj(part, f)  # part 为只读流，大文件经 mmap 区间直写磁盘
            # hash 重复检测
            h = sha256_file(dest)
            conn = db_conn()
            dup = conn.execute("SELECT id, filename, abs_path FROM files WHERE sha256=? AND abs_path!=?", (h, dest)).fetchone()
            conn.close()
            dup_info = None
            if dup:
                dup_info = {"id": dup["id"], "filename": dup["filename"], "path": dup["abs_path"]}
            # hash 在重复检测时已算好，直接传给 _ingest，避免大文件二次读盘
            rec = self._ingest(dest, sha256_hex=h)
            results.append({"name": fname, "ok": True, "file": rec, "is_duplicate": bool(dup_info), "duplicate_of": dup_info})
        self._send(200, {"results": results})

    def _is_earliest_dup(self, conn, row):
        """SHA256 重复且本行不是最早副本则返回 False（不可归档）。"""
        s = (row["sha256"] or "").strip()
        if not s:
            return True
        grp = conn.execute("SELECT id, created_at FROM files WHERE sha256=?", (s,)).fetchall()
        if len(grp) <= 1:
            return True
        earliest = min(grp, key=lambda r: (r["created_at"] or "", r["id"]))
        return earliest["id"] == row["id"]

    def _apply_row(self, conn, row, do_rename=True):
        """对单行执行归档（移动 + 改名 + 更新索引），返回结果 dict。供 /api/apply 与 apply-plan 复用。"""
        fid = row["id"]
        if not row or row["status"] == "applied":
            return {"id": fid, "ok": False, "error": "不存在或已执行"}
        # 重复文件防护：SHA256 重复且非最早副本，不允许归档
        if not self._is_earliest_dup(conn, row):
            return {"id": fid, "ok": False, "error": "重复文件（非最早副本）不可归档", "skipped_duplicate": True}
        old = file_full_path(row)
        if not os.path.exists(old):
            return {"id": fid, "ok": False, "error": "源文件不存在"}
        tdir = os.path.join(LIBRARY_ROOT, row["target_dir"]) if row["target_dir"] else LIBRARY_ROOT
        os.makedirs(tdir, exist_ok=True)
        new_name = row["filename"]
        if do_rename and row["alias"]:
            clean = sanitize_alias(row["alias"])
            if clean:  # 清洗后为空则保留原文件名，杜绝 ../ 等注入落盘
                new_name = clean + ".3mf"
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
            conn.execute("UPDATE files SET abs_path=?, rel_path=?, filename=?, folder=?, status='applied', applied_at=? WHERE id=?",
                         (new_path, new_rel, os.path.basename(new_path),
                          os.path.dirname(new_rel) if new_rel else "",
                          time.strftime("%Y-%m-%d %H:%M:%S"), fid))
            return {"id": fid, "ok": True, "new_path": new_path, "new_name": os.path.basename(new_path)}
        except Exception as e:
            return {"id": fid, "ok": False, "error": str(e)}

    def _api_apply(self, data):
        ids = data.get("ids", [])
        do_rename = data.get("rename", True)
        if not ids:
            self._send(400, {"error": "no ids"}); return
        conn = db_conn()
        results = []
        for fid in ids:
            row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            results.append(self._apply_row(conn, row, do_rename))
        conn.commit(); conn.close()
        self._send(200, {"results": results})

    def _api_recategorize(self, data):
        """手动重选分类。data: {id, category, alias?}"""
        fid = data.get("id"); cat = data.get("category"); alias = (data.get("alias") or "").strip()
        if not fid or not cat:
            self._send(400, {"error": "need id+category"}); return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        if row["status"] != "pending":
            conn.close(); self._send(400, {"error": "已归档文件不可重新分类"}); return
        target = target_relpath(cat, row["filename"], row["title"], row["folder"])
        alias = sanitize_alias(alias) or make_alias(row["filename"], row["title"])
        conn.execute("UPDATE files SET category=?, target_dir=?, alias=?, status='pending' WHERE id=?", (cat, target, alias, fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "category": cat, "target_dir": target, "alias": alias})

    def _api_return_pending(self, data):
        """退回整理：把已归档文件退回「待整理」。索引 status 翻回 pending，
        并把主 .3mf 及其关联附件物理移回 00_待整理（INBOX）。data: {id}"""
        fid = data.get("id")
        if not fid:
            self._send(400, {"error": "need id"}); return
        inbox = os.path.join(LIBRARY_ROOT, "00_待整理")
        os.makedirs(inbox, exist_ok=True)
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        if row["status"] == "pending":
            conn.close(); self._send(400, {"error": "已经是待整理状态"}); return
        # 1) 主文件移回 INBOX（冲突则加 _2/_3 后缀）
        old_main = file_full_path(row) or ""
        moved_main = None
        if old_main and os.path.exists(old_main):
            base, ext = os.path.splitext(os.path.basename(old_main))
            dest = os.path.join(inbox, os.path.basename(old_main))
            i = 2
            while os.path.exists(dest):
                dest = os.path.join(inbox, f"{base}_{i}{ext}"); i += 1
            try:
                shutil.move(old_main, dest)
                moved_main = dest
            except Exception as e:
                conn.close(); self._send(500, {"error": f"移动主文件失败：{e}"}); return
        new_path = moved_main or old_main
        new_name = os.path.basename(new_path)
        # 2) 关联附件一并移回 INBOX，更新其 abs_path/rel_path
        moved_att = []
        for a in conn.execute("SELECT * FROM attachments WHERE file_id=?", (fid,)).fetchall():
            ap = a["abs_path"] or ""
            if not ap or not os.path.exists(ap):
                continue
            ab, ae = os.path.splitext(os.path.basename(ap))
            adest = os.path.join(inbox, os.path.basename(ap))
            j = 2
            while os.path.exists(adest):
                adest = os.path.join(inbox, f"{ab}_{j}{ae}"); j += 1
            try:
                shutil.move(ap, adest)
                rel = os.path.relpath(adest, LIBRARY_ROOT)
                conn.execute("UPDATE attachments SET abs_path=?, rel_path=? WHERE id=?", (adest, rel, a["id"]))
                moved_att.append(os.path.basename(adest))
            except Exception:
                LOG.warning("附件移回待整理失败 id=%s: %s", a["id"], traceback.format_exc(limit=1))
        # 3) 更新索引：物理位置回到 INBOX，状态 pending（保留 category/target_dir/alias 便于再次归档）
        new_rel = rel_to_root(new_path)
        conn.execute("UPDATE files SET abs_path=?, rel_path=?, filename=?, folder=?, status='pending' WHERE id=?",
                     (new_path, new_rel, new_name, "00_待整理", fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "id": fid, "new_path": new_path, "moved_attachments": moved_att})

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

    def _api_open_in_bambu(self, data):
        """用 Bambu Studio 打开该文件对应的 3MF。data: {id}"""
        fid = data.get("id")
        if not fid:
            self._send(400, {"error": "need id"}); return
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        conn.close()
        if not row:
            self._send(404, {"error": "not found"}); return
        path = file_full_path(row)
        if not path or not os.path.exists(path):
            self._send(404, {"error": "文件不存在于磁盘"}); return
        try:
            if sys.platform == "darwin":
                # 优先用 BambuStudio 打开，失败则回退系统默认
                r = subprocess.run(["open", "-a", "BambuStudio", path], capture_output=True, timeout=20)
                if r.returncode != 0:
                    r2 = subprocess.run(["open", path], capture_output=True, timeout=20)
                    if r2.returncode != 0:
                        self._send(500, {"error": "Bambu Studio 打开失败，请确认已安装 BambuStudio.app"}); return
                self._send(200, {"ok": True}); return
            elif sys.platform == "win32":
                os.startfile(path)  # noqa: F821
                self._send(200, {"ok": True}); return
            else:
                subprocess.run(["xdg-open", path], check=True)
                self._send(200, {"ok": True}); return
        except Exception as e:
            self._send(500, {"error": str(e)})

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
        alias = sanitize_alias(alias)
        conn.execute("UPDATE files SET alias=? WHERE id=?", (alias, fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "alias": alias})

    def _api_set_target(self, data):
        """手动设置归档路径（可含子目录）。data: {id, target_dir}

        仅待整理状态可改；路径必须是相对路径（可含子目录），禁止绝对路径、
        父级穿越(..)、空串（允许传空以回退到分类默认路径）。
        """
        fid = data.get("id")
        if not fid:
            self._send(400, {"error": "need id"}); return
        raw_in = (data.get("target_dir") or "").strip()
        # 显式拒绝绝对路径（以 / 或 \\ 开头）
        if raw_in.startswith("/") or raw_in.startswith("\\"):
            self._send(400, {"error": "归档路径不合法（仅支持相对子目录路径）"}); return
        raw = raw_in.strip("/\\")
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "not found"}); return
        if row["status"] != "pending":
            conn.close(); self._send(400, {"error": "已归档文件不可修改归档路径"}); return
        if not raw:
            # 空路径：回退到分类默认路径
            target = target_relpath(row["category"], row["filename"], row["title"], row["folder"]) if row["category"] else ""
        else:
            # 安全校验：按段检查——禁父级穿越(..)、当前目录(.)、盘符/冒号（Windows 盘符绝对路径会逃逸库根）
            target = validate_rel_target(raw)
            if target is None:
                conn.close(); self._send(400, {"error": "归档路径不合法（仅支持相对子目录路径）"}); return
        conn.execute("UPDATE files SET target_dir=? WHERE id=?", (target, fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "target_dir": target})

    def _api_llm_batch_classify(self, data):
        """批量 AI 分类（SSE）。data: {ids, include_rule_matched?, hint?}

        逐个文件调用 LLM 并以 text/event-stream 推送进度：
          data: {"type":"skip","id":..,"done":n,"total":N,"rule_category":..}   规则已命中跳过
          data: {"type":"progress","id":..,"done":n,"total":N,"filename":.., result...}  单个完成
          data: {"type":"progress","id":..,"done":n,"total":N,"filename":..,"error":..} 单个失败
          data: {"type":"done","total":N,"skipped":k}
        客户端断开（取消/暂停）即终止循环，已完成部分由客户端保留。
        """
        ids = data.get("ids") or []
        include_rule = bool(data.get("include_rule_matched", False))
        hint = (data.get("hint") or "").strip()
        if not ids:
            self._send(400, {"error": "no ids"}); return
        if not llm_client.llm_configured():
            self._send(400, {"error": "LLM 未配置"}); return
        conn = db_conn()
        rows = []
        for fid in ids:
            r = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            if r and r["status"] == "pending":
                rows.append(dict(r))
        existing = [r["category"] for r in conn.execute("SELECT DISTINCT category FROM files WHERE category!=''")]
        conn.close()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")  # close 定界，SSE 客户端读到 EOF
        self.end_headers()

        def emit(obj):
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        total = len(rows)
        done = skipped = 0
        try:
            for row in rows:
                rule_cat = categorize(row["folder"], row["filename"], row["title"], custom_cats=load_custom_categories())
                if rule_cat != "其他/未分类" and not include_rule:
                    skipped += 1
                    done += 1
                    emit({"type": "skip", "id": row["id"], "filename": row["filename"],
                          "done": done, "total": total, "rule_category": rule_cat})
                    continue
                try:
                    res = llm_classify(row, existing, rule_cat, hint)
                    res["id"] = row["id"]
                    res["filename"] = row["filename"]
                    res["thumb"] = row["thumb"] or ""
                    res.setdefault("confidence", "medium")
                    done += 1
                    emit({"type": "progress", "done": done, "total": total, **res})
                except Exception as e:
                    done += 1
                    LOG.warning("批量 AI 分类单文件失败 id=%s: %s", row["id"], e)
                    emit({"type": "progress", "id": row["id"], "filename": row["filename"],
                          "done": done, "total": total, "error": str(e)})
            emit({"type": "done", "total": total, "skipped": skipped})
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端取消/暂停
        except Exception as e:
            LOG.exception("批量 AI 分类异常")
            try:
                emit({"type": "error", "error": str(e)})
            except Exception:
                pass

    def _api_apply_plan(self, data):
        """应用 AI 整理计划。data: {items:[{id, category, alias, target_dir?}], new_categories?:[...]}

        流程：新分类写入 custom_categories → 逐行写 category/alias/target（仅 pending，
        路径经安全校验，非法路径该行失败不中断）→ 复用 _apply_row 归档 → 汇总结果。
        """
        items = data.get("items") or []
        new_categories = [str(c).strip() for c in (data.get("new_categories") or []) if str(c).strip()]
        if not items:
            self._send(400, {"error": "no items"}); return
        conn = db_conn()
        # 1) 新分类入库（供后续规则分类与手动下拉使用）
        if new_categories:
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('custom_categories',?)", ("",))
            row = conn.execute("SELECT value FROM settings WHERE key='custom_categories'").fetchone()
            cur = row["value"] if row else ""
            lst = [x for x in cur.split(",") if x] if cur else []
            for c in new_categories:
                if c not in lst:
                    lst.append(c)
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('custom_categories',?)", (",".join(lst),))
        conn.commit()
        # 2) 逐行落分类/别名/路径并归档
        ok_list, failed = [], []
        for it in items:
            fid = it.get("id")
            cat = (it.get("category") or "").strip()
            alias = sanitize_alias(it.get("alias") or "")
            raw_target = (it.get("target_dir") or "").strip()
            row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            if not row:
                failed.append({"id": fid, "error": "not found"}); continue
            if row["status"] != "pending":
                failed.append({"id": fid, "error": "已归档文件不可重新分类"}); continue
            if not cat:
                failed.append({"id": fid, "error": "category required"}); continue
            target = target_relpath(cat, row["filename"], row["title"], row["folder"])
            if raw_target:
                v = validate_rel_target(raw_target)
                if v is None:
                    failed.append({"id": fid, "error": "归档路径不合法"}); continue
                target = v
            alias_final = alias or make_alias(row["filename"], row["title"])
            conn.execute("UPDATE files SET category=?, target_dir=?, alias=? WHERE id=?",
                         (cat, target, alias_final, fid))
            conn.commit()  # 先落编辑再归档，失败可追溯
            row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            r = self._apply_row(conn, row, do_rename=True)
            (ok_list if r.get("ok") else failed).append(r if r.get("ok") else {"id": fid, "error": r.get("error")})
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "applied": ok_list, "failed": failed})

    def _api_dirs(self, _=None):
        """返回模型根目录下所有已存在的目录（相对路径，按层级排序），供归档路径选择器使用。

        排除 00_待整理 与隐藏项；每个目录也返回它在磁盘上是否真实存在。
        """
        dirs = []
        root_abs = LIBRARY_ROOT
        # 预置一个空串代表根目录本身
        if os.path.isdir(root_abs):
            for base, subdirs, _files in os.walk(root_abs):
                # 跳过 00_待整理 与隐藏目录
                subdirs[:] = [d for d in subdirs if not d.startswith(".") and d != "00_待整理"]
                for d in subdirs:
                    full = os.path.join(base, d)
                    rel = os.path.relpath(full, root_abs)
                    if rel.startswith("."):
                        continue
                    dirs.append({"path": rel.replace(os.sep, "/"), "exists": True})
        dirs.sort(key=lambda x: (x["path"].count("/"), x["path"]))
        self._send(200, {"dirs": dirs, "root": LIBRARY_ROOT})

    def _api_set_tags(self, data):
        fid = data.get("id"); tags = data.get("tags", [])
        if isinstance(tags, str):
            tags = [t for t in tags.split(",") if t.strip()]
        # 去重（保持顺序），避免重复标签
        seen = set()
        dedup = []
        for t in tags:
            t = t.strip()
            if t and t not in seen:
                seen.add(t); dedup.append(t)
        tags = dedup
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
        moved_main = trash_move(file_full_path(row))
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

    def _api_thumbnail(self, _=None):
        fields, files = self._read_multipart()
        if not files:
            self._send(400, {"error": "need image"}); return
        fid = int(fields.get("id", 0))
        part = files[0]
        data = part.read()  # 图片体积小，读全即可
        # 按文件内容识别格式（不依赖扩展名），HEIC/BMP/TIFF 等自动转 JPEG
        ext = _sniff_image_type(data)
        if ext is None:
            self._send(400, {"error": "无法识别的图片格式，请使用 PNG/JPG/WebP/GIF（或 HEIC 等常见照片格式）"}); return
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            conv = _convert_to_jpeg(data, ext)
            if conv is None:
                self._send(400, {"error": "图片格式不支持（HEIC/BMP 等已尝试自动转换失败，请先转成 JPG/PNG 再上传）"}); return
            data, ext = conv
        conn = db_conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "file not found"}); return
        # 替换时删除旧缩略图文件，避免 thumbs/ 孤儿文件累积
        if row["thumb"]:
            oldp = os.path.join(THUMB_DIR, row["thumb"])
            if os.path.exists(oldp):
                try:
                    os.remove(oldp)
                except OSError:
                    pass
        # 随机后缀避免同一秒内替换时重名覆盖
        tname = f"{fid}_{int(time.time())}_{os.urandom(3).hex()}{ext}"
        tpath = os.path.join(THUMB_DIR, tname)
        with open(tpath, "wb") as f:
            f.write(data)
        conn.execute("UPDATE files SET thumb=? WHERE id=?", (tname, fid))
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "thumb": tname})

    def _api_delete_thumb(self, data):
        """删除缩略图：清空 files.thumb 并删除 thumbs/ 中的图片文件。"""
        fid = data.get("id")
        conn = db_conn()
        row = conn.execute("SELECT thumb FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            conn.close(); self._send(404, {"error": "file not found"}); return
        tname = row["thumb"]
        if tname:
            tp = os.path.join(THUMB_DIR, tname)
            if os.path.exists(tp):
                try:
                    os.remove(tp)
                except Exception:
                    pass
            conn.execute("UPDATE files SET thumb='' WHERE id=?", (fid,))
            conn.commit()
        conn.close()
        self._send(200, {"ok": True})

    # ---- 附件 ----
    def _api_attach(self, _=None):
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
        for part in files:
            safe = re.sub(r'[\\/:*?"<>|]', '_', part.filename)
            dest = os.path.join(tdir, f"{int(time.time())}_{safe}")
            with open(dest, "wb") as f:
                shutil.copyfileobj(part, f)
            rel = os.path.relpath(dest, LIBRARY_ROOT)
            conn.execute("INSERT INTO attachments (file_id, name, abs_path, rel_path, size_mb, created_at) VALUES (?,?,?,?,?,?)",
                         (fid, safe, dest, rel, round(part.size/1024/1024, 2), time.strftime("%Y-%m-%d %H:%M:%S")))
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
        hint = str(data.get("hint") or "").strip()
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
            res = llm_classify(info, existing, info["category"], hint)
            res["id"] = fid
            self._send(200, res)
        except Exception as e:
            self._send(500, {"error": f"LLM 分类失败：{e}"})

    def _api_confirm_new_category(self, data):
        """用户确认 LLM 建议的新分类，落地到规则库并应用到文件。"""
        fid = data.get("id")
        category = data.get("category")
        alias = (data.get("alias") or "").strip()
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
        alias = sanitize_alias(alias) or make_alias(row["filename"], row["title"])
        conn.execute("UPDATE files SET category=?, target_dir=?, alias=?, status='pending' WHERE id=?", (category, target, alias, fid))
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
        rows = prefilter_files(query)
        res = llm_semantic_search(query, rows)
        self._send(200, res)

    def _api_chat(self, data):
        sid = data.get("session_id", "default")
        msg = data.get("message", "")
        if not llm_client.llm_configured():
            self._send(400, {"error": "LLM 未配置"}); return
        # 只注入与消息相关的文件清单（预筛），不再全库 300 条
        rows = prefilter_files(msg)
        summary = "\n".join(
            f"{r['id']}. {r['filename']} | {r['title']} | 分类:{r['category']} | tags:{r['tags']} | 状态:{r['status']}"
            for r in rows)
        llm_client.session_add(sid, "user", msg)
        try:
            reply = llm_chat_reply(llm_client.session_get(sid), summary)
            llm_client.session_add(sid, "assistant", reply)
            self._send(200, {"reply": reply})
        except Exception as e:
            llm_client.session_clear(sid)
            self._send(500, {"error": f"对话失败：{e}"})

# ---------------------------------------------------------------
# 路由表：路径 -> Handler 方法（统一签名 (self, payload)）
# GET 的 payload 为 parse_qs 结果；POST 的 payload 为 JSON body
# ---------------------------------------------------------------
GET_ROUTES = {
    "/api/stats": Handler._api_stats,
    "/api/files": Handler._api_search,
    "/api/about": Handler._api_about,
    "/api/categories": Handler._api_categories,
    "/api/config": Handler._api_get_config,
    "/api/attachments": Handler._api_attachments,
    "/api/dirs": Handler._api_dirs,
}

POST_ROUTES = {
    "/api/upload": Handler._api_upload,
    "/api/apply": Handler._api_apply,
    "/api/tags": Handler._api_set_tags,
    "/api/delete": Handler._api_delete,
    "/api/thumbnail": Handler._api_thumbnail,
    "/api/delete-thumb": Handler._api_delete_thumb,
    "/api/llm-classify": Handler._api_llm_classify,
    "/api/llm-batch-classify": Handler._api_llm_batch_classify,
    "/api/apply-plan": Handler._api_apply_plan,
    "/api/confirm-new-category": Handler._api_confirm_new_category,
    "/api/chat": Handler._api_chat,
    "/api/search-llm": Handler._api_search_llm,
    "/api/config": Handler._api_set_config,
    "/api/attach": Handler._api_attach,
    "/api/attachment-delete": Handler._api_attachment_delete,
    "/api/recategorize": Handler._api_recategorize,
    "/api/return-pending": Handler._api_return_pending,
    "/api/reset-library": Handler._api_reset_library,
    "/api/open-folder": Handler._api_open_folder,
    "/api/open-in-bambu": Handler._api_open_in_bambu,
    "/api/set-alias": Handler._api_set_alias,
    "/api/set-target": Handler._api_set_target,
}

# 以 multipart/form-data 收请求体的端点（body 是二进制，不能走 JSON 预读）
MULTIPART_ROUTES = {"/api/upload", "/api/thumbnail", "/api/attach"}


try:
    from version import __version__
except ImportError:  # 直接以脚本运行且模块缺失时的兜底
    __version__ = "dev"

# ---------------------------------------------------------------
def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    _setup_logging()
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 60)
    print(f"  3MF Manager v{__version__} — 3D 打印文件管理器")
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
