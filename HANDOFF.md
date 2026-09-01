> **新接手的 agent 请先读 AGENT_ONBOARDING.md**
>
# 交接说明

> 最新状态：v0.53 开发交接基线，2026-09-01。项目可运行和继续修改，但未完成稳定性验收。最完整的项目叙事、真实测试过程和待办请读 `交接的项目上下文.md`；本文件只保留快速启动和风险摘要。

## 项目概述

AI 电话评价 Agent — 从猎头访谈 TXT 中提炼策略流程图、路由条件和目标专家原话，编译为电话执行 Prompt 与评价 Prompt 的受控链。
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
- 模型：当前运行快照为 gpt-5.6（在“审计与设置”页可改；模型名不是产品契约）
- API Key 不进入项目文件；通过 `AI_CALL_EVAL_API_KEY` 环境变量或当前后端进程临时注入

## 数据状态（db.json）

交接包生成前的运行快照有 4 个实验任务，均未被本文声明为正式知识版本：

1. `task_480709a4c7f6` — `kiki的话术学习1.txt` — G2
2. `task_6888d3456188` — `looper学习1.txt` — G2
3. `task_5a26a7faccda` — `looper学习2.txt` — G2
4. `task_c9893e037b5e` — `looper学习3.txt` — G2

任务、Gate 和候选可能在交接后继续变化；以 `backend/data/db.json` 为准。v0.52 曾清理旧实验任务，之后用户重新建立了上述干净测试任务。

## v0.53 最新进展与风险

| 状态 | 内容 |
|---|---|
| 已实现 | 多出口空条件、显式不确定未确认、同源同条件异目标会阻断整图批准与 Prompt 编译 |
| 已实现 | 问题提示会列出来源节点、条件、全部目标，并提供每条相关边的直接定位按钮 |
| 已局部验证 | 真实 case 中两条边均为 `confirmed`、但条件仍完全相同时，后台继续正确阻断；点击定位按钮可打开对应编辑器 |
| 待稳定性审核 | 边条件保存后的刷新、候选切换、继承基线边、不同任务和前后端状态一致性 |
| 待稳定性审核 | Graph 布局模式、布局画像缓存/过期、LLM 分析降级、人工坐标恢复和重新初始化之间的优先级与重复加载稳定性 |
| 已知体验问题 | 密集 Graph 仍会出现边交叉、标签/节点遮挡、初始缩放过小；不同加载状态下图的长相可能变化 |

不要把“代码存在”或“单次浏览器成功”写成“稳定完成”。继任者当前优先执行执行文档 W-02～W-04。

## 历史补丁（2026-08-25）

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

