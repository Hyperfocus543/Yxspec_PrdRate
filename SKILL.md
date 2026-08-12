---
name: prd-rate
description: "PRD 六维质量打分（GQ-5 体系，源自 yxspec prd-generation-pipeline）。对任意 PRD markdown 自动执行 GQ-5 六维评分（CMP完整性/VER可验证性/CON一致性/TRC追溯性/CLR明确性/PRI优先级），输出加权总分、逐维度明细与改进建议。当用户要求给 PRD 打分、PRD 质量评分、六维评分、产品需求文档评分、PRD 评审量化、GQ-5 时使用。触发词：给PRD打分、PRD评分、六维评分、PRD质量评估、GQ-5、打分系统。"
triggers:
  - "给PRD打分"
  - "PRD评分"
  - "六维评分"
  - "PRD质量评估"
  - "GQ-5"
  - "打分系统"
---

# PRD 六维质量打分（GQ-5）

> 源自 **yxspec prd-generation-pipeline** 的 GQ-5 门控，独立成可复用的评分工具。对任意 PRD markdown 自动执行六维评分，量化 PRD 质量。

## 何时用

- 需要量化评估一份 PRD（产品需求规格书）的质量
- 在提交 PRD 评审前自检
- 对比多份 PRD 版本质量
- 作为门禁判定（加权分 ≥75 通过）

## 六维评分体系（GQ-5）

加权平均 ≥75 分方可通过。六维权重与细则（权威定义见 yxspec `GQ-rules.yaml`）：

| 维度 | ID | 权重 | 检查内容 |
|---|---|---|---|
| **完整性** Completeness | CMP | 0.20 | MANDATORY 章节全覆盖、占位符 `{{}}` 清零、强制附录存在 |
| **可验证性** Verifiability | VER | 0.20 | 模糊词清零（快速/稳定/良好等）、性能指标全量化、验收准则可直接转测试用例 |
| **一致性** Consistency | CON | 0.20 | 条目间无逻辑矛盾、优先级标注一致、接口引用一致 |
| **追溯性** Traceability | TRC | 0.20 | 无源项=0、Must 条目 100% 有原文来源、编号连续可追溯 |
| **明确性** Clarity | CLR | 0.15 | 需求描述清晰、术语表覆盖专业术语、缩写展开 |
| **优先级** Priority | PRI | 0.05 | 优先级分配合理，Must/Should/May 比例适当（40-60/25-40/10-20） |

## 用法

```bash
# 默认评分（自动探测 PRD 形态：锚点/章节/优先级/来源）
python scripts/score_prd.py project/specs/prd/prd-aima_bcm-2026.md

# 输出 JSON（供脚本/流水线消费）
python scripts/score_prd.py prd.md --json

# 输出 Markdown 报告
python scripts/score_prd.py prd.md --report report.md

# 查看探测到的形态（不打分）
python scripts/score_prd.py prd.md --show-profile

# 结构特殊时用配置覆盖探测结果
python scripts/score_prd.py prd.md --config prd-rate.config.json
```

## 自适应（Adaptive）

**零配置即可对不同模板的 PRD 打分**。脚本自动探测：
- **锚点形态**：`<a id="REQ-...">` / `<a id="FR-...">` / 自定义 ID 正则
- **优先级体系**：`must/should/could` 或 `P0/P1/P2` 或 `high/medium/low`
- **章节体系**：按 PRD 实际的一级/二级标题判完整性（非硬套固定章节名）
- **来源路径**：从现有链接推断来源目录特征
- **表格列序**：从表头推断描述/优先级/验收/来源列位置

结构特殊时可 `--config` 覆盖（JSON，字段见脚本头注释）：章节名、锚点正则、优先级词表、来源路径、列序、模糊词等。

> **边界**：打分面向"表格化结构化 PRD"（含 `<a id>` 锚点 + 需求表格）。纯段落式 PRD（无锚点无表格）可判章节/占位符等维度，但需求条目数为 0（CON/TRC 无从判定），属设计边界。

## 输出

- **加权总分**（0-100，≥75 通过）
- **逐维度得分**（各维度 0-100 + 权重贡献）
- **明细**：每维度的检查项命中/未命中、检测到的具体问题行
- **改进建议**：未通过维度的可操作修复项

## 对账说明

本 skill 的 `score_prd.py` 是 **GQ-5 的确定性量化代理**——在 yxspec 流水线中，GQ-5 由 generate-worker agent 语义评分 + `prd_verify.py generate` 机械校验共同完成。本工具将六维 checklist 提炼为可计数的自动度量，逼近同一评分口径（在爱玛 X1 BCM PRD 实测对账 GQ-5=91.0 一致）。若 PRD frontmatter 已带 `_meta.gq_results.GQ-5`，脚本会输出并对比。

## 文件结构

```
prd-rate/
├── SKILL.md              # 本文件
├── scripts/
│   └── score_prd.py      # 六维评分脚本（纯 Python 标准库，无第三方依赖）
└── README.md             # 仓库说明
```
