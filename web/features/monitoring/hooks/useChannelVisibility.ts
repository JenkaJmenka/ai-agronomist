"use client";

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
   * Empty when no anomaly jump is active.
   */
  readonly jumpFocus: readonly string[];
  /** Toggle a channel on or off. Clears the anomaly jump pre-select on first call. */
  readonly toggleChannel: (ch: string) => void;
}

export function useChannelVisibility({
  discoveredNow,
  channelParam,
  tenant,
  gh,
  channelFilter,
  channelMatch = "includes",
}: UseChannelVisibilityOptions): UseChannelVisibilityResult {
  // Remember every channel seen this session so rolling Last 15/30 min windows
  // do not remove sensors that only appear on some ticks.
  const [knownChannels, setKnownChannels] = useState<string[]>([]);
  useEffect(() => {
    setKnownChannels([]);
  }, [tenant, gh]);
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

  const [hidden, setHidden] = useState<ReadonlySet<string> | null>(null);
  const userToggledChannels = useRef(false);

  // New tenant / gh / jump focus clears manual toggles so preselect can re-apply.
  useEffect(() => {
    userToggledChannels.current = false;
    setHidden(null);
  }, [tenant, gh, channelParam]);

  // Anomaly jump: keep affected sensors from `channel=` pre-selected (and others
  // off) until the user toggles a channel themselves.
  useEffect(() => {
    if (!channelParam || channels.length === 0 || userToggledChannels.current) return;
    const matched = matchChannelParam(channels, channelParam);
    if (matched.length === 0) return;
    const matchedSet = new Set(matched);
    setHidden(new Set(channels.filter((ch) => !matchedSet.has(ch))));
  }, [channelParam, channels]);

  const jumpFocus = useMemo(
    () => (channelParam ? matchChannelParam(channels, channelParam) : []),
    [channelParam, channels],
  );

  const visible = useMemo(() => {
    if (hidden !== null) return new Set(channels.filter((ch) => !hidden.has(ch)));
    if (jumpFocus.length > 0) return new Set(jumpFocus);
    return new Set(channels);
  }, [channels, hidden, jumpFocus]);

  const toggleChannel = useCallback(
    (ch: string): void => {
      userToggledChannels.current = true;
      setHidden(() => {
        const next = new Set(channels.filter((candidate) => !visible.has(candidate)));
        if (next.has(ch)) next.delete(ch);
        else next.add(ch);
        return next;
      });
      // Profile after scheduling state so the paint path is not delayed by the probe.
      measureInteraction("ui.channel_toggle", { channel: ch });
    },
    [channels, visible],
  );

  return { channels, visible, jumpFocus, toggleChannel };
}
