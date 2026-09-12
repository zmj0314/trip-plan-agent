import type { Phase } from "../api/types";

const STEPS = ["澄清", "计划", "同意", "执行"] as const;

/** Maps a state-machine phase onto the four-step rail. */
function stepIndex(phase: Phase | undefined): number {
  switch (phase) {
    case "COLLECT":
      return 0;
    case "READY":
    case "PREVIEW":
      return 1;
    case "AWAIT_CONSENT":
      return 2;
    case "EXECUTE":
      return 3;
    default:
      return 4;
  }
}

export function GateRail({ phase }: { phase?: Phase }) {
  const active = stepIndex(phase);

  return (
    <aside className="rail">
      <div className="rail__brand">行程档案</div>
      <div className="rail__steps">
        <div className="rail__line" />
        {STEPS.map((label, index) => {
          const state = index < active ? "done" : index === active ? "active" : "idle";
          return (
            <div key={label} className={`step step--${state}`}>
              <span className="step__dot" />
              <span className="step__label">{label}</span>
            </div>
          );
        })}
      </div>
      <div className="rail__foot">Travel&nbsp;Dossier</div>
    </aside>
  );
}
