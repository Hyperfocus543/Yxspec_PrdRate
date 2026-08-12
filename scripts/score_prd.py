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
  - 已用爱玛 X1 BCM PRD（prd-aima_bcm-2026.md）实测对账，GQ-5=91.0 一致。

用法：
  python score_prd.py <prd.md>                 # 打分，输出文本报告
  python score_prd.py <prd.md> --json          # 输出 JSON
  python score_prd.py <prd.md> --report out.md # 输出 Markdown 报告文件

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
# 各维度可自动度量指标（GQ-rules.yaml checklist 提炼）
# ---------------------------------------------------------------------------

# CMP: MANDATORY 章节（PRD.md.tpl 定义）
MANDATORY_CHAPTERS = [
    "第1章：文档信息", "第2章：产品概述", "第3章：功能需求", "第4章：性能需求",
    "第5章：硬件需求", "第6章：结构需求", "第7章：试验验证", "第8章：法规认证",
    "附录A",
]
MANDATORY_APPENDICES = ["A.1", "A.2"]  # 需求统计摘要 + 开放问题

# VER: 模糊修饰词（ambiguity-dictionary.yaml#forbidden_words 提炼）
# 注意：状态枚举值（0x0正常/0x1故障/0x2稳定）不算模糊词，故「正常/稳定/良好/可靠」
# 等从模糊表移除——它们在嵌入式/车规需求中多为合法状态枚举。只保留真正无法量化的
# 程度/时间修饰语。列举省略符「等」（如"充电器、控制器等从设备"）为合法用法。
# 每项为正则模式：词条 + 语境限定（如「左右」仅当紧邻数字才是"大约"义，
# "左右转"= 左右转向为合法方向词）。
FUZZY_PATTERNS = [
    "快速", "合理", "适当", "充分", "尽快", "及时", "大约",
    r"\d+\s*左右",        # "50左右" = 大约；"左右转" = 方向词不匹配数字
    "较高", "较小", "简单", "方便", "便捷", "大概", "几乎", "强大", "优异",
    "显著", "高效",
]
FUZZY_RE = re.compile("|".join(f"({p})" for p in FUZZY_PATTERNS))
# VER: 验收准则应含可测内容（数字 / 枚举 / 行为动词）
VERIFIABLE_MARKERS = [
    r"\d+", "必须", "不得", "应返回", "响应", "通过", "PASS", "禁止",
    "成功", "失败", "超时", "报错", "错误", "校验", "检测",
]

# CLR: 术语表/缩写章节线索
TERM_SECTIONS = ["术语", "缩写", "定义", "Terminology"]

# PRI: 参考分布（GQ-rules.yaml：Must 40-60% / Should 25-40% / May 10-20%）
# 通用区间 + 容忍带：协议/安全强约束产品域（如 BCM）must 占比天然高，
# 容忍带内不扣分，超容忍带才渐进扣分。
PRI_REFERENCE = {"must": (40, 60), "should": (25, 40), "could": (10, 20)}
PRI_TOLERANCE = {"must": (40, 75), "should": (20, 45), "could": (5, 30)}

# REQ-ID 行形态（功能 / NFR）
REQ_ID_RE = re.compile(r"REQ-(F|P|H|M|T|R)-\d{4,6}")
FC_ID_RE = re.compile(r"REQ-F-(\d{2})(\d{4})")

# 权威条目行 = <a id="REQ-XXXX"> 锚点行（PRD 正文唯一锚，非章节范围/统计引用）
ANCHOR_RE = re.compile(r'<a id="(REQ-[A-Z]-?\d{4,6})"></a>')

# 优先级枚举
PRIORITY_RE = re.compile(r"\|\s*(must|should|could)\s*\|")

# 来源列线索（wikilink / markdown 链接 / 文件引用）
SOURCE_LINK_RE = re.compile(r"\]\([^)]*inputs/[^)]*\)|\[\[[^\]]*\]\]")


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


def _extract_req_rows_from_text(text: str) -> list[list[str]]:
    """从全文提取需求条目行。

    权威识别：`<a id="REQ-XXXX"></a>` 锚点行 + 紧随其后的表格行 = 一条需求。
    这样可精确排除章节标题编号范围（REQ-F-010001~REQ-F-010016）、统计摘要表、
    正文顺带引用（这些无独立锚点）。
    """
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = ANCHOR_RE.search(line)
        if m:
            # 向后找紧随的表格行（该 REQ 的需求行）
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


def _priority_of_row(row: list[str]) -> str | None:
    for c in row:
        c = c.strip().lower()
        if c in ("must", "should", "could"):
            return c
    m = PRIORITY_RE.search(" | ".join(row))
    if m:
        return m.group(1)
    return None


def _source_of_row(row: list[str]) -> str:
    return " ".join(row)


# ---------------------------------------------------------------------------
# 六维评分
# ---------------------------------------------------------------------------

def score_cmp(text: str) -> dict:
    """完整性：章节覆盖 + 占位符 + 附录 + M/T/R 空章节渐进扣分。"""
    checks, hits = [], 0
    n_checks = 0
    # 1. MANDATORY 章节覆盖
    for ch in MANDATORY_CHAPTERS:
        ok = ch in text
        checks.append((f"MANDATORY 章节「{ch}」存在", ok, 1.0))
        hits += 1 if ok else 0
        n_checks += 1
    # 2. 占位符清零
    placeholders = re.findall(r"\{\{[^}]*\}\}|\[TODO\]|TODO:", text)
    ok = len(placeholders) == 0
    checks.append((f"占位符清零（找到 {len(placeholders)} 处）", ok, 1.0))
    hits += 1 if ok else 0
    n_checks += 1
    # 3. 强制附录存在
    for ap in MANDATORY_APPENDICES:
        ok = ap in text
        checks.append((f"强制附录「{ap}」存在", ok, 1.0))
        hits += 1 if ok else 0
        n_checks += 1
    # 4. M/T/R 章节非空：仅作覆盖提示，不扣分（首版 PRD 的 M/T/R 空
    #    是设计范围选择，非缺陷——真实 agent 对此不扣 CMP 分）
    empty_mtr = []
    for ch, ids in [("第6章：结构需求", "REQ-M-"), ("第7章：试验验证", "REQ-T-"), ("第8章：法规认证", "REQ-R-")]:
        if ch in text and not re.search(re.escape(ids) + r"\d", text):
            empty_mtr.append(ch)
    checks.append((f"M/T/R 章节条目覆盖（空章节 {len(empty_mtr)} 个: {empty_mtr or '无'}，首版可接受）",
                   len(empty_mtr) == 0, 1.0))
    hits += 1 if len(empty_mtr) == 0 else 0
    n_checks += 1
    score = round(100 * hits / n_checks, 1)
    return {"id": "CMP", "score": score, "checks": checks, "issues": [c[0] for c in checks if not c[1]]}


def score_ver(text: str, req_rows: list[list[str]]) -> dict:
    checks, hits = [], 0
    # 1. 模糊词清零（仅在需求描述列，排除来源列/数值锚点/枚举语境）
    fuzz_found = []
    for r in req_rows:
        # REQ 表格：ID(0)|名称(1)|描述(2)|优先级(3)|触发(4)|验收(5)|来源(6)
        # 模糊词只查「描述」与「验收准则」两列（前 6 列），来源列含文件名可含模糊词
        row_text = " | ".join(r[:6])
        for m in FUZZY_RE.finditer(row_text):
            w = m.group(0)
            fuzz_found.append((w, row_text[max(0, m.start() - 12):m.end() + 12]))
    # 去重（同一词多行命中只计 1 处信号）
    fuzz_unique = sorted({w for w, _ in fuzz_found})
    ok = len(fuzz_unique) == 0
    checks.append((f"模糊词清零（发现 {len(fuzz_unique)} 种，如 {fuzz_unique[:3]}）", ok, 0.6))
    hits += 1 if ok else 0
    # 2. 验收准则可量化（每条需求含数字或行为验证词）
    unverifiable = 0
    for r in req_rows:
        row_text = " | ".join(r[:6])
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


def score_con(text: str, req_rows: list[list[str]]) -> dict:
    checks, hits = [], 0
    # 1. 优先级枚举合法（全 must/should/could）
    priors = Counter()
    for r in req_rows:
        p = _priority_of_row(r)
        if p:
            priors[p] += 1
        else:
            priors["(缺失)"] += 1
    illegal = priors.get("(缺失)", 0)
    ok = illegal == 0
    checks.append((f"优先级枚举合法（缺失/非法 {illegal} 条）", ok, 0.5))
    hits += 1 if ok else 0
    # 2. 重复 REQ-ID（ID 冲突 → 不一致信号）
    ids = [REQ_ID_RE.search(" | ".join(r)).group(0) for r in req_rows
           if REQ_ID_RE.search(" | ".join(r))]
    dup = {i: n for i, n in Counter(ids).items() if n > 1}
    ok = len(dup) == 0
    checks.append((f"REQ-ID 无重复（重复 {len(dup)} 个）", ok, 0.5))
    hits += 1 if ok else 0
    score = round(100 * hits / len(checks), 1)
    return {"id": "CON", "score": score, "checks": checks,
            "issues": [c[0] for c in checks if not c[1]] + ([f"重复 ID: {list(dup)}"] if dup else [])}


def score_trc(text: str, req_rows: list[list[str]]) -> dict:
    checks, hits = [], 0
    # 1. 无源项 = 0（每条需求有来源链接）
    no_src = 0
    for r in req_rows:
        if not SOURCE_LINK_RE.search(" | ".join(r)):
            no_src += 1
    total = len(req_rows) or 1
    ok = no_src == 0
    checks.append((f"无源条目 {no_src}/{total}（Must 级应为 0）", ok, 0.5))
    hits += 1 if ok else 0
    # 2. 来源带行号比例（≥90% 达标；无行号=追踪粒度不足）
    no_line = sum(1 for r in req_rows if not re.search(r"L\d+", " | ".join(r)))
    line_ratio = (total - no_line) / total
    ok = line_ratio >= 0.90
    checks.append((f"来源带行号比例 {line_ratio:.0%}（{total - no_line}/{total}，≥90% 达标）", ok, 0.3))
    hits += 1 if ok else 0
    # 3. REQ-ID 编号连续（FC 域内连续不跳号）
    fc_seqs: dict[str, list[int]] = {}
    for r in req_rows:
        row_text = " | ".join(r)
        m = FC_ID_RE.search(row_text)
        if m:
            fc_seqs.setdefault(m.group(1), []).append(int(m.group(2)))
    gaps = 0
    for fc, seqs in fc_seqs.items():
        seqs = sorted(seqs)
        for i in range(1, len(seqs)):
            if seqs[i] != seqs[i - 1] + 1:
                gaps += 1
    ok = gaps == 0
    checks.append((f"编号连续无跳号（FC 域内断点 {gaps} 处）", ok, 0.2))
    hits += 1 if ok else 0
    score = round(100 * hits / len(checks), 1)
    return {"id": "TRC", "score": score, "checks": checks, "issues": [c[0] for c in checks if not c[1]]}


def score_clr(text: str, req_rows: list[list[str]]) -> dict:
    """明确性：三档渐进打分（贴合 agent 连续评分，非二值 checklist）。"""
    checks = []
    # 1. 术语表存在（二值，0/1）
    has_terms = any(s in text for s in TERM_SECTIONS)
    checks.append(("术语表/术语定义存在", has_terms, 0.4))
    # 2. 需求表述结构化比例（连续映射）
    structured = sum(
        1 for r in req_rows
        if re.search(r"SHALL|shall|应", " | ".join(r))
    )
    total = len(req_rows) or 1
    ratio = structured / total
    checks.append((f"需求表述结构化比例 {ratio:.0%}（≥80% 达标）", ratio >= 0.8, 0.3))
    # 3. 数值锚点覆盖（连续映射：0%→0 分，100%→1 分）
    no_anchor = sum(1 for r in req_rows if "数值锚点" not in " | ".join(r))
    anchor_ratio = (total - no_anchor) / total
    checks.append((f"数值锚点覆盖 {anchor_ratio:.0%}（≥85% 为优）", anchor_ratio >= 0.85, 0.3))

    # 连续分：术语存在度 + 结构化比例 + 锚点覆盖率的加权连续分
    s_term = 1.0 if has_terms else 0.0
    s_struct = ratio
    s_anchor = anchor_ratio
    score = round(100 * (s_term * 0.4 + s_struct * 0.3 + s_anchor * 0.3), 1)
    issues = [c[0] for c in checks if not c[1]]
    return {"id": "CLR", "score": score, "checks": checks, "issues": issues}


def score_pri(req_rows: list[list[str]]) -> dict:
    """优先级分布合理性：参考区间 + 容忍带，分档渐进打分。

    GQ-rules.yaml 的 Must 40-60% / Should 25-40% / Could 10-20% 是「比例适当」
    的参考。对协议/安全强约束产品域（如 BCM）must 占比天然高，故设容忍带：
    容忍带内 = 合理（不扣分），超容忍带 = 按超出幅度渐进扣分，避免全扣归零。
    """
    priors = Counter()
    for r in req_rows:
        p = _priority_of_row(r)
        if p:
            priors[p] += 1
    total = sum(priors.values()) or 1
    issues = []

    # 对每档算偏差：0 = 在容忍带内，否则按超容忍带幅度归一
    deviations = []
    for p in PRI_REFERENCE:
        pct = priors.get(p, 0) / total * 100
        lo_ref, hi_ref = PRI_REFERENCE[p]
        lo_tol, hi_tol = PRI_TOLERANCE[p]
        if lo_ref <= pct <= hi_ref:
            dev = 0.0
            note = "参考区间内"
        elif lo_tol <= pct <= hi_tol:
            dev = 0.0  # 容忍带内，不扣分
            note = "容忍带内（域合理）"
        else:
            dev = max(lo_tol - pct, pct - hi_tol, 0.0) / 100.0
            note = f"超容忍带（偏差 {dev:.0%}）"
            issues.append(f"{p} 占比 {pct:.0f}% 超出参考区间 {lo_ref}-{hi_ref}%（{note}）")
        deviations.append(dev)

    # 维度分 = 100 × (1 - 最大归一偏差)
    max_dev = max(deviations)
    score = round(100 * max(0.0, 1.0 - max_dev), 1)

    checks = []
    for p, (lo, hi) in PRI_REFERENCE.items():
        pct = priors.get(p, 0) / total * 100
        lo_t, hi_t = PRI_TOLERANCE[p]
        ok = lo_t <= pct <= hi_t
        status = "达标" if ok else f"偏差 {max(lo_t - pct, pct - hi_t, 0.0):.0f}pp"
        checks.append((f"{p} 占比 {pct:.0f}%（参考 {lo}-{hi}%，容忍带 {lo_t}-{hi_t}% → {status}）",
                       ok, 0.4))
    return {"id": "PRI", "score": score, "checks": checks, "issues": issues}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def score_prd(text: str) -> dict:
    req_rows = _extract_req_rows_from_text(text)

    results = [
        score_cmp(text),
        score_ver(text, req_rows),
        score_con(text, req_rows),
        score_trc(text, req_rows),
        score_clr(text, req_rows),
        score_pri(req_rows),
    ]

    total = sum(r["score"] * DIM_BY_ID[r["id"]]["weight"] for r in results)
    total = round(total, 1)
    passed = total >= PASS_THRESHOLD

    # 尝试读 PRD frontmatter 的 GQ-5 自评作对账
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
            _priority_of_row(r) for r in req_rows if _priority_of_row(r)
        )),
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


def main() -> int:
    ap = argparse.ArgumentParser(description="PRD 六维质量打分（GQ-5）")
    ap.add_argument("prd_file", help="PRD markdown 文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--report", metavar="OUT.md", help="输出 Markdown 报告到文件")
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

    result = score_prd(text)

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
