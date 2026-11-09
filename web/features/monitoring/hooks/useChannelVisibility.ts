"use client";

import { parseAsString, useQueryState } from "nuqs";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { mergeChannelLists } from "@/features/monitoring/api/readings";
import { matchChannelParam } from "@/features/monitoring/lib/anomalyHighlight";
import { measureInteraction } from "@/lib/profiling";

interface UseChannelVisibilityOptions {
  /** Channels seen in the current data window (used to build the session memory). */
  readonly discoveredNow: readonly string[];
  /** `channel=` URL param for anomaly-jump pre-select (null = no jump). */
  readonly channelParam: string | null;
  /** Tenant/gh identity — resets session memory and manual toggles on change. */
  readonly tenant: string;
  readonly gh: string;
  /**
   * Live vs historical mode. Toggling live resets session channel memory so
   * Connext raw headers do not stay mixed with mock/live canonical channels.
   */
  readonly live?: boolean;
  /** When set, only channels matching the filter are surfaced to the view. */
  readonly channelFilter?: readonly string[];
  /**
   * How `channelFilter` matches discovered names.
   * - `includes` (default): substring match
   * - `exact`: only exact channel names
   */
  readonly channelMatch?: "includes" | "exact";
}

interface UseChannelVisibilityResult {
  /**
   * Filtered session-remembered channel list. Stable across rolling windows
   * so sensors only present in some ticks do not flicker out.
   */
  readonly channels: string[];
  /** Currently visible channels (respects manual toggles and jump pre-select). */
  readonly visible: ReadonlySet<string>;
  /**
   * Channels that match the `channel=` jump param on the current page.
   * Empty when no anomaly jump is active or when no match was found (fall
   * back to showing all channels so the chart is never blank).
   */
  readonly jumpFocus: readonly string[];
  /** Toggle a channel on or off. Clears the anomaly jump pre-select on first call. */
  readonly toggleChannel: (ch: string) => void;
}

/**
 * Channel visibility with pure jump focus.
 *
 * When `channel=` matches nothing on this page (wrong page / unmatched names),
 * we show **all** page channels instead of an empty chart — operators used to
 * see a blank series until they manually toggled a sensor.
 */
export function useChannelVisibility({
  discoveredNow,
  channelParam,
  tenant,
  gh,
  live = false,
  channelFilter,
  channelMatch = "includes",
}: UseChannelVisibilityOptions): UseChannelVisibilityResult {
  const [, setChannelParam] = useQueryState("channel", parseAsString);

  // Remember every channel seen this session so rolling Last 15/30 min windows
  // do not remove sensors that only appear on some ticks.
  const [knownChannels, setKnownChannels] = useState<string[]>([]);
  useEffect(() => {
    setKnownChannels([]);
  }, [tenant, gh, live]);
  useEffect(() => {
    setKnownChannels((prev) => {
      const next = mergeChannelLists(prev, discoveredNow);
      if (next.length === prev.length && next.every((ch, i) => ch === prev[i])) return prev;
      return next;
    });
  }, [discoveredNow]);

  const channels = useMemo(() => {
    const discovered = knownChannels.length > 0 ? knownChannels : discoveredNow;
    if (!channelFilter || channelFilter.length === 0) return [...discovered];
    if (channelMatch === "exact") {
      const wanted = new Set(channelFilter);
      return discovered.filter((ch) => wanted.has(ch));
    }
    return discovered.filter((ch) =>
      channelFilter.some((f) => ch.toLowerCase().includes(f.toLowerCase())),
    );
  }, [knownChannels, discoveredNow, channelFilter, channelMatch]);

  /** Manual override — null means "follow jump focus / show all". */
  const [manualVisible, setManualVisible] = useState<ReadonlySet<string> | null>(null);

  // The last `channelParam` a pre-select was applied for. Lets us tell a
  // fresh jump (new non-null param) apart from `toggleChannel` clearing the
  // param back to null, which must NOT re-apply/clear the manual selection.
  const lastAppliedJumpParamRef = useRef<string | null>(null);

  // New tenant/gh clears manual overrides and jump bookkeeping outright.
  useEffect(() => {
    setManualVisible(null);
    lastAppliedJumpParamRef.current = null;
  }, [tenant, gh]);

  // A new, different, non-null jump param re-applies the pre-select. A
  // transition to null (e.g. from toggleChannel dropping sticky focus) is
  // intentionally not handled here, so it leaves manualVisible alone.
  useEffect(() => {
    if (channelParam && channelParam !== lastAppliedJumpParamRef.current) {
      setManualVisible(null);
      lastAppliedJumpParamRef.current = channelParam;
    }
  }, [channelParam]);

  const jumpFocus = useMemo(
    () => (channelParam ? matchChannelParam(channels, channelParam) : []),
    [channelParam, channels],
  );

  const visible = useMemo(() => {
    if (manualVisible !== null) {
      // Keep only channels that still exist on this page.
      return new Set(channels.filter((ch) => manualVisible.has(ch)));
    }
    // Matched jump sensors → focus them. Unmatched / no jump → show all.
    if (jumpFocus.length > 0) return new Set(jumpFocus);
    return new Set(channels);
  }, [channels, manualVisible, jumpFocus]);

  const toggleChannel = useCallback(
    (ch: string): void => {
      setManualVisible((prev) => {
        const base = prev ?? new Set(jumpFocus.length > 0 ? jumpFocus : channels);
        const next = new Set(base);
        if (next.has(ch)) next.delete(ch);
        else next.add(ch);
        return next;
      });
      // Drop sticky jump focus so later Monitoring visits are not stuck filtered.
      void setChannelParam(null);
      measureInteraction("ui.channel_toggle", { channel: ch });
    },
    [jumpFocus, channels, setChannelParam],
  );

  return { channels, visible, jumpFocus, toggleChannel };
}
