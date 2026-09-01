# AI 电话评价 Agent

从猎头访谈记录中提炼策略流程图和话术，编译为电话评价 Prompt。

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

详见 `docs/` 下的 PRD 和执行文档。

## 数据与凭据

本私有仓库包含继任开发需要的项目资料、运行数据、参考材料和历史日志。API Key 不进入 Git；克隆项目后通过 `AI_CALL_EVAL_API_KEY` 注入模型凭据。
