# Kiki 猎头通话策略节点结构图 v2｜纵向决策泳道版

> 本版仅优化读图方式，不改变 v1 的节点、分支条件和 59 条业务跳转。  
> 纵向背景表示“决策阶段”；节点形状表示“动作、判断、结果或规则空缺”；连接线颜色表示“候选人反馈性质”。

## 1. 读图规则

- **七条纵向阶段泳道**：按照候选人状态从“联系依据”逐步推进到“跨通话治理”，而不是按照提问、介绍、挽留等操作类型划层。
- **绿色实线**：正反馈或开放信号，允许关系继续推进。
- **橙红实线**：负反馈、拒绝或停止信号。
- **蓝灰虚线**：中立、情境性、模糊或尚未定性的反馈。
- **细灰实线**：结构跳转，不代表候选人态度。
- **紫色点线**：跨通话记录、风险判断和再次触达回环。
- **灰色虚线节点**：原始资料没有规定后续，必须保留为 `UNSPECIFIED`。

## 2. 纵向阶段泳道图

```mermaid
%%{init: {
  "theme": "base",
  "flowchart": {
    "curve": "basis",
    "nodeSpacing": 28,
    "rankSpacing": 42,
    "diagramPadding": 8,
    "htmlLabels": true
  }
}}%%
flowchart TB

    subgraph L0["L0｜联系依据准备层"]
        direction TB
        P0["<b>本层回答</b><br/>使用哪些已核实信息，形成什么联系理由？"]
        N00["N00 外呼前画像读取<br/>分数、学历、公司、title、方向、地点、薪资、年限"]
        N01["N01 生成 1 个最强切入点<br/>默认选择 1 个岗位作为详细切入"]
        P0 ~~~ N00
    end

    subgraph L1["L1｜对话许可判断层"]
        direction TB
        P1["<b>本层回答</b><br/>候选人此刻是否允许交流，怎样保留下一次机会？"]
        N10{"N10 接通后：现在是否方便"}
        N11["N11 先约方便时间<br/>可行时再要真实手机号或微信"]
        B11{"回拨与联系方式结果"}
        P1 ~~~ N10
    end

    subgraph L2["L2｜建联方式与初始意向识别层"]
        direction TB
        P2["<b>本层回答</b><br/>以什么方式进入，并把候选人识别为积极、观望、拒绝或需要解释？"]
        G20{"选择开场模式"}
        N20["N20 标准开场<br/>身份、来源、为什么找他、简短机会范围<br/>问想看什么类型或公司"]
        N21["N21 专家访谈式建联<br/>说明真实项目目的并询问是否接受"]
        N22{"N22 候选人首轮状态或反馈"}
        N23["N23 只用已核实的画像和岗位事实<br/>说明为什么联系、合适点在哪里"]
        U01["UNSPECIFIED<br/>着急看机会与积极状态的动作差异未定义"]
        U08["UNSPECIFIED<br/>拒绝专家访谈后的其他走法未定义"]
        P2 ~~~ G20
    end

    subgraph L3["L3｜机会响应与推进深度层"]
        direction TB
        P3["<b>本层回答</b><br/>候选人愿意在当前机会中投入到什么程度？"]
        N30["N30 单岗位详细介绍<br/>一个最强抓手、业务属性、2 至 3 个相关优势<br/>不完全一致时禁止强称完全匹配"]
        N31{"N31 对该岗位的反馈"}
        N33{"是否有足够时间继续深聊"}
        N34["N34 可核实方向、城市、职级、薪资范围和机会偏好<br/>薪资不在开场过早追问"]
        N32["N32 把观望转为长期价值<br/>市场、行业、岗位资源或负责人连接"]
        P3 ~~~ N30
    end

    subgraph L4["L4｜拒绝诊断与机会空间重构层"]
        direction TB
        P4["<b>本层回答</b><br/>拒绝关闭了什么，又留下了什么可能性？"]
        N40["N40 开场拒绝入口"]
        C40{"候选人是否仍允许继续说"}
        R40{"拒绝的是当前岗位或公司，还是所有机会"}
        N41["N41 不再硬推原岗位<br/>反问想考虑哪些方向或公司"]
        R41{"候选人是否给出目标方向"}
        N43["N43 用候选人目标方向承接<br/>其他合作资源、反向 BD 或行业分析"]
        G43{"确有可提供的资源或价值，且候选人愿继续"}
        N45["N45 可回退到画像深挖<br/>地点、个人背景、求职经历、海外经历等事实"]
        U02["UNSPECIFIED<br/>含糊拒绝的分类规则未定义"]
        U10["UNSPECIFIED<br/>目标明确但无资源或继续拒绝时的统一收口未定义"]
        P4 ~~~ N40
    end

    subgraph L5["L5｜关系承接与本通结果层"]
        direction TB
        P5["<b>本层回答</b><br/>本通形成什么关系资产，或者以什么状态结束？"]
        N50["N50 先给加微信价值，再提出请求<br/>市场信息、后续岗位、行业行情、负责人对接或薪酬支持"]
        W50{"是否愿意加微信或留真实号码"}
        N52["N52 退到脉脉留言<br/>留下猎头微信和手机号"]
        U03["UNSPECIFIED<br/>联系请求无回应时的追问次数未定义"]
        E01(["E01 主成功：进入持续跟进池"])
        E02(["E02 已约回拨或取得真实号码"])
        E03(["E03 平台承接：已留言并留下己方联系方式"])
        E04(["E04 当前话题关闭：停止推岗位"])
        E05(["E05 本通结束：无法继续表达"])
        P5 ~~~ N50
    end

    subgraph L6["L6｜跨通话治理与再次触达层"]
        direction TB
        P6["<b>本层回答</b><br/>是否再次联系，以及再次联系必须发生什么变化？"]
        N70["N70 记录触达时间、反馈、下次时间和下次切入点"]
        N60{"N60 二次触达风险门"}
        N61["N61 承认上次联系过并给新理由<br/>换时间、措辞、切入点、话题或价值点"]
        BACK10["↺ 下一次触达重新进入<br/>L1 / N10 对话许可判断"]
        E06(["E06 降低频率或停止"])
        U04["UNSPECIFIED<br/>高价值与强反感并存时的优先级或阈值未定义"]
        P6 ~~~ N70
    end

    N00 --> N01
    N01 --> N10

    N10 -- "不方便，但仍在通话" --> N11
    N11 --> B11
    B11 -- "约到时间或取得真实号码" --> E02
    B11 -- "拒绝留联系方式但仍允许承接" --> N52
    B11 -- "直接挂断或不给表达空间" --> E05
    N10 -- "直接挂断或不给表达空间" --> E05

    N10 -- "方便" --> G20
    G20 -- "默认" --> N20
    G20 -- "确有相关访谈或客户需求时的可选分支" --> N21
    N21 -- "接受交流" --> N50
    N21 -- "只拒绝加微信" --> N52
    N21 -- "拒绝访谈后的其他走法" --> U08

    N20 --> N22
    N22 -- "积极且有时间" --> N30
    N22 -- "积极但时间不足或当下忙" --> N50
    N22 -- "只是观望" --> N32
    N32 --> N50
    N22 -- "着急看机会" --> U01
    N22 -- "开场说不考虑" --> N40
    N22 -- "反问为什么找我" --> N23
    N23 --> N22

    N30 --> N31
    N31 -- "有兴趣" --> N33
    N33 -- "有" --> N34
    N33 -- "没有" --> N50
    N34 --> N50
    N31 -- "不感兴趣或已经接触过" --> N41
    N31 -- "只是观望" --> N32

    N40 --> C40
    C40 -- "否：打断、秒挂或不给表达空间" --> E05
    C40 -- "是" --> R40
    R40 -- "所有机会" --> E04
    R40 -- "当前岗位或公司" --> N41
    R40 -- "含糊且不回答" --> U02

    N41 --> R41
    R41 -- "给出" --> N43
    N43 --> G43
    G43 -- "是" --> N50
    G43 -- "无资源或继续拒绝" --> U10
    R41 -- "未给目标，但仍愿交流" --> N45
    N45 --> N41
    R41 -- "连续拒绝或不再交流" --> E04

    N50 --> W50
    W50 -- "愿意" --> E01
    W50 -- "不愿意" --> N52
    W50 -- "未明确回应" --> U03
    N52 --> E03

    E01 --> N70
    E02 --> N70
    E03 --> N70
    E04 --> N70
    E05 --> N70
    N70 -. "后续允许再次触达时" .-> N60
    N60 -- "可继续跟进" --> N61
    N61 --> BACK10
    N60 -- "低匹配或强烈反感" --> E06
    N60 -- "高价值与强反感同时存在" --> U04

    classDef phaseNote fill:transparent,stroke:transparent,color:#475569,font-style:italic
    classDef action fill:#FFFFFF,stroke:#64748B,stroke-width:1.3px,color:#0F172A
    classDef decision fill:#FEFCE8,stroke:#CA8A04,stroke-width:1.5px,color:#422006
    classDef success fill:#ECFDF5,stroke:#16A34A,stroke-width:1.7px,color:#14532D
    classDef handoff fill:#ECFEFF,stroke:#0891B2,stroke-width:1.5px,color:#164E63
    classDef closed fill:#FFF1F2,stroke:#E11D48,stroke-width:1.5px,color:#881337
    classDef unknown fill:#F8FAFC,stroke:#94A3B8,stroke-width:1.3px,color:#64748B,stroke-dasharray:6 4
    classDef loopRef fill:#F5F3FF,stroke:#7C3AED,stroke-width:1.5px,color:#5B21B6,stroke-dasharray:4 3

    class P0,P1,P2,P3,P4,P5,P6 phaseNote
    class N00,N01,N11,N20,N21,N23,N30,N34,N32,N40,N41,N43,N45,N50,N52,N70,N61 action
    class N10,B11,G20,N22,N31,N33,C40,R40,R41,G43,W50,N60 decision
    class E01 success
    class E02,E03 handoff
    class E04,E05,E06 closed
    class U01,U02,U03,U04,U08,U10 unknown
    class BACK10 loopRef

    style L0 fill:#F8FAFC,stroke:#CBD5E1,stroke-width:1px,color:#334155
    style L1 fill:#EFF6FF,stroke:#BFDBFE,stroke-width:1px,color:#1E3A8A
    style L2 fill:#F5F3FF,stroke:#DDD6FE,stroke-width:1px,color:#4C1D95
    style L3 fill:#F0FDF4,stroke:#BBF7D0,stroke-width:1px,color:#14532D
    style L4 fill:#FFF7ED,stroke:#FED7AA,stroke-width:1px,color:#7C2D12
    style L5 fill:#ECFEFF,stroke:#A5F3FC,stroke-width:1px,color:#164E63
    style L6 fill:#FAF5FF,stroke:#E9D5FF,stroke-width:1px,color:#581C87

    linkStyle default stroke:#94A3B8,stroke-width:1.3px,color:#475569

    %% 透明说明节点的不可见布局锚点
    linkStyle 0,1,2,3,4,5,6 stroke:transparent,stroke-width:0px,color:transparent

    %% 正反馈或开放信号
    linkStyle 11,15,18,22,23,26,31,32,39,44,46,52 stroke:#16A34A,stroke-width:2.4px,color:#166534

    %% 负反馈、拒绝或停止信号
    linkStyle 12,13,14,19,20,27,35,38,40,41,50,53,64 stroke:#D65A31,stroke-width:2.4px,color:#9A3412

    %% 中立、情境性或模糊反馈
    linkStyle 9,24,28,33,36,42,47,48,54,65 stroke:#64748B,stroke-width:2px,stroke-dasharray:6 4,color:#475569

    %% 跨通话生命周期回环
    linkStyle 56,57,58,59,60,61,62,63 stroke:#7C3AED,stroke-width:2.2px,stroke-dasharray:3 3,color:#6D28D9
```

## 3. 七层节点归属核对

| 决策层 | 节点数量 | 节点 |
|---|---:|---|
| L0 联系依据准备 | 2 | `N00`、`N01` |
| L1 对话许可判断 | 3 | `N10`、`N11`、`B11` |
| L2 建联方式与初始意向识别 | 7 | `G20`、`N20`、`N21`、`N22`、`N23`、`U01`、`U08` |
| L3 机会响应与推进深度 | 5 | `N30`、`N31`、`N32`、`N33`、`N34` |
| L4 拒绝诊断与机会空间重构 | 10 | `N40`、`C40`、`R40`、`N41`、`R41`、`N43`、`G43`、`N45`、`U02`、`U10` |
| L5 关系承接与本通结果 | 9 | `N50`、`W50`、`N52`、`U03`、`E01`、`E02`、`E03`、`E04`、`E05` |
| L6 跨通话治理与再次触达 | 5 | `N70`、`N60`、`N61`、`E06`、`U04` |
| **总计** | **41** | 与 v1 一致 |

## 4. 分层解释

- `L3` 不是单纯的“深入推进”：它同时容纳具体岗位深聊与观望价值承接，表示候选人愿意投入到什么程度。
- `L4` 不叫“挽留层”：这里的目标不是继续说服候选人接受原岗位，而是诊断拒绝范围并重新定义剩余机会空间。
- 所有 `E01–E05` 统一放进本通结果层。这样从早期阶段直接跳到结果的路径会更清楚，例如“不方便但约到时间”直接落到 `E02`。
- `UNSPECIFIED` 节点保留在产生它的阶段内，而不是集中到一个未知区，便于定位具体是哪一层存在规则缺口。
- `N61 → N10` 是跨通话回环，表示一次新的接通与许可判断，不是本通内部的普通返回。
- 为避免 Mermaid 自动布局被跨通话回边拉歪，图中将该边显示为 `N61 → BACK10（回到 L1/N10）`；`BACK10` 只是视觉引用，不是新增业务节点。

## 5. 数据完整性声明

- 业务节点总数：41；另有 7 个阶段说明节点和 1 个跨层视觉引用节点，均不参与业务状态计数。
- 业务跳转总数：59。
- 未增加新的业务规则、结束条件、重拨次数或薪资异议分支。
- 节点规则、原文空缺、来源索引与评估边界继续以 `kiki通话策略节点结构图_v1.md` 为准。
