> **新接手的 agent 请先读 AGENT_ONBOARDING.md**
>
# 交接说明

## 项目概述

AI 电话评价 Agent — 从猎头访谈 TXT 中提炼策略流程图和话术，编译为评价 Prompt 的受控链。
后端 Python（单文件 HTTPServer），前端原生 HTML/JS（无框架），数据存 JSON。

## 启动

```powershell
.\start.ps1
```

或手动：
```powershell
# 后端
cd backend; python app.py          # 端口 8898

# 前端（另开窗口）
cd frontend; python -m http.server 8897 --bind 127.0.0.1

# 打开 http://127.0.0.1:8897/
```

## LLM 配置

- API：token-hub.in.taou.com（OpenAI 兼容格式）
- 模型：glm-5.2（在"审计与设置"页可改）
- API Key 不进入项目文件；通过 `AI_CALL_EVAL_API_KEY` 环境变量或当前后端进程临时注入

## 数据状态（db.json）

3 个任务：
1. kiki的话术学习1.txt — target_expert=null, G0
2. looper学习3.txt — target_expert=looper, G3
3. looper学习4.txt — target_expert=looper, G3

## 已完成的补丁（2026-08-25）

三个 LLM 提炼函数改为 expert-aware（只从目标专家亲口发言中提炼策略）：
- `llm_extract_evidence` — 正确插值 expert_name
- `llm_extract_strategy` — 修复了 broken interpolation（三引号内 literal text 改为 .replace()）
- `llm_map_scripts` — 修复了 expert_name 缩进

## 2026-08-25 J 阶段修复状态

| 状态 | 内容 |
|---|---|
| 已修 | 目标专家服务端归属与明确认可双证据；非专家支撑在写入/G3 阻断 |
| 已修 | 分析运行指纹、重分析候选版本、精确 baseline 和 immutable 话术导入 |
| 已修 | task/group 编译发布隔离、Gate 对象态/流程态、错误状态码和前端按钮状态 |
| 已修 | UTF-8 BOM/非法 JSON 请求体、app.py LF 换行、验证脚本相对路径 |
| 已修 | Key 移出项目配置、配置接口不返回 Key 片段、限制本地 CORS |
| 待外部条件 | 新 Key 到位后的真实 GLM 小样本质量回归 |
| 待补验 | I-07 多样例截图级 Graph 验收；多人部署前的会话认证与任务授权 |

## 目录结构

```
ai-call-eval-agent/
├── start.ps1              一键启动
├── HANDOFF.md             本文件
├── README.md
├── backend/
│   ├── app.py             后端主程序 (127KB)
│   ├── llm_client.py      LLM 客户端
│   ├── README.md
│   └── data/
│       ├── db.json        数据库 (1.4MB)
│       └── llm_config.json
├── frontend/
│   ├── app.js             前端逻辑 (60KB)
│   ├── index.html
│   └── styles.css
├── input-docs/            原始访谈 TXT
├── docs/                  PRD、执行文档、进度、决策草案
├── scripts/               验证脚本、补丁脚本
├── reference/             策略流程图、Prompt 原型
└── runs/                  MVP 快照数据
```

