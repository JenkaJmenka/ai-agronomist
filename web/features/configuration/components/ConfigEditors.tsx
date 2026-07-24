"use client";

import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import type { OperationalConfigDocument } from "@/features/configuration/api/operationalConfig";

const MONTH_LABELS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

type Season = "summer" | "winter";

interface SeasonMapEditorProps {
  readonly seasonMap: Readonly<Record<string, Season>>;
  readonly onChange: (next: Record<string, Season>) => void;
}

/** Twelve-month summer/winter mapping used for seasonal detector baselines. */
export function SeasonMapEditor({ seasonMap, onChange }: SeasonMapEditorProps) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4" data-testid="season-map">
      {MONTH_LABELS.map((label, index) => {
        const key = String(index + 1);
        const value = seasonMap[key] ?? "winter";
        return (
          <label key={key} className="flex flex-col gap-1 text-sm">
            <span className="font-medium text-text-muted">{label}</span>
            <select
              className="rounded-[var(--radius-control)] border border-border bg-surface px-2 py-1.5"
              value={value}
              onChange={(event) => {
                const season = event.target.value === "summer" ? "summer" : "winter";
                onChange({ ...seasonMap, [key]: season });
              }}
              data-testid={`season-month-${key}`}
            >
              <option value="summer">Summer</option>
              <option value="winter">Winter</option>
            </select>
          </label>
        );
      })}
    </div>
  );
}

interface SetpointBand {
  readonly day?: number[] | null;
  readonly night?: number[] | null;
}

interface SetpointsEditorProps {
  readonly setpoints: Readonly<Record<string, SetpointBand>>;
  readonly onChange: (next: Record<string, SetpointBand>) => void;
}

/** Day/night min–max bands for crop-specific categorical anomalies. */
export function SetpointsEditor({ setpoints, onChange }: SetpointsEditorProps) {
  const channels = useMemo(() => Object.keys(setpoints).sort(), [setpoints]);

  const updateBand = useCallback(
    (channel: string, phase: "day" | "night", index: 0 | 1, raw: string) => {
      const parsed = Number(raw);
      if (!Number.isFinite(parsed)) return;
      const current = setpoints[channel] ?? { day: [0, 0], night: null };
      const band = [...(current[phase] ?? current.day ?? [0, 0])] as [number, number];
      band[index] = parsed;
      onChange({
        ...setpoints,
        [channel]: {
          day: phase === "day" ? band : (current.day ?? band),
          night: phase === "night" ? band : (current.night ?? null),
        },
      });
    },
    [onChange, setpoints],
  );

  if (channels.length === 0) {
    return <p className="text-sm text-text-muted">No setpoints for this crop.</p>;
  }

  return (
    <div className="overflow-x-auto" data-testid="setpoints-editor">
      <table className="w-full min-w-[36rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-border text-left text-text-muted">
            <th className="py-2 pr-3 font-medium">Channel</th>
            <th className="py-2 pr-3 font-medium">Day min</th>
            <th className="py-2 pr-3 font-medium">Day max</th>
            <th className="py-2 pr-3 font-medium">Night min</th>
            <th className="py-2 font-medium">Night max</th>
          </tr>
        </thead>
        <tbody>
          {channels.map((channel) => {
            const band = setpoints[channel];
            const day = band?.day ?? [0, 0];
            const night = band?.night;
            return (
              <tr key={channel} className="border-b border-border/60">
                <td className="py-2 pr-3 font-medium capitalize">{channel}</td>
                <td className="py-2 pr-3">
                  <input
                    type="number"
                    step="any"
                    className="w-24 rounded border border-border bg-surface px-2 py-1"
                    value={day[0] ?? ""}
                    onChange={(e) => updateBand(channel, "day", 0, e.target.value)}
                  />
                </td>
                <td className="py-2 pr-3">
                  <input
                    type="number"
                    step="any"
                    className="w-24 rounded border border-border bg-surface px-2 py-1"
                    value={day[1] ?? ""}
                    onChange={(e) => updateBand(channel, "day", 1, e.target.value)}
                  />
                </td>
                <td className="py-2 pr-3">
                  {night == null ? (
                    <span className="text-text-muted">—</span>
                  ) : (
                    <input
                      type="number"
                      step="any"
                      className="w-24 rounded border border-border bg-surface px-2 py-1"
                      value={night[0] ?? ""}
                      onChange={(e) => updateBand(channel, "night", 0, e.target.value)}
                    />
                  )}
                </td>
                <td className="py-2">
                  {night == null ? (
                    <span className="text-text-muted">—</span>
                  ) : (
                    <input
                      type="number"
                      step="any"
                      className="w-24 rounded border border-border bg-surface px-2 py-1"
                      value={night[1] ?? ""}
                      onChange={(e) => updateBand(channel, "night", 1, e.target.value)}
                    />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Clone a server document into editable local draft state. */
export function cloneConfig(doc: OperationalConfigDocument): OperationalConfigDocument {
  return structuredClone(doc);
}

/** Keep draft in sync when the server payload identity changes. */
export function useConfigDraft(serverConfig: OperationalConfigDocument | null): {
  readonly draft: OperationalConfigDocument | null;
  readonly setDraft: Dispatch<SetStateAction<OperationalConfigDocument | null>>;
  readonly isDirty: boolean;
} {
  const [draft, setDraft] = useState<OperationalConfigDocument | null>(null);

  useEffect(() => {
    if (serverConfig) setDraft(cloneConfig(serverConfig));
  }, [serverConfig]);

  const isDirty = useMemo(() => {
    if (!draft || !serverConfig) return false;
    return JSON.stringify(draft) !== JSON.stringify(serverConfig);
  }, [draft, serverConfig]);

  return { draft, setDraft, isDirty };
}
