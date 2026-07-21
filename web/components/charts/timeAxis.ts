import type uPlot from "uplot";

/**
 * Min px between x-axis ticks. Default (~40–50) packs minute/second labels
 * until they collide — especially when a day-rollover date is prepended.
 */
const TIME_AXIS_SPACE = 88;

/**
 * Rollover stamp table (seconds scale).
 *
 * Differs from uPlot defaults in two ways that fix the live-window timeline:
 * 1. Second-level default is a clock time (`h:mmaa`), not bare `:ss`.
 * 2. Day/year rollovers are a short `\n{MMM} {D}` line — never
 *    `"M/D/YY h:mmaa"` concatenated onto the first tick (that was overflowing
 *    into the neighbor label).
 *
 * Columns: [incr, default, year, month, day, hour, min, sec, mode]
 * mode 1 = concat rollover onto default (uPlot convention).
 */
export const TIME_AXIS_VALUES: uPlot.Axis.TimeValuesConfig = [
  [3600 * 24 * 365, "{YYYY}", null, null, null, null, null, null, 1],
  [3600 * 24 * 28, "{MMM}", "\n{YYYY}", null, null, null, null, null, 1],
  [3600 * 24, "{MMM} {D}", "\n{YYYY}", null, null, null, null, null, 1],
  [3600, "{h}{aa}", "\n{MMM} {D}", null, "\n{MMM} {D}", null, null, null, 1],
  [60, "{h}:{mm}{aa}", "\n{MMM} {D}", null, "\n{MMM} {D}", null, null, null, 1],
  [1, "{h}:{mm}{aa}", "\n{MMM} {D}", null, "\n{MMM} {D}", null, null, null, 1],
  [0.001, "{h}:{mm}:{ss}", "\n{MMM} {D}", null, "\n{MMM} {D}", null, null, null, 1],
];

/** Shared x-axis defaults for every time-series chart. */
export const TIME_X_AXIS: uPlot.Axis = {
  space: TIME_AXIS_SPACE,
  values: TIME_AXIS_VALUES,
  gap: 8,
  /** Room for an optional two-line day rollover under the tick. */
  size: 52,
};
