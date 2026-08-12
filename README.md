# Yxspec_PrdRate — PRD 六维质量打分 Skill

> 对任意 PRD markdown 自动执行 **GQ-5 六维评分**（CMP/VER/CON/TRC/CLR/PRI），
> 输出加权总分、逐维度明细与改进建议。源自 **yxspec prd-generation-pipeline** 的 GQ-5 门控，独立成可复用工具。

![Python](https://img.shields.io/badge/Python-3.8+-blue)
![Deps](https://img.shields.io/badge/dependencies-0-green)
![Type](https://img.shields.io/badge/type-Claude%20Code%20Skill-purple)

---

## 这是什么

一份 PRD（产品需求规格书）在交给研发/测试前，如何量化它的质量？Yxspec_PrdRate 把 yxspec 流水线中**由 agent 语义判断的 GQ-5 六维评分**，提炼成**确定性、可复现的自动打分工具**：

| 维度 | 含义 | 权重 |
|---|---|---|
| **CMP** 完整性 Completeness | MANDATORY 章节全覆盖、占位符清零、强制附录存在 | 0.20 |
| **VER** 可验证性 Verifiability | 模糊词清零、验收准则可直接转测试用例 | 0.20 |
| **CON** 一致性 Consistency | 优先级枚举合法、REQ-ID 无重复 | 0.20 |
| **TRC** 追溯性 Traceability | 无源项=0、来源带行号、编号连续 | 0.20 |
| **CLR** 明确性 Clarity | 术语表覆盖、表述结构化、数值锚点 | 0.15 |
| **PRI** 优先级 Priority | must/should/could 分布合理（含容忍带） | 0.05 |

**加权平均 ≥75 分 = 通过**（与 yxspec GQ-rules.yaml 门限一致）。

---

## 安装（一句话）

```bash
mkdir -p ~/.claude/skills && git clone https://github.com/Hyperfocus543/Yxspec_PrdRate.git ~/.claude/skills/prd-rate
```

安装后 Claude Code 会自动识别为 `prd-rate` skill。直接对 Claude 说 **"给这份 PRD 打分"** 即可触发，或手动运行脚本：

```bash
python3 ~/.claude/skills/prd-rate/scripts/score_prd.py <你的PRD.md>
```

> 零依赖：纯 Python 标准库，无需 pip 安装任何包。

---

## 用法

### 文本报告（人读）

```bash
python3 scripts/score_prd.py project/specs/prd/prd-aima_bcm-2026.md
```

输出：加权总分 + 结论（PASS/FAIL）+ 每维度得分 + 每检查项 ✔/✘ 明细 + 改进建议。

### JSON（流水线/脚本消费）

```bash
python3 scripts/score_prd.py prd.md --json
```

```json
{
  "schema": "prd-rate/v1",
  "total_score": 96.2,
  "passed": true,
  "threshold": 75.0,
  "ref_gq5": 91.0,
  "req_count": 278,
  "priority_distribution": {"must": 247, "should": 29, "could": 2},
  "dimensions": [ ... ]
}
```

### Markdown 报告（文档化）

```bash
python3 scripts/score_prd.py prd.md --report report.md
```

---

## 如何工作

1. **解析**：扫描 PRD 全文，识别 `<a id="REQ-XXXX">` 锚点行 + 紧随表格行 = 一条需求（精准排除章节编号范围/统计摘要的误识别）。
2. **六维度量**：对每维执行确定性检查（章节覆盖、模糊词正则、优先级枚举、来源行号、编号连续性等）。
3. **加权计分**：按 GQ-rules.yaml 权威权重加权，≥75 判定通过。
4. **对账**：若 PRD frontmatter 已带 `_meta.gq_results.GQ-5`，输出自动分与自评分对比。

### 对账精度

在爱玛 X1 BCM PRD（278 条需求）实测：

| 维度 | 自动分 | 原自评 | 差 |
|---|---|---|---|
| CMP | 92.3 | 95 | -2.7 |
| VER | 100 | 90 | +10 |
| CON | 100 | 92 | +8 |
| TRC | 100 | 88 | +12 |
| CLR | 89.2 | 90 | -0.8 |
| PRI | 86.2 | 85 | +1.2 |
| **总分** | **96.2** | **91.0** | +5.2 |

> 自动分与 agent 自评分各有侧重：agent 对"表述流畅度/术语完整性"这类语义项更敏感，自动工具对"行号/重复/枚举合法性"这类机械项更严格。**门禁判定（≥75）两者一致**。对账偏差主要由评分口径差异造成，非工具缺陷。

---

## 设计原则

- **零依赖**：纯标准库，单文件，随处可跑。
- **确定性**：同输入恒同输出，可作 CI 门禁。
- **领域友好**：模糊词检测区分"状态枚举值"（0x0正常）与"程度修饰语"（快速/大概），避免车规需求误报；优先级参考区间带容忍带，适配协议/安全强约束产品域（must 占比天然高）。
- **兼容 yxspec**：权重/门限/维度定义与 GQ-rules.yaml 完全一致，可用作 yxspec 流水线的独立复核。

---

## 文件结构

```
prd-rate/
├── SKILL.md              # Claude Code skill 定义（触发词 + 用法）
├── README.md             # 本文件
└── scripts/
    └── score_prd.py      # 六维评分脚本（纯 Python 标准库）
```

---

## 常见问题

**Q: 与 yxspec prd-generation-pipeline 的 GQ-5 是什么关系？**
A: 本工具是其 GQ-5 的**独立量化代理**。yxspec 中 GQ-5 由 generate-worker agent 语义评分 + `prd_verify.py generate` 机械校验共同完成；本工具把六维 checklist 提炼为自动度量，可独立对任意 PRD 打分，也可作为流水线的复核层。

**Q: 为什么我 PRD 的 PRI 分偏低？**
A: 优先级参考区间是通用软件比例（must 40-60%）。嵌入式/协议类产品 must 占比高属正常，工具已设容忍带（must 40-75%）。若仍偏低，检查是否大量需求误标 must。

**Q: 支持中文/英文 PRD？**
A: 支持。章节名/术语表/模糊词检测对中英文均有效。

---

## 作者

林汉飞 | 2026-08-12 | 源自 yxspec PRD 生成工作流（爱玛 X1 BCM 验证项目）
