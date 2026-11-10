"use client";

import { usePathname } from "next/navigation";

import { Sidebar } from "@/features/shell/Sidebar";

// layout.tsx is an async server component (it awaits auth()), so the
// sidebar-visibility branch can't live there — usePathname needs a client
// component. /login has no authenticated session to drive the sidebar with,
// so it renders a bare, centered shell instead.
const ROUTES_WITHOUT_SIDEBAR = new Set(["/login"]);

export function AppShell({ children }: { readonly children: React.ReactNode }) {
  const pathname = usePathname();

  if (ROUTES_WITHOUT_SIDEBAR.has(pathname)) {
    return <main className="flex min-h-dvh items-center justify-center bg-bg p-6">{children}</main>;
  }

  return (
    <div className="flex">
      <Sidebar />
      <main className="min-h-dvh flex-1 overflow-x-hidden p-6">{children}</main>
    </div>
  );
}
