interface SkeletonProps {
  readonly className?: string;
}

/** Loading placeholder — plan §7.4: skeletons, not spinners. */
export function Skeleton({ className = "h-4 w-full" }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={`animate-pulse rounded-[var(--radius-control)] bg-border ${className}`}
    />
  );
}
