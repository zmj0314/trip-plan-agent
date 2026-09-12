import type { AgentEvent, Health, LLMProfileInput, ProbeResult, Snapshot } from "./types";

/**
 * Thin REST client. Everything the UI renders comes from `snapshot`; the event
 * list is a narrative, not state (framework §9.7).
 */

export interface ClientOptions {
  token?: string;
  llmKey?: string;
  llmProvider?: LLMProfileInput["provider"];
  llmBaseUrl?: string;
  llmModel?: string;
}

function headers(options: ClientOptions, extra: Record<string, string> = {}): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json", ...extra };
  if (options.token) h["X-Access-Token"] = options.token;
  if (options.llmKey) h["X-LLM-Api-Key"] = options.llmKey;
  if (options.llmProvider) h["X-LLM-Provider"] = options.llmProvider;
  // The model name is meaningful for every provider; the base URL only for a
  // local one. Sending a stale local URL while on the DeepSeek tab is how a
  // DeepSeek key ends up being sent to 127.0.0.1.
  if (options.llmProvider === "local" && options.llmBaseUrl) {
    h["X-LLM-Base-Url"] = options.llmBaseUrl;
  }
  if (options.llmModel) h["X-LLM-Model"] = options.llmModel;
  return h;
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(explain(response.status, detail));
  }
  return (await response.json()) as T;
}

/** Turn a raw API error into something a traveller can read. */
function explain(status: number, detail: string): string {
  if (status === 401) return "访问被拒绝：请检查访问令牌。";
  if (status === 409) return "计划已更新，请重新确认。";
  if (status === 422) return "提交的数据格式不对（这通常是前端与后端的契约不一致）。";
  // The dev proxy answers 500 when it cannot reach the upstream. Reporting
  // that as "request failed (500)" sends the reader looking for a backend bug
  // when the actual condition is "the backend is not running". A real API
  // error carries a JSON body; a proxy failure does not.
  const looksLikeProxy =
    [500, 502, 503, 504].includes(status) && !detail.trim().startsWith("{");
  if (looksLikeProxy) {
    return "连不上后端：请确认后端已启动（scripts\\dev.ps1），然后重试。";
  }
  return `请求失败（${status}）${detail ? `：${detail.slice(0, 160)}` : ""}`;
}

/**
 * Live token stream for one session.
 *
 * The POST that sends a message already returns the turn's events, so this is
 * only about *when* they appear: while a turn is running, streamed chunks arrive
 * here first and are replaced by the node's rendered text when it finishes.
 * Nothing here is authoritative -- a dropped connection costs timing, not
 * content, because the snapshot still has the final plan.
 */
export function openStream(
  sessionId: string,
  handlers: {
    onDelta: (data: { node?: string; seq?: number; text?: string; done?: boolean; ok?: boolean; reset?: boolean }) => void;
    onEvent?: (event: AgentEvent) => void;
    onState?: (snapshot: Snapshot) => void;
    onError?: () => void;
  }
): () => void {
  const url = `/sessions/${encodeURIComponent(sessionId)}/stream?live=true`;
  const source = new EventSource(url);

  source.addEventListener("text_delta", (raw) => {
    try {
      handlers.onDelta(JSON.parse((raw as MessageEvent).data));
    } catch {
      /* a malformed chunk must not kill the stream */
    }
  });

  for (const name of [
    "interrupt",
    "scope_rejected",
    "degradation_notice",
    "error",
    "done",
    "tool_result",
    "plan_preview",
    "state_update"
  ]) {
    source.addEventListener(name, (raw) => {
      try {
        const payload = JSON.parse((raw as MessageEvent).data);
        if (name === "state_update" && payload?.snapshot && handlers.onState) {
          handlers.onState(payload.snapshot as Snapshot);
          return;
        }
        handlers.onEvent?.({ type: name, data: payload } as AgentEvent);
      } catch {
        /* ignore */
      }
    });
  }

  source.onerror = () => handlers.onError?.();
  return () => source.close();
}

export function createClient(getOptions: () => ClientOptions) {
  const rid = () => crypto.randomUUID();

  return {
    health: () => fetch("/healthz").then(asJson<Health>),

    start: (userId = "local") =>
      fetch("/sessions", {
        method: "POST",
        headers: headers(getOptions()),
        body: JSON.stringify({ user_id: userId })
      }).then(asJson<{ session_id: string; phase: string; status: string }>),

    send: (sessionId: string, text: string) =>
      fetch(`/sessions/${sessionId}/messages`, {
        method: "POST",
        headers: headers(getOptions()),
        body: JSON.stringify({ text, client_request_id: rid() })
      }).then(asJson<{ events: AgentEvent[] }>),

    resume: (sessionId: string, payload: Record<string, unknown>) =>
      fetch(`/sessions/${sessionId}/resume`, {
        method: "POST",
        headers: headers(getOptions()),
        body: JSON.stringify({ ...payload, client_request_id: rid() })
      }).then(asJson<{ events: AgentEvent[] }>),

    snapshot: (sessionId: string) =>
      fetch(`/sessions/${sessionId}/snapshot`, { headers: headers(getOptions()) }).then(
        asJson<Snapshot>
      ),

    receipt: (sessionId: string, actionId: string, orderNo?: string) =>
      fetch(`/sessions/${sessionId}/receipt`, {
        method: "POST",
        headers: headers(getOptions()),
        body: JSON.stringify({
          action_id: actionId,
          order_no: orderNo ?? null,
          status: "completed",
          client_request_id: rid()
        })
      }).then(asJson<Record<string, unknown>>)
    ,

    probe: (profile: LLMProfileInput) =>
      fetch("/llm/probe", {
        method: "POST",
        headers: headers(getOptions()),
        body: JSON.stringify({
          provider: profile.provider,
          base_url: profile.provider === "local" ? profile.baseUrl : undefined,
          model: profile.model,
          api_key: profile.apiKey
        })
      }).then(asJson<ProbeResult>)
  };
}

export type Client = ReturnType<typeof createClient>;
