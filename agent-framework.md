# 旅行规划 Agent · 框架设计（v1 国内 · 审批稿）

> **这份文档是什么**：对 `travel-agent-handoff.md`（方案与选型文档）的**框架层补充**。原文档锁定了"做什么、不做什么、用什么源"，但没有回答"系统怎么跑起来"。本文档补齐编排、契约、边界与异常路径的设计。
>
> **怎么用**：本文档与原文档**配套使用**，不替代它。原文档负责需求与选型，本文档负责实现骨架。第 13 节的决策台账是实现时的既定前提。
>
> **状态**：设计已完整，50 条决策已确认。第 14 节列出的 5 项开放问题需要项目所有人拍板。

---

## 1. 分析结论：原文档的性质与缺口

### 1.1 性质判定

原文档是**方案与选型文档**，不是**可实施框架文档**。证据：

- §3.1 给了双闸门流程图，但没有状态字段、转移守卫、并发与恢复语义；
- §3.6 给了状态机名字，但没有"谁能触发哪个转移"的判据；
- §6 给了 MCP 清单和配置，但没有适配层接口定义；
- §5 的"逐环节方案"是候选源表，不是能力契约。

### 1.2 已锁定的硬约束

R1 多段跨域 · R2 双闸门 · R3 流式 · R4 工具风险分级 · R5 半自动购票+幂等+审计 · R6 天气参与决策且可解释 · R8 门票与车票同等对待 · R9 可降级可重规划。

技术栈锁定：LangGraph + FastAPI + Pydantic v2 + SQLite + React/Vite。

§12 的四条可度量验收是本框架的核心设计约束：

| 验收条款 | 对框架的硬性要求 |
|---|---|
| 信息闸门 100% 拦截缺槽位 | 槽位校验必须是纯确定性代码 |
| 同意前 0 次非只读调用 | 工具调用必须经统一出口，出口处做风险裁决 |
| 断线恢复不重复下单 | 每个副作用动作必须有可推导的幂等键 |
| 理由含可指认的天气/偏好因子 | 打分必须产出结构化理由链，LLM 只负责渲染 |

### 1.3 五个隐含结论（原文档没说，但框架必须显式化）

1. **语义归 LLM，裁决归代码。** "100% 拦截"这个指标 LLM 做不到。
2. **必须做能力契约层。** 各 MCP 工具名各异且功能重叠，编排层不能绑定工具名，否则换源要改图。
3. **"计划"必须可哈希、可版本化。** 否则"同意留痕"无法落地。
4. **幂等键必须由业务指纹派生。** 否则断线续传判不了重。
5. **多 leg 图需要统一基准量。** 坐标系统、时刻格式、时间窗口径必须统一到领域层。

### 1.4 缺口清单

**原文档自标的 4 项待定**：美团 ht-ai 免费额度、didi 可用性、住宿是否入 v1、多用户上线时点。

**本框架新发现的 8 项缺口**（原文档没提但绕不过去）：

| 缺口 | 为什么必须在框架阶段定 |
|---|---|
| LLM 选型 / 预算 / 延迟目标 | Agent 框架的核心参数，决定节点切分粒度 |
| 应用层最小鉴权 | 防止他人驱动用户登录态的浏览器 |
| 观测后端 | 原文只说"结构化日志 + trace"，未定技术 |
| 订单回执闭环 | §3.8 说靠用户回执推进，但无入口与流程 |
| 计划重算触发条件 | 改偏好 / 天气变化 / 余票变化时旧同意是否失效 |
| 会话与同意的 TTL | §3.1 只说"可挂起并超时过期" |
| 失败预算 | 工具失败几次后降级、几次后终止 |
| 高德搜索池预算分配 | §11 指出 5000/月 是坑，但无量化策略 |

### 1.5 三处需要调和的张力

- **"免费版" vs Variflight 积分制**：体验金是消耗品，航班能力必须可整体摘除。
- **高德"限非商业 + 仅 1 年"**：provider 必须可替换，且需到期警戒。
- **浏览器兜底层与 L0 的冲突**：§7 要求浏览器操作挂闸门，但 §11 要求 12306 余票在浏览器里查（L0 只读）。解法是"通道"与"风险"正交。

---

## 2. 核心原则

这七条是全部设计的裁判。任何一层与它冲突即为设计错误。

| 原则 | 内容 | 服务的约束 |
|---|---|---|
| **P1 语义归 LLM，裁决归代码** | LLM 只做两件事：把自然语言抽成结构化槽位、把结构化结果渲染成人话。闸门、打分、约束校验、风控、幂等全部是确定性代码 | 信息闸门 100% 拦截 |
| **P2 一切能力走统一出口** | 编排层只认"能力"，不认 MCP 工具名。能力是稳定接口，通道是可替换实现 | 随时可换源可降级 |
| **P3 计划即版本** | 计划是 `PlanVersion`，有 id 和 hash。同意绑定 hash。任何编辑产生新版本，旧同意立即作废并重新开闸 | 同意留痕 |
| **P4 副作用必幂等** | 幂等键由业务指纹派生，落库唯一约束 | 断线恢复不重复下单 |
| **P5 通道与风险正交** | "怎么取数"（HTTP/MCP/Browser）与"要不要问"（L0/L1/L2）是两个独立维度 | 化解浏览器与 L0 的矛盾 |
| **P6 降级是一等公民** | 每个能力在契约里预先声明降级阶梯，不是出错时才临时想 | 可降级可重规划 |
| **P7 边界归代码，LLM 无自由出口** | 用户可见文本只有一个出口且必经确定性校验。LLM 没有"自由回复"通道 | 领域边界约束 |

**P7 的推论**：不给出通道，而不是命令不要做。这一条同时服务三件事——领域边界、内部策略不外泄、永不自动支付。

---

## 3. 分层架构与职责边界

### 3.1 八层架构

```
L1  交互层        React + Vite：对话流 / 预览卡 / 同意·编辑 / 行程单 / 回执入口
L2  流事件层      SSE 下行 + REST 上行：事件信封、序号、续传、心跳
L3  编排层        LangGraph：父图 + 子图 + 状态机 + 双闸门 interrupt
                    ← 唯一允许出现 LLM 调用的层（且必须有结构化输出 schema）
L4  决策策略层    确定性：槽位校验 / 偏好打分 / 天气因子 / 约束校验 / 风险裁决 / 理由链
L5  能力契约层    Capability Registry：统一契约 + 风险等级 + 缓存 + 降级阶梯 + 成本模型
L6  连接适配层    MCP / HTTP / Browser 三通道同构：启动·健康·超时·重试·限流·配额记账
L7  领域状态层    Domain（Trip/Leg/PlanVersion/Consent/ActionLedger）+ Store
L8  安全合规层    横切：凭证 / PII 脱敏 / 审计 / 到期与配额警戒
```

### 3.2 相对原文档的关键改动

原文档 §4 把 MCP 适配层紧贴编排层，但 §6.10 第 1 条又要求"编排层不直接依赖 MCP 工具名"——两者矛盾，中间少了东西。

补上 **L5 能力契约层**后职责清晰：

| 层 | 负责 | 明确不做 |
|---|---|---|
| L5 能力契约层 | 定义 `route.plan`、`weather.forecast` 这类**稳定能力**；声明风险等级、缓存 TTL、降级阶梯、成本 | 不关心数据从哪来 |
| L6 连接适配层 | 把能力落到具体通道：amap / 12306 / variflight / 美团 / Open-Meteo / 浏览器 | 不做业务判断，不做风险裁决 |

### 3.3 分层依赖铁律

- 只允许**向下依赖**。
- **L3 禁止 import 任何 MCP SDK、任何 provider SDK。**
- **L4 禁止直接调 L6**，必须经 L5。
- **LLM 调用只出现在 L3 的语义节点。L4 里一行 LLM 都没有。**
- L8 是横切层，不参与业务流。

---

## 4. 三大契约

### 4.1 能力契约（L5）

#### 4.1.1 统一返回信封

```
CapabilityResult {
  status:       ok | degraded | unavailable | error
  data:         <归一化载荷>
  provenance:   { channel, provider, fetched_at, cache_hit, ttl_left, confidence }
  degradation:  { level: D0|D1|D2|D3, reason }
  cost:         { units, currency, quota_pool }
  warnings:     []
}
```

`provenance` 是最值钱的字段：让理由链能引用"数据来自哪个源、什么时候取的、是不是缓存"。

#### 4.1.2 降级阶梯

| 档 | 含义 |
|---|---|
| D0 | 实时完整数据 |
| D1 | 缓存数据，带时间戳标注 |
| D2 | 无实时数据，给方案骨架，显式标注"未经实时校验" |
| D3 | 深链跳转 + 结构化清单，用户自取 |

#### 4.1.3 能力清单（v1）

**数据获取类（全部 L0 只读）**

| 能力 | 说明 | 主通道/主源 | 备源 | 缓存 TTL |
|---|---|---|---|---|
| `clock.today` | 当天日期锚点 | variflight `getTodayDate`（免费） | 系统时钟 | 当日 |
| `place.resolve` | 地址/地名 → 坐标+行政区 | amap geocode | OSM | 7 天 |
| `place.disambiguate` | POI 消歧 | amap poi_detail **优先** | amap poi_search | 24h |
| `route.plan` | 两点路径（transit/driving/walking/bicycling） | amap direction_* | OSRM | 15 min |
| `route.matrix` | 多点距离矩阵 | amap distance | — | 15 min |
| `weather.forecast` | 预报 + 派生风险因子 | Open-Meteo（免 key） | amap weather | 30 min |
| `air.quality` | AQI | Open-Meteo air quality | amap | 60 min |
| `intercity.rail.search` | 火车/高铁（含过站与中转） | 12306-mcp | — | 60 s |
| `intercity.rail.availability` | 余票（需 cookie） | **浏览器通道** | 12306-mcp | 60 s |
| `intercity.flight.search` | 航班 | variflight-mcp | — | 5 min |
| `intercity.multimodal.search` | 空铁联运 | tripmatch-mcp | — | 5 min |
| `price.quote` | 票价 | variflight / 12306 / 美团 | — | 5 min |
| `poi.discover` | 景点/酒店推荐 | 美团 ht-ai | — | 24h |
| `attraction.reservation.status` | 门票预约状态 | 浏览器通道 | 人工小表 | 6 h |

**动作类**

| 能力 | 风险 | 说明 |
|---|---|---|
| `booking.deeplink.build` | L0 | 纯本地计算，无外部副作用 |
| `booking.checklist.export` | L0 | 纯本地 |
| `browser.navigate` / `read` / `screenshot` | L0 | 只读 |
| `browser.click` / `type` / `submit` | L1 | 逐次确认 |
| `booking.reserve.submit` | L2 | 不可逆 |
| `booking.order.submit` | L2 | 强确认 + 二次确认 |

**没有 `payment.*`。** 用能力缺席表达产品承诺（见 P7）。

#### 4.1.4 配额池与预算

| 池 | 额度 | 策略 |
|---|---|---|
| amap LBS | 15 万/月（限非商业，**自认证起 1 年**） | 80% 告警；到期前 60 天告警 |
| amap 搜索 | **5,000/月** | 80% 告警；`place.disambiguate` 优先走 `poi_detail`，`poi_search` 仅兜底，LRU 淘汰 |
| variflight 积分 | ¥50 体验金（消耗品） | 余额 < 20% 自动降级；航班能力可整体摘除 |
| Open-Meteo / 12306-mcp | 无配额 | 仅低频礼貌限流 |

### 4.2 状态契约（L7）

```
AgentState {
  session_id, user_id, thread_id
  created_at, last_active_at, ttl, suspended_for

  phase: COLLECT | READY | PREVIEW | AWAIT_CONSENT | EXECUTE | DONE | FAILED | EXPIRED

  scope: { verdict, strikes, rejected_count, last_verdict }

  slots: { <slot_id>: SlotValue }
  missing_required: [slot_id]
  question_budget: { asked_rounds, max_rounds }

  plan_versions: [PlanVersionMeta]
  current_plan_version_id
  plan_status: none | draft | previewed | approved | superseded
  consents: [ConsentRef]
  pending_interrupt: { kind, action_id?, plan_version_id? } | null

  actions: [ActionState]
  ready_actions: [action_id]

  trip_draft: Trip
  degradation_log: []
  errors: []
  usage: { llm_tokens, capability_calls, cost_estimate }
}

SlotValue {
  value, source: user | inferred | default
  confidence
  status: filled | unknown | user_declined
  confirmed_at
}
```

四个关键设计点：

1. **槽位值不是裸值。** 必须区分"用户明确说的""系统推断的""用户说不知道的"，这决定追问行为。
2. **`unknown` / `user_declined` 是合法终态。** 用户说"随便"记为 `user_declined`，改用保守默认 + 显式标注假设，不再追问。
3. **槽位注册表是声明式的**（id / 必填性 / 条件表达式 / 校验器 / 追问模板 / 消歧策略）。
4. **追问预算 4 轮。** 超预算不吃死循环，改为输出假设清单让用户确认。

> 注意：闸门拦的是"没有值就执行"，`user_declined` + 保守默认算**有值**。两者不冲突。

### 4.3 事件契约（L2）

#### 4.3.1 事件信封

```
Event {
  id:               <会话内单调递增序号>
  session_id, ts, type
  phase             # 当前状态机阶段
  plan_version_id?  # 当前计划版本
  data
  replay:           bool
}
```

`phase` 与 `plan_version_id` 让**每条事件自带状态坐标**，前端不需要推断或额外查询。

#### 4.3.2 下行事件类型

| 类型 | 说明 |
|---|---|
| `text_delta` | 正文流式 |
| `tool_call_start` / `tool_call_delta` / `tool_call_end` | 工具进度 |
| `tool_result` | 结构化结果摘要（非原始大 JSON） |
| `plan_preview` | 计划预览卡（含 plan_version_id 与 hash） |
| `interrupt` | 闸门阻塞，该段流随即关闭 |
| `state_update` | 阶段/槽位变更 |
| `scope_rejected` | 领域越界（R10） |
| `degradation_notice` | 数据降级提示 |
| `usage` / `error` / `done` / `heartbeat` | 常规 |

**`reasoning_delta` 已从协议中移除**（DC-1）。

#### 4.3.3 上行接口

| 端点 | 用途 |
|---|---|
| `POST /sessions` | 建会话 |
| `POST /sessions/{id}/messages` | 用户输入 |
| `POST /sessions/{id}/resume` | approve / reject / edit |
| `POST /sessions/{id}/receipt` | 订单回执 |
| `GET /sessions/{id}/snapshot` | 完整状态快照 |
| `GET /sessions/{id}/trip` | 行程单 |
| `GET /sessions/{id}/events?since=<id>` | 断线补发 |
| `GET /sessions/{id}/stream` | SSE 流 |

#### 4.3.4 两条工程细节

- **`tool_call_delta` 只用于 UI 展示，绝不在此阶段解析参数。** 参数只在 `tool_call_end` 一次性解析。
- **续传前必须先查 ActionLedger。** 事件重放与动作重放是两件不同的事。

---

## 5. 编排图

### 5.1 三层图结构

```
父图 TripGraph（会话级）
  START → intake → scope_gate ─┬─ out_of_scope/abuse → refuse ──┐
                               └─ in_scope/ambiguous            │
                                        ↓                       │
                                  slot_validate ──缺槽位──→ clarify ──┘ (interrupt 追问)
                                        │ 齐 / 到上限转假设
                                        ↓
                                   plan_draft ──调用──→ [LegPlanner 子图] × N 段
                                        ↓
                                  trip_validate ──不通过──→ replan (有界重试)
                                        ↓ 通过
                                  plan_finalize (生成 plan_version + hash)
                                        ↓
                                  preview_render → consent_gate (interrupt)
                                        ↑                    │
                                        └── edit/reject ─────┤ approve
                                                             ↓
                                   execute ──调用──→ [ActionExecutor 子图]
                                        ↓
                                  assemble_itinerary → remind → END
```

`plan_draft` 与 `execute` 做成子图的理由：LegPlanner 会被调用 N 次，ActionExecutor 需要内嵌自己的闸门逻辑。

### 5.2 节点清单

**父图**

| 节点 | 类型 | 说明 |
|---|---|---|
| `intake` | **LLM** | 唯一做语义抽取的节点：槽位补丁 + `scope` 判定 |
| `scope_gate` | 确定性 | R10 落地 |
| `refuse` | 确定性 | 固定模板话术，不经 LLM |
| `slot_validate` | 确定性 | 纯函数，可单测 |
| `clarify` | **LLM** + interrupt | 生成追问，模板兜底 |
| `assume_fallback` | 确定性 | 到上限时转假设清单 |
| `plan_draft` | 编排 | 调 LegPlanner 子图 |
| `trip_validate` | 确定性 | 跨段约束校验 |
| `replan` | 确定性 | 有界重试（≤3），含降级重算 |
| `plan_finalize` | 确定性 | 生成 plan_version + hash |
| `preview_render` | **LLM** | 只引用结构化字段 |
| `consent_gate` | interrupt | 同意闸门 |
| `apply_edit` | 确定性 | 决定回 COLLECT 还是回 PREVIEW |
| `execute` | 编排 | 调 ActionExecutor 子图 |
| `assemble_itinerary` | 确定性 | 生成 Trip |
| `remind` | 确定性 + LLM 渲染 | 提醒列表 |

**LegPlanner 子图**

| 节点 | 类型 | 说明 |
|---|---|---|
| `scope_classify` | 确定性 | 距离/行政区边界 → local / regional / intercity。小地区是单 leg 的退化情形 |
| `candidates_gather` | 能力调用（L0 批量） | place / route / intercity |
| `weather_enrich` | 能力调用 | weather + AQI |
| `score` | 确定性 | 偏好权重 × 天气因子 → 分数 + 理由链 |
| `select` | 确定性 | 取优 + tie-break |
| `reason` | **LLM** | 从理由链渲染人话 |
| `leg_degrade` | 确定性 | 按阶梯降级 |

**ActionExecutor 子图**

| 节点 | 类型 | 说明 |
|---|---|---|
| `revalidate` | 能力调用（L0 批量） | **执行前复核**，数据变了回 PREVIEW |
| `plan_actions` | 确定性 | 动作 DAG 拓扑排序 |
| `dispatch` | 确定性 | 按风险分拣 |
| `run_l0_batch` | 能力调用 | 批内并行，单项失败不阻断整批 |
| `action_consent_gate` | interrupt | L1/L2 动作级闸门 |
| `run_action` | 能力调用 | — |
| `verify` | 确定性 | 浏览器动作必须页面状态验收 |
| `reconcile` | 确定性 | `in_flight` 动作的核对，不重跑 |
| `ledger_commit` | 确定性 | 幂等键落库 |

### 5.3 状态机转移表

状态沿用 §3.6，不新增状态。

| 当前状态 | 事件 | 守卫 | 下一状态 |
|---|---|---|---|
| COLLECT | USER_REPLY | `scope ∈ {in_scope, ambiguous}` | COLLECT（回 intake） |
| COLLECT | USER_REPLY | `scope = out_of_scope` | COLLECT（经 refuse 自环） |
| COLLECT | USER_REPLY | `strikes ≥ 3` | **FAILED**(`SCOPE_ABUSE`) |
| COLLECT | SLOT_COMPLETE | `missing_required = ∅` | **READY** |
| COLLECT | SLOT_COMPLETE | `rounds ≥ max_rounds` 且仍有 unknown | READY（携假设清单） |
| READY | 自动 | — | PREVIEW |
| PREVIEW | PLAN_READY | 约束校验通过 | **AWAIT_CONSENT** |
| PREVIEW | PLAN_READY | 校验失败且 `retry < 3` | PREVIEW（replan） |
| PREVIEW | PLAN_READY | 校验失败且 `retry ≥ 3` | 降级 D2 出方案 / FAILED |
| AWAIT_CONSENT | USER_APPROVE | **`consent.plan_hash == 当前版本 hash`** | **EXECUTE** |
| AWAIT_CONSENT | USER_APPROVE | hash 不匹配 | 拒绝恢复（409），回 PREVIEW |
| AWAIT_CONSENT | USER_REJECT | — | COLLECT 或 PREVIEW |
| AWAIT_CONSENT | USER_EDIT | 改槽位 | **COLLECT** |
| AWAIT_CONSENT | USER_EDIT | 改计划 | **PREVIEW** |
| AWAIT_CONSENT | TIMEOUT | — | EXPIRED |
| EXECUTE | APPROVAL_REQUIRED | `action.risk ≥ L1` | AWAIT_CONSENT（嵌套） |
| EXECUTE | TOOL_ERROR | 未超降级阶梯 | EXECUTE（降级继续） |
| EXECUTE | TOOL_ERROR | 超阶梯且为必需动作 | FAILED |
| EXECUTE | 全部动作完成 | — | **DONE** |
| 任意 | 会话 TTL 到 | — | EXPIRED |

### 5.4 三个实现级问题的精确回答

#### Q1：用户点"同意"时，系统按了什么？

`Command(resume=<payload>)`：

```
{
  decision: approve | reject | edit
  plan_version_id, plan_hash
  consent_scope: { kind: gate2_full } | { kind: action, action_id }
  confirm_token?   # 仅 L2 二次确认
  edits?, client_ts
}
```

服务端处理顺序**不能换**：

1. **先校验 hash** → 不匹配返回 `409 PLAN_VERSION_STALE`，不恢复执行
2. **再落 consent 记录**（谁、何时、同意哪版、scope）
3. **最后才恢复图执行**

先落库再执行：即使恢复瞬间崩溃，"用户同意过"的审计不会丢。

#### Q2：断线重连从哪个 checkpoint 继续？

三种"断线"必须区分：

| 类型 | 处理 |
|---|---|
| **A. SSE 断但图在跑** | 事件补发问题。按 `since` 补发 + **全量快照校准** |
| **B. 图停在 interrupt** | 图已挂起，checkpoint 落盘。重连**不需要补发历史事件**，直接取 `pending_interrupt` 重建卡片 |
| **C. 执行途中崩溃** | 用 `thread_id` 恢复，但恢复前必须读 ActionLedger：`completed` 跳过、`in_flight` 走 `reconcile` 核对、`pending` 执行 |

**`in_flight` 绝不重跑。** 核对方式：有浏览器通道的读页面确认；无通道的降级为"请用户确认订单是否已提交"。

#### Q3：L1/L2 在 EXECUTE 中途怎么插闸门？

复用同一 interrupt 机制，只换 payload 的 `kind`：

- L0 → 批量并行执行，不进闸门
- L1 → 构造动作确认卡，`interrupt({kind: action_approval, action_id, preview})`
- L2 → 同 L1，通过后**再 interrupt** 要求回传 `confirm_token`

不破坏主流程的四个原因：

1. 只挂起不跳出，`phase` 保持 EXECUTE，已完成结果都在 state 里；
2. 复用同一套机制，checkpoint 天然覆盖嵌套闸门；
3. 同意粒度分层（`gate2_full` vs `action`），审计可精确回答；
4. 幂等键绑定动作，与闸门无关。

用户拒绝某动作时：标 `skipped`，检查下游依赖——关键前置被跳过则下游 `blocked`，否则继续。

### 5.5 编码规范级别的陷阱

> **`interrupt()` 之前的代码必须是纯的。**

LangGraph 在 resume 时会**从节点开头重新执行该节点**，`interrupt()` 在那里返回 resume 值。若 `interrupt()` 之前发了外部请求，resume 时会**再发一次**。

规范：**任何有副作用的操作一律放在 `interrupt()` 之后，或拆到独立节点。**

---

## 6. 决策与策略层

这一层全部是确定性代码，**一行 LLM 都没有**。

### 6.1 偏好分两类

| 类别 | 例子 | 作用 |
|---|---|---|
| **硬约束** | 无障碍需求、服务日有效、时间窗、票种可得 | **过滤候选**，不参与打分 |
| **软偏好** | 时间、费用、换乘、步行、准点、舒适 | **加权打分** |

**为什么拆**：混在一起时"权重设为 0"和"完全不可接受"无法区分。带轮椅出行的体验是"没有无障碍通道的方案根本不该出现"，而不是"分数略低"。

软权重预设（每档和为 1.0）：

| 画像 | 时间 | 费用 | 换乘 | 步行 | 准点 | 舒适 |
|---|---|---|---|---|---|---|
| 默认 | 0.25 | 0.20 | 0.20 | 0.10 | 0.15 | 0.10 |
| 赶时间 | 0.45 | 0.10 | 0.15 | 0.05 | 0.20 | 0.05 |
| 省钱 | 0.15 | 0.45 | 0.15 | 0.05 | 0.10 | 0.10 |
| 带老人/小孩 | 0.20 | 0.15 | 0.15 | 0.30 | 0.10 | 0.10 |
| 求稳 | 0.15 | 0.15 | 0.15 | 0.05 | 0.30 | 0.20 |

来源优先级：**用户显式 > 从 travelers 推断画像 > 默认**。用户改权重 → 重算 → hash 变化 → 旧同意失效。

### 6.2 天气两路作用

| 因子 | 判据 | 影响 | 路径 |
|---|---|---|---|
| 降水 | 强度 + 天气码 + 降雪 | 自驾、步行、户外景点、索道 | 软惩罚；大到暴雨升级为硬否决 |
| 风速 | 10m 风速 | 索道、缆车、轮渡、航班 | **硬否决** |
| 雷暴 | 雷暴码 / 对流指数 | 山区、户外 | **硬否决** |
| 能见度 | 能见度值 | 观景、航班 | 软惩罚 |
| 温度 | 最高/最低温 + 体感 | 出发时段、装备 | 软惩罚 + 时段建议 |
| AQI | 空气质量接口 | 户外强度 | 软惩罚 |
| 结冰 | 温度 + 降水 | 自驾、步行 | **硬否决** |

**两路必须分开**：若全部当软惩罚，"雷暴"会被"便宜 300 块"抵消——这是安全事故。硬否决是安全阀，软惩罚是调优旋钮。

### 6.3 打分公式

```
Score(c) = Σ_i w_i × (1 − f_i(c))   偏好软加权
         − Σ_j penalty_j(c)          天气软惩罚
         − λ × u(c)                  数据不确定度惩罚
```

| 数据等级 | u(c) |
|---|---|
| D0 实时 | 0.00 |
| D1 缓存 | 0.10 |
| D2 无实时 | 0.30 |
| D3 深链 | 0.50 |

λ 默认 **0.15**。意义：**降级数据不应"免费"获得高分**。

**缺维处理**：某特征缺失时从加权和中剔除，**剩余权重重新归一化**，同时抬高 `u(c)`。既不当 0（会误判为最优），也不当中性值（会掩盖不确定性）。

### 6.4 理由链

```
ReasonTrace {
  candidate_id, score_total
  contributors: [
    { kind: preference | weather | constraint | uncertainty
      key, weight, feature, contribution, evidence }
  ]
  vetoes:      [ { kind, reason } ]
  assumptions: [ ... ]
  degradation: [ ... ]
}
```

三条硬规则：

1. **`reason` 节点只能引用 `|contribution|` 最大的前 3 项，且必须带 `evidence`。** 把"可指认"从提示词要求变成结构性保证。
2. **每条推荐的 ReasonTrace 必须至少含一个 `weather` 或 `preference` 且 `contribution ≠ 0` 的条目。** 这是可写断言的验收条款。
3. **防过度包装。** 若 top-3 全是 `constraint`，说明只是"没被淘汰"而非"被选中"，理由必须照实说，不得渲染成"因为天气很好"。

### 6.5 跨段约束校验

| 约束 | 形式 | 违规处理 |
|---|---|---|
| 时序衔接 | `leg[i].arrive + buffer ≤ leg[i+1].depart` | 淘汰 |
| 时间窗 | 发车落在用户时间窗内 | 淘汰 |
| 服务日 | 车次/航班当日在运行 | 淘汰 |
| 预算 | `Σ price ≤ budget` | 排序调整（除非是硬上限） |
| 票种可得 | 学生/儿童/老人票有货 | 淘汰 |
| 住宿衔接 | stay 距次日出发点合理 | 提示 |
| 往返闭合 | 末段能回到 origin | 淘汰 |
| **坐标一致** | 已统一到同一坐标系 | **报错，不降级**（这是 bug） |

**动态 buffer**：

| 前一段 | 基础 buffer |
|---|---|
| 航班 | 120 min |
| 高铁/火车 | 60 min |
| 同城中转 | 45 min |
| 跨站换乘 | 90 min |

再乘数据不确定度系数：前段 provenance 为 D1/D2 时 `× 1.5`。

**修复顺序**：局部修复 → 该 leg 重选 → 全图重排并降级 D2 → 重试上限 3 次。

### 6.6 tie-break 与备选

- 分数差距 < **0.03** 视为并列，按稳定性优先：provenance 等级更高 → 换乘更少 → 发车更晚
- 输出 top-1 + 1～2 个备选，并说明**备选在哪个维度更优**

### 6.7 输出接口

```
LegDecision {
  leg_id, selected, alternatives
  reasontrace
  rejected: [ { candidate_id, reason } ]   # 保留：用户会问"为什么不坐飞机"
  uncertainty: { level, sources }
  assumptions
}
```

---

## 7. 连接适配层

### 7.1 三通道统一抽象

| 通道 | 覆盖 |
|---|---|
| MCP | amap、OSM、12306-mcp、variflight、tripmatch、美团 |
| HTTP 直连 | Open-Meteo |
| Browser 桥 | 门票预约、打车、12306 余票 |

```
ChannelAdapter {
  id, kind: mcp | http | browser
  supports(capability_id) -> bool
  health() -> { state, last_check, fail_count }
  invoke(capability_id, params, ctx) -> CapabilityResult
  cost_model(capability_id) -> { pool, units }
}
```

### 7.2 Provider 解析与降级阶梯

```
1. 新鲜缓存命中          → D1（带 fetched_at）
2. 主 provider 调用       → D0
3. 备 provider 调用       → 按声明档位
4. 过期缓存              → D1（时间戳显著标注）
5. 骨架 + 显式标注        → D2
6. 深链 + 清单           → D3
```

**缓存命中记为 D1 而非 D0**：让不确定度惩罚自动生效。

### 7.3 启动与健康检查

- 启动时并行连接，单连接超时 **5 s**，**失败不阻断整体启动**
- 健康状态：`healthy | degraded | unavailable`
- 后台探测 **60 s**，连续失败走指数退避
- **健康探测只做协议层握手，不做业务调用**（Variflight 协议层免费）
- 生产用 **HTTP 常驻进程**，不用 `npx` 冷启动

### 7.4 超时与重试

| 能力 | 超时 |
|---|---|
| weather / air.quality / place.resolve | 5 s |
| route.plan / route.matrix | 8 s |
| intercity.rail.search | 15 s |
| intercity.flight.search | **30 s** |
| browser.read | 20 s |

重试分三档：

| 错误类型 | 处理 |
|---|---|
| 网络类（超时、连接失败、5xx） | 退避重试，**最多 2 次** |
| 业务类（参数错、无结果、鉴权失败） | **不重试**，直接降级 |
| 配额类（403、超限） | **不重试**，标记池耗尽，降级 |

**铁律：L1 / L2 动作永不自动重试**，只走 `reconcile`。必须写死在连接层。

### 7.5 缓存

```
cache_key = capability_id + hash(归一化参数) + 坐标精度截断
```

坐标精度截断到约 100 米（4 位小数），提升命中率。

| 能力 | TTL |
|---|---|
| 天气 / AQI | 30 min / 60 min |
| 路线 / 距离矩阵 | 15 min |
| 余票 | **60 s** |
| 航班 | 5 min |
| 地理编码 | **7 天** |
| POI 详情 | 24 h |
| 门票预约状态 | 6 h |

两条禁令：**不缓存错误**；**不缓存含 PII 的结果**。

### 7.6 配额记账

**调用前预扣，失败后回滚。** 依据：Variflight"调用失败不扣费"——只做成功后累加无法在调用瞬间阻止超额；只做预扣不回滚会与实际账单对不上。两个都要。

| 用量 | 动作 |
|---|---|
| 80% | 告警 |
| 95% | 强制降级到备源 |
| 100% | 禁用主源，只走备源 / D2 / D3 |

池耗尽时**不报错**，而是降级 + 发 `degradation_notice`。

### 7.7 浏览器通道特殊规则

| 规则 | 依据 |
|---|---|
| 每会话**独占**一个浏览器槽 | §11 多 agent 抢页 |
| **强制前台执行** | §11 后台标签页不渲染 |
| 操作前校验当前 URL 与预期一致 | 防错页操作 |
| 每步 `assert_page_state` 验收 | §7"返回已点击不算数" |
| 截图留证挂到 trace | 事后可复核 |
| **只读操作也限频** | 12306 风控；免确认免的是打扰，不是风控风险 |
| 登录态失效检测 → 降级 D3 + 提示 | 不静默失败 |

### 7.8 失败可解释与凭证

```
FailureReport { capability, provider, kind, user_message, suggested_action }
```

映射为 `degradation_notice` 事件。**禁止静默吞掉。**

凭证规则：只进环境变量；启动校验存在性，缺失标 `unavailable` 不崩；日志统一脱敏；**启动时探测高德 key 类型**（Web 服务 vs Web JS，配错的表现是"一直失败但不报鉴权错"）。

---

## 8. 执行层

### 8.1 执行前复核

计划是在 `plan_draft` 时算的，用户同意时可能已过几十分钟。所以第一个节点是 **`revalidate`**：

- 对关键 L0 数据批量复核（余票、价格、服务日、天气）
- 没变 → 继续执行
- **变了 → 新版计划 → hash 变化 → 旧同意失效 → 回 PREVIEW**

没有这一步，系统会在用户同意后拿着过期数据去下单。

### 8.2 动作模型

| 动作 | 风险 |
|---|---|
| `revalidate` | L0 |
| `booking.deeplink.build` | L0 |
| `booking.checklist.export` | L0 |
| `reservation.status.check` | L0 |
| `reservation.submit` | **L1** |
| `seat.reserve` | **L1** |
| `order.submit` | **L2** |
| `reminder.create` | L0 |

**没有 `payment.*`。**

### 8.3 裁决器：唯一调用出口

```
arbiter(action, state) -> allow | deny(reason)

  L0 且计划已同意        → allow
  L0 且计划尚未同意      → deny(ILLEGAL_PRE_CONSENT)
  L1                     → 需对应 consent 记录
  L2                     → 需 consent + 有效 confirm_token
  能力不在注册表          → deny(CAPABILITY_NOT_FOUND)
```

效果：**"同意前 0 次非只读调用"从行为要求变成可静态检查的结构属性。** 进一步做法是让 arbiter 同时充当调用网关，每次 `invoke` 必须带 `consent_ref`。

### 8.4 幂等键

```
idem_key = sha256( session_id ‖ action_kind ‖ subject_fingerprint )

购票  → 车次 + 乘车站/到站 + 日期 + 席别 + 乘车人集合
预约  → 景点 + 日期 + 时段 + 实名集合
```

幂等键标识"业务上是否同一件事"，不是"代码上是否同一次调用"。

三条规则：

1. **hash 前规范化参数**（去空白、日期 `YYYY-MM-DD`、坐标精度、乘车人排序）
2. **唯一约束建在数据库层**（`UNIQUE(idem_key)`）
3. **自动判重可被显式覆盖，但覆盖必须是人为的**：恢复场景直接跳过；用户显式重新发起时需确认"你之前已为这张票下过单，确定再来一次吗"

### 8.5 动作台账

```
ActionLedgerEntry {
  idem_key (UNIQUE)
  session_id, plan_version_id, action_id
  capability_id, subject_fingerprint, params_hash
  consent_ref
  status: pending | in_flight | completed | failed | skipped | blocked
  attempt, started_at, finished_at
  result_ref, evidence, failure
}
```

### 8.6 L0 批量执行

- 批内并发，**并发度 5**
- `allSettled` 语义，单项失败不阻断整批
- 失败项标 `degraded`，按阶梯补位
- 整批结果只写一次 state，减少 checkpoint 写放大

### 8.7 页面状态验收

```
BrowserAction {
  intent:    语义意图
  steps:     操作序列
  assertion: 验收断言
}
```

- 执行完 → 执行 `assertion`
- **断言失败 ≠ 重试**，转 `reconcile` 读页面真实状态
- 每步截图挂到 `evidence`

### 8.8 L2 二次确认

1. 首次 interrupt → approve → 服务端生成 **`confirm_token`**（一次性、绑定 action_id 与 idem_key、有效期 5 分钟）
2. 二次 interrupt → 要求回传 token
3. 校验匹配 + 未过期 + 未使用 → 执行

用 token 而非"再点一次同意"，是为了排除 UI 误触和网络重发造成的双重授权。

### 8.9 订单回执闭环

- `POST /sessions/{id}/receipt` 接收回执（订单号、状态、可选截图）
- 用户逐条认领"这条我买好了"
- 回执把 status 更新为 `completed`，标记 `source=user_receipt`
- **框架永远不假设用户买了票。** 没收到回执就是"待确认"。

### 8.10 失败预算

| 层次 | 处理 |
|---|---|
| 单动作 | 不重试，失败即 `failed` |
| 依赖下游 | `blocked` |
| 必需动作全失败 | **降级 D3**（深链 + 清单） |
| 连降级都给不出 | 才是 FAILED |

**"买不成票"不等于"系统失败"**——计划、深链、清单、提醒仍有价值。

---

## 9. 存储与数据模型

### 9.1 核心原则

> **业务表是唯一真相源，checkpoint 只是恢复执行的载体。**

| | checkpoint | 业务表 |
|---|---|---|
| 谁拥有 | LangGraph 框架 | 我们自己 |
| 装什么 | 图执行状态、过程态 | 业务事实、结论态 |
| 格式 | 随库版本变 | 我们控制的稳定 schema |
| 生命周期 | 7 天，**可重建** | 长期，**不可重建** |
| 恢复时角色 | 载体 | **唯一真相源** |

**不变式：checkpoint 可以丢，业务表不能丢。**

### 9.2 表结构

```
sessions(session_id PK, user_id, phase, created_at, last_active_at,
         suspended_until, expires_at, scope_strikes, scope_rejected_count, status)

plan_versions(plan_version_id PK, session_id FK, version_no, plan_hash,
              slots_snapshot, legs, actions, cost_estimate,
              degradation_summary, assumptions, status, created_at)

consents(consent_id PK, session_id FK, plan_version_id FK, plan_hash,
         scope_kind, action_id, granted_at, channel, superseded_by)

action_ledger(idem_key UNIQUE, session_id, plan_version_id, action_id,
              capability_id, subject_fingerprint, params_hash, consent_ref,
              status, attempt, started_at, finished_at, result_ref,
              evidence, failure)

events(seq PK, session_id, type, payload, ts)          # 仅可重放类型

cache_entries(cache_key PK, capability_id, normalized_params_hash, payload,
              provenance, fetched_at, ttl_seconds, expires_at, contains_pii)

quota_entries(idem UNIQUE, pool_id, window, units,
              state: reserved|committed|rolled_back, ts)

receipts(receipt_id, session_id, action_id, idem_key,
         order_no, status, note, attachment_ref, created_at)

audit_log(audit_id, session_id, ts, actor, event_kind, subject, ref_id, metadata)

browser_slots(slot_id, session_id, leased_at, expires_at, state)
```

两个说明：

- **`superseded` 是状态而非删除**，历史版本要留着。
- **`slots_snapshot`**：槽位过程态在 checkpoint，**计划生成时那组槽位是结论态**，必须随版本存档。规则：过程态进 checkpoint，结论态进业务表。

**事件保留策略**：只落库可重放类型。

| 类别 | 例子 | 落库 |
|---|---|---|
| 可重放 | `state_update` / `interrupt` / `tool_result` / `degradation_notice` / `error` / `done` / `scope_rejected` | 是 |
| 增量 | `text_delta` / `tool_call_delta` | 否 |

### 9.3 PII 规则

| 类别 | 例子 | 落库 | 日志 | 缓存 |
|---|---|---|---|---|
| **直接 PII** | 姓名、身份证号、手机号 | **不持久化** | 禁止 | 禁止 |
| **准 PII** | 起点精确地址 | 可存 | 脱敏到区级 | 可（key 用 hash） |
| 普通 | 车次、天气、路线 | 正常 | 正常 | 正常 |

实名信息只在**内存中**用于生成深链或清单，生成后即释放。**数据库被拿走也拿不到身份证号。**

行程单分享时默认只显示到区级。

### 9.4 事务边界

必须原子的三处：

1. **幂等键插入 + `in_flight` 标记同一事务。** 否则会留下"键不存在但调用已发出"的窗口，恢复时无法判重。
2. **配额预扣 + 调用记录同事务。**
3. **consent 先落库再恢复执行**（顺序问题，非事务问题）。

### 9.5 SQLite 约束与换库条件

- 开 **WAL 模式**，写操作走单写连接或串行队列
- 事件表按会话定期清理

换库触发条件：多用户并发写 / 事件台账超规模 / 浏览器槽需跨进程协调。v1 均不触发。

---

## 10. 流式协议

### 10.1 传输分工

下行 SSE，上行 REST POST。不用 WebSocket：本场景是"服务端推、客户端偶尔发"，SSE 自带重连语义。

### 10.2 序号与补发

- `id` 会话内单调递增
- 支持 `Last-Event-ID` 与 `?since=<id>`
- 补发 `id > since` 的可重放事件，标 `replay: true`
- **补发之后必须紧跟一次全量状态快照**

完整重连语义：**补发差异 + 全量快照校准**。只补发会留下"看起来对但实际有偏差"的状态。

### 10.3 中断即关流

```
1. 图进入 interrupt
2. 发 interrupt 事件
3. 服务端【主动关闭】SSE
4. 前端渲染卡片
5. 用户决策 → POST /resume
6. 写 consent（先落库）→ 恢复图
7. 前端【新建】SSE，带 since
8. 补发 → 全量快照 → 继续实时流
```

主动关而不是保持：挂起可能 30 分钟，保持必然超时；"关闭"是明确的信号，不需要靠计时器猜。

### 10.4 心跳分两层

| 层 | 机制 | 频率 | 进事件表 |
|---|---|---|---|
| 传输层保活 | SSE 注释行 `:keepalive` | 15 s | 否 |
| 业务层进度 | `state_update` 携带进度 | 按需 | 是 |

15 秒依据：航班查询约 30 秒，常见反代空闲超时 60 秒。

### 10.5 残缺 JSON 容错

1. delta 事件是**不完整字符串片段**，前端只做展示拼接，绝不解析
2. 参数只在 `tool_call_end` 解析一次，宽容解析器容忍尾随逗号、未闭合字符串
3. **解析失败标失败，不重试**——参数不完整重试一百次还是不完整
4. 前端按 event id 去重（记录 `last_applied_id`）

要重试的是"参数完整但网络失败"，不是"参数本身是坏的"。

### 10.6 状态驱动渲染

| | 事件驱动 | **状态驱动（选这个）** |
|---|---|---|
| 渲染依据 | 按序累积事件 | 从快照渲染，事件只触发重渲染 |
| 断线 | 必须精确补全所有事件 | 取一次快照即可校准 |
| 幂等要求 | 恰好一次 | 可重复、可丢失 |

渲染依据映射：

| UI 元素 | 依据 |
|---|---|
| 聊天气泡正文 | 事件增量 + 快照中的持久化文本为准 |
| 澄清卡 | `pending_interrupt` |
| 计划预览卡 | 当前 `plan_version` + `pending_interrupt` |
| 动作确认卡 | `pending_interrupt(kind=action)` |
| 行程单 | `trip` 快照 |
| 进度指示 | `phase` + 当前工具调用 |
| 降级提示 | `degradation_log` |
| 越界提示 | `scope_rejected` |

统一入口 `GET /sessions/{id}/snapshot` 让前端成为**无状态渲染器**：首屏与重连共用一套逻辑。

### 10.7 错误分级与上行幂等

| 级别 | 事件 | 前端 | 图 |
|---|---|---|---|
| 可降级 | `degradation_notice` | 轻提示 | 继续 |
| 业务失败 | `error`(recoverable) | 提示 + 引导 | 局部继续 |
| 致命 | `error`(fatal) + `done` | 明确失败态 | FAILED |
| 会话过期 | `error`(expired) | 提示可复活 | EXPIRED |

`done` 必须携带终态（done / failed / expired）。

上行请求带 **`client_request_id`**，服务端 10 分钟窗口去重。**用户点两次"同意"不能恢复两次图**——L2 有 token 保护，计划级 approve 靠这个兜住。

---

## 11. 工程结构与架构守卫

### 11.1 目录结构（按 8 层组织）

```
travel-agent/
├── app/
│   ├── api/                  L2：routes(sessions/messages/resume/receipt/stream)
│   │                         sse.py / dedup.py
│   ├── graph/                L3：state.py / trip_graph.py / routing.py
│   │                         nodes/ / subgraphs(leg_planner, action_executor)
│   │                         llm/(context.py, schemas.py)
│   ├── policy/               L4：slots / scope / scoring / weather_rules /
│   │                         reason_trace / constraints / buffer / risk /
│   │                         idempotency / response_guard
│   ├── capabilities/         L5：contract.py / registry.py / resolver.py / specs/
│   ├── channels/             L6：base.py / mcp/ / http/ / browser/ /
│   │                         resilience.py / cache.py / quota.py
│   ├── domain/               L7：trip / leg / candidate / plan / consent /
│   │                         action / receipt / geo.py / time.py
│   ├── store/                L7：db.py / migrations/ / repositories/ / checkpointer.py
│   ├── events/               L2：types.py / serialization.py
│   ├── security/             L8：credentials / redaction / audit / expiry
│   └── config/               settings.py / weights.py
├── web/src/                  state/ / stream/ / cards/ / api/
├── tests/                    unit/policy / contract / protocol /
│                             integration / e2e / redteam
├── mcp/config.example.json
├── .env.example
└── docs/
```

### 11.2 架构守卫（CI 检查）

| 守卫 | 检查内容 |
|---|---|
| 依赖方向 | `graph/` 禁止 import `channels/` 和任何 MCP SDK |
| 决策层纯净 | `policy/` 禁止 import 任何 IO 库 |
| 能力缺席 | 禁止出现 `payment` 能力注册 |
| 高风险新增 | 新增 L1/L2 能力必须显式声明并触发人工评审 |
| 出口唯一 | 能力调用必须经 `arbiter` |
| 上下文完整 | `<role>` / `<domain>` 段永不裁剪 |
| **凭证隔离** | **API key 禁止进入 `AgentState` / checkpoint** |

**这些检查必须和单测一起跑。** 没有它，前面所有"结构保证"都只是假象。

### 11.3 LLM 接入与凭证模式

#### 11.3.1 为什么不做成二选一

"自带 key"与"用户自带 key（BYOK）"的分歧其实是两件独立的事：**谁承担成本**、**信任边界画在哪**。把两者分开后，结论是：**这个决策不应固化进架构，而应做成运行时可切换的凭证来源。**

| 问题 | 自带 key | BYOK |
|---|---|---|
| 谁付钱 | 项目方 | 使用者 |
| key 存放 | 服务端环境变量 | 用户侧（不落服务端） |
| 服务端是否高价值攻击目标 | **是**（泄露即变现） | 否 |
| 使用者门槛 | 零 | 高（需自行注册充值） |
| 与非商业个人项目的匹配度 | 偏离 | **一致** |

**匹配度判断**：原文档 §1.4 定为"非商业个人项目，可分享但不商业化"。自带 key 的本质是为使用者支付推理成本，分享面一扩大就不再是个人项目；BYOK 提供的是软件而非服务，与定位天然一致。

#### 11.3.2 凭证来源抽象

```
CredentialProvider {
  resolve(scope: server | request) -> key
}

LLM_CREDENTIAL_MODE = server | request | both
```

- `server`：从环境变量取
- `request`：从请求上下文取（**内存中，不落库**）
- `both`：优先请求携带，缺失则回落 server

`both` 覆盖真实路径：自测用 server，分享给他人时对方带自己的 key，两拨人共用同一部署。

#### 11.3.3 BYOK 的唯一正确实现：每请求透传

| 实现 | 做法 | 判断 |
|---|---|---|
| (a) 浏览器直连 LLM | key 不出浏览器 | **不可行**——图在服务端跑，LLM 调用必须在服务端 |
| (b) **每请求 header 透传** | key 只在内存与传输中，服务端不持久化 | **推荐** |
| (c) 服务端加密存储 | 加密后落库 | 不推荐——服务端成为高价值目标，且与 §6.11 冲突 |

选 (b) 的理由：它与 **DI-4（实名 PII 不持久化）** 是同一设计精神，可复用同一套审计规则。

**并要求一条逻辑一致性**：凭证比实名信息更敏感。身份证号泄露是隐私事故，API key 泄露是**立即的资金损失**。既然连身份证号都不落库，就没有理由把能直接换钱的凭证落库。

#### 11.3.4 必须写进编码规范的陷阱

> **API key 绝不能进入 `AgentState`。**

`AgentState` 会被 LangGraph 序列化进 checkpoint（SQLite）。key 一旦进 state 就等于被持久化，"服务端不存 key"的承诺当场作废。

正确做法：key 通过 LangGraph 的 `configurable` / context var 传递，只在该次调用的内存范围内可见，与 checkpoint 完全隔离。

这与 §5.5"`interrupt()` 之前的代码必须是纯的"属同一类陷阱：**看起来无害，但会静默破坏某项承诺。**

可行的前提：BYOK 模式下 key 每次请求都重新携带，而这个场景本来就是"用户在操作"（发消息、点同意），不会出现"后台无人时拿不到 key"。

#### 11.3.5 DeepSeek 三个要点

**其一：用 chat 类模型，不用 reasoner。** 这是 P1 的红利——裁决权在确定性代码里，LLM 只做抽取与渲染，不需要强推理。且 DC-1 已定推理不外发，reasoner 无额外收益，只会更慢更贵。

**其二：结构化输出可靠性必须在 M0 验证。** `intake` 节点的 schema 含必填 `scope` 字段与槽位补丁结构。若模型不能稳定产出严格合规的 JSON，整套"语义归 LLM"的设计会打折扣。**M0 加一个 spike：用真实 prompt 跑 50 次统计 schema 合规率**，达标（建议 >98%）直接用；不达标则给节点加"Pydantic 校验 + 一次修复重试"兜底。

DeepSeek 为 OpenAI 兼容协议，改 `base_url` 即可接入，验证成本低。

**其三：注意上下文固定开销。** §4 的五段装配中 `<role>` / `<domain>` 永不裁剪，等于**每轮调用都携带固定前缀开销**。多轮澄清 + 多 leg + 多节点会放大它。若 DeepSeek 支持 prompt caching，这两段稳定前缀正适合缓存，建议 M2 前后实测。

#### 11.3.6 成本归属

| 模式 | 记账 | 熔断 |
|---|---|---|
| `server` | 记到项目成本 | **需要全局预算熔断**（超限拒绝服务，而非继续烧钱） |
| `request` | 记到该请求 | 不需要熔断，仅向用户展示消耗 |

**注意**：LLM 记账与 §7.6 的外部数据源配额规则不同。外部源有"调用失败不扣费"的说法，LLM 调用失败也可能计费，所以**不能复用预扣/回滚那套**，需单独处理。

#### 11.3.7 预算与熔断配置

**已定**：`server` 模式月预算上限 **50,000,000 token**。

**消耗模型**（用于验证该数值是否合理）：

| 阶段 | 节点 | 次数 |
|---|---|---|
| COLLECT | `intake`（每轮用户输入一次） | 5 |
| COLLECT | `clarify`（追问，上限 4 轮） | 3 |
| PREVIEW | `preview_render` | 1 |
| PREVIEW | `reason`（每条 leg 一次） | 2～3 |
| EXECUTE | `remind` 等 | 1 |
| | **合计** | **约 13～15 次** |

| 上下文段 | 估算 token |
|---|---|
| `<role>` + `<domain>`（永不裁剪） | ~800 |
| `<task>` + 输出 schema | ~600 |
| `<state>` 状态摘要 | ~800 |
| `<input>` | ~200 |
| 输入小计 | ~2,400 |
| 输出（均值） | ~300 |
| **单次调用合计** | **~2,700** |

**单次规划 ≈ 15 × 2,700 ≈ 4 万 token**（含重试取 4～6 万）。

**换算**：50,000,000 ÷ 50,000 ≈ **1,000 次规划/月 ≈ 33 次/天**。

> 设计红利：因为 §4.2 定的上下文装的是**状态摘要**而非完整对话史，单次调用输入**有界**，不随对话轮数线性增长。若改为回放完整聊天记录，可支撑次数会掉一个数量级。

**结论：50M 对自测版不是约束，而是安全网。** 这改变了熔断的设计目标——不是为了省日常用量，而是为了**在出 bug 时止损**。

**三道闸（只设月预算有一个明显缺口：某天出循环 bug 一天就能烧光整月额度）**：

| 闸 | 建议值 | 作用 |
|---|---|---|
| **日预算** | 月额 ÷ 20 = **2.5M**（≈50 次规划） | 半天内止损 |
| **单会话上限** | **500K**（正常的 10 倍余量） | 防会话级死循环 |
| **月预算** | **50M** | 总兜底 |

**记账口径**：DeepSeek 输入/输出价格不同，缓存命中更便宜。只记一个总数会导致预算与账单脱节。建议分三个量：

```
llm_usage { input_tokens, output_tokens, cached_input_tokens }
```

按价格折算成**标准 token 当量**再与 50M 比较。折算系数取一次当前价目，后续调价只改系数。

**熔断行为**：

| 用量 | 动作 |
|---|---|
| 80% | 告警 |
| 95% | **切精简模式**：跳过 LLM 渲染类节点（`reason` / `preview_render` / `remind` 改用确定性模板） |
| 100% | 停止 LLM 服务，明确告知（不静默失败） |

95% 那档值得留意：**降级方式是"同一件事用更省的方式做"，而不是"少服务几个人"**。因为规划、打分、约束校验、深链、清单全部不依赖 LLM，即使 LLM 停了用户仍能拿到可执行的行程，只是文案变成模板。这是 P1 换来的抗风险能力。

窗口按自然月重置，与 §7.6 的配额池共用同一套窗口机制。

#### 11.3.8 建议方案

| 阶段 | 模式 | 配套要求 |
|---|---|---|
| v1 自测版 | `server` | 加**最小访问控制**（共享 token 即可），**不公开部署** |
| 分享给他人 | `request`（BYOK） | 每请求透传、不持久化、不进 checkpoint |
| 架构层 | 现在就做 `CredentialProvider` 抽象 | 增量很小，省掉一次返工 |
| **永久禁止** | **自带 key + 无鉴权公开访问** | 唯一绝对不能出现的组合 |

最后一行与 P7 同源：**不想被刷爆，就不要给出无鉴权的公开通道。**

---

## 12. 里程碑与验收映射

### 12.1 里程碑

| 阶段 | 交付 | 覆盖决策 | 验收 |
|---|---|---|---|
| **M0** 骨架 | store + checkpointer；capabilities + resolver；channels/mcp + resilience/cache/quota；policy/slots + scope + response_guard；graph 空骨架；api/sse；**CredentialProvider 抽象 + 结构化输出 spike** | D0、DC-5、DC-6、DG、DI、DJ、**DL** | 至少一个 MCP 工具可调用；空跑澄清→闸门→执行；**结构化输出 schema 合规率达标** |
| **M1** local 单 leg | place/route/weather；leg_planner；scoring + weather_rules + reason_trace；consent_gate | DF、DE | "朝阳区某点→长城"出带理由方案 |
| **M2** 流协议 | sse 完整 + snapshot；前端状态驱动；残缺 JSON 容错 | DJ（深化） | 信息齐才执行；可暂停续传；残缺 JSON 不崩 |
| **M3** intercity | rail + transfer；constraints + buffer；replan | DE-4、DF-5、DF-6 | 跨省多段衔接正确（含中转） |
| **M4** 航班/联运/美团 | flight / multimodal / poi；variflight 配额池 | DG-3、DG-6 | 含中转与价格的方案 |
| **M5** 半自动购票 | browser 通道；booking 能力；action_executor + arbiter + ledger + token；receipts | DH 全部 | 查询自动、付款需确认、幂等生效 |
| **M6** 自测版 | e2e 黄金行程；redteam | 全部 | 真实行程端到端 |

**关键排序**：**D0（R10）与 DC-5 必须在 M0 落地**。它们是结构性的，等 M5 才补等于回头改所有节点和调用路径。

**起手式：M0 + M1**（不依赖美团额度与 didi 可用性）。

### 12.2 验收映射

| 变更面 | 测试位置 | 验证方式 |
|---|---|---|
| 槽位校验 | `tests/unit/policy/test_slots.py` | 缺槽位必须追问、不得进入执行 |
| 闸门 | `tests/integration/test_gates.py` | 暂停/恢复、拒绝分支、同意前 0 次非只读调用 |
| 工具契约 | `tests/contract/` | 每个能力 schema 与错误路径 |
| 流协议 | `tests/protocol/` | 事件序列契约、残缺 JSON 容错 |
| 约束求解 | `tests/unit/policy/test_constraints.py` | 跨段衔接/预算/时间窗边界 |
| 断线续传 | `tests/integration/test_resume.py` | 从 checkpoint 续且不重复下单 |
| 端到端 | `tests/e2e/` | 黄金行程（local + intercity） |

**四条可度量验收的实现方式**：

| 验收 | 怎么测 |
|---|---|
| 信息闸门 100% 拦截 | 对每个 required 槽位逐个置空，断言必须追问且不进入 READY |
| 同意前 0 次非只读调用 | 注入 fake channel 记录所有调用，断言 consent 前无 L1/L2，且每个 L0 都带 `consent_ref` |
| 断线恢复不重复下单 | 构造 `in_flight` 后模拟崩溃，重启断言走 `reconcile` 而非重新调用 |
| 理由含可指认因子 | 断言 ReasonTrace 至少一个 weather/preference 条目；渲染文本引用的都在 top-3 内 |

**本框架新增的验收项**：

| 新增项 | 来源 |
|---|---|
| R10 红队用例集（漏拒/误拒/注入/长对话保真） | D0 |
| 幂等键对参数顺序变化稳定 | DH-2 |
| 缓存命中标记为 D1 | DG-2 |
| 配额回滚正确 | DG-3 |
| plan_hash 不匹配返回 409 | DE-2 |
| 复活后必须重新确认 | DE-5 补充 |
| 幂等插入与 in_flight 同事务 | DI-5 |
| 前端事件去重 | DJ-7 |
| 架构守卫全部通过 | 11.2 |
| 结构化输出 schema 合规率（50 次采样） | DL-8 |
| API key 不出现在 checkpoint / 日志中 | DL-4、11.2 凭证隔离守卫 |

---

## 13. 决策台账（64 条，已锁定）

| 组 | 编号 | 内容 |
|---|---|---|
| 领域边界 | D0 | 情感类边缘归 `ambiguous`，归一化承接，不做情感疏导 |
| 框架基线 | DC-1 | 推理过程不外发（本地 trace only） |
| | DC-2 | 追问预算 4 轮 |
| | DC-3 | v1 住宿只推荐 + 深链 |
| | DC-4 | 深链生成 / 清单导出定 L0，PII 脱敏 |
| | DC-5 | 不注册 payment 能力 |
| | DC-6 | 浏览器读 L0 / 写 L1 |
| 编排与同意 | DE-1 | 统一用 interrupt 等待 |
| | DE-2 | plan_hash 不匹配 → 409 回 PREVIEW |
| | DE-3 | 假设清单并入预览卡置顶 + 显式回应 |
| | DE-4 | L0 批内单项失败不阻断整批 |
| | DE-5 | TTL 30 分钟 / checkpoint 7 天；可复活但需重新确认 |
| 决策策略 | DF-1 | 偏好分硬约束 / 软权重 |
| | DF-2 | 天气分硬否决 / 软惩罚 |
| | DF-3 | 数据不确定度进打分，λ=0.15 |
| | DF-4 | 理由只引用 top-3，且必须含天气或偏好因子 |
| | DF-5 | buffer 动态计算 |
| | DF-6 | replan 先局部后全图，上限 3 次 |
| | DF-7 | 输出保留 rejected 列表 |
| 连接适配 | DG-1 | L1/L2 永不自动重试 |
| | DG-2 | 缓存命中记为 D1 |
| | DG-3 | 配额预扣 + 失败回滚 |
| | DG-4 | 每能力独立超时表 |
| | DG-5 | 浏览器独占 + 前台 + 只读限频 |
| | DG-6 | HTTP 常驻；探测只用免费接口 |
| 执行层 | DH-1 | 裁决器为唯一调用出口 |
| | DH-2 | 幂等键用业务指纹 |
| | DH-3 | 幂等唯一约束建在 DB 层 |
| | DH-4 | 执行前复核 |
| | DH-5 | L2 用一次性 confirm_token |
| | DH-6 | 订单状态只由用户回执推进 |
| | DH-7 | 浏览器动作带断言，失败转 reconcile |
| 存储 | DI-1 | 业务表为唯一真相源 |
| | DI-2 | 只落库可重放事件 |
| | DI-3 | 配额三态记账 |
| | DI-4 | 实名 PII 不持久化 |
| | DI-5 | 幂等插入与 in_flight 同事务 |
| | DI-6 | SQLite WAL + 单写队列 |
| 流协议 | DJ-1 | 事件携带 phase + plan_version_id |
| | DJ-2 | 重连 = 补发 + 全量快照 |
| | DJ-3 | interrupt 主动关流 |
| | DJ-4 | 心跳分两层 |
| | DJ-5 | 前端状态驱动 + snapshot |
| | DJ-6 | 上行 client_request_id 去重 |
| | DJ-7 | 参数仅 end 时解析；失败不重试；前端去重 |
| 工程 | DK-1 | 目录按 8 层组织 |
| | DK-2 | CI 架构守卫 |
| | DK-3 | R10 与 DC-5 必须在 M0 落地 |
| | DK-4 | 验收映射 + 新增验收项作为测试入口 |
| | DK-5 | 起手式 M0 + M1 |
| LLM 接入 | DL-1 | LLM 选型 = DeepSeek |
| | DL-2 | 抽象 `CredentialProvider`，支持 server / request / both |
| | DL-3 | v1 自测用 server 模式 + 最小访问控制 + 不公开部署 |
| | DL-4 | 分享时切 request；key 每请求透传、不持久化、不进 checkpoint |
| | DL-5 | 禁止"自带 key + 无鉴权公开访问" |
| | DL-6 | 用 chat 类模型，不用 reasoner |
| | DL-7 | LLM 用量单独记账；server 模式需全局预算熔断 |
| | DL-8 | M0 增加结构化输出可靠性 spike 与验收项 |
| LLM 预算 | DN-1 | `server` 模式月预算 = **50,000,000 token** |
| | DN-2 | 增设日预算闸（月额 ÷ 20 = 2.5M），防单日跑飞 |
| | DN-3 | 增设单会话上限 500K，防会话级死循环 |
| | DN-4 | 记账分 input / output / cached-input，折算为标准 token 当量再比对阈值 |
| | DN-5 | 熔断 80% 告警 / 95% 切精简模式 / 100% 停 LLM 并明确告知 |
| | DN-6 | 窗口按自然月重置，与 §7.6 配额池共用机制 |

---

## 14. 待审批：5 项开放问题

这些不是技术决策，涉及成本、账号与时间投入，需要项目所有人拍板。

| 编号 | 问题 | 影响 | 建议 |
|---|---|---|---|
| ~~**OPEN-1**~~ | ~~LLM 选型与预算~~ | **已完全解决**：选型 = DeepSeek；凭证模式见 §11.3（DL-1～DL-8）；月预算 50M token（DN-1～DN-6） | — |
| **OPEN-2** | 高德 key 的自认证时间 | §5.3 明确"自认证起仅 1 年" | 现在就确认认证日期，写进 `security/expiry.py` 警戒配置。**这是唯一的硬性时间炸弹** |
| **OPEN-3** | 是否走浏览器 MCP 做写操作 | M5 门票预约依赖它，而 §9 承认"浏览器 MCP 是第三方小项目" | M5 之前只做只读，写操作推迟到 M6 之后，先用深链兜住 |
| **OPEN-4** | 美团 ht-ai 额度 | 影响 M4 的景点/酒店能力 | 注册后查账单；不阻塞 M0～M3 |
| **OPEN-5** | didi 打车 MCP 是否可用 | 只影响末段打车 | 先不上，走深链跳转 |

---

## 15. 残余风险登记

| 风险 | 等级 | 说明 | 缓解 |
|---|---|---|---|
| 高德免费额度 1 年后到期 | **高** | 唯一的硬性时间炸弹 | 到期警戒 + provider 可替换（channels 层已隔离） |
| 免费源非官方、可能限流 | 中 | 原文档已识别 | 降级阶梯 + 缓存 + 配额记账 |
| Variflight 体验金耗尽 | 中 | 积分制非免费 | 余额告警 + 航班能力可整体摘除 |
| 浏览器风控（12306） | 中 | DC-6 免确认带来的新风险 | 只读限频 + 前台执行 + 会话独占 |
| 浏览器 MCP 第三方依赖 | 中 | 原文档已识别 | Playwright MCP 做备 |
| SQLite 单写者 | 低（v1） | 多用户时触发 | 换库触发条件已写明 |
| 部分失败产生不自洽输入 | 低 | DE-4 的代价 | 打分器容缺维 + `degradation_log` 随版本存档 |
| 30 分钟 TTL 对上班场景偏短 | 低 | DE-5 的代价 | 复活机制 + 复活后重新确认 |

---

## 附：与原文档的对照索引

| 本文档 | 原文档 |
|---|---|
| §3 分层架构 | 补充 §4（拆出能力契约层） |
| §4.1 能力契约 | 落实 §6.10 第 1 条 |
| §4.2 状态契约 | 落实 §3.6、§3.1 必需槽位 |
| §4.3 事件契约 | 落实 §3.3 |
| §5 编排图 | 落实 §3.1 双闸门、§3.6 状态机 |
| §6 决策策略 | 落实 §3.5 天气与偏好 |
| §7 连接适配 | 落实 §6.10 六条要求、§6.11 |
| §8 执行层 | 落实 §3.2 风险分级、§3.8 购票 M1+M2、§7 浏览器层 |
| §9 存储 | 补充 §4 持久化 |
| §10 流协议 | 落实 §3.3 |
| §11 工程结构 | 补充 §4 工程结构 |
| §12 里程碑与验收 | 落实 §8、§12 |
