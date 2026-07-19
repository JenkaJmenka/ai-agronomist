"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import { parseAsBoolean, parseAsIsoDateTime, useQueryState, useQueryStates } from "nuqs";

import { Button } from "@/components/ui/Button";

/**
 * "Go Live" toggle button — drives the `live` URL param.
 * Disabled (with tooltip) when an explicit from/to window is set; the date
 * window takes precedence over live mode.
 */
export function LiveToggle() {
  const [live, setLive] = useQueryState("live", parseAsBoolean.withDefault(false));
  const [{ from, to }] = useQueryStates({
    from: parseAsIsoDateTime,
    to: parseAsIsoDateTime,
  });

  const hasWindow = from !== null && to !== null;
  const isDisabled = hasWindow;

  function handleClick(): void {
    if (isDisabled) return;
    void setLive((prev) => !prev);
  }

  const button = (
    <Button
      variant={live && !isDisabled ? "primary" : "secondary"}
      size="sm"
      onClick={handleClick}
      disabled={isDisabled}
      aria-pressed={live && !isDisabled}
      data-testid="live-toggle"
      data-live={live && !isDisabled ? "true" : "false"}
    >
      <span
        className={`inline-block h-2 w-2 rounded-full ${
          live && !isDisabled ? "animate-pulse bg-green-400" : "bg-text-muted"
        }`}
        aria-hidden="true"
      />
      Go Live
    </Button>
  );

  if (!isDisabled) {
    return button;
  }

  return (
    <Tooltip.Provider delayDuration={300}>
      <Tooltip.Root>
        {/* Wrap disabled button in a span so pointer events reach the trigger */}
        <Tooltip.Trigger asChild>
          <span data-testid="live-toggle-wrapper">{button}</span>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content
            side="bottom"
            className="z-50 rounded bg-bg-elevated px-2 py-1 text-xs text-text shadow"
          >
            Clear the date window to enable live mode
            <Tooltip.Arrow className="fill-bg-elevated" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}
