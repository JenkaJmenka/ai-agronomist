import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

import { auth } from "@/auth";
import { Sidebar } from "@/features/shell/Sidebar";
import { Providers } from "./providers";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });

export const metadata: Metadata = {
  title: "AI Agronomist",
  description: "Greenhouse monitoring and anomaly detection",
};

// Static literal (no user input): restores the persisted theme before first
// paint so a dark-mode reload never flashes light. Key matches ThemeToggle.
const THEME_INIT_SCRIPT = `(function () {
  try {
    var theme = localStorage.getItem("theme");
    if (theme === "dark" || theme === "light") {
      document.documentElement.dataset.theme = theme;
    }
  } catch (e) {}
})();`;

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Seeds useScope()'s tenant default (see SessionTenantContext) — proxy.ts
  // already fail-closes anonymous requests before they reach a page, so a
  // null session here only happens on public routes like /login.
  const session = await auth();
  const tenantId = session?.user?.tenantId ?? null;

  return (
    <html lang="en" className={`${inter.variable} h-full antialiased`}>
      <body className="min-h-full">
        {/* biome-ignore lint/security/noDangerouslySetInnerHtml: static theme-restore literal */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <Providers tenantId={tenantId}>
          <div className="flex">
            <Sidebar />
            <main className="min-h-dvh flex-1 overflow-x-hidden p-6">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
