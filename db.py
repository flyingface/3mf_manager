#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""SQLite 存储层：schema、版本化迁移与文件/哈希/路径工具。

不持有运行时全局状态：库根目录、各产物目录、DB 路径均由调用方（server.py
组合根）显式传入，便于测试隔离与配置热更新。
"""
import hashlib
import os
import shutil
import sqlite3

# 迁移版本历史：
#   1 = 基础表（files/attachments/settings + 索引 + files.plate_imgs）
#   2 = files.rel_path / attachments.rel_path（相对库根路径，切换库根后可重定 abs_path）
#   3 = 作品关联图层（asset_groups / group_members，目录之上的关系层）
#   4 = files.printed（模型级打印标记，可按已打印/未打印筛选）
SCHEMA_VERSION = 4


def db_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _add_column(conn, table, col, ddl):
    if col not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _migrate_v1(conn):
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
    # 老库（v1 之前建表）可能缺 plate_imgs
    _add_column(conn, "files", "plate_imgs", "TEXT DEFAULT ''")


def _migrate_v2(conn):
    _add_column(conn, "files", "rel_path", "TEXT DEFAULT ''")
    _add_column(conn, "attachments", "rel_path", "TEXT DEFAULT ''")


def _migrate_v3(conn):
    # 作品关联图层：目录管存放，图管关系。删除文件时由应用层清理成员行。
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS asset_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kind TEXT DEFAULT 'kit',
        cover_file_id INTEGER,
        print_state TEXT DEFAULT '',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        file_id INTEGER NOT NULL,
        role TEXT DEFAULT 'component',
        confidence TEXT DEFAULT 'high',
        is_primary INTEGER DEFAULT 0,
        confirmed INTEGER DEFAULT 1,
        printed INTEGER DEFAULT 0,
        UNIQUE(group_id, file_id)
    );
    CREATE INDEX IF NOT EXISTS idx_gm_group ON group_members(group_id);
    CREATE INDEX IF NOT EXISTS idx_gm_file ON group_members(file_id);
    """)


def _migrate_v4(conn):
    # 模型级打印标记：1=已打印 0=未打印（旧记录默认 0）；仅加列加索引，不动既有数据
    _add_column(conn, "files", "printed", "INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_printed ON files(printed)")


_MIGRATIONS = {1: _migrate_v1, 2: _migrate_v2, 3: _migrate_v3, 4: _migrate_v4}


def init_db(db_path, library_root, inbox, thumb_dir, attach_dir):
    """确保目录与 schema 就绪，按 user_version 逐级迁移到最新。"""
    for d in (library_root, inbox, thumb_dir, attach_dir):
        os.makedirs(d, exist_ok=True)
    conn = db_conn(db_path)
    try:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        for ver in range(v + 1, SCHEMA_VERSION + 1):
            _MIGRATIONS[ver](conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def rebase_paths(db_path, library_root):
    """库根目录变更后，按 rel_path 重算 files/attachments 的 abs_path。"""
    conn = db_conn(db_path)
    try:
        for table in ("files", "attachments"):
            rows = conn.execute(
                f"SELECT id, rel_path FROM {table} WHERE rel_path IS NOT NULL AND rel_path!=''").fetchall()
            for r in rows:
                conn.execute(f"UPDATE {table} SET abs_path=? WHERE id=?",
                             (os.path.join(library_root, r["rel_path"]), r["id"]))
        conn.commit()
    finally:
        conn.close()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_to_root(path, library_root):
    try:
        return os.path.relpath(path, library_root)
    except Exception:
        return os.path.basename(path)


def trash_move(src, trash_dir):
    """把 src 移入回收站目录，避免重名覆盖；返回目标路径，失败返回 None。"""
    if not src or not os.path.exists(src):
        return None
    os.makedirs(trash_dir, exist_ok=True)
    base = os.path.basename(src)
    dest = os.path.join(trash_dir, base)
    if os.path.abspath(dest) == os.path.abspath(src):
        return dest
    i = 2
    b, e = os.path.splitext(base)
    while os.path.exists(dest):
        dest = os.path.join(trash_dir, f"{b}_{i}{e}")
        i += 1
    try:
        shutil.move(src, dest)
    except Exception:
        return None
    return dest


def _row_get(row, key):
    return row.get(key) if hasattr(row, "get") else row[key]


def attachment_full_path(row, library_root):
    """把附件记录解析为当前库根下的绝对路径（相对路径优先，兼容老库绝对 abs_path）。"""
    rel = _row_get(row, "rel_path") or ""
    abs_p = _row_get(row, "abs_path") or ""
    if rel:
        p = os.path.join(library_root, rel)
        if os.path.exists(p):
            return p
    if abs_p and os.path.exists(abs_p):
        return abs_p
    # 都不存在时仍返回相对解析结果，便于上层 404 统一处理
    return os.path.join(library_root, rel) if rel else abs_p


def file_full_path(row, library_root):
    """把文件记录解析为当前库根下的绝对路径（rel_path 优先，兼容纯 abs_path 老记录）。"""
    rel = _row_get(row, "rel_path") or ""
    abs_p = _row_get(row, "abs_path") or ""
    if rel:
        p = os.path.join(library_root, rel)
        if os.path.exists(p):
            return p
    if abs_p and os.path.exists(abs_p):
        return abs_p
    return os.path.join(library_root, rel) if rel else abs_p
