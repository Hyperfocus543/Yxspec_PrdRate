#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score_prd.py — PRD 六维质量打分（GQ-5 体系，源自 yxspec prd-generation-pipeline）
================================================================================

对任意 PRD markdown 自动执行 GQ-5 六维评分：
  CMP 完整性 / VER 可验证性 / CON 一致性 / TRC 追溯性 / CLR 明确性 / PRI 优先级

六维加权平均 >= 75 分方可通过（与 yxspec GQ-rules.yaml 一致）。

设计原则：
  - 纯 Python 标准库，零第三方依赖，单文件自包含。
  - 确定性量化代理：将 GQ-rules.yaml 的六维 checklist 提炼为可计数的自动度量。
  - 【自适应】自动探测 PRD 的锚点/章节/优先级/来源形态，适配不同模板的 PRD；
    结构特殊时可用 --config 精确覆盖。
  - 已用爱玛 X1 BCM PRD（prd-aima_bcm-2026.md）实测，自动分与自评 GQ-5
    （91.0）各维贴近、门禁判定一致（自动分更严/更宽属口径差异）。

用法：
  python score_prd.py <prd.md>                 # 打分（自动探测形态）
  python score_prd.py <prd.md> --json          # 输出 JSON
  python score_prd.py <prd.md> --report out.md # 输出 Markdown 报告文件
  python score_prd.py <prd.md> --config cfg.json # 用配置文件覆盖探测结果
  python score_prd.py <prd.md> --show-profile  # 显示探测到的形态

--config 配置字段（JSON，均为可选，缺省用探测结果）：
{
  "chapters": ["第1章：文档信息", ...],      # CMP 完整性检查的 MANDATORY 章节
  "appendices": ["A.1", "A.2"],              # CMP 强制附录
  "anchor_pattern": "REQ-[A-Z]-?\\d{4,6}",   # 锚点内的 ID 正则（自动加 <a id="..."> 包裹）
  "priority_words": ["must","should","could"],# 优先级词表（小写）
  "priority_reference": {"must":[40,60],...}, # 优先级参考区间（可选，缺省用默认+容忍带）
  "source_path_markers": ["inputs/"],         # 来源链接路径特征（任一命中即算有源）
  "term_sections": ["术语","缩写"],           # CLR 术语表章节线索
  "fuzzy_words": ["快速","大约"],             # VER 模糊词（追加到默认表）
  "req_columns": {"desc":2,"priority":3,"criteria":5,"source":6}  # 表格列序（0-based）
}

作者：林汉飞 | 2026-08-12
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter

# ---------------------------------------------------------------------------
# 六维权重（GQ-rules.yaml 权威值）
# ---------------------------------------------------------------------------
DIMENSIONS = [
    {"id": "CMP", "name": "完整性", "en": "Completeness",    "weight": 0.20},
    {"id": "VER", "name": "可验证性", "en": "Verifiability", "weight": 0.20},
    {"id": "CON", "name": "一致性",   "en": "Consistency",    "weight": 0.20},
    {"id": "TRC", "name": "追溯性",   "en": "Traceability",   "weight": 0.20},
    {"id": "CLR", "name": "明确性",   "en": "Clarity",        "weight": 0.15},
    {"id": "PRI", "name": "优先级",   "en": "Priority",       "weight": 0.05},
]
PASS_THRESHOLD = 75.0
DIM_BY_ID = {d["id"]: d for d in DIMENSIONS}

# ---------------------------------------------------------------------------
# 默认形态（yxspec PRD：BCM 版）——探测失败时的回退值
# ---------------------------------------------------------------------------
DEFAULT_CHAPTERS = [
    "第1章：文档信息", "第2章：产品概述", "第3章：功能需求", "第4章：性能需求",
    "第5章：硬件需求", "第6章：结构需求", "第7章：试验验证", "第8章：法规认证",
    "附录A",
]
DEFAULT_APPENDICES = ["A.1", "A.2"]
DEFAULT_ANCHOR_PATTERN = r"REQ-[A-Z]-?\d{4,6}"
DEFAULT_PRIORITY_WORDS = ["must", "should", "could"]
DEFAULT_PRIORITY_REFERENCE = {
    "must": [40, 60], "should": [25, 40], "could": [10, 20],
}
DEFAULT_PRIORITY_TOLERANCE = {
    "must": [40, 75], "should": [20, 45], "could": [5, 30],
}
DEFAULT_SOURCE_MARKERS = ["inputs/"]
DEFAULT_TERM_SECTIONS = ["术语", "缩写", "定义", "Terminology"]
# VER: 模糊修饰词（ambiguity-dictionary.yaml#forbidden_words 提炼）
# 状态枚举值（0x0正常/0x1故障/0x2稳定）不算模糊词；列举省略符「等」为合法用法。
# 「左右」仅当紧邻数字才是"大约"义（"左右转"= 方向词）。
DEFAULT_FUZZY_PATTERNS = [
    "快速", "合理", "适当", "充分", "尽快", "及时", "大约",
    r"\d+\s*左右",
    "较高", "较小", "简单", "方便", "便捷", "大概", "几乎", "强大", "优异",
    "显著", "高效",
]
VERIFIABLE_MARKERS = [
    r"\d+", "必须", "不得", "应返回", "响应", "通过", "PASS", "禁止",
    "成功", "失败", "超时", "报错", "错误", "校验", "检测",
]
# REQ 表格默认列序（0-based，来自 yxspec 形态）
DEFAULT_REQ_COLUMNS = {"desc": 2, "priority": 3, "criteria": 5, "source": 6}

# 常见锚点形态探测线索（用于自适应：从 PRD 实际格式推断 ID 正则）
_ANCHOR_FORM_CANDIDATES = [
    # (正则, 样例说明)
    (r"REQ-[A-Z]-?\d{4,6}",             "REQ-F-020040"),
    (r"REQ-\d{4,6}",                    "REQ-000234"),
    (r"[A-Z]{2,5}-\d{2,6}",             "FR-01234"),
    (r"[A-Za-z]+-\d+",                  "ID-123"),
]
# 优先级词表探测候选（小写）
_PRIORITY_CANDIDATES = [
    ["must", "should", "could"],
    ["p0", "p1", "p2"],
    ["high", "medium", "low"],
    ["critical", "major", "minor"],
]


# ---------------------------------------------------------------------------
# Profile：自适应形态（探测结果 + 配置覆盖）
# ---------------------------------------------------------------------------

class Profile:
    def __init__(self, text: str, overrides: dict | None = None):
        self.anchor_pattern = DEFAULT_ANCHOR_PATTERN
        self.priority_words = list(DEFAULT_PRIORITY_WORDS)
        self.priority_reference = {k: list(v) for k, v in DEFAULT_PRIORITY_REFERENCE.items()}
        self.priority_tolerance = {k: list(v) for k, v in DEFAULT_PRIORITY_TOLERANCE.items()}
        self.source_markers = list(DEFAULT_SOURCE_MARKERS)
        self.term_sections = list(DEFAULT_TERM_SECTIONS)
        self.fuzzy_patterns = list(DEFAULT_FUZZY_PATTERNS)
        self.req_columns = dict(DEFAULT_REQ_COLUMNS)

        self._detect(text)
        if overrides:
            self._apply_overrides(overrides)

        # 编译正则
        self.anchor_re = re.compile(r'<a id="(' + self.anchor_pattern + r')"></a>')
        self.fuzzy_re = re.compile("|".join(f"({p})" for p in self.fuzzy_patterns))
        self.priority_set = {w.lower() for w in self.priority_words}
        source_alt = "|".join(re.escape(m) for m in self.source_markers)
        self.source_re = re.compile(
            r"\]\([^)]*" + source_alt + r"[^)]*\)|\[\[[^\]]*\]\]"
        )

    # ---- 自动探测 ----
    def _detect(self, text: str) -> None:
        # 1. 锚点形态：看实际 <a id="..."> 用哪种
        for pattern, _ in _ANCHOR_FORM_CANDIDATES:
            if re.search(r'<a id="' + pattern + r'">', text):
                self.anchor_pattern = pattern
                break
        # 2. 章节：探测 # 第\d章 标题，匹配不到用默认
        chapters = self._detect_chapters(text)
        if chapters:
            self.chapters = chapters
        else:
            self.chapters = list(DEFAULT_CHAPTERS)
        self.appendices = self._detect_appendices(text)
        # 3. 优先级词表：看表格里出现哪套
        self.priority_words = self._detect_priority_words(text)
        # 4. 来源路径特征：看现有链接用什么目录
        self._detect_source_markers(text)
        # 5. 表格列序：看 REQ 表格表头
        self._detect_columns(text)

    def _detect_chapters(self, text: str) -> list[str]:
        """从标题探测章节体系。

        优先收集一级标题（排除文档标题），不足 2 个时用二级标题补足——
        覆盖纯段落式 PRD（无锚点无表格，章节用 ## 组织）的场景。
        一级+二级都不足 2 个 → 回退默认 yxspec 章节。
        """
        found = []
        # 一级标题
        for m in re.finditer(r"^#\s+(.*?)\s*$", text, re.M):
            title = m.group(1).strip()
            title = re.sub(r"<!--.*?-->", "", title).strip()
            if not title or re.search(r"PRD|需求规格|产品需求|文档编号|SPEC-|规格书", title, re.I):
                continue
            found.append(title)
        # 不足 2 个时用二级标题补足
        if len(found) < 3:
            for m in re.finditer(r"^##\s+(.*?)\s*$", text, re.M):
                title = m.group(1).strip()
                title = re.sub(r"<!--.*?-->", "", title).strip()
                if title:
                    found.append(title)
        # 去重且保留顺序
        seen = set()
        uniq = []
        for t in found:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        if len(uniq) >= 2:
            return uniq
        return list(DEFAULT_CHAPTERS)

    def _detect_appendices(self, text: str) -> list[str]:
        """探测 PRD 实际使用的附录编号。

        命中 A.1/A.2/A.3/A.4/附录A 等返回命中项；一项都没有 → 返回空列表
        （该模板无附录约定，跳过附录检查，避免误用默认 A.1/A.2 判缺失）。
        """
        found = []
        for ap in ["A.1", "A.2", "A.3", "A.4", "附录A", "Appendix A"]:
            if ap in text:
                found.append(ap)
        return found

    def _detect_priority_words(self, text: str) -> list[str]:
        """统计表格里出现哪套优先级词，用占比最高的那套。"""
        best, best_count = list(DEFAULT_PRIORITY_WORDS), 0
        for cand in _PRIORITY_CANDIDATES:
            cnt = 0
            for w in cand:
                # 在表格单元格里独立出现（must 等）
                cnt += len(re.findall(r"\|\s*" + re.escape(w) + r"\s*\|", text, re.I))
            if cnt > best_count:
                best, best_count = cand, cnt
        return best if best_count > 0 else list(DEFAULT_PRIORITY_WORDS)

    def _detect_source_markers(self, text: str) -> None:
        """从现有链接里找路径特征（inputs/ 或 src/ 或 docs/ 等）。"""
        links = re.findall(r"\]\(([^)]+)\)", text)
        dirs = Counter()
        for link in links:
            parts = link.split("/")
            if len(parts) >= 2:
                dirs[parts[0] + "/"] += 1
            elif "/" in link:
                dirs[link.split("/")[0] + "/"] += 1
        if dirs:
            # 取最常见的目录做来源标记
            self.source_markers = [dirs.most_common(1)[0][0]]

    def _detect_columns(self, text: str) -> None:
        """探测 REQ 表格表头列序：找含 REQ-ID 表格的表头行。"""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("|") and ("ID" in line or "编号" in line or "Req" in line.lower()):
                cells = [c.strip().lower() for c in line.strip("|").split("|")]
                colmap = {}
                for i, c in enumerate(cells):
                    if "id" in c or "编号" in c:
                        colmap["id"] = i
                    elif "名称" in c or "title" in c or "name" in c:
                        colmap["name"] = i
                    elif "描述" in c or "desc" in c or "需求" in c:
                        colmap.setdefault("desc", i)
                    elif "优先" in c or "pri" in c or "p0" in c:
                        colmap["priority"] = i
                    elif "验收" in c or "accept" in c or "crit" in c or "可测" in c:
                        colmap["criteria"] = i
                    elif "来源" in c or "source" in c or "溯源" in c:
                        colmap["source"] = i
                if colmap.get("id") is not None:
                    self.req_columns.update(
                        {k: v for k, v in colmap.items() if v is not None}
                    )
                    return

    # ---- 配置覆盖 ----
    def _apply_overrides(self, ov: dict) -> None:
        if "chapters" in ov:
            self.chapters = ov["chapters"]
        if "appendices" in ov:
            self.appendices = ov["appendices"]
        if "anchor_pattern" in ov:
            self.anchor_pattern = ov["anchor_pattern"]
        if "priority_words" in ov:
            self.priority_words = [str(w).lower() for w in ov["priority_words"]]
        if "priority_reference" in ov:
            self.priority_reference = {k: list(v) for k, v in ov["priority_reference"].items()}
        if "priority_tolerance" in ov:
            self.priority_tolerance = {k: list(v) for k, v in ov["priority_tolerance"].items()}
        if "source_path_markers" in ov:
            self.source_markers = ov["source_path_markers"]
        if "term_sections" in ov:
            self.term_sections = ov["term_sections"]
        if "fuzzy_words" in ov:
            self.fuzzy_patterns = list(self.fuzzy_patterns) + [str(w) for w in ov["fuzzy_words"]]
        if "req_columns" in ov:
            self.req_columns.update({k: int(v) for k, v in ov["req_columns"].items()})

    def to_dict(self) -> dict:
        return {
            "chapters": self.chapters,
            "appendices": self.appendices,
            "anchor_pattern": self.anchor_pattern,
            "priority_words": self.priority_words,
            "priority_reference": self.priority_reference,
            "priority_tolerance": self.priority_tolerance,
            "source_markers": self.source_markers,
            "term_sections": self.term_sections,
            "req_columns": self.req_columns,
        }


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------

def _parse_rows(text: str) -> list[list[str]]:
    """提取 markdown 表格行，返回单元格列表（含表头）。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|") and not line.startswith("|-"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append(cells)
    return rows


def _extract_req_rows_from_text(text: str, profile: Profile) -> list[list[str]]:
    """从全文提取需求条目行。

    权威识别：`<a id="REQ-XXXX"></a>` 锚点行 + 紧随其后的表格行 = 一条需求。
    锚点格式来自 profile.anchor_re（自适应探测或配置覆盖）。
    """
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = profile.anchor_re.search(line)
        if m:
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("|"):
                j += 1
            if j < len(lines) and lines[j].strip().startswith("|") and lines[j].strip().endswith("|"):
                row_text = lines[j].strip().strip("|")
                cells = [c.strip() for c in row_text.split("|")]
                out.append(cells)
            i = j
        else:
            i += 1
    return out


def _priority_of_row(row: list[str], profile: Profile) -> str | None:
    for c in row:
        c = c.strip().lower()
        if c in profile.priority_set:
            return c
    return None


# ---------------------------------------------------------------------------
# 六维评分（全部接收 profile）
# ---------------------------------------------------------------------------

def _row_text(row: list[str], profile: Profile, upto: int | None = None) -> str:
    """取需求行指定列区间的拼接文本（默认到来源列）。"""
    cols = profile.req_columns
    src = cols.get("source", len(row) - 1)
    end = src if upto is None else upto
    return " | ".join(row[: end + 1])


def score_cmp(text: str, profile: Profile) -> dict:
    """完整性：章节覆盖 + 占位符 + 附录。"""
    checks, hits = [], 0
    n_checks = 0
    for ch in profile.chapters:
        ok = ch in text
        checks.append((f"MANDATORY 章节「{ch}」存在", ok, 1.0))
        hits += 1 if ok else 0
        n_checks += 1
    placeholders = re.findall(r"\{\{[^}]*\}\}|\[TODO\]|TODO:", text)
    ok = len(placeholders) == 0
    checks.append((f"占位符清零（找到 {len(placeholders)} 处）", ok, 1.0))
    hits += 1 if ok else 0
    n_checks += 1
    for ap in profile.appendices:
        ok = ap in text
        checks.append((f"强制附录「{ap}」存在", ok, 1.0))
        hits += 1 if ok else 0
        n_checks += 1
    score = round(100 * hits / n_checks, 1)
    return {"id": "CMP", "score": score, "checks": checks, "issues": [c[0] for c in checks if not c[1]]}


def score_ver(text: str, req_rows: list[list[str]], profile: Profile) -> dict:
    """可验证性：模糊词 + 验收准则可量化。"""
    checks, hits = [], 0
    fuzz_found = []
    for r in req_rows:
        row_text = _row_text(r, profile)
        for m in profile.fuzzy_re.finditer(row_text):
            w = m.group(0)
            fuzz_found.append((w, row_text[max(0, m.start() - 12):m.end() + 12]))
    fuzz_unique = sorted({w for w, _ in fuzz_found})
    ok = len(fuzz_unique) == 0
    checks.append((f"模糊词清零（发现 {len(fuzz_unique)} 种，如 {fuzz_unique[:3]}）", ok, 0.6))
    hits += 1 if ok else 0
    unverifiable = 0
    for r in req_rows:
        row_text = _row_text(r, profile)
        if not any(re.search(m, row_text) for m in VERIFIABLE_MARKERS):
            unverifiable += 1
    total = len(req_rows) or 1
    ratio = (total - unverifiable) / total
    ok = ratio >= 0.9
    checks.append((f"可验证条目比例 {ratio:.0%}（{total - unverifiable}/{total}，≥90% 达标）", ok, 0.4))
    hits += 1 if ok else 0
    score = round(100 * hits / len(checks), 1)
    issues = [c[0] for c in checks if not c[1]]
    if unverifiable:
        issues.append(f"{unverifiable} 条需求无明显可测内容")
    return {"id": "VER", "score": score, "checks": checks, "issues": issues}


def score_con(text: str, req_rows: list[list[str]], profile: Profile) -> dict:
    """一致性：优先级枚举合法 + 无重复 ID。"""
    checks, hits = [], 0
    priors = Counter()
    for r in req_rows:
        p = _priority_of_row(r, profile)
        if p:
            priors[p] += 1
        else:
            priors["(缺失)"] += 1
    illegal = priors.get("(缺失)", 0)
    ok = illegal == 0
    checks.append((f"优先级枚举合法（缺失/非法 {illegal} 条）", ok, 0.5))
    hits += 1 if ok else 0
    ids = [profile.anchor_re.search(" | ".join(r)).group(1)
           for r in req_rows if profile.anchor_re.search(" | ".join(r))]
    dup = {i: n for i, n in Counter(ids).items() if n > 1}
    ok = len(dup) == 0
    checks.append((f"REQ-ID 无重复（重复 {len(dup)} 个）", ok, 0.5))
    hits += 1 if ok else 0
    score = round(100 * hits / len(checks), 1)
    return {"id": "CON", "score": score, "checks": checks,
            "issues": [c[0] for c in checks if not c[1]] + ([f"重复 ID: {list(dup)}"] if dup else [])}


def score_trc(text: str, req_rows: list[list[str]], profile: Profile) -> dict:
    """追溯性：无源项 + 行号 + 编号连续。"""
    checks, hits = [], 0
    total = len(req_rows) or 1
    no_src = 0
    for r in req_rows:
        if not profile.source_re.search(" | ".join(r)):
            no_src += 1
    ok = no_src == 0
    checks.append((f"无源条目 {no_src}/{total}（Must 级应为 0）", ok, 0.5))
    hits += 1 if ok else 0
    no_line = sum(1 for r in req_rows if not re.search(r"L\d+", " | ".join(r)))
    line_ratio = (total - no_line) / total
    ok = line_ratio >= 0.90
    checks.append((f"来源带行号比例 {line_ratio:.0%}（{total - no_line}/{total}，≥90% 达标）", ok, 0.3))
    hits += 1 if ok else 0
    # 编号连续：按锚点 ID 的数字部分分组，组内连续
    groups: dict[str, list[int]] = {}
    for r in req_rows:
        row_text = " | ".join(r)
        m = profile.anchor_re.search(row_text)
        if m:
            num = re.search(r"(\d+)$", m.group(1))
            if num:
                prefix = m.group(1)[: -len(num.group(1))]
                groups.setdefault(prefix, []).append(int(num.group(1)))
    gaps = 0
    for g, seqs in groups.items():
        seqs = sorted(seqs)
        for i in range(1, len(seqs)):
            if seqs[i] != seqs[i - 1] + 1:
                gaps += 1
    ok = gaps == 0
    checks.append((f"编号连续无跳号（ID 组内断点 {gaps} 处）", ok, 0.2))
    hits += 1 if ok else 0
    score = round(100 * hits / len(checks), 1)
    return {"id": "TRC", "score": score, "checks": checks, "issues": [c[0] for c in checks if not c[1]]}


def score_clr(text: str, req_rows: list[list[str]], profile: Profile) -> dict:
    """明确性：术语表 + 结构化表述 + 数值锚点（渐进打分）。"""
    checks = []
    has_terms = any(s in text for s in profile.term_sections)
    checks.append(("术语表/术语定义存在", has_terms, 0.4))
    structured = sum(
        1 for r in req_rows
        if re.search(r"SHALL|shall|应", _row_text(r, profile))
    )
    total = len(req_rows) or 1
    ratio = structured / total
    checks.append((f"需求表述结构化比例 {ratio:.0%}（≥80% 达标）", ratio >= 0.8, 0.3))
    no_anchor = sum(1 for r in req_rows if "数值锚点" not in _row_text(r, profile))
    anchor_ratio = (total - no_anchor) / total
    checks.append((f"数值锚点覆盖 {anchor_ratio:.0%}（≥85% 为优）", anchor_ratio >= 0.85, 0.3))
    s_term = 1.0 if has_terms else 0.0
    score = round(100 * (s_term * 0.4 + ratio * 0.3 + anchor_ratio * 0.3), 1)
    issues = [c[0] for c in checks if not c[1]]
    return {"id": "CLR", "score": score, "checks": checks, "issues": issues}


def score_pri(req_rows: list[list[str]], profile: Profile) -> dict:
    """优先级分布合理性：参考区间 + 容忍带，分档渐进打分。"""
    priors = Counter()
    for r in req_rows:
        p = _priority_of_row(r, profile)
        if p:
            priors[p] += 1
    total = sum(priors.values()) or 1
    issues = []
    deviations = []
    for p in profile.priority_reference:
        pct = priors.get(p, 0) / total * 100
        lo_ref, hi_ref = profile.priority_reference[p]
        lo_tol, hi_tol = profile.priority_tolerance.get(p, [lo_ref, hi_ref])
        if lo_ref <= pct <= hi_ref:
            dev = 0.0
            note = "参考区间内"
        elif lo_tol <= pct <= hi_tol:
            dev = 0.0
            note = "容忍带内（域合理）"
        else:
            dev = max(lo_tol - pct, pct - hi_tol, 0.0) / 100.0
            note = f"超容忍带（偏差 {dev:.0%}）"
            issues.append(f"{p} 占比 {pct:.0f}% 超出参考区间 {lo_ref}-{hi_ref}%（{note}）")
        deviations.append(dev)
    max_dev = max(deviations)
    score = round(100 * max(0.0, 1.0 - max_dev), 1)
    checks = []
    for p, (lo, hi) in profile.priority_reference.items():
        pct = priors.get(p, 0) / total * 100
        lo_t, hi_t = profile.priority_tolerance.get(p, [lo, hi])
        ok = lo_t <= pct <= hi_t
        status = "达标" if ok else f"偏差 {max(lo_t - pct, pct - hi_t, 0.0):.0f}pp"
        checks.append((f"{p} 占比 {pct:.0f}%（参考 {lo}-{hi}%，容忍带 {lo_t}-{hi_t}% → {status}）",
                       ok, 0.4))
    return {"id": "PRI", "score": score, "checks": checks, "issues": issues}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def score_prd(text: str, profile: Profile) -> dict:
    req_rows = _extract_req_rows_from_text(text, profile)

    results = [
        score_cmp(text, profile),
        score_ver(text, req_rows, profile),
        score_con(text, req_rows, profile),
        score_trc(text, req_rows, profile),
        score_clr(text, req_rows, profile),
        score_pri(req_rows, profile),
    ]

    total = round(sum(r["score"] * DIM_BY_ID[r["id"]]["weight"] for r in results), 1)
    passed = total >= PASS_THRESHOLD

    ref_gq5 = None
    fm = re.search(r"---\n(.*?)\n---", text, re.S)
    if fm:
        m = re.search(r'"GQ-5"\s*:\s*{[^}]*"score"\s*:\s*([0-9.]+)', fm.group(1))
        if not m:
            m = re.search(r"GQ-5:\s*\n\s*result:.*?\n\s*score:\s*([0-9.]+)", fm.group(1), re.S)
        if m:
            ref_gq5 = float(m.group(1))

    return {
        "schema": "prd-rate/v1",
        "total_score": total,
        "passed": passed,
        "threshold": PASS_THRESHOLD,
        "ref_gq5": ref_gq5,
        "req_count": len(req_rows),
        "priority_distribution": dict(Counter(
            _priority_of_row(r, profile) for r in req_rows if _priority_of_row(r, profile)
        )),
        "profile": profile.to_dict(),
        "dimensions": results,
    }


def _format_text(result: dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("PRD 六维质量评分（GQ-5）")
    lines.append("=" * 60)
    lines.append(f"需求条目数: {result['req_count']}")
    lines.append(f"优先级分布: {result['priority_distribution']}")
    verdict = "PASS" if result["passed"] else "FAIL"
    lines.append(f"加权总分: {result['total_score']}/100  [{'>=%.0f' % result['threshold']} 门限]  → {verdict}")
    if result.get("ref_gq5"):
        lines.append(f"对账: PRD frontmatter GQ-5 = {result['ref_gq5']} "
                     f"({'一致' if abs(result['total_score'] - result['ref_gq5']) <= 5 else '有偏差，见下方明细'})")
    lines.append("-" * 60)
    lines.append("已识别形态: 锚点=%s | 优先级=%s" % (
        result.get("profile", {}).get("anchor_pattern", "?"),
        "/".join(result.get("profile", {}).get("priority_words", []))
    ))
    lines.append("-" * 60)
    for d in result["dimensions"]:
        dim = DIM_BY_ID[d["id"]]
        lines.append(f"[{d['id']}] {dim['name']}({dim['en']}) 权重{dim['weight']} 得分 {d['score']}")
        for c in d["checks"]:
            mark = "✔" if c[1] else "✘"
            lines.append(f"    {mark} {c[0]}")
        for issue in d["issues"]:
            lines.append(f"    ⚠ {issue}")
    lines.append("=" * 60)
    if result["passed"]:
        lines.append("结论: 通过（≥75）")
    else:
        lines.append("结论: 未通过（<75）— 按上方 ✘ 项逐条修复")
    return "\n".join(lines)


def _format_markdown(result: dict) -> str:
    lines = ["# PRD 六维质量评分（GQ-5）", ""]
    lines.append(f"- **需求条目数**: {result['req_count']}")
    lines.append(f"- **优先级分布**: {result['priority_distribution']}")
    verdict = "✅ **通过**" if result["passed"] else "❌ **未通过**"
    lines.append(f"- **加权总分**: **{result['total_score']}/100**（门限 ≥{result['threshold']}）{verdict}")
    if result.get("ref_gq5"):
        agree = "一致" if abs(result["total_score"] - result["ref_gq5"]) <= 5 else "有偏差"
        lines.append(f"- **对账**: PRD frontmatter GQ-5 = {result['ref_gq5']}（{agree}）")
    prof = result.get("profile", {})
    lines.append(f"- **识别形态**: 锚点 `{prof.get('anchor_pattern')}` · 优先级 `{'/'.join(prof.get('priority_words', []))}` · 章节数 {len(prof.get('chapters', []))}")
    lines.append("")
    lines.append("| 维度 | 权重 | 得分 | 加权贡献 | 检查项 |")
    lines.append("|---|---|---|---|---|")
    for d in result["dimensions"]:
        dim = DIM_BY_ID[d["id"]]
        checks_ok = sum(1 for c in d["checks"] if c[1])
        lines.append(f"| **{d['id']}** {dim['name']} | {dim['weight']} | {d['score']} | "
                     f"{round(d['score'] * dim['weight'], 1)} | {checks_ok}/{len(d['checks'])} 通过 |")
    lines.append("")
    lines.append("## 明细与改进建议")
    for d in result["dimensions"]:
        lines.append(f"### {d['id']} {DIM_BY_ID[d['id']]['name']}")
        for c in d["checks"]:
            lines.append(f"- {'✅' if c[1] else '❌'} {c[0]}")
        for issue in d["issues"]:
            lines.append(f"- ⚠️ {issue}")
    return "\n".join(lines)


def _load_overrides(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"错误: 配置文件不存在 {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"错误: 配置文件不是合法 JSON: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="PRD 六维质量打分（GQ-5，自适应形态）")
    ap.add_argument("prd_file", help="PRD markdown 文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--report", metavar="OUT.md", help="输出 Markdown 报告到文件")
    ap.add_argument("--config", metavar="CFG.json", help="用 JSON 配置覆盖探测结果")
    ap.add_argument("--show-profile", action="store_true", help="只显示探测到的形态并退出")
    args = ap.parse_args()

    try:
        with open(args.prd_file, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        print(f"错误: 文件不存在 {args.prd_file}", file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        try:
            with open(args.prd_file, "r", encoding="utf-8-sig") as f:
                text = f.read()
        except Exception:
            print(f"错误: 无法读取文件（编码）{args.prd_file}", file=sys.stderr)
            return 1

    overrides = _load_overrides(args.config) if args.config else None
    profile = Profile(text, overrides)

    if args.show_profile:
        print(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2))
        return 0

    result = score_prd(text, profile)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(_format_markdown(result))
        print(f"报告已写入: {args.report}")
    elif args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_format_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
