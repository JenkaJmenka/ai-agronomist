"use client";

import { channelColor } from "@/lib/colors";

interface ChannelTogglesProps {
  readonly channels: string[];
  readonly visible: ReadonlySet<string>;
  readonly onToggle: (channel: string) => void;
}

/** Pure client-side visibility toggles — the chart re-renders locally,
 *  never refetching (fixes the 35-45 s Streamlit channel-toggle cost). */
export function ChannelToggles({ channels, visible, onToggle }: ChannelTogglesProps) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-2" data-testid="channel-toggles">
      {channels.map((ch, i) => (
        <label key={ch} className="flex cursor-pointer items-center gap-1.5 text-sm">
          <input
            type="checkbox"
            checked={visible.has(ch)}
            onChange={() => onToggle(ch)}
            className="accent-current"
            style={{ color: channelColor(i) }}
          />
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: channelColor(i) }}
            aria-hidden="true"
          />
          {ch}
        </label>
      ))}
    </div>
  );
}
