/** Mirrors the backend event + snapshot contract (framework §4.3, §9.7). */

export type Phase =
  | "COLLECT"
  | "READY"
  | "PREVIEW"
  | "AWAIT_CONSENT"
  | "EXECUTE"
  | "DONE"
  | "FAILED"
  | "EXPIRED";

export type EventType =
  | "text_delta"
  | "tool_call_start"
  | "tool_call_delta"
  | "tool_call_end"
  | "tool_result"
  | "plan_preview"
  | "interrupt"
  | "state_update"
  | "scope_rejected"
  | "degradation_notice"
  | "usage"
  | "error"
  | "done"
  | "heartbeat";

export interface AgentEvent {
  id: number;
  session_id: string;
  type: EventType;
  ts: string;
  phase: Phase;
  plan_version_id: string | null;
  data: Record<string, unknown>;
  replay: boolean;
}

export interface PendingInterrupt {
  kind: "question" | "gate2_full" | "action" | "confirm_token" | string;
  questions?: string[];
  prompt?: string;
  preview?: string;
  assumptions?: string[];
  plan_version_id?: string | null;
  plan_hash?: string | null;
  action_id?: string;
  capability_id?: string;
  risk?: number;
  requires_second_confirmation?: boolean;
}

export interface ActionSummary {
  action_id: string;
  capability_id: string;
  status: string;
  /** Present once executed: the de-duplication key (P4). */
  idem_key?: string | null;
  /** Present once executed: the deliverable (export content, deep-link URL). */
  result?: ActionResult | null;
}

export interface ActionResult {
  kind?: string;
  url?: string | null;
  missing?: string[];
  steps?: string[];
  filename?: string;
  media_type?: string;
  content?: string;
  bytes?: number;
  mode?: string;
  note?: string;
  payment?: string;
  [key: string]: unknown;
}

export interface DegradationNote {
  capability_id: string;
  provider: string | null;
  level: number;
  reason: string;
}

export interface SlotValue {
  value: unknown;
  source: string;
  status: string;
  confidence: number;
}

export interface Snapshot {
  session_id: string;
  phase: Phase;
  pending_interrupt: PendingInterrupt | null;
  current_plan_version_id: string | null;
  plan_hash: string | null;
  plan_status: string;
  slots: Record<string, SlotValue>;
  derived_slots?: Record<string, unknown>;
  missing_required: string[];
  assumptions: string[];
  preview_text: string;
  /** Content layer (F1): the day-by-day plan, present once planning has run. */
  planned_content?: TripContent;
  trip: Trip | null;
  actions: ActionSummary[];
  degradation_log: DegradationNote[];
  scope: Record<string, unknown>;
}

export interface TripLeg {
  leg_id: string;
  scope?: "local" | "regional" | "intercity" | string;
  origin_text?: string | null;
  destination_text?: string | null;
  day?: string | null;
  selected_candidate_id?: string | null;
  candidates?: TripCandidate[];
  kind?: "rail" | "road" | string;
  depart?: string | null;
  arrive?: string | null;
  duration_min?: number | null;
  distance_km?: number | null;
  price?: number | null;
  train_code?: string | null;
  buffer_minutes?: number | null;
  approx?: boolean;
}

export interface TraceContributor {
  kind: string;
  key: string;
  weight: number;
  contribution: number;
  evidence?: Record<string, unknown>;
}

export interface TripCandidate {
  candidate_id: string;
  destination_name?: string;
  destination_admin1?: string | null;
  lat?: number | null;
  lon?: number | null;
  mode?: string;
  distance_km?: number | null;
  duration_min?: number | null;
  route_provider?: string | null;
  mode_label?: string;
  price?: number | null;
  train_code?: string | null;
  depart_time?: string | null;
  arrive_time?: string | null;
  from_station?: string | null;
  to_station?: string | null;
  seats?: { name?: string; left?: string; price?: number }[];
  weather?: {
    day?: string;
    hours?: number;
    out_of_range?: boolean;
    precipitation_mm?: number;
    wind_speed_ms?: number;
    temperature_c?: number;
    temperature_min_c?: number;
    visibility_m?: number;
    weather_code?: number;
  };
  weather_veto?: boolean;
  weather_notes?: string[];
  score?: number;
  explainable?: boolean;
  reason_trace?: { contributors?: TraceContributor[]; vetoes?: string[] };
}

export interface DayAttraction {
  attraction_id: string;
  name: string;
  category?: string;
  recommend_duration_min?: number;
  ticket_price?: number | null;
  rating?: number | null;
  tags?: string[];
  description?: string | null;
  provider?: string | null;
  confidence?: number;
}

export interface DayScheduleItem {
  kind: "city_transfer" | "attraction" | "meal" | "lodging" | "rest" | string;
  ref_id?: string | null;
  start?: string | null;
  end?: string | null;
  duration_min?: number | null;
  cost?: number | null;
  transport_to_next?: { duration_min?: number; estimated?: boolean } | null;
  weather_note?: string | null;
  evidence?: string[];
}

export interface LodgingOption {
  name?: string;
  area?: string | null;
  nightly_price?: number | null;
  nights?: number;
  provider?: string | null;
  advisory?: boolean;
}

export interface DayPlan {
  day_index: number;
  date?: string | null;
  city?: string;
  segment_index?: number;
  overnight_city?: string | null;
  is_transfer_day?: boolean;
  theme?: string;
  summary?: string;
  weather?: {
    out_of_range?: boolean;
    precipitation_mm?: number;
    wind_speed_ms?: number;
    temperature_c?: number;
    temperature_min_c?: number;
  };
  weather_veto?: boolean;
  items?: DayScheduleItem[];
  attractions?: DayAttraction[];
  meals?: { slot?: string; name?: string; kind?: string }[];
  lodging?: LodgingOption[];
  notes?: string[];
}

export interface TripSegment {
  segment_id: string;
  city: string;
  day_count: number;
  lodging_area?: string | null;
  notes?: string[];
}

export interface TripContent {
  segments?: TripSegment[];
  days?: DayPlan[];
  /** Cross-city legs, one per hop between two segments. */
  transfers?: TripLeg[];
  content_notes?: string[];
  degraded_content?: DegradationNote[];
  cost_estimate?: number | null;
  cost_note?: string;
}

export interface Trip {
  trip_id: string;
  session_id?: string;
  legs?: TripLeg[];
  reminders?: string[];
  assumptions?: string[];
  degradation?: DegradationNote[];
  actions?: { action_id: string; capability_id: string; status: string }[];
  date_window?: Record<string, unknown>;
  travelers?: Record<string, unknown>[];
  /** Content layer (F1): the same shape as ``TripContent``. */
  segments?: TripSegment[];
  days?: DayPlan[];
  content_notes?: string[];
  cost_estimate?: number | null;
  cost_note?: string;
}

export interface Health {
  status: string;
  capabilities: number;
  llm: { mode: string; server_key_configured: boolean; model: string; base_url: string };
  llm_runtime: {
    active_client: string;
    key_source: string;
    calls: {
      total: number;
      failures: number;
      by_node: Record<string, number>;
      input_tokens: number;
      output_tokens: number;
      last_at: string | null;
      last_error: string | null;
    };
  };
  persistence: boolean;
  channels: boolean;
}

export type LLMProvider = "deepseek" | "local";

export interface LLMProfileInput {
  provider: LLMProvider;
  baseUrl?: string;
  model?: string;
  apiKey?: string;
}

export interface ProbeResult {
  ok: boolean;
  provider?: string;
  client?: string;
  base_url?: string;
  model?: string;
  latency_ms?: number;
  available_models?: string[];
  raw?: string;
  error?: string | null;
  note?: string | null;
  local?: boolean;
}
