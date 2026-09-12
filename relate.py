#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""确定性关联：从文件元数据聚出"可能相关"的候选簇（纯本地规则，无 AI）。

三路信号（由强到弱）：
  1. design_id 相同        → 同一 MakerWorld 设计的不同导出
  2. sha256 相同           → 精确重复
  3. geom_sig 相同         → 缩放变体（缩放不改变顶点/三角面拓扑）
  4. 文件名词干聚类        → 共享词干、差异词恰好是版本/尺寸/板型模式（弱信号）

输出候选簇列表，供 LLM 判型命名（批次2）或直接确认（同设计/重复可免 AI）。
"""
import re
from collections import defaultdict

# 版本/尺寸/板型类噪音差异词：命中才允许词干聚类成立
_NOISE_DIFF = re.compile(
    r"^("
    r"v\d+(\.\d+)*|[\d.]+%|150|200|[\d]+cm|[\d]+mm|"
    r"a1|minix1|a1mini|p1p|p1s|x1|x1c|x1e|"
    r"ams|多色|单色|双色|全彩|"
    r"复制|副本|copy|old|旧|new|新|final|最终|最新|"
    r"[\d_]+"
    r")$", re.I)

# 文件名分词：分隔符 + 中英/字母数字边界（中文整段、英文/数字成词）
_SPLIT = re.compile(r"[\s_\-—~()\[\]（）【】·+,，;；!！??.]+|(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])")

MIN_TOKENS_FOR_STEM = 2
NOISE_TOKENS = {"3mf", "print", "打印", "模型", "final", "复制", "副本", "版"}


def tokenize(filename):
    return [t for t in _SPLIT.split(filename.lower()) if t]


def _strip_noise(tokens):
    out = []
    for t in tokens:
        if t in NOISE_TOKENS or t.isdigit():
            continue
        if _NOISE_DIFF.match(t):
            continue
        out.append(t)
    return out


_CJK = re.compile(r"[\u4e00-\u9fff]")


def _stem_info_ok(toks):
    """词干信息量判断：中文≥2 字的单 token 足够具体；纯英文需 ≥2 个 token。"""
    if len(toks) >= MIN_TOKENS_FOR_STEM:
        return True
    return len(toks) == 1 and len(_CJK.findall(toks[0])) >= 2


def stem_of(filename):
    """文件名词干：完整分词，仅剥离尾部的版本/尺寸/板型噪音 token 与 .3mf。

    只去尾部噪音：中段实义词（如「高达模型」）保留，避免误丢导致无法聚类。
    中文单 token（≥2 汉字）视为足够具体的词干。
    """
    toks = tokenize(filename)
    # 去掉扩展名 token
    toks = [t for t in toks if t != "3mf"]
    # 从尾部剥噪音（版本/尺寸/板型、纯数字）
    while len(toks) > 1 and (toks[-1].isdigit() or _NOISE_DIFF.match(toks[-1]) or toks[-1] in NOISE_TOKENS):
        toks.pop()
    if not _stem_info_ok(toks):
        return None
    return " ".join(toks)


class UnionFind:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def find_clusters(rows):
    """对 files 行列表做确定性聚类。

    rows: dict 列表，需要 id/filename/design_id/sha256/geom_sig。
    返回 [{files:[row...], signals:[...], confidence:'high'|'low'}]，
    只输出 ≥2 个成员的簇；design_id/sha256/geom_sig 命中为 high，仅词干为 low。
    """
    uf = UnionFind()
    signals = defaultdict(list)  # (file_id) -> [信号描述]

    def link(a, b, why):
        uf.union(a["id"], b["id"])
        if why not in signals[a["id"]]:
            signals[a["id"]].append(why)
        if why not in signals[b["id"]]:
            signals[b["id"]].append(why)

    # 1) design_id
    by_design = defaultdict(list)
    for r in rows:
        d = (r.get("design_id") or "").strip()
        if d:
            by_design[d].append(r)
    for grp in by_design.values():
        for other in grp[1:]:
            link(grp[0], other, "同设计编号")

    # 2) sha256（精确重复）
    by_sha = defaultdict(list)
    for r in rows:
        s = (r.get("sha256") or "").strip()
        if s:
            by_sha[s].append(r)
    for grp in by_sha.values():
        for other in grp[1:]:
            link(grp[0], other, "内容完全相同")

    # 3) geom_sig（缩放变体）：需排除同 sha（内容不同才谈变体）
    by_geom = defaultdict(list)
    for r in rows:
        g = (r.get("geom_sig") or "").strip()
        if g and g != "0|0":
            by_geom[g].append(r)
    for grp in by_geom.values():
        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                if (a.get("sha256") or "") and a.get("sha256") == b.get("sha256"):
                    continue
                link(a, b, "几何指纹相同（可能为缩放变体）")

    # 4) 词干聚类（弱信号）：同词干且至少一个差异词是版本/尺寸/板型
    by_stem = defaultdict(list)
    for r in rows:
        st = stem_of(r.get("filename") or "")
        if st:
            by_stem[st].append(r)
    for grp in by_stem.values():
        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                if uf.find(a["id"]) == uf.find(b["id"]):
                    continue
                ta = set(tokenize(a["filename"] or ""))
                tb = set(tokenize(b["filename"] or ""))
                diff = (ta ^ tb) - {"3mf"}
                if diff and all(_NOISE_DIFF.match(t) for t in diff):
                    link(a, b, "文件名词干相似")

    # 收簇
    groups = defaultdict(list)
    for r in rows:
        groups[uf.find(r["id"])].append(r)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        hard = any(signals.get(m["id"]) and
                   any(("编号" in w or "相同" in w) for w in signals[m["id"]])
                   for m in members)
        out.append({
            "files": sorted(members, key=lambda r: (r.get("created_at") or "", r["id"])),
            "signals": sorted({w for m in members for w in signals.get(m["id"], [])}),
            "confidence": "high" if hard else "low",
        })
    out.sort(key=lambda c: -len(c["files"]))
    return out
