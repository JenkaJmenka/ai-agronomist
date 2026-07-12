import type { ReactNode } from "react";

interface CardProps {
  readonly title?: string;
  readonly children: ReactNode;
  readonly className?: string;
}

export function Card({ title, children, className = "" }: CardProps) {
  return (
    <section
      className={`rounded-[var(--radius-card)] border border-border bg-bg-elevated p-4 shadow-[var(--shadow-elevation)] ${className}`}
    >
      {title ? (
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-text-muted">
          {title}
        </h2>
      ) : null}
      {children}
    </section>
  );
}
