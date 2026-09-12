# M0 接口冻结说明

本文件是 M0 阶段各模块之间的**接口契约**。并行实现的模块必须严格遵守，
任何一方需要改接口，先在 `docs/m0-interfaces.md` 修改并通知其他模块。

设计依据见 `agent-framework.md`（章节号在文中引用）。

## 0. 运行环境

```
解释器   .venv\Scripts\python.exe   (Python 3.13)
测试     .venv\Scripts\python.exe -m pytest -q
类型风格 from __future__ import annotations + pydantic v2 BaseModel
```

**未装依赖前先用 `.venv` 装机命令**（见任务说明）。pydantic / pytest / httpx 已可用。

## 1. 已冻结的公共类型（不要改动签名）

| 类型 | 位置 |
|---|---|
| `RiskLevel` / `ChannelKind` / `DegradationLevel` / `ResultStatus` | `app/capabilities/contract.py` |
| `Provenance` / `Degradation` / `Cost` / `CapabilityResult` / `CapabilitySpec` | `app/capabilities/contract.py` |
| `CapabilityRegistry` / `CapabilityBinding` | `app/capabilities/registry.py` |
| `Event` / `EventType` / `REPLAYABLE_EVENT_TYPES` | `app/events/types.py` |
| `Phase` / `ScopeState` / `ScopeVerdict` / `SlotValue` / `SlotStatus` / `SlotSource` | `app/domain/models.py` |
| `PendingInterrupt` / `PendingInterruptKind` / `PlanVersionMeta` / `ConsentRef` / `ConsentScopeKind` / `ActionState` / `ActionStatus` / `DegradationNote` | `app/domain/models.py` |
| `Coord` / `Crs` / `normalize_key` / `assert_same_crs` | `app/domain/geo.py` |
| `to_ymd` / `parse_ymd` / `now_local` / `combine_ymd_hm` / `minutes_between` | `app/domain/timebase.py` |
| `new_id` | `app/domain/ids.py` |
| `Settings` / `get_settings` | `app/config/settings.py` |
| 异常族（`CapabilityNotFound` / `IllegalPreConsentCall` / `PlanVersionStale` / …） | `app/errors.py` |

## 2. Policy 层接口（L4，纯确定性，禁止任何 IO）

**硬约束：`app/policy/**` 不得 import `httpx`、`openai`、`sqlite3`、`mcp`、
`app.channels`、`app.store`、`app.graph`。** 只能 import 标准库、pydantic、
`app.domain`、`app.errors`、`app.capabilities.contract`（仅类型）、`app.config.weights`。

### 2.1 `app/policy/slots.py`

```python
class Requirement(StrEnum): ALWAYS = "always"; CONDITIONAL = "conditional"; OPTIONAL = "optional"

class SlotSpec(BaseModel):
    slot_id: str
    title: str                     # 中文短名，用于追问
    requirement: Requirement
    condition: str | None = None   # CONDITIONAL 时的条件键，如 "intent:booking"
    ask_template: str              # 追问话术
    disambiguation: str | None = None
    default: Any = None            # user_declined 时的保守默认

SLOT_REGISTRY: tuple[SlotSpec, ...]        # 必需槽位见 framework §4.2

class SlotValidation(BaseModel):
    ok: bool
    missing: list[str]                         # 需要追问的 slot_id
    resolved: list[str]
    assumptions: list[str]                     # 到轮次上限时使用的默认值说明

def active_required(context: Mapping[str, Any]) -> list[SlotSpec]: ...
def validate(slots: Mapping[str, SlotValue], *, context: Mapping[str, Any],
             rounds_used: int, max_rounds: int) -> SlotValidation: ...
def apply_defaults(slots: dict[str, SlotValue], missing: list[str]) -> list[str]: ...
```

规则（DD-2 / §4.2）：

* `SlotStatus.USER_DECLINED` 与 `FILLED` 一样算**已解决**，不产生追问。
* `rounds_used >= max_rounds` 且仍有缺失 → `ok=True`，走 `apply_defaults` 产生 assumptions。
* `ok=False` 时**禁止**进入执行——这是 §12 第一条验收的落点。

### 2.2 `app/policy/scope.py`（R10）

```python
IN_SCOPE_TOPICS: frozenset[str]     # 旅行邻域白名单
OUT_OF_SCOPE_TOPICS: frozenset[str]
INJECTION_PATTERNS: tuple[re.Pattern[str], ...]

class ScopeDecision(BaseModel):
    verdict: ScopeVerdict
    reason: str
    is_injection: bool = False

def prefilter(text: str) -> ScopeDecision | None    # None = 交给 LLM 判定
def apply(state: ScopeState, verdict: ScopeVerdict, *, is_injection: bool = False) -> ScopeState
def is_fused(state: ScopeState, *, limit: int) -> bool
def refusal_text(state: ScopeState) -> str          # 确定性模板，不经 LLM
```

规则（D0 / R10）：

* `OUT_OF_SCOPE` → `rejected_count += 1`，`off_topic_streak += 1`。
* 注入类 → `strikes += 1`，且**不使用常规拒答话术**。
* `AMBIGUOUS` → 归一化承接一次（`off_topic_streak` 不加）。
* `strikes >= limit` → 会话终止（`FAILED(SCOPE_ABUSE)`）。

### 2.3 `app/policy/scoring.py`、`weather_rules.py`、`reason_trace.py`

```python
# scoring.py
FEATURE_KEYS: tuple[str, ...]   # f_time,f_cost,f_transfer,f_walk,f_punctual,f_comfort,f_access,f_risk
def normalize_features(raw: Mapping[str, float | None]) -> dict[str, float | None]
def score(*, features, weights: Mapping[str, float], penalties: Sequence[float],
          uncertainty: DegradationLevel, lam: float) -> ScoreBreakdown
class ScoreBreakdown(BaseModel):
    total: float; contributions: dict[str, float]; missing: list[str]; uncertainty_penalty: float

# weather_rules.py
class WeatherFactor(StrEnum): PRECIPITATION,WIND,THUNDERSTORM,VISIBILITY,TEMPERATURE,AQI,ICE
class FactorAssessment(BaseModel): factor, severity: float, hard_veto: bool, note: str
class WeatherAssessment(BaseModel):
    factors: list[FactorAssessment]; vetoes: list[str]; penalties: list[float]; notes: list[str]
def assess(weather: Mapping[str, Any], *, modes: Sequence[str]) -> WeatherAssessment

# reason_trace.py
class ContributorKind(StrEnum): PREFERENCE,WEATHER,CONSTRAINT,UNCERTAINTY
class Contributor(BaseModel): kind, key, weight, feature, contribution, evidence: dict
class ReasonTrace(BaseModel):
    candidate_id: str; score_total: float
    contributors: list[Contributor]; vetoes: list[str]
    assumptions: list[str] = []; degradation: list[str] = []
def build(candidate_id, breakdown, weather, *, evidence: Mapping[str, dict],
          assumptions=(), degradation=()) -> ReasonTrace
def citable(trace: ReasonTrace, k: int = 3) -> list[Contributor]     # 按 |contribution| 排序取前 k
def is_explainable(trace: ReasonTrace) -> bool   # §12 第 4 条：至少一个 weather/preference 且 contribution != 0
def render_hint(trace: ReasonTrace, k: int = 3) -> str   # 交给 LLM 渲染时的结构化提示
```

### 2.4 `app/policy/constraints.py`、`buffer.py`

```python
# buffer.py
BUFFER_TABLE_MINUTES: dict[str, int]     # flight/high_speed/train/local_transfer/cross_station
UNCERTAINTY_BUFFER_MULTIPLIER: dict[int, float]   # {0:1.0, 1:1.5, 2:1.5, 3:1.5}
def required_buffer_minutes(*, prev_mode: str, cross_station: bool,
                            uncertainty: DegradationLevel) -> int

# constraints.py
class Violation(BaseModel): kind: str; leg_index: int; detail: str; recoverable: bool
def validate_trip(legs: Sequence[Any], *, constraints: Mapping[str, Any]) -> list[Violation]
```

`validate_trip` 必须包含坐标一致性检查：混用 CRS → 抛
`CoordinateSystemMismatch`（是 bug，不是降级）。

### 2.5 `app/policy/risk.py`（裁决器，DH-1）

```python
class Decision(StrEnum): ALLOW = "allow"; DENY = "deny"
class Arbitration(BaseModel):
    decision: Decision; reason: str; capability_id: str; risk: RiskLevel

class Arbiter:
    def __init__(self, registry: CapabilityRegistry) -> None: ...
    def decide(self, *, capability_id: str, plan_consented: bool,
               consent_ref: str | None,
               has_action_consent: bool = False,
               confirm_token_ok: bool = False) -> Arbitration: ...
```

规则：

* 未注册能力 → `CapabilityNotFound`。
* L0 且 `plan_consented=False` → `DENY(ILLEGAL_PRE_CONSENT)`。
* L1 → 需要 `has_action_consent`。
* L2 → 需要 `has_action_consent` 且 `confirm_token_ok`。
* `ALLOW` 时必须带 `consent_ref`，否则 `DENY`。

### 2.6 `app/policy/idempotency.py`（DH-2）

```python
def normalize_params(params: Mapping[str, Any]) -> dict[str, Any]
def subject_fingerprint(action_kind: str, subject: Mapping[str, Any]) -> str
def idem_key(*, session_id: str, action_kind: str, subject: Mapping[str, Any]) -> str
```

规范化规则：去空白、日期统一 `YYYY-MM-DD`、坐标统一到 WGS-84 4 位小数、
乘车人/票种列表排序。**同一业务动作在参数顺序变化时必须得到相同 key。**

### 2.7 `app/policy/response_guard.py`（R10 第 4 层）

```python
class GuardResult(BaseModel): ok: bool; reason: str = ""; replacement: str | None = None
MAX_VISIBLE_CHARS: int
def check(text: str) -> GuardResult
def fallback_text(reason: str) -> str
```

## 3. Channels 层接口（L6）

### 3.1 `app/channels/base.py`

```python
class HealthState(StrEnum): HEALTHY = "healthy"; DEGRADED = "degraded"; UNAVAILABLE = "unavailable"

class HealthStatus(BaseModel):
    adapter_id: str; state: HealthState = HealthState.HEALTHY
    last_check: datetime | None = None; fail_count: int = 0; detail: str = ""

class ChannelAdapter(ABC):
    id: ClassVar[str]
    kind: ClassVar[ChannelKind]
    def supports(self, capability_id: str, remote_name: str) -> bool: ...
    async def health(self) -> HealthStatus: ...
    async def invoke(self, binding: CapabilityBinding, params: dict[str, Any], *,
                     timeout: float) -> CapabilityResult: ...
    async def aclose(self) -> None: ...
```

### 3.2 `app/channels/mcp/adapter.py`

* stdio JSON-RPC 2.0 客户端，方法：`initialize` → `tools/list` → `tools/call`。
* 启动超时用 `settings.channel_connect_timeout_seconds`（5s），**失败不抛，标 `UNAVAILABLE`**。
* `health()` 只做协议层握手，**不做业务调用**（DG-6）。
* 参数/结果通过 `CapabilityBinding.param_map` / `result_map` 映射。

### 3.3 `app/channels/resolver.py`（provider 链 + 降级阶梯）

```python
class CapabilityResolver:
    def __init__(self, registry, adapters: Mapping[str, ChannelAdapter],
                 cache: CacheStore | None = None, quota: QuotaLedger | None = None,
                 arbiter: Arbiter | None = None): ...
    async def call(self, capability_id: str, params: dict[str, Any], *,
                   call_context: CallContext | None = None) -> CapabilityResult: ...

class CallContext(BaseModel):
    session_id: str | None = None
    plan_consented: bool = False
    consent_ref: str | None = None
    has_action_consent: bool = False
    confirm_token_ok: bool = False
    allow_network: bool = True
```

解析顺序：新鲜缓存(D1) → 主 provider(D0) → 备源 → 过期缓存(D1) → D2 → D3。
**`arbiter` 存在时，任何调用必须先裁决。** 这是"同意前 0 次非只读调用"的落点。

### 3.4 `app/channels/cache.py`

```python
class CacheStore:
    def __init__(self, db: Database, *, coord_precision: int = 4): ...
    def make_key(self, capability_id: str, params: Mapping[str, Any]) -> str
    def get(self, key: str, *, now: datetime | None = None) -> CachedEntry | None   # 过期也返回，标 stale
    def put(self, key: str, result: CapabilityResult, *, ttl_seconds: int) -> None
    def get_fresh(self, key) -> CachedEntry | None
```

**不缓存 `status=ERROR` 的结果；`contains_pii` 的条目不写。**

### 3.5 `app/channels/quota.py`

```python
class QuotaState(StrEnum): RESERVED,COMMITTED,ROLLED_BACK
class QuotaDecision(StrEnum): OK,WARN,DEGRADE,EXHAUSTED
class QuotaLedger:
    def __init__(self, db, *, limits: Mapping[str, int], warn_ratio: float, degrade_ratio: float): ...
    def reserve(self, pool: str, units: int, *, idem: str) -> QuotaDecision
    def commit(self, idem: str) -> None
    def rollback(self, idem: str) -> None
    def used(self, pool: str) -> int
```

`used = Σ committed + Σ reserved`。**同一 `idem` 重复 reserve 必须幂等。**

### 3.6 `app/channels/resilience.py`

```python
class ErrorKind(StrEnum): NETWORK,BUSINESS,QUOTA,TIMEOUT,UNKNOWN
def classify(exc: BaseException) -> ErrorKind
def should_retry(kind: ErrorKind, *, risk: RiskLevel) -> bool   # L1/L2 永远 False (DG-1)
async def with_timeout(coro, seconds: float): ...
```

## 4. Store 层接口（L7）

### 4.1 `app/store/db.py`

```python
class Database:
    def __init__(self, path: Path | str): ...
    def connect(self) -> sqlite3.Connection          # WAL, row_factory=sqlite3.Row
    def migrate(self) -> None                        # 幂等，建全部表
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]   # 单写串行
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]
```

### 4.2 表（DD-1：业务表是唯一真相源）

```
sessions(session_id PK, user_id, phase, created_at, last_active_at,
         suspended_until, expires_at, scope_json, status)
plan_versions(plan_version_id PK, session_id, version_no, plan_hash,
              slots_snapshot_json, legs_json, actions_json, cost_estimate,
              degradation_json, assumptions_json, status, created_at)
consents(consent_id PK, session_id, plan_version_id, plan_hash, scope_kind,
         action_id, granted_at, channel, superseded_by)
action_ledger(idem_key PK, session_id, plan_version_id, action_id,
              capability_id, subject_fingerprint, params_hash, consent_ref,
              status, attempt, started_at, finished_at, result_ref,
              evidence_json, failure_json)
events(seq INTEGER PK AUTOINCREMENT, session_id, type, payload_json, ts, replayable)
cache_entries(cache_key PK, capability_id, payload_json, provenance_json,
              fetched_at, ttl_seconds, expires_at, contains_pii)
quota_entries(idem PK, pool_id, window_key, units, state, ts)
receipts(receipt_id PK, session_id, action_id, idem_key, order_no, status,
         note, attachment_ref, created_at)
audit_log(audit_id PK, session_id, ts, actor, event_kind, subject, ref_id, metadata_json)
browser_slots(slot_id PK, session_id, leased_at, expires_at, state)
llm_usage(id PK, session_id, window_day, input_tokens, output_tokens,
          cached_input_tokens, standard_equivalent, ts)
```

**不变式（必须写测试）：**

1. `action_ledger.idem_key` 唯一约束由**数据库**保证。
2. 幂等键插入与 `in_flight` 标记必须在**同一事务**（DI-5）。
3. `events` 只落 `REPLAYABLE_EVENT_TYPES`（DI-2）。

### 4.3 Repository API（每个 repo 构造参数都是 `Database`）

```python
SessionsRepo: create/get/update_phase/touch/set_pending?/list_expired
PlansRepo:    insert/get/latest_for_session/set_status
ConsentsRepo: insert/get_active(session_id, plan_hash)/supersede_all_for_session
LedgerRepo:   insert_in_flight(...) -> bool   # False 表示幂等键已存在（自动判重）
              mark_completed/mark_failed/get/skip_remaining
EventsRepo:   append(session_id, event) -> int; list_since(session_id, since) ; prune(...)
QuotaRepo / CacheRepo / ReceiptsRepo / AuditRepo / LlmUsageRepo 同理
```

## 5. Graph 与 API（由主 agent 实现，其他模块不要碰）

`app/graph/**`、`app/api/**`、`app/llm/**` 由主 agent 负责。子模块只依赖
上面 1–4 节的接口。

## 6. 通用约定

* 所有时间用 `app.domain.timebase` 里的工具函数，不直接 `datetime.now()`。
* 日志用 `logging.getLogger(__name__)`；**任何凭证/PII 一律不得进日志**。
* 中文注释可以，但代码标识符用英文。
* 不要新增第三方依赖。
* 每个模块自带 `__init__.py` 导出公共符号。
