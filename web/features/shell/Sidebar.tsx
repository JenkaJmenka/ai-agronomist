"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { NAV_SECTIONS } from "@/features/shell/nav";
import { TenantSelector } from "@/features/shell/TenantSelector";

function NavLinks() {
  const pathname = usePathname();
  const search = useSearchParams();
  // Keep tenant/gh context on every navigation.
  const scope = new URLSearchParams();
  for (const key of ["tenant", "gh"] as const) {
    const value = search.get(key);
    if (value) scope.set(key, value);
  }
  const suffix = scope.size > 0 ? `?${scope.toString()}` : "";

  return (
    <nav className="flex-1 overflow-y-auto px-2 py-3">
      {NAV_SECTIONS.map((section) => (
        <div key={section.title ?? "root"} className="mb-4">
          {section.title ? (
            <p className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
              {section.title}
            </p>
          ) : null}
          {section.entries.map((entry) => {
            const isActive = pathname === entry.href;
            return (
              <Link
                key={entry.href}
                href={`${entry.href}${suffix}`}
                aria-current={isActive ? "page" : undefined}
                className={`block rounded-[var(--radius-control)] px-2 py-1.5 text-sm ${
                  isActive ? "bg-accent/15 font-medium text-accent" : "text-text hover:bg-border/50"
                }`}
              >
                {entry.label}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

export function Sidebar() {
  return (
    <aside className="flex h-dvh w-60 shrink-0 flex-col border-r border-border bg-bg-elevated">
      <div className="px-4 py-4">
        <p className="text-sm font-bold text-accent">AI Agronomist</p>
      </div>
      <Suspense>
        <NavLinks />
        <TenantSelector />
      </Suspense>
    </aside>
  );
}
