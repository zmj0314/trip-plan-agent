import { useState } from "react";
import type { PendingInterrupt } from "../api/types";

interface Props {
  payload: PendingInterrupt;
  busy: boolean;
  onResume: (payload: Record<string, unknown>) => void;
}

/** A short, human-checkable form of a plan hash (P3 made tangible). */
function fingerprint(hash?: string | null): string {
  if (!hash) return "—";
  return `${hash.slice(0, 4)} ${hash.slice(4, 8)} ${hash.slice(8, 12)} … ${hash.slice(-4)}`;
}

export function GateCard({ payload, busy, onResume }: Props) {
  const [acked, setAcknowledged] = useState(false);

  if (payload.kind === "question") {
    const questions = payload.questions ?? [];
    return (
      <div className="gate">
        <div className="gate__bar">
          <span>信息闸门 · 待补齐</span>
          <span>{questions.length} 项</span>
        </div>
        <div className="gate__body">
          <p className="gate__lede">补充以下信息后才会生成计划。</p>
          <ul className="gate__questions">
            {questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </div>
      </div>
    );
  }

  if (payload.kind === "gate2_full") {
    const assumptions = payload.assumptions ?? [];
    const needsAck = assumptions.length > 0;
    const blocked = needsAck && !acked;

    return (
      <div className="gate gate--consent">
        <div className="gate__bar">
          <span>同意闸门 · 执行前确认</span>
          <span>v{payload.plan_version_id?.slice(-6) ?? "—"}</span>
        </div>

        <div className="gate__body">
          <p className="gate__lede">{payload.preview || "已生成行程方案。"}</p>

          {needsAck && (
            <div className="assumptions">
              <div className="assumptions__title">系统采用的假设</div>
              <ul>
                {assumptions.map((a) => (
                  <li key={a}>{a}</li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {/* Signature block. Sticky so the decision can never scroll out of reach. */}
        <div className="gate__foot">
          {needsAck && (
            <label className="ack">
              <input
                type="checkbox"
                checked={acked}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              <span>我已确认上述假设。同意后系统将按此执行；如需修改请先拒绝。</span>
            </label>
          )}

          <div className="sign">
            <div className="sign__row">
              <span>计划指纹</span>
              <span className="fingerprint">{fingerprint(payload.plan_hash)}</span>
            </div>
          </div>

          <div className="gate__actions">
            <button
              className="btn btn--primary"
              disabled={busy || blocked}
              onClick={() =>
                onResume({ decision: "approve", plan_hash: payload.plan_hash ?? undefined })
              }
            >
              同意并执行
            </button>
            <button
              className="btn btn--ghost"
              disabled={busy}
              onClick={() =>
                onResume({ decision: "reject", plan_hash: payload.plan_hash ?? undefined })
              }
            >
              拒绝
            </button>
          </div>

          {blocked && <div className="gate__blocked">先勾选上面的确认项，才能执行。</div>}
        </div>
      </div>
    );
  }

  // L1 / L2 action gate
  const risk = payload.risk ?? 1;
  return (
    <div className="gate gate--consent">
      <div className="gate__bar">
        <span>{risk >= 2 ? "不可逆操作 · 二次确认" : "有副作用操作 · 逐次确认"}</span>
        <span>L{risk}</span>
      </div>
      <div className="gate__body">
        <p className="gate__lede">{payload.capability_id}</p>
      </div>
      <div className="gate__foot">
        <div className="gate__actions" style={{ marginTop: 0 }}>
          <button
            className="btn btn--primary"
            disabled={busy}
            onClick={() => onResume({ decision: "approve", action_id: payload.action_id })}
          >
            允许这一步
          </button>
          <button
            className="btn btn--ghost"
            disabled={busy}
            onClick={() => onResume({ decision: "reject", action_id: payload.action_id })}
          >
            跳过
          </button>
        </div>
      </div>
    </div>
  );
}
