import { useState } from "react";
import type { ActionSummary, DayPlan, Trip, TripCandidate, TripLeg, TripSegment } from "../api/types";

const SCOPE_LABEL: Record<string, string> = {
  local: "同城",
  regional: "省内",
  intercity: "跨省"
};

const STATUS_LABEL: Record<string, string> = {
  completed: "已备好",
  failed: "未完成",
  skipped: "已跳过",
  blocked: "受阻",
  pending: "待执行"
};

const CONTRIBUTOR_LABEL: Record<string, string> = {
  f_time: "用时",
  f_cost: "费用",
  f_transfer: "换乘",
  f_walk: "步行",
  f_punctual: "准点",
  f_comfort: "舒适",
  f_access: "无障碍",
  f_risk: "风险",
  "weather.precipitation": "降水",
  "weather.wind": "风速",
  "weather.temperature": "气温",
  "weather.visibility": "能见度",
  "weather.aqi": "空气质量",
  "weather.thunderstorm": "雷暴",
  "weather.ice": "结冰",
  uncertainty: "数据不确定度"
};

function legTitle(leg: TripLeg): string {
  const from = leg.origin_text?.trim();
  const to = leg.destination_text?.trim();
  if (from && to) return `${from} → ${to}`;
  if (to) return `前往 ${to}`;
  if (from) return `从 ${from} 出发`;
  return "未命名路段";
}

function clock(iso?: string | null): string {
  if (!iso) return "";
  const match = iso.match(/T(\d{2}:\d{2})/);
  return match ? match[1] : "";
}

function weatherLine(c: TripCandidate): string {
  const w = c.weather ?? {};
  if (w.out_of_range) return "出行日期超出预报范围，尚无天气数据";
  const bits: string[] = [];
  if (typeof w.temperature_c === "number") bits.push(`${Math.round(w.temperature_c)}°C`);
  if (typeof w.precipitation_mm === "number" && w.precipitation_mm > 0) {
    bits.push(`降水 ${w.precipitation_mm}mm`);
  }
  if (typeof w.wind_speed_ms === "number") bits.push(`风速 ${(w.wind_speed_ms * 3.6).toFixed(0)}km/h`);
  return bits.join(" · ") || "无天气数据";
}

/** Deterministic phrasing of the citable contributors — no LLM required. */
function reasonText(c: TripCandidate): string {
  const contributors = (c.reason_trace?.contributors ?? [])
    // Uncertainty is a data-quality caveat, not a reason to prefer one option
    // over another -- it is surfaced as a data-level stamp instead.
    .filter((x) => x.key !== "uncertainty" && Math.abs(x.contribution) > 0.001)
    .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))
    .slice(0, 3);
  if (contributors.length === 0) return "满足全部硬性要求，按综合成本取最优。";
  return contributors
    .map((x) => {
      const label = CONTRIBUTOR_LABEL[x.key] ?? x.key;
      return x.kind === "weather" ? `${label}影响 ${x.contribution.toFixed(2)}` : `${label}占优`;
    })
    .join(" · ");
}

/** "08:30" out of an ISO timestamp, without pulling in a date library. */
function clockTime(iso?: string | null): string {
  if (!iso) return "";
  const match = iso.match(/T(\d{2}:\d{2})/);
  return match ? match[1] : "";
}

function dayWeather(day: DayPlan): string {
  const w = day.weather ?? {};
  if (w.out_of_range) return "日期超出预报范围";
  const bits: string[] = [];
  if (typeof w.temperature_c === "number") {
    const low = typeof w.temperature_min_c === "number" ? `${Math.round(w.temperature_min_c)}–` : "";
    bits.push(`${low}${Math.round(w.temperature_c)}°C`);
  }
  if (typeof w.precipitation_mm === "number" && w.precipitation_mm > 0) {
    bits.push(`降水 ${w.precipitation_mm}mm`);
  }
  if (typeof w.wind_speed_ms === "number") bits.push(`风 ${(w.wind_speed_ms * 3.6).toFixed(0)}km/h`);
  return bits.join(" · ");
}

/**
 * The day-by-day plan (content layer F1).
 *
 * Kept separate from the leg list rather than merged into it: legs answer "how
 * do I get there", days answer "what am I doing once I am there", and collapsing
 * them loses the transfer-day distinction that matters most on a multi-city trip.
 */
function DayList({ days, segments }: { days: DayPlan[]; segments: TripSegment[] }) {
  return (
    <>
      {segments.length > 0 && (
        <div className="itin__section">
          <div className="itin__label">行程分段</div>
          <div className="segs">
            {segments.map((segment, index) => (
              <span key={segment.segment_id} className="seg">
                <em>{index + 1}</em>
                {segment.city}
                <span className="seg__days">{segment.day_count} 天</span>
              </span>
            ))}
          </div>
        </div>
      )}

      <ol className="days">
        {days.map((day) => (
          <li key={day.day_index} className={`day${day.is_transfer_day ? " day--transfer" : ""}`}>
            <div className="day__head">
              <span className="day__num">Day {day.day_index}</span>
              <span className="day__date">{day.date ?? ""}</span>
              <span className="day__city">{day.city}</span>
              {day.is_transfer_day && (
                <span className="day__transfer">转场 → {day.overnight_city ?? "下一站"}</span>
              )}
              {day.theme && <span className="day__theme">{day.theme}</span>}
              {dayWeather(day) && <span className="day__weather">{dayWeather(day)}</span>}
            </div>

            {day.summary && <div className="day__summary">{day.summary}</div>}

            {(day.attractions ?? []).length > 0 && (
              <ul className="day__stops">
                {(day.attractions ?? []).map((spot) => {
                  const item = (day.items ?? []).find((i) => i.ref_id === spot.attraction_id);
                  const next = item?.transport_to_next;
                  return (
                    <li key={spot.attraction_id} className="stop">
                      <span className="stop__time">
                        {clockTime(item?.start) || "—"}
                        {item?.end ? `–${clockTime(item.end)}` : ""}
                      </span>
                      <span className="stop__name">{spot.name}</span>
                      <span className="stop__meta">
                        {spot.recommend_duration_min ? `${spot.recommend_duration_min}min` : ""}
                        {spot.ticket_price != null ? ` · ¥${spot.ticket_price} 参考价` : ""}
                        {spot.rating != null ? ` · 评分 ${spot.rating}` : ""}
                        {spot.provider ? ` · ${spot.provider}` : ""}
                      </span>
                      {next?.duration_min ? (
                        <span className="stop__hop">
                          前往下一站 {next.duration_min} 分钟{next.estimated ? "（估算）" : ""}
                        </span>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}

            {(day.lodging ?? []).length > 0 && (
              <div className="day__lodging">
                今晚住 {(day.lodging ?? [])[0].area ?? (day.lodging ?? [])[0].name}
                {(day.lodging ?? [])[0].nightly_price != null
                  ? ` · 参考价 ¥${(day.lodging ?? [])[0].nightly_price}/晚`
                  : " · 未获取实时房价"}
              </div>
            )}

            {day.weather_veto && (
              <div className="pick__veto">该日触发天气硬性否决，请谨慎安排或改期。</div>
            )}

            {(day.notes ?? []).length > 0 && (
              <ul className="pick__notes">
                {(day.notes ?? []).map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ol>
    </>
  );
}

/**
 * What the local channel produced for this trip: the booking deep-links and the
 * exported file. Only shown when there is something to click or download --
 * a status list with nothing behind it is noise.
 */
function Deliverables({ actions }: { actions: ActionSummary[] }) {
  const deeplinks = actions.filter(
    (a) => a.capability_id === "booking.deeplink.build" && a.status === "completed" && a.result?.url
  );
  const exportAction = actions.find(
    (a) => a.capability_id === "plan.export" && a.status === "completed" && a.result?.content
  );
  const share = actions.find(
    (a) => a.capability_id === "plan.share" && a.status === "completed" && a.result?.content
  );

  const download = (filename: string, content: string, mediaType: string) => {
    const blob = new Blob([content], { type: mediaType || "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  if (!deeplinks.length && !exportAction && !share) return null;

  return (
    <div className="itin__section">
      <div className="itin__label">可带走</div>
      <div className="itin__actions">
        {deeplinks.map((action) => (
          <a
            key={action.action_id}
            className="itin__action itin__action--link"
            href={String(action.result?.url)}
            target="_blank"
            rel="noreferrer"
          >
            打开官方购票页
          </a>
        ))}
        {exportAction?.result ? (
          <button
            className="btn btn--sm"
            onClick={() =>
              download(
                String(exportAction.result?.filename ?? "itinerary.md"),
                String(exportAction.result?.content ?? ""),
                String(exportAction.result?.media_type ?? "")
              )
            }
          >
            下载行程单（{String(exportAction.result?.filename ?? "md")}）
          </button>
        ) : null}
        {share?.result ? (
          <button
            className="btn btn--sm"
            onClick={() =>
              download(
                String(share.result?.filename ?? "itinerary.html"),
                String(share.result?.content ?? ""),
                String(share.result?.media_type ?? "")
              )
            }
          >
            导出分享文件
          </button>
        ) : null}
      </div>
      {share?.result?.note ? <div className="specimen">{String(share.result.note)}</div> : null}
    </div>
  );
}

/** The payoff: what the whole flow was for. */
export function ItineraryCard({ trip }: { trip: Trip }) {
  const [copied, setCopied] = useState(false);
  const legs = trip.legs ?? [];
  const reminders = trip.reminders ?? [];
  const degradation = trip.degradation ?? [];
  const actions = trip.actions ?? [];
  const days = trip.days ?? [];
  const segments = trip.segments ?? [];
  const contentNotes = trip.content_notes ?? [];

  const copy = async () => {
    const dayLines = days.map((day) => {
      const head = `Day ${day.day_index}（${day.date ?? ""}）${day.city ?? ""}${
        day.is_transfer_day ? ` → ${day.overnight_city ?? "下一站"}` : ""
      }${day.theme ? ` · ${day.theme}` : ""}`;
      const stops = (day.attractions ?? []).map(
        (spot) =>
          `    ${clockTime((day.items ?? []).find((i) => i.ref_id === spot.attraction_id)?.start) || "—"} ${spot.name}`
      );
      return [head, ...stops].join("\n");
    });
    const text = [
      "行程单",
      ...(segments.length ? [`分段：${segments.map((s) => `${s.city} ${s.day_count} 天`).join(" / ")}`] : []),
      ...dayLines,
      ...legs.map((leg, i) => {
        const day = leg.day ? `（${leg.day}）` : "";
        const scope = SCOPE_LABEL[leg.scope ?? ""] ?? leg.scope ?? "";
        const chosen = (leg.candidates ?? []).find((c) => c.candidate_id === leg.selected_candidate_id);
        const detail = chosen
          ? ` ${chosen.duration_min ?? "?"} 分钟 · ${chosen.distance_km ?? "?"} 公里 · ${weatherLine(chosen)}`
          : "";
        return `${i + 1}. ${day}${legTitle(leg)}${scope ? ` · ${scope}` : ""}${detail}`;
      }),
      ...(contentNotes.length ? ["\n注意事项：", ...contentNotes.map((n) => `- ${n}`)] : []),
      ...(trip.cost_note ? [`\n费用：${trip.cost_note}`] : []),
      reminders.length ? "\n提醒：" : "",
      ...reminders.map((r) => `- ${r}`)
    ]
      .filter(Boolean)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="itin">
      <div className="itin__bar">
        <span>行程单</span>
        <span>{trip.trip_id.slice(-8)}</span>
      </div>

      <div className="itin__body">
        {days.length > 0 && <DayList days={days} segments={segments} />}

        {legs.length === 0 && days.length === 0 ? (
          <div className="itin__empty">本次没有生成任何路段。</div>
        ) : legs.length === 0 ? null : (
          <ol className="itin__legs">
            {legs.map((leg, index) => {
              const candidates = leg.candidates ?? [];
              const chosen = candidates.find((c) => c.candidate_id === leg.selected_candidate_id);
              const others = candidates.filter((c) => c.candidate_id !== leg.selected_candidate_id);
              return (
                <li key={leg.leg_id} className="leg">
                  <div className="leg__mark">{index + 1}</div>
                  <div className="leg__main">
                    <div className="leg__title">{legTitle(leg)}</div>
                    <div className="leg__meta">
                      {leg.day && <span className="leg__day">{leg.day}</span>}
                      {leg.depart && (
                        <span className="leg__window">
                          {clock(leg.depart)} – {clock(leg.arrive)}
                        </span>
                      )}
                      {leg.scope && (
                        <span className="pill pill--idle">{SCOPE_LABEL[leg.scope] ?? leg.scope}</span>
                      )}
                      {leg.approx && <span className="leg__approx">车站位置为估算</span>}
                      {candidates.length > 1 && <span>比较了 {candidates.length} 个去处</span>}
                    </div>

                    {chosen ? (
                      <div className="pick">
                        <div className="pick__main">
                          <span className="pick__mode">{chosen.mode_label ?? chosen.mode ?? "方案"}</span>
                          {chosen.train_code && (
                            <>
                              <span className="pick__train">{chosen.train_code}</span>
                              <span className="pick__stat">
                                {chosen.depart_time} → {chosen.arrive_time}
                              </span>
                            </>
                          )}
                          <span className="pick__stat">{chosen.duration_min ?? "?"} min</span>
                          {chosen.distance_km != null && (
                            <span className="pick__stat">{chosen.distance_km} km</span>
                          )}
                          {chosen.price != null && (
                            <span className="pick__stat">¥{chosen.price}</span>
                          )}
                          <span className="pick__weather">{weatherLine(chosen)}</span>
                        </div>
                        {chosen.from_station && (
                          <div className="pick__stops">
                            {chosen.from_station} → {chosen.to_station}
                          </div>
                        )}
                        {!!chosen.seats?.length && (
                          <div className="pick__seats">
                            {chosen.seats.map((s) => (
                              <span key={s.name ?? ""} className="seat">
                                {s.name} <em>{s.left === "无" ? "无票" : `¥${s.price}`}</em>
                              </span>
                            ))}
                          </div>
                        )}
                        <div className="pick__reason">推荐理由：{reasonText(chosen)}</div>
                        {chosen.weather_notes?.length ? (
                          <ul className="pick__notes">
                            {chosen.weather_notes.slice(0, 2).map((n) => (
                              <li key={n}>{n}</li>
                            ))}
                          </ul>
                        ) : null}
                        {chosen.weather_veto && (
                          <div className="pick__veto">该方案触发天气硬性否决，请谨慎考虑或改期。</div>
                        )}
                      </div>
                    ) : (
                      <div className="pick pick--slim">
                        <div className="pick__main">
                          <span className="pick__stat">{leg.duration_min ?? "?"} min</span>
                          {leg.distance_km != null && (
                            <span className="pick__stat">{leg.distance_km} km</span>
                          )}
                          <span className="pick__weather">{leg.kind === "road" ? "市内接驳" : ""}</span>
                        </div>
                      </div>
                    )}

                    {leg.buffer_minutes != null && (
                      <div className="leg__buffer">
                        与下一段之间预留 {leg.buffer_minutes} 分钟缓冲
                      </div>
                    )}

                    {others.length > 0 && (
                      <div className="alts">
                        <span className="alts__label">备选</span>
                        {others.map((c) => (
                          <span key={c.candidate_id} className="alts__item">
                            {c.destination_name}
                            <em>
                              {c.mode_label ? `${c.mode_label} ` : ""}
                              {c.train_code ? `${c.train_code} ` : ""}
                              {c.duration_min ?? "?"} min
                              {c.distance_km != null ? ` · ${c.distance_km} km` : ""}
                              {c.price != null ? ` · ¥${c.price}` : ""}
                            </em>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        )}

        {actions.length > 0 && (
          <div className="itin__section">
            <div className="itin__label">已准备</div>
            <div className="itin__actions">
              {actions.map((a) => (
                <span key={a.action_id} className="itin__action">
                  {a.capability_id}
                  <em>{STATUS_LABEL[a.status] ?? a.status}</em>
                </span>
              ))}
            </div>
          </div>
        )}

        {reminders.length > 0 && (
          <div className="itin__section">
            <div className="itin__label">提醒</div>
            <ul className="itin__reminders">
              {reminders.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}

        {contentNotes.length > 0 && (
          <div className="itin__section">
            <div className="itin__label">注意事项</div>
            <ul className="itin__reminders">
              {contentNotes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </div>
        )}

        <Deliverables actions={actions} />

        {degradation.length > 0 && (
          <div className="itin__section">
            <div className="itin__label">数据等级</div>
            <div>
              {degradation.map((note, i) => (
                <span key={`${note.capability_id}-${i}`} className="stamp">
                  D{note.level} · {note.capability_id}
                </span>
              ))}
            </div>
            <div className="specimen" style={{ marginTop: "8px" }}>
              降级表示该数据不是实时的，方案仍可用但请留意变化
            </div>
          </div>
        )}
      </div>

      <div className="itin__foot">
        {trip.cost_note && <span className="specimen">费用：{trip.cost_note}</span>}
        <button className="btn btn--sm" onClick={() => void copy()}>
          {copied ? "已复制" : "复制行程单"}
        </button>
        <span className="specimen">付款始终由你在官方渠道完成</span>
      </div>
    </div>
  );
}
