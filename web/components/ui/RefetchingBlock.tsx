import type { ReactNode } from "react";

interface RefetchingBlockProps {
  readonly isRefetching: boolean;
  readonly className?: string;
  readonly children: ReactNode;
}

/**
 * Loading cue for keepPreviousData refetches (plan §4.3): a range/tenant
 * switch keeps the stale view on screen, so pulse-dim it — matching the
 * skeleton animation — instead of dropping back to full skeletons.
 */
export function RefetchingBlock({ isRefetching, className = "", children }: RefetchingBlockProps) {
  return (
    <div
      aria-busy={isRefetching || undefined}
      className={`${className} ${isRefetching ? "animate-pulse opacity-60" : ""}`.trim()}
    >
      {children}
    </div>
  );
}
