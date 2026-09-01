# AI 电话评价 Agent

从猎头访谈记录中提炼“策略 + 路由条件 + 专家原话”Graph，并编译电话执行 Prompt 与评价 Prompt。

> 当前版本：v0.53 开发交接基线（2026-09-01）。项目可运行、可继续开发，但尚未达到稳定生产状态。问题边定位已经实现并做过局部真实验收；边条件 Gate 的跨任务稳定性以及 Graph 的布局、缓存、重排和密集图可读性仍需继任者重点复测。

## 快速开始

```powershell
.\start.ps1
```

打开 http://127.0.0.1:8897/

LLM API Key 不保存在项目中。需要真实模型调用时，先在启动后端的终端设置 `AI_CALL_EVAL_API_KEY`；详见 `backend/README.md`。

## 技术栈

- 后端：Python 3.13 + http.server（单文件，无框架）
- 前端：原生 HTML/CSS/JS + Cytoscape.js（Graph 可视化）
- 数据：JSON 文件存储（无数据库）
- LLM：OpenAI 兼容 API（当前运行配置为 gpt-5.6，可切换）

## 核心流程

1. 导入 TXT → 解析发言 → 确认目标专家（G1 Gate）
2. LLM 证据分类（strategy/script/context/meta）
3. LLM 策略结构提炼（nodes/edges/triggers）
4. LLM 话术映射（direct_script/partial_script/strategy_only）
5. Gate 审核（G1→G2→G3→G4→G5）
6. 编译 Prompt → 发布包 → 交付

建议阅读顺序：`交接的项目上下文.md` → `AGENT_ONBOARDING.md` → `docs/AI电话评价Agent_主PRD.md` → `docs/AI电话评价Agent_MVP执行文档_v0.4.md`。

## 当前已知风险

- 单条边显示 `confirmed` 不代表整个来源节点可执行；同一条件指向不同目标仍会阻断整图批准。v0.53 已在问题提示中列出来源、条件、全部目标和逐边定位按钮。
- Graph 展示不是稳定产物。不同布局模式、布局画像是否存在/过期、候选内容变化、人工坐标和浏览器缓存都可能影响加载结果；密集图仍可能出现边交叉、遮挡或初始视野过小。
- 当前数据和候选 Graph 都属于实验快照。未经人工完成 G3/G5 与真实回归，不得直接用于正式外呼。

## 数据与凭据

本私有仓库包含继任开发需要的项目资料、运行数据、参考材料和历史日志。API Key 不进入 Git；克隆项目后通过 `AI_CALL_EVAL_API_KEY` 注入模型凭据。
