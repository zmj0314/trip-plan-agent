import type { Health, LLMProfileInput, ProbeResult, Snapshot } from "../api/types";
import { ModelPanel } from "./ModelPanel";

const SLOT_LABELS: Record<string, string> = {
  origin_address: "出发地",
  destination: "目的地",
  depart_date: "出发日期",
  travelers: "同行人",
  intent: "意图",
  time_window: "时间窗",
  transport_preference: "交通偏好",
  budget: "预算",
  mobility_needs: "通行需求",
  lodging: "住宿"
};

const STATUS_LABELS: Record<string, string> = {
  pending: "待执行",
  in_flight: "执行中",
  completed: "已完成",
  failed: "失败",
  skipped: "已跳过",
  blocked: "受阻"
};

function statusClass(status: string): string {
  if (status === "completed") return "pill pill--ok";
  if (status === "failed" || status === "blocked") return "pill pill--bad";
  if (status === "skipped") return "pill pill--warn";
  return "pill pill--idle";
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.map((v) => String(v)).join(" / ");
  if (typeof value === "object") return renderObject(value as Record<string, unknown>);
  return String(value);
}

const OBJECT_KEYS: Record<string, string> = {
  count: "人数",
  types: "票种",
  accessibility: "通行",
  real_name: "实名"
};

/** Slot values may be structured; show them readably instead of raw JSON. */
function renderObject(value: Record<string, unknown>): string {
  const parts = Object.entries(value)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([key, v]) => {
      const label = OBJECT_KEYS[key] ?? key;
      const rendered = Array.isArray(v) ? v.map((x) => String(x)).join("/") : String(v);
      return `${label} ${rendered}`;
    });
  return parts.length ? parts.join(" · ") : "—";
}

interface Props {
  snapshot: Snapshot | null;
  health: Health | null;
  onProfileChange: (profile: LLMProfileInput) => void;
  onProbe: (profile: LLMProfileInput) => Promise<ProbeResult>;
}

export function Dossier({ snapshot, health, onProfileChange, onProbe }: Props) {
  const slots = Object.entries(snapshot?.slots ?? {});
  const actions = snapshot?.actions ?? [];
  const degradation = snapshot?.degradation_log ?? [];

  return (
    <aside className="dossier">
      <div className="dossier__head">
        <span>行程卷宗</span>
        <span>{snapshot?.phase ?? "—"}</span>
      </div>

      <ModelPanel health={health} onProfileChange={onProfileChange} onProbe={onProbe} />

      <section className="block">
        <div className="block__title">需求槽位</div>
        {slots.length === 0 ? (
          <div className="specimen">尚未收集</div>
        ) : (
          <dl className="kv">
            {slots.map(([key, slot]) => (
              <div key={key} style={{ display: "contents" }}>
                <dt>{SLOT_LABELS[key] ?? key}</dt>
                <dd className={slot.status === "filled" ? "" : "empty"}>
                  {renderValue(slot.value)}
                  {slot.status === "user_declined" && "（用户委托）"}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <section className="block">
        <div className="block__title">计划版本</div>
        <dl className="kv">
          <dt>版本</dt>
          <dd>{snapshot?.current_plan_version_id ?? "—"}</dd>
          <dt>状态</dt>
          <dd>{snapshot?.plan_status ?? "—"}</dd>
          <dt>指纹</dt>
          <dd style={{ fontFamily: "var(--mono)", fontSize: "11px" }}>
            {snapshot?.plan_hash ? `${snapshot.plan_hash.slice(0, 16)}…` : "—"}
          </dd>
        </dl>
      </section>

      <section className="block">
        <div className="block__title">执行动作</div>
        {actions.length === 0 ? (
          <div className="specimen">同意后才会执行</div>
        ) : (
          <div style={{ display: "grid", gap: "9px" }}>
            {actions.map((action) => (
              <div
                key={action.action_id}
                style={{ display: "flex", justifyContent: "space-between", gap: "8px" }}
              >
                <span style={{ fontFamily: "var(--mono)", fontSize: "11px" }}>
                  {action.capability_id}
                </span>
                <span className={statusClass(action.status)}>
                  {STATUS_LABELS[action.status] ?? action.status}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      {degradation.length > 0 && (
        <section className="block">
          <div className="block__title">数据等级</div>
          <div>
            {degradation.map((note, index) => (
              <span key={`${note.capability_id}-${index}`} className="stamp">
                D{note.level} · {note.capability_id}
              </span>
            ))}
          </div>
          <div className="specimen" style={{ marginTop: "9px" }}>
            降级数据会降低该方案的置信度
          </div>
        </section>
      )}

      <section className="block" style={{ borderBottom: "none" }}>
        <div className="block__title">说明</div>
        <div className="specimen" style={{ lineHeight: 2 }}>
          同意绑定计划指纹 · 计划一变同意作废
          <br />
          付款永不自动执行
        </div>
      </section>
    </aside>
  );
}
