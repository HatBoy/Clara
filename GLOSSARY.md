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
- **规范行（canonical row）/ 行口径编解码（row_codec）**：`services/row_codec.py`
  定义的同步域唯一行形态——datetime 族 isoformat 字符串 / JSON 列对象 /
  非空布尔 None→False / 列集过滤 / pk 规范序 / 冻结 sha1 字节形态（I1-I6
  不变量，docstring 可执行对照 test_row_codec.py）。`writable_row` 产物只可
  即时喂 execute（含 null() 构造）。策略（SYNC_TABLES / NATURAL_KEYS / 合并
  真值表 / 写库编排）住 sync_engine，不属编解码——策略变更与口径变更的速率
  和失败形态不同。
- **SSE 运行时（sse_kit）**：`services/sse_kit.py`——连接生命周期的 seam：
  帧（sse_frame/CONNECTED/PING）、订阅流（subscribe_stream）、线程桥
  （StreamDone/StreamFailure 信封 + deliver + stream_worker）、强引用池
  （spawn）。kit 不懂业务帧语义、不做错误翻译（降级矩阵是各端点职责）。
  前端镜像：`src/lib/sse.ts streamSsePost`（POST 分帧单源，刻意无重连——
  请求作用域流重发会重复落用户消息）。
- **共享工具面（agents/common）**：`agents/common.py`——子图与主图节点的
  公共库（时间文本解析 / 标题归一相似 / 草案钳制 / 问询文案 / emitter /
  排期钳制常量 / bump_day_load）。nodes.py 的同名下划线名是兼容锚点，
  新代码一律直连 common。
- **排期缓冲口径（engine 常量单源）**：`agents/engine.py` 顶部四常量——
  TAIL_BUFFER_MIN（决策层批内虚拟延长）/ CHAIN_TAIL_BUFFER_MIN（长会链尾/
  差旅落地）/ MEETING_TAIL_MIN / MEETING_HEAD_MIN（单场会议前后，会前与
  前端虚拟块同口径）。改值 = 排期行为面变化，须评估全入口。
