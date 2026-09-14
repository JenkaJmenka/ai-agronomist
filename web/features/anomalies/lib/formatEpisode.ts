import type { AnomalyEpisode } from "@/features/anomalies/api/episodes";

const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;

/** Compact span label: "45 min", "2.5 hr", or "single reading" for a lone sample. */
export function formatDuration(seconds: number): string {
  if (seconds <= 0) return "single reading";
  if (seconds < SECONDS_PER_HOUR) {
    return `${Math.round(seconds / SECONDS_PER_MINUTE)} min`;
  }
  return `${(seconds / SECONDS_PER_HOUR).toFixed(1)} hr`;
}

/** "2026-12-25 09:55 → 10:40", collapsing the date when both ends share a day. */
export function formatEpisodeSpan(startTs: string, endTs: string): string {
  const startDate = startTs.slice(0, 10);
  const endDate = endTs.slice(0, 10);
  const startTime = startTs.slice(11, 16);
  const endTime = endTs.slice(11, 16);
  if (startTs === endTs) return `${startDate} ${startTime}`;
  if (startDate === endDate) return `${startDate} ${startTime} → ${endTime}`;
  return `${startDate} ${startTime} → ${endDate} ${endTime}`;
}

/** Arrow marking which way the channel moved out of band. */
export function directionArrow(direction: AnomalyEpisode["direction"]): string {
  if (direction === "drop") return "↓";
  if (direction === "spike") return "↑";
  return "•";
}

const TREND_LABELS: Readonly<Record<AnomalyEpisode["trend"], string>> = {
  worsening: "worsening",
  easing: "easing",
  flat: "steady",
};

export function formatTrend(trend: AnomalyEpisode["trend"]): string {
  return TREND_LABELS[trend];
}

/**
 * "32.0% → 29.0%" — where the channel started and the worst value it reached.
 *
 * The extremum matters more than the final reading: an operator needs to know
 * humidity bottomed at 29%, not that it happened to be 30% when the episode
 * ended.
 */
export function formatValueRange(episode: AnomalyEpisode, unit: string): string {
  const { first_value: first, peak_value: peak } = episode;
  if (first === null && peak === null) return "";
  if (first === null || peak === null) {
    return `${(peak ?? first)?.toFixed(1)}${unit}`;
  }
  if (first === peak) return `${peak.toFixed(1)}${unit}`;
  return `${first.toFixed(1)}${unit} → ${peak.toFixed(1)}${unit}`;
}

const CHANNEL_UNITS: Readonly<Record<string, string>> = {
  temperature: "°C",
  humidity: "%",
  co2: " ppm",
  light: " W/m²",
};

export function channelUnit(channel: string): string {
  return CHANNEL_UNITS[channel] ?? "";
}

/** Sentence-cased channel name for display ("humidity" → "Humidity"). */
export function formatChannel(channel: string): string {
  return channel.charAt(0).toUpperCase() + channel.slice(1).replace(/_/g, " ");
}
