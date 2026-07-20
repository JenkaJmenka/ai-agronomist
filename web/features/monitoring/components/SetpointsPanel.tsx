"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import type { SetpointRange, Setpoints } from "@/features/monitoring/hooks/useSetpoints";
import { CHANNEL_META } from "@/features/monitoring/lib/channelMeta";

interface SetpointsPanelProps {
  /** Discovered channels — only those with CHANNEL_META entries are shown. */
  readonly channels: string[];
  /** Effective setpoints (defaults merged with user overrides). */
  readonly setpoints: Setpoints;
  readonly onUpdate: (channel: string, range: SetpointRange) => void;
  readonly onReset: () => void;
}

// ---------------------------------------------------------------------------
// Per-channel row — isolated state so validation errors don't cross channels.
// ---------------------------------------------------------------------------

interface ChannelRowProps {
  readonly channel: string;
  readonly range: SetpointRange;
  readonly onUpdate: (channel: string, range: SetpointRange) => void;
}

function ChannelSetpointRow({ channel, range, onUpdate }: ChannelRowProps) {
  const meta = CHANNEL_META[channel];
  const [minStr, setMinStr] = useState(String(range[0]));
  const [maxStr, setMaxStr] = useState(String(range[1]));
  const [error, setError] = useState<string | null>(null);

  // Sync display when setpoints are reset externally.
  useEffect(() => {
    setMinStr(String(range[0]));
    setMaxStr(String(range[1]));
    setError(null);
  }, [range[0], range[1]]);

  if (meta === undefined) return null;

  // F13: validation runs on blur instead of per-keystroke to prevent transient
  // "Must be numeric" flashes while the user types partial values like "-" or "3.".
  function tryUpdate(rawMin: string, rawMax: string): void {
    const min = parseFloat(rawMin);
    const max = parseFloat(rawMax);
    if (!Number.isFinite(min) || !Number.isFinite(max)) {
      setError("Must be numeric");
      return;
    }
    if (min >= max) {
      setError("Min must be less than max");
      return;
    }
    setError(null);
    onUpdate(channel, [min, max]);
  }

  const inputBase =
    "w-20 rounded-[var(--radius-control)] border border-border bg-bg px-2 py-1 text-sm tabular-nums text-text focus:outline-accent";

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span className="w-28 shrink-0 text-sm text-text-muted">
          {meta.label}
          <span className="ml-1 text-xs text-text-muted/60">({meta.unit})</span>
        </span>
        <label className="flex items-center gap-1 text-xs text-text-muted">
          Min
          <input
            type="number"
            value={minStr}
            data-testid={`setpoint-min-${channel}`}
            className={inputBase}
            onChange={(e) => {
              setMinStr(e.target.value);
              // Clear stale error while the user is still editing — it will
              // re-appear on blur if the value remains invalid.
              setError(null);
            }}
            onBlur={(e) => {
              // Use e.target.value (current DOM value) to avoid stale closure;
              // maxStr is the last-committed state from the peer field.
              tryUpdate(e.target.value, maxStr);
            }}
          />
        </label>
        <label className="flex items-center gap-1 text-xs text-text-muted">
          Max
          <input
            type="number"
            value={maxStr}
            data-testid={`setpoint-max-${channel}`}
            className={inputBase}
            onChange={(e) => {
              setMaxStr(e.target.value);
              setError(null);
            }}
            onBlur={(e) => {
              tryUpdate(minStr, e.target.value);
            }}
          />
        </label>
      </div>
      {error !== null ? (
        <p className="text-xs text-sev-critical" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

/**
 * Setpoints panel — a floating dropdown opened from the "Setpoints" button.
 *
 * Shows per-channel min/max inputs for all known channels that are currently
 * discovered in the data. Changes take immediate effect in tiles and chart.
 *
 * F13: moves focus into the panel on open and returns it to the trigger on
 * Escape, satisfying WCAG 2.1 SC 2.1.1 and SC 2.4.3 focus-management requirements.
 */
export function SetpointsPanel({ channels, setpoints, onUpdate, onReset }: SetpointsPanelProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  // F13: panelRef lets us move focus into the dialog on open.
  const panelRef = useRef<HTMLDivElement>(null);
  // F13: capture the element that had focus before the dialog opened so we can
  // restore focus to the trigger on close. Avoids needing ref-forwarding on Button.
  const savedFocusRef = useRef<Element | null>(null);

  // F13: Move focus into the dialog whenever it opens.
  useEffect(() => {
    if (!isOpen || panelRef.current === null) return;
    // Prefer the first input; fall back to the dialog container itself
    // (which has tabIndex={-1} for programmatic focus).
    const firstInput = panelRef.current.querySelector<HTMLElement>("input");
    (firstInput ?? panelRef.current).focus();
  }, [isOpen]);

  // Close when user clicks outside the panel.
  useEffect(() => {
    if (!isOpen) return;
    function handleOutsideClick(e: MouseEvent): void {
      if (containerRef.current !== null && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleOutsideClick);
    return () => document.removeEventListener("mousedown", handleOutsideClick);
  }, [isOpen]);

  // F13: Close on Escape and return focus to the element that triggered the dialog.
  useEffect(() => {
    if (!isOpen) return;
    function handleKeyDown(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        setIsOpen(false);
        if (savedFocusRef.current instanceof HTMLElement) {
          savedFocusRef.current.focus();
        }
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  // Only show channels that have CHANNEL_META entries.
  const knownChannels = channels.filter((ch) => ch in CHANNEL_META);

  return (
    <div ref={containerRef} className="relative" data-testid="setpoints-panel">
      <Button
        variant="secondary"
        size="sm"
        onClick={() => {
          if (!isOpen) {
            // Capture focus before the dialog opens; restored on Escape.
            savedFocusRef.current = document.activeElement;
          }
          setIsOpen((o) => !o);
        }}
        aria-expanded={isOpen}
        aria-haspopup="dialog"
        data-testid="setpoints-trigger"
      >
        ⚙ Setpoints
      </Button>

      {isOpen ? (
        <div
          ref={panelRef}
          role="dialog"
          aria-label="Optimal setpoint ranges"
          // tabIndex={-1} makes the container programmatically focusable as a
          // fallback when there are no inputs (empty / loading state).
          tabIndex={-1}
          className={[
            "absolute right-0 top-full z-20 mt-1 w-[26rem]",
            "rounded-[var(--radius-card)] border border-border",
            "bg-bg-elevated p-4 shadow-[var(--shadow-elevation)]",
            "focus:outline-none",
          ].join(" ")}
          data-testid="setpoints-dropdown"
        >
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-text">Optimal setpoints</h3>
            <Button variant="ghost" size="sm" onClick={onReset} data-testid="setpoints-reset">
              Reset to defaults
            </Button>
          </div>

          {knownChannels.length === 0 ? (
            <p className="text-sm text-text-muted">No sensor data available yet.</p>
          ) : (
            <div className="space-y-3 overflow-y-auto" style={{ maxHeight: "min(70vh, 20rem)" }}>
              {knownChannels.map((ch) => {
                const optimal = CHANNEL_META[ch]?.optimal;
                if (optimal === undefined) return null;
                return (
                  <ChannelSetpointRow
                    key={ch}
                    channel={ch}
                    range={setpoints[ch] ?? optimal}
                    onUpdate={onUpdate}
                  />
                );
              })}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
