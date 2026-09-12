import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createClient, openStream, type ClientOptions } from "../api/client";
import type { AgentEvent, Health, PendingInterrupt, Snapshot } from "../api/types";
import type { LLMProfileInput } from "../api/types";

export type Tone = "quiet" | "info" | "warn" | "alert" | "ok";

export type TranscriptItem =
  | { id: string; kind: "user"; text: string }
  | { id: string; kind: "agent"; text: string }
  | { id: string; kind: "stream"; text: string; node: string }
  | { id: string; kind: "notice"; text: string; tone: Tone; detail?: string[] }
  | { id: string; kind: "gate"; payload: PendingInterrupt };

let seq = 0;
const nextId = () => `t${++seq}`;
const STREAM_ID = "streaming";

const GREETING =
  "我是行程档案。告诉我你从哪出发、想去哪，我会先把细节问清楚，出一份计划，等你点头之后才动手。\n\n" +
  "付款永远不会自动发生——那一步始终由你自己在官方渠道完成。";

/**
 * State-driven session store (framework §9.7).
 *
 * `snapshot` is the single source of truth for every card. Events only add
 * narrative to the transcript and trigger a refresh; a reconnect can therefore
 * rebuild the whole screen from one GET.
 */
export function useSession() {
  const [options, setOptions] = useState<ClientOptions>({});
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const client = useMemo(() => createClient(() => optionsRef.current), []);

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [transcript, setTranscript] = useState<TranscriptItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [pending, setPending] = useState<PendingInterrupt | null>(null);

  const sessionRef = useRef<string | null>(null);
  sessionRef.current = sessionId;

  const push = useCallback((item: TranscriptItem) => {
    setTranscript((prev) => [...prev, item]);
  }, []);

  /**
   * Fold one streamed chunk into a single live row.
   *
   * `reset` clears what was already shown: the model's previous answer was
   * rejected and re-asked, so keeping half of it would render text the system
   * never accepted.
   */
  const applyDelta = useCallback(
    (data: { node?: string; text?: string; done?: boolean; ok?: boolean; reset?: boolean }) => {
      const node = data.node ?? "";
      if (data.reset) {
        setTranscript((prev) =>
          prev.map((item) =>
            item.kind === "stream" && item.id === STREAM_ID ? { ...item, text: "" } : item
          )
        );
        return;
      }
      if (data.done) {
        // The node's own rendered text is authoritative and arrives through the
        // snapshot; this row only existed to show progress.
        setTranscript((prev) => prev.filter((item) => item.id !== STREAM_ID));
        return;
      }
      const text = data.text ?? "";
      if (!text) return;
      setTranscript((prev) => {
        const existing = prev.find(
          (item): item is Extract<TranscriptItem, { kind: "stream" }> =>
            item.kind === "stream" && item.id === STREAM_ID
        );
        if (!existing) {
          return [...prev, { id: STREAM_ID, kind: "stream", text, node }];
        }
        return prev.map((item) =>
          item.kind === "stream" && item.id === STREAM_ID
            ? { ...item, text: item.text + text }
            : item
        );
      });
    },
    []
  );

  const applyEvents = useCallback((events: AgentEvent[]) => {
    const batch: TranscriptItem[] = [];
    for (const event of events) {
      if (event.replay) continue;
      switch (event.type) {
        case "interrupt": {
          const payload = event.data as unknown as PendingInterrupt;
          setPending(payload);
          batch.push({ id: nextId(), kind: "gate", payload });
          break;
        }
        case "scope_rejected":
          batch.push({
            id: nextId(),
            kind: "notice",
            tone: (event.data as { fused?: boolean }).fused ? "alert" : "warn",
            text: String((event.data as { text?: string }).text ?? "该请求不在旅行规划范围内。"),
            detail: (event.data as { fused?: boolean }).fused
              ? ["会话已终止（越界次数达上限）"]
              : undefined
          });
          break;
        case "degradation_notice":
          batch.push({
            id: nextId(),
            kind: "notice",
            tone: "warn",
            text: `${String((event.data as { capability_id?: string }).capability_id ?? "数据源")} 不可用，已降级`,
            detail: [String((event.data as { reason?: string }).reason ?? "")]
          });
          break;
        case "error":
          batch.push({
            id: nextId(),
            kind: "notice",
            tone: "alert",
            text:
              (event.data as { code?: string }).code === "PLAN_VERSION_STALE"
                ? "计划已更新，请重新确认。"
                : String((event.data as { message?: string }).message ?? "出错了。"),
            detail: (event.data as { detail?: string }).detail
              ? [String((event.data as { detail?: string }).detail)]
              : undefined
          });
          break;
        case "tool_result": {
          const finished = (event.data as { batch?: { capability: string; ok: boolean }[] }).batch;
          if (finished?.length) {
            batch.push({
              id: nextId(),
              kind: "notice",
              tone: "quiet",
              text: "执行结果",
              detail: finished.map((b) => `${b.ok ? "✓" : "✗"} ${b.capability}`)
            });
          }
          break;
        }
        case "done":
          batch.push({ id: nextId(), kind: "notice", tone: "ok", text: "行程已生成。" });
          break;
        default:
          break;
      }
    }
    if (batch.length) setTranscript((prev) => [...prev, ...batch]);
  }, []);

  const refresh = useCallback(async () => {
    const id = sessionRef.current;
    if (!id) return;
    const next = await client.snapshot(id);
    setSnapshot(next);
    setPending(next.pending_interrupt ?? null);
  }, [client]);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await client.health());
    } catch {
      setHealth(null);
    }
  }, [client]);

  /** Provider + BYOK entry point: both ride along as per-request headers. */
  const setLlmProfile = useCallback((profile: LLMProfileInput) => {
    setOptions((prev) => ({
      ...prev,
      llmProvider: profile.provider,
      llmBaseUrl: profile.provider === "local" ? profile.baseUrl?.trim() || undefined : undefined,
      llmModel: profile.model?.trim() || undefined,
      llmKey: profile.apiKey?.trim() || undefined
    }));
  }, []);

  const probe = useCallback(
    (profile: LLMProfileInput) => client.probe(profile),
    [client]
  );

  const start = useCallback(async () => {
    setBusy(true);
    setError(null);
    // Reset here, not in an effect keyed on sessionId: that effect also fired
    // right after a fresh snapshot landed and silently wiped it.
    setTranscript([]);
    setSnapshot(null);
    setPending(null);
    try {
      const h = await client.health();
      setHealth(h);
      const session = await client.start();
      setSessionId(session.session_id);
      sessionRef.current = session.session_id;
      const snap = await client.snapshot(session.session_id);
      setSnapshot(snap);
      setPending(snap.pending_interrupt ?? null);
      setTranscript([{ id: nextId(), kind: "agent", text: GREETING }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [client]);

  const send = useCallback(
    async (text: string) => {
      if (!text.trim()) return;
      push({ id: nextId(), kind: "user", text });
      setBusy(true);
      setError(null);
      setPending(null);
      try {
        // A message typed while a question gate is open is an answer to it.
        const current = pending;
        const result =
          current && current.kind === "question"
            ? await client.resume(sessionRef.current!, { text, decision: null })
            : await client.send(sessionRef.current!, text);
        applyEvents(result.events);
        await refresh();
        await refreshHealth();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [applyEvents, client, pending, push, refresh, refreshHealth]
  );

  const resume = useCallback(
    async (payload: Record<string, unknown>) => {
      if (!sessionRef.current) return;
      setBusy(true);
      setError(null);
      setPending(null);
      try {
        const result = await client.resume(sessionRef.current, payload);
        applyEvents(result.events);
        await refresh();
        await refreshHealth();
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        if (message.includes("计划已更新")) {
          push({ id: nextId(), kind: "notice", tone: "alert", text: message });
          await refresh();
        } else {
          setError(message);
        }
      } finally {
        setBusy(false);
      }
    },
    [applyEvents, client, push, refresh, refreshHealth]
  );

  // Live token stream. Attached while a session is open and torn down with it;
  // a failed stream is not an error state -- the turn still returns its events.
  useEffect(() => {
    if (!sessionId) return;
    const close = openStream(sessionId, {
      onDelta: applyDelta,
      onState: (next) => {
        setSnapshot(next);
        setPending(next.pending_interrupt ?? null);
      }
    });
    return close;
  }, [applyDelta, sessionId]);

  // Keep the health strip honest if the backend goes away mid-session.
  useEffect(() => {
    if (!sessionId) return;
    const timer = window.setInterval(() => {
      client.health().then(setHealth).catch(() => setHealth(null));
    }, 20000);
    return () => window.clearInterval(timer);
  }, [client, sessionId]);

  return {
    options,
    setOptions,
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
    newSession: start,
    send,
    resume,
    refresh
  };
}
