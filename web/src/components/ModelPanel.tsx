import { useEffect, useState } from "react";
import type { Health, LLMProfileInput, LLMProvider, ProbeResult } from "../api/types";

const STORAGE_KEY = "travel-agent.llm-profile";

const DEFAULTS: LLMProfileInput = {
  provider: "deepseek",
  baseUrl: "http://127.0.0.1:8080/v1",
  model: "deepseek-chat",
  apiKey: ""
};

const PROVIDER_MODELS: Record<LLMProvider, string[]> = {
  deepseek: ["deepseek-chat", "deepseek-reasoner"],
  local: ["qwen3.5-0.8b"]
};

/** Provider-scoped payload: a base URL only means anything for a local server. */
function payloadFor(profile: LLMProfileInput): LLMProfileInput {
  return {
    provider: profile.provider,
    model: profile.model?.trim() || undefined,
    apiKey: profile.apiKey?.trim() || undefined,
    baseUrl: profile.provider === "local" ? profile.baseUrl?.trim() || undefined : undefined
  };
}

function load(): LLMProfileInput {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? { ...DEFAULTS, ...(JSON.parse(raw) as LLMProfileInput) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

interface Props {
  health: Health | null;
  onProfileChange: (profile: LLMProfileInput) => void;
  onProbe: (profile: LLMProfileInput) => Promise<ProbeResult>;
}

/**
 * Model provider panel (DM-3).
 *
 * The headline is not "which model" but **"is a real model being used at all"**
 * -- answered from the service's own telemetry rather than from optimism. The
 * probe exists because "configured" and "reachable" are different things, and
 * only a real call tells them apart.
 */
export function ModelPanel({ health, onProfileChange, onProbe }: Props) {
  const [profile, setProfile] = useState<LLMProfileInput>(load);
  const [probing, setProbing] = useState(false);
  const [result, setResult] = useState<ProbeResult | null>(null);

  useEffect(() => {
    onProfileChange(payloadFor(profile));
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(profile));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const update = (patch: Partial<LLMProfileInput>) => {
    const next = { ...profile, ...patch };
    if (patch.provider && patch.provider !== profile.provider) {
      // Switching provider must not carry the other side's model name over.
      next.model = PROVIDER_MODELS[patch.provider][0];
    }
    setProfile(next);
    setResult(null);
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    onProfileChange(payloadFor(next));
  };

  const run = async () => {
    setProbing(true);
    setResult(null);
    try {
      setResult(await onProbe(payloadFor(profile)));
    } catch (e) {
      setResult({ ok: false, error: e instanceof Error ? e.message : String(e) });
    } finally {
      setProbing(false);
    }
  };

  const runtime = health?.llm_runtime;
  const active = runtime?.active_client ?? "—";
  const usingReal = active !== "offline-stub" && active !== "—" && active !== "none";
  const isLocal = profile.provider === "local";

  return (
    <section className="block">
      <div className="block__title">模型</div>

      <div className={`model-state ${usingReal ? "model-state--live" : "model-state--stub"}`}>
        <span className="model-state__dot" />
        {usingReal ? `正在调用 ${active}` : "未调用真实模型 · 使用离线规则桩"}
      </div>

      <div className="seg" role="tablist">
        {(["deepseek", "local"] as LLMProvider[]).map((provider) => (
          <button
            key={provider}
            role="tab"
            aria-selected={profile.provider === provider}
            className={`seg__item ${profile.provider === provider ? "seg__item--on" : ""}`}
            onClick={() => update({ provider })}
          >
            {provider === "deepseek" ? "DeepSeek 云端" : "本地 llama.cpp"}
          </button>
        ))}
      </div>

      {isLocal ? (
        <>
          <label className="field">
            <span>服务地址</span>
            <input
              value={profile.baseUrl ?? ""}
              placeholder="http://127.0.0.1:8080/v1"
              onChange={(e) => update({ baseUrl: e.target.value })}
            />
          </label>
          <label className="field">
            <span>模型名</span>
            <input
              value={profile.model ?? ""}
              placeholder="qwen3.5-0.8b"
              onChange={(e) => update({ model: e.target.value })}
            />
          </label>
          <div className="specimen" style={{ lineHeight: 1.9 }}>
            llama-server 起服务：<code>llama-server -m model.gguf --port 8080</code>
            <br />
            它本身就是 OpenAI 兼容接口，所以本地模型不需要额外适配层。
          </div>
        </>
      ) : (
        <>
          <div className="field">
            <span>模型</span>
            <div className="chips">
              {PROVIDER_MODELS.deepseek.map((name) => (
                <button
                  key={name}
                  className={`chip ${profile.model === name ? "chip--on" : ""}`}
                  onClick={() => update({ model: name })}
                >
                  {name}
                </button>
              ))}
            </div>
          </div>
          <label className="field">
            <span>API Key</span>
            <input
              type="password"
              value={profile.apiKey ?? ""}
              placeholder="sk-…"
              onChange={(e) => update({ apiKey: e.target.value })}
            />
          </label>
          <div className="specimen" style={{ lineHeight: 1.9, marginBottom: "4px" }}>
            端点固定为 api.deepseek.com/v1；密钥只随单次请求透传，不落库。
          </div>
        </>
      )}

      <div className="model-key">
        <button className="btn btn--sm" disabled={probing} onClick={() => void run()}>
          {probing ? "测试中…" : "测试连接"}
        </button>
        <span className="specimen" style={{ alignSelf: "center" }}>
          会真调一次模型
        </span>
      </div>

      {result && (
        <div className={`probe ${result.ok ? "probe--ok" : "probe--bad"}`}>
          <div className="probe__head">
            {result.ok ? "✓ 连接成功" : "✗ 连接失败"}
            {result.latency_ms !== undefined && <span>{result.latency_ms} ms</span>}
          </div>
          {result.note && <div className="probe__line">{result.note}</div>}
          {result.error && <div className="probe__line">{result.error}</div>}
          {result.base_url && <div className="probe__line">端点：{result.base_url}</div>}
          {result.client && <div className="probe__line">客户端：{result.client}</div>}
          {!!result.available_models?.length && (
            <div className="probe__line">可用模型：{result.available_models.join(" · ")}</div>
          )}
        </div>
      )}

      <dl className="kv" style={{ marginTop: "12px" }}>
        <dt>密钥来源</dt>
        <dd>{runtime?.key_source ?? "—"}</dd>
        <dt>调用次数</dt>
        <dd>
          {runtime?.calls.total ?? 0}
          {runtime && runtime.calls.failures > 0 && `（失败 ${runtime.calls.failures}）`}
        </dd>
        <dt>按节点</dt>
        <dd>
          {runtime && Object.keys(runtime.calls.by_node).length > 0
            ? Object.entries(runtime.calls.by_node)
                .map(([node, n]) => `${node}×${n}`)
                .join(" · ")
            : "—"}
        </dd>
      </dl>

      {runtime?.calls.last_error && (
        <div className="model-error">上次错误：{runtime.calls.last_error}</div>
      )}

      <div className="specimen" style={{ marginTop: "10px", lineHeight: 1.9 }}>
        密钥与地址只存在本标签页，关闭即清除，只随单次请求透传，不落任何数据库。
      </div>
    </section>
  );
}
