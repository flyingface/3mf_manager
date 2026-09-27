#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""备份与恢复：库数据全量打包到指定目录（可为 NAS 挂载路径）/ 从备份包还原。

备份范围（「所有数据」）：
  - library.db     SQLite backup API 一致性快照（服务运行中、WAL 模式下也可安全复制）
  - config.json    LLM 与路径配置
  - 模型根目录      library_root 下全部用户文件（含 00_待整理）
  - thumbs/ attachments/ .trash/   缩略图、附件、回收站
产物：目标目录下单个 zip（3mf_manager_backup_YYYYMMDD_HHMMSS.zip），先写
`.part` 临时文件再原子改名，避免 NAS 上留下半截包。

恢复语义：备份包整体替换当前数据；替换前自动把当前状态快照到
`<data_dir>/backups/pre_restore_*.zip`（best-effort），恢复有误可用其回退。
所有路径由调用方（server.py 组合根）显式传入，便于测试隔离。

后台任务：备份/恢复可能耗时数分钟（大库 + NAS），通过 start_job 放到独立
线程执行并以上报进度；同一时间全局只允许一个备份/恢复任务。
"""
import itertools
import json
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from datetime import datetime

import db as dbm

try:
    from version import __version__
except ImportError:  # 直接以脚本运行且模块缺失时的兜底
    __version__ = "dev"

BACKUP_KIND = "3mf_manager_backup"
ZIP_PREFIX = "3mf_manager_backup_"
# 允许出现在备份包顶层的条目（其余一律拒绝，防 zip-slip / 垃圾条目）
TOP_FILES = ("meta.json", "library.db", "config.json")
TOP_DIRS = ("library_root/", "thumbs/", "attachments/", ".trash/")


class BackupError(Exception):
    """用户可读的备份/恢复失败原因。"""


class BusyError(Exception):
    """已有备份/恢复任务在进行中。"""


# ---------------------------------------------------------------
# 进度上报
# ---------------------------------------------------------------
class _Progress:
    """线程安全进度：阶段 + 已完成/总数（文件数），供状态接口轮询。

    用 RLock：update() 持锁期间会调 snapshot() 组装返回值，普通 Lock 会自死锁。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.phase = "prepare"
        self.message = ""
        self.done = 0
        self.total = 0

    def update(self, phase=None, message=None, done=None, total=None):
        with self._lock:
            if phase is not None:
                self.phase = phase
            if message is not None:
                self.message = message
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {"phase": self.phase, "message": self.message,
                    "done": self.done, "total": self.total}


def snapshot_db(db_path, dest_path):
    """用 SQLite backup API 生成一致性快照（运行中/WAL 也可安全复制）。"""
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _iter_dir_files(top_dir, top_name, exclude_abs=()):
    """遍历目录产出 (绝对路径, zip内弧名)；跳过排除路径与 .DS_Store。"""
    top_dir = os.path.abspath(top_dir)
    for base, dirs, files in os.walk(top_dir):
        dirs[:] = [d for d in dirs
                   if os.path.join(os.path.abspath(base), d) not in exclude_abs]
        for fn in files:
            if fn == ".DS_Store":
                continue
            p = os.path.join(base, fn)
            if os.path.abspath(p) in exclude_abs:
                continue
            yield p, top_name + "/" + os.path.relpath(p, top_dir).replace(os.sep, "/")


def _backup_meta(db_snapshot_path, library_root, n_user_files):
    """从快照库读取真实 user_version 与记录数，写进 meta.json。"""
    counts = {}
    user_version = dbm.SCHEMA_VERSION
    try:
        conn = sqlite3.connect(db_snapshot_path)
        try:
            user_version = conn.execute("PRAGMA user_version").fetchone()[0] or user_version
            for t in ("files", "attachments", "asset_groups"):
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return {
        "kind": BACKUP_KIND,
        "app_version": __version__,
        "schema_version": user_version,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "library_root": library_root,
        "user_files": n_user_files,
        "counts": counts,
    }


def create_backup(*, db_path, config_path, library_root, thumb_dir,
                  attach_dir, trash_dir, dest_dir, data_dir=None,
                  extra_exclude=(), progress=None):
    """打包全部库数据到 dest_dir，返回备份包信息 dict。"""
    p = progress or _Progress()
    dest_dir = os.path.abspath(os.path.expanduser(dest_dir))
    if not os.path.isdir(dest_dir):
        raise BackupError(f"目标目录不存在：{dest_dir}（请确认 NAS / 移动硬盘已挂载）")
    if not os.path.exists(db_path):
        raise BackupError(f"数据库不存在：{db_path}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final = os.path.join(dest_dir, f"{ZIP_PREFIX}{ts}.zip")
    i = 2
    while os.path.exists(final):  # 同一秒内多次备份不互相覆盖
        final = os.path.join(dest_dir, f"{ZIP_PREFIX}{ts}_{i}.zip")
        i += 1
    part = final + ".part"

    exclude = {os.path.abspath(x) for x in extra_exclude}
    # 数据目录下的备份产物与恢复中转目录不进包（库根与数据目录重合时防递归膨胀）
    if data_dir:
        exclude.add(os.path.abspath(os.path.join(data_dir, "backups")))

    # ---- 第一遍：收集清单并计数（进度条真实） ----
    p.update(phase="collect", message="正在统计文件…", done=0, total=0)
    items = []
    for top_name, top_dir in (("library_root", library_root),
                              ("thumbs", thumb_dir),
                              ("attachments", attach_dir),
                              (".trash", trash_dir)):
        if os.path.isdir(top_dir):
            sub_exclude = set(exclude)
            if top_name == "library_root":
                for other in (thumb_dir, attach_dir, trash_dir):
                    if os.path.abspath(other) != os.path.abspath(top_dir):
                        sub_exclude.add(os.path.abspath(other))
            items.extend(_iter_dir_files(top_dir, top_name, sub_exclude))
    p.update(total=len(items), message=f"共 {len(items)} 个文件待打包")

    tmp_db = part + ".dbtmp"
    try:
        # ---- SQLite 一致性快照 ----
        p.update(phase="snapshot", message="正在生成数据库快照…")
        snapshot_db(db_path, tmp_db)
        meta = _backup_meta(tmp_db, library_root, len(items))

        # ---- 打包 ----
        p.update(phase="pack", message="正在打包…", done=0)
        with zipfile.ZipFile(part, "w", allowZip64=True) as z:
            z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
            z.write(tmp_db, "library.db", compress_type=zipfile.ZIP_DEFLATED)
            if os.path.exists(config_path):
                z.write(config_path, "config.json", compress_type=zipfile.ZIP_DEFLATED)
            done = 0
            for ap, arc in items:
                z.write(ap, arc, compress_type=zipfile.ZIP_DEFLATED if arc.endswith((".db", ".json")) else zipfile.ZIP_STORED)
                done += 1
                p.update(done=done, message=f"正在打包 {arc}")

        # ---- 原子落盘（NAS 同卷 rename）----
        p.update(phase="finish", message="正在写入备份包…", done=len(items))
        os.replace(part, final)
    finally:
        if os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass

    return {"path": final, "name": os.path.basename(final),
            "size": os.path.getsize(final), "meta": meta}


# ---------------------------------------------------------------
# 备份包读取与恢复
# ---------------------------------------------------------------
def read_backup_meta(zip_path):
    """读取并校验备份包 meta.json；不是有效备份包则抛 BackupError。"""
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            if "meta.json" not in names or "library.db" not in names:
                raise BackupError("不是有效的 3MF Manager 备份包（缺少 meta.json / library.db）")
            meta = json.loads(z.read("meta.json").decode("utf-8"))
    except zipfile.BadZipFile:
        raise BackupError("文件不是有效的 zip 备份包")
    if meta.get("kind") != BACKUP_KIND:
        raise BackupError("不是有效的 3MF Manager 备份包")
    return meta


def list_backups(path):
    """列出目录下的备份包（也接受直接指定 zip 文件），按时间倒序。"""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        candidates = [path]
    elif os.path.isdir(path):
        names = sorted(n for n in os.listdir(path)
                       if n.startswith(ZIP_PREFIX) and n.endswith(".zip"))
        candidates = [os.path.join(path, n) for n in names]
    else:
        raise BackupError(f"路径不存在：{path}")
    out = []
    for c in candidates:
        entry = {"path": c, "name": os.path.basename(c),
                 "size": os.path.getsize(c), "mtime": os.path.getmtime(c),
                 "valid": False, "meta": None}
        try:
            entry["meta"] = read_backup_meta(c)
            entry["valid"] = True
        except BackupError:
            pass
        out.append(entry)
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


def _safe_arc(name):
    """zip-slip 防护：只放行白名单顶层下的相对路径。"""
    name = name.replace("\\", "/")
    if not name or name.startswith("/") or ".." in name.split("/"):
        return None
    if name in TOP_FILES:
        return name
    for top in TOP_DIRS:
        if name.startswith(top):
            return name
    return None


def _extract_backup(zip_path, stage, progress):
    """解包到 stage；返回 (meta, 提取的条目数)。"""
    p = progress
    with zipfile.ZipFile(zip_path) as z:
        members = [(mi, _safe_arc(mi.filename)) for mi in z.infolist()]
        members = [(mi, arc) for mi, arc in members if arc and not mi.is_dir()]
        if not any(arc == "library.db" for _, arc in members):
            raise BackupError("不是有效的 3MF Manager 备份包（缺少 library.db）")
        p.update(phase="extract", message="正在解包备份…", done=0, total=len(members))
        meta = None
        for i, (mi, arc) in enumerate(members):
            dest = os.path.join(stage, arc.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(mi) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            if arc == "meta.json":
                meta = json.loads(open(dest, encoding="utf-8").read())
            p.update(done=i + 1)
    if not meta or meta.get("kind") != BACKUP_KIND:
        raise BackupError("不是有效的 3MF Manager 备份包")
    return meta, len(members)


_SWAP_TARGETS = ("library_root", "thumbs", "attachments", ".trash")


def restore_backup(*, zip_path, db_path, config_path, library_root,
                   thumb_dir, attach_dir, trash_dir, data_dir, progress=None):
    """从备份包还原全部数据，返回恢复摘要 dict。

    流程：校验 → 解包到 data_dir 下的暂存目录 → 把当前数据快照到
    backups/pre_restore_*.zip（best-effort）→ 整体替换 → 按 rel_path 重算
    abs_path。中途失败会尽力回滚到替换前的状态。
    """
    p = progress or _Progress()
    data_dir = os.path.abspath(data_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = os.path.join(data_dir, f".restore_stage_{ts}")
    old_dir = os.path.join(data_dir, f".restore_old_{ts}")
    backups_dir = os.path.join(data_dir, "backups")
    warnings = []
    pre_restore = None

    zip_path = os.path.abspath(os.path.expanduser(zip_path))
    if os.path.isdir(zip_path):  # 给目录时取其中最新有效的备份包
        valid = [e for e in list_backups(zip_path) if e["valid"]]
        if not valid:
            raise BackupError(f"该目录下没有有效的备份包：{zip_path}")
        zip_path = valid[0]["path"]
    if not os.path.isfile(zip_path):
        raise BackupError(f"备份包不存在：{zip_path}")
    meta = read_backup_meta(zip_path)
    if meta.get("schema_version", 0) > dbm.SCHEMA_VERSION:
        raise BackupError(
            f"备份来自更高版本（schema v{meta['schema_version']} > v{dbm.SCHEMA_VERSION}），"
            "请先把本程序升级到对应版本再恢复")

    # 备份包若放在将被替换的目录里（如库根），先复制出来再动手，防止恢复过程自毁备份
    src_copy = None
    orig_zip = zip_path
    protected = {os.path.abspath(x) for x in (library_root, thumb_dir, attach_dir, trash_dir)}
    if any(os.path.realpath(zip_path).startswith(os.path.realpath(d) + os.sep) for d in protected):
        src_copy = os.path.join(data_dir, f".restore_src_{ts}.zip")
        shutil.copy2(zip_path, src_copy)
        zip_path = src_copy

    os.makedirs(stage, exist_ok=False)
    try:
        meta, n_entries = _extract_backup(zip_path, stage, p)

        # ---- 恢复前安全快照当前数据（best-effort，失败不阻断恢复）----
        if os.path.exists(db_path):
            p.update(phase="pre-backup", message="正在备份当前数据以防万一…", done=0, total=0)
            os.makedirs(backups_dir, exist_ok=True)
            try:
                snap = create_backup(
                    db_path=db_path, config_path=config_path,
                    library_root=library_root, thumb_dir=thumb_dir,
                    attach_dir=attach_dir, trash_dir=trash_dir,
                    dest_dir=backups_dir, data_dir=data_dir,
                    extra_exclude=(stage, old_dir),
                    progress=_Progress())  # 子任务进度不上报主进度条
                pre_restore = snap["path"]
            except Exception as e:  # noqa: BLE001 —— 快照失败仅告警
                warnings.append(f"恢复前快照失败（已继续恢复）：{e}")

        # ---- 整体替换 ----
        p.update(phase="swap", message="正在替换数据…", done=0, total=1)
        _swap_in(stage, old_dir, db_path=db_path, config_path=config_path,
                 library_root=library_root, thumb_dir=thumb_dir,
                 attach_dir=attach_dir, trash_dir=trash_dir, p=p)

        # ---- 按 rel_path 重算 abs_path ----
        # 恢复 config 时保留当前库根目录设置：库位置是机器相关配置而非数据本身，
        # 模型文件始终还原到当前库根，跨机恢复也不会指向不存在的路径
        if os.path.exists(config_path):
            try:
                with open(config_path, encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg.setdefault("paths", {})["library_root"] = library_root
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
            except (OSError, ValueError):
                pass
        try:
            dbm.rebase_paths(db_path, library_root)
        except sqlite3.Error as e:
            warnings.append(f"abs_path 重算失败（可在设置里改库根目录触发重算）：{e}")

        shutil.rmtree(old_dir, ignore_errors=True)
        # 用户备份包原本在被替换目录内：恢复完成后把副本放回原位
        if src_copy:
            try:
                os.makedirs(os.path.dirname(orig_zip), exist_ok=True)
                shutil.copy2(src_copy, orig_zip)
            except OSError as e:
                warnings.append(f"原位备份包回填失败（副本仍在 {src_copy}）：{e}")
                src_copy = None  # 回填失败时保留副本，不进 finally 删除
        p.update(phase="done", message="恢复完成", done=1, total=1)
        return {"meta": meta, "entries": n_entries, "zip": zip_path,
                "library_root": library_root, "pre_restore_backup": pre_restore,
                "warnings": warnings}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if src_copy and os.path.exists(src_copy):
            try:
                os.remove(src_copy)
            except OSError:
                pass


def _swap_in(stage, old_dir, *, db_path, config_path, library_root,
             thumb_dir, attach_dir, trash_dir, p):
    """把暂存目录里的数据替换到正式位置；旧数据先移入 old_dir，失败可回滚。"""
    moved = []  # (新位置, 旧位置 or None)

    def move_aside(cur):
        """把现有数据移入 old_dir；返回旧路径，不存在返回 None。"""
        if not os.path.exists(cur) and not os.path.islink(cur):
            return None
        os.makedirs(old_dir, exist_ok=True)
        dest = os.path.join(old_dir, os.path.basename(cur.rstrip(os.sep)) or "db")
        i = 2
        while os.path.exists(dest):
            dest = f"{dest}_{i}"
            i += 1
        shutil.move(cur, dest)
        return dest

    def swap_one(new_src, cur):
        old = move_aside(cur)
        moved.append((cur, old))
        if os.path.exists(new_src):
            parent = os.path.dirname(cur)
            os.makedirs(parent, exist_ok=True)
            shutil.move(new_src, cur)

    try:
        # 数据库：旧 -wal/-shm 属于旧库，直接清掉（内容已含在快照里）
        for suffix in ("-wal", "-shm"):
            side = db_path + suffix
            if os.path.exists(side):
                os.remove(side)
        swap_one(os.path.join(stage, "library.db"), db_path)
        if os.path.exists(os.path.join(stage, "config.json")):
            swap_one(os.path.join(stage, "config.json"), config_path)
        top_map = {"library_root": library_root, "thumbs": thumb_dir,
                   "attachments": attach_dir, ".trash": trash_dir}
        for top, cur in top_map.items():
            swap_one(os.path.join(stage, top), cur)
    except Exception:
        # 尽力回滚：新数据让位，旧数据移回原位
        for cur, old in reversed(moved):
            try:
                if os.path.exists(cur):
                    if os.path.isdir(cur):
                        shutil.rmtree(cur, ignore_errors=True)
                    else:
                        os.remove(cur)
                if old and os.path.exists(old):
                    os.makedirs(os.path.dirname(cur), exist_ok=True)
                    shutil.move(old, cur)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------
# 后台任务（备份/恢复全局互斥，进度可轮询）
# ---------------------------------------------------------------
_JOBS = {}
_JOBS_SEQ = itertools.count(1)
_JOBS_LOCK = threading.Lock()
_ACTIVE = None  # 当前运行中的 job_id


class _Job:
    def __init__(self, kind, param):
        self.id = f"{kind}-{next(_JOBS_SEQ)}"
        self.kind = kind
        self.param = param
        self.state = "running"
        self.error = None
        self.result = None
        self.created = time.time()
        self.progress = _Progress()

    def snapshot(self):
        return {"id": self.id, "kind": self.kind, "param": self.param,
                "state": self.state, "error": self.error,
                "result": self.result, "progress": self.progress.snapshot()}


def start_job(kind, param, fn):
    """启动后台任务；已有任务在跑时抛 BusyError。fn(progress) -> result dict。"""
    global _ACTIVE
    with _JOBS_LOCK:
        if _ACTIVE:
            running = _JOBS.get(_ACTIVE)
            raise BusyError(f"已有{'备份' if running and running.kind == 'backup' else '恢复'}任务"
                            f"在进行中（{running.param if running else ''}），请等它结束")
        job = _Job(kind, param)
        _JOBS[job.id] = job
        _ACTIVE = job.id
        # 顺手清理 1 小时前已结束的任务，防止长驻进程内存增长
        cutoff = time.time() - 3600
        for jid in [j for j, job_ in _JOBS.items()
                    if job_.state != "running" and job_.created < cutoff]:
            _JOBS.pop(jid, None)

    def run():
        global _ACTIVE
        try:
            job.result = fn(job.progress)
            job.state = "done"
        except Exception as e:  # noqa: BLE001 —— 任务内异常转成可轮询的错误状态
            job.state = "error"
            job.error = str(e) or e.__class__.__name__
        finally:
            with _JOBS_LOCK:
                if _ACTIVE == job.id:
                    _ACTIVE = None

    threading.Thread(target=run, name=f"mfmanager-{kind}", daemon=True).start()
    return job.snapshot()


def job_snapshot(kind, job_id=None):
    """取指定任务；不给 id 时返回该类型最近一次任务（页面刷新后恢复显示）。"""
    with _JOBS_LOCK:
        if job_id:
            job = _JOBS.get(job_id)
            return job.snapshot() if job and job.kind == kind else None
        latest = None
        for job in _JOBS.values():
            if job.kind == kind and (latest is None or job.created > latest.created):
                latest = job
        return latest.snapshot() if latest else None
