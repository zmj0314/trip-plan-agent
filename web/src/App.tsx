import { useEffect, useRef, useState } from "react";
import { Dossier } from "./components/Dossier";
import { GateCard } from "./components/GateCard";
import { GateRail } from "./components/GateRail";
import { ItineraryCard } from "./components/ItineraryCard";
import { useSession, type TranscriptItem } from "./state/useSession";

function TranscriptRow({ item }: { item: TranscriptItem }) {
  if (item.kind === "user") {
    return (
      <div className="turn turn--user">
        <div className="bubble">{item.text}</div>
      </div>
    );
  }
  if (item.kind === "agent") {
    return (
      <div className="turn">
        <div className="bubble bubble--agent">{item.text}</div>
      </div>
    );
  }
  if (item.kind === "stream") {
    return (
      <div className="turn">
        <div className="bubble bubble--agent bubble--stream">
          {item.text}
          <span className="caret" aria-hidden="true" />
        </div>
      </div>
    );
  }
  if (item.kind === "notice") {
    return (
      <div className="turn">
        <div className={`notice notice--${item.tone}`}>
          {item.text}
          {item.detail && item.detail.filter(Boolean).length > 0 && (
            <ul className="notice__detail">
              {item.detail.filter(Boolean).map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          )}
        </div>
      </div>
    );
  }
  return null;
}

export default function App() {
  const {
    sessionId,
    snapshot,
    transcript,
    pending,
    busy,
    error,
    health,
    setLlmProfile,
    probe,
    start,
    newSession,
    send,
    resume
  } = useSession();

  const [draft, setDraft] = useState("");
  const bodyRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: "smooth" });
  }, [transcript.length, pending]);

  useEffect(() => {
    if (sessionId) textareaRef.current?.focus();
  }, [sessionId]);

  const consentOpen = pending?.kind === "gate2_full" || pending?.kind === "action";

  const submit = () => {
    if (!draft.trim() || busy || consentOpen) return;
    void send(draft);
    setDraft("");
  };

  return (
    <div className="shell">
      <GateRail phase={snapshot?.phase} />

      <main className="stream">
        <header className="stream__head">
          <div>
            <h1 className="stream__title">门到门行程规划</h1>
            <div className="stream__sub">
              {sessionId ? sessionId : "未建立会话"} · 先问清 → 出计划 → 等同意 → 流式执行
            </div>
          </div>
          <div className="stream__actions">
            {sessionId && (
              <button
                className="btn btn--ghost btn--sm"
                disabled={busy}
                onClick={() => void newSession()}
                title="结束当前会话并重新开始"
              >
                新建会话
              </button>
            )}
            <div className="stream__sub" style={{ textAlign: "right", lineHeight: 1.9 }}>
              <div>能力 {health?.capabilities ?? "—"} 项</div>
              <div>
                {health?.channels ? "通道已接入" : "通道未接入"} ·{" "}
                {health?.persistence ? "持久化开" : "无持久化"}
              </div>
            </div>
          </div>
        </header>

        {busy && <div className="busy-bar" />}
        {error && <div className="error-bar">{error}</div>}

        <div className="stream__body" ref={bodyRef}>
          {!sessionId ? (
            <div className="empty-state">
              把「我从某地想去某地」交给我。
              <br />
              我会先问清楚，再出计划，等你点头才动手。
              <br />
              <br />
              付款永远不会自动发生。
            </div>
          ) : (
            <>
              {transcript.map((item) =>
                item.kind === "gate" && item.payload.kind !== "question" ? null : (
                  <TranscriptRow key={item.id} item={item} />
                )
              )}
              {pending && (
                <div className="turn">
                  <GateCard payload={pending} busy={busy} onResume={resume} />
                </div>
              )}
              {snapshot?.trip && !pending && (
                <div className="turn reveal">
                  <ItineraryCard trip={snapshot.trip} />
                </div>
              )}
            </>
          )}
        </div>

        <div className="composer">
          {!sessionId ? (
            <div className="composer__row">
              <button className="btn btn--primary" disabled={busy} onClick={() => void start()}>
                建立会话
              </button>
              <div className="composer__hint" style={{ margin: 0, alignSelf: "center" }}>
                无需登录 · 数据只存在你本机
              </div>
            </div>
          ) : (
            <>
              <div className="composer__row">
                <textarea
                  ref={textareaRef}
                  value={draft}
                  placeholder={
                    consentOpen ? "请先在上方做出同意或拒绝" : "例如：从北京市朝阳区望京SOHO出发，去长城"
                  }
                  disabled={consentOpen}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      submit();
                    }
                  }}
                />
                <button className="btn btn--primary" disabled={busy || consentOpen || !draft.trim()} onClick={submit}>
                  发送
                </button>
              </div>
              <div className="composer__hint">
                <span>Enter 发送 · Shift + Enter 换行</span>
                <span>{consentOpen ? "等待你在闸门处决策" : "信息不全时系统会先追问"}</span>
              </div>
            </>
          )}
        </div>
      </main>

      <Dossier
        snapshot={sessionId ? snapshot : null}
        health={health}
        onProfileChange={setLlmProfile}
        onProbe={probe}
      />
    </div>
  );
}
