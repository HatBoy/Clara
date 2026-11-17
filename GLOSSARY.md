# GLOSSARY · 领域词汇表

> 架构审查（2026-11-17 起）沉淀的领域概念。新概念在深化落地时登记；术语与
> docs/ 模块文档冲突时以本表为准并回改文档。架构词汇（module / interface /
> depth / seam / adapter / leverage / locality）见 codebase-design 体系，不入本表。

## 后端

- **注入上下文（InjectedRules）**：`services/ai_data.injected_rules(db)` 的产出——
  记忆/人格注入配方的单源形态：core（铁律段）+ ref（参考段）+ all（合并单列表，
  `state["memory_rules"]` 契约）。顺序约束（settings 读取 → personality_rules →
  format_memory_lines 双段）全部藏在其 implementation 内；`with_personality=False`
  是「草案口吻由引擎承载」的夜间预排语义（有意降格），`settings=` 供并行只读段
  防竞态传入。
- **AI 可用性闸门（check_ai / AiGate）**：`services/llm.check_ai(db)` 的产出——
  「ai_enabled + 零激活」两条件判定与规范文案的单源。reason 域 {"disabled",
  "no_model"}；响应机制（SSE 帧 / HTTPException / 静默跳过 / 渠道 hint）是各
  调用方的合法差异，不属闸门。纯模型探测（不判 ai_enabled）不是闸门，别混用。
