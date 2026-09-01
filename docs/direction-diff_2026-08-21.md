# H-01 方向差异清单

日期：2026-08-21
依据：主 PRD v0.37-draft 的 1.2、4.10、4.11、6.4；执行文档 v0.3 的 A-G 产物。

## 结论

主 PRD 的产品目标没有方向性错误，不改产品愿景、LLM 四个介入点、策略/话术双 Prompt、独立平台边界或人工 Gate 原则。需要纠正的是实现层没有完整落实这些约束。

## 差异与处理

| PRD 约束 | 当前实现证据 | 处理决定 | 回归标准 |
|---|---|---|---|
| 目标专家确认后才进入 LLM 提炼（6.4、4.10.1） | task 的 `target_expert` 可为空，D-02/D-03/D-04 仍可直接调用 | H-03：G1 approved 成为提炼前置条件 | 无 G1 时提炼返回 `gate_required` |
| Graph/Fragment/Overlay 是一等知识对象（1.3、5.5） | `knowledge` 主要只有 `strategy_node`/`script_fragment`，边和触发仅在接口返回值中 | H-02：持久化 graph/fragment/overlay/node/edge/trigger/script 关系 | 边/触发可查询、审核、版本化、回链 |
| 抽象对象必须回链直接证据（5.6、4.11.3） | 历史对象有 `utterance_id`，新对象已有规范化函数但未统一校验 | H-02：写入前规范化并阻断未知引用 | 新对象只接受可解析的 evidence ID |
| Prompt 只能来自 approved Graph（4.11.3/4.11.4） | E-02 主要把 approved strategy nodes 传给 LLM，未传 Edge/Trigger | H-04：编译输入包含已批准 Graph 结构，缺结构阻断 | 缺 Edge/Trigger 不生成完整流程 Prompt |
| G1→G2→G3→G4→G5 是发布门槛（7.2、6.4） | Gate 记录可创建，但接口未强制前置关系；release 可直接创建 | H-03/H-05：实际校验 Gate transition，release 强制 G5 | 缺 Gate 返回明确错误，不静默发布 |
| Graph/Script 应支持结构 diff（4.10.2、4.4） | 当前 diff 主要是同类型文本 diff | H-05：补充结构关系和映射差异字段 | diff 展示节点/边/触发/话术关系变化 |
| D3 默认脱敏（7.3、4.10.2） | 已在 G-02 修正 snapshot/utterance API | 保留现有修复 | D3 API 不返回原文正文 |

## 保留的前置成果

- A 阶段独立平台和 UI 骨架保留。
- B 阶段不可变来源、TXT 解析、source/utterance/evidence 定位保留。
- C 阶段版本、候选对象、变更提案和证据回链基础保留，后续增加兼容层。
- F 阶段暂不继续扩展新业务入口，等待 C/D/E 语义重基线后再调整显示。

## LLM 可用性

2026-08-21 对 `/api/llm-config/test` 的探测返回 `429 DAILY_LIMIT_EXCEEDED`。H 阶段结构性修正不以真实模型成功为前提；真实提炼回归在 H-06 记录为 `llm_unavailable`，不得用规则拼接伪装成通过。
