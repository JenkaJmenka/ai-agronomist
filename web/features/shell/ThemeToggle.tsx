"use client";

import { useEffect, useState } from "react";

type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "theme";

/** Google Material Symbols "light_mode" (sun), 24px viewBox. */
function SunIcon() {
  return (
    <svg width="18" height="18" viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true">
      <path d="M480-360q50 0 85-35t35-85q0-50-35-85t-85-35q-50 0-85 35t-35 85q0 50 35 85t85 35Zm0 80q-83 0-141.5-58.5T280-480q0-83 58.5-141.5T480-680q83 0 141.5 58.5T680-480q0 83-58.5 141.5T480-280ZM200-440H40v-80h160v80Zm720 0H760v-80h160v80ZM440-760v-160h80v160h-80Zm0 720v-160h80v160h-80ZM256-650l-101-97 57-59 96 100-52 56Zm492 496-97-101 53-55 101 97-57 59Zm-98-550 97-101 59 57-100 96-56-52ZM154-212l101-97 55 53-97 101-59-57Z" />
    </svg>
  );
}

/** Google Material Symbols "dark_mode" (moon), 24px viewBox. */
function MoonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true">
      <path d="M480-120q-150 0-255-105T120-480q0-150 105-255t255-105q14 0 27.5 1t26.5 3q-41 29-65.5 75.5T444-660q0 90 63 153t153 63q55 0 101-24.5t75-65.5q2 13 3 26.5t1 27.5q0 150-105 255T480-120Z" />
    </svg>
  );
}

function resolveInitialTheme(): Theme {
  const attr = document.documentElement.dataset.theme;
  if (attr === "light" || attr === "dark") return attr;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/**
 * Dark/light switch: stamps data-theme on <html> (globals.css tokens flip on
 * it) and persists the choice; the layout's inline script restores it before
 * first paint. Sun in light mode, moon in dark.
 */
export function ThemeToggle() {
  // null until mounted — the server can't know the stored theme.
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    setTheme(resolveInitialTheme());
  }, []);

  function handleToggle(): void {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Storage unavailable (private mode) — theme still applies this session.
    }
    setTheme(next);
  }

  return (
    <button
      type="button"
      onClick={handleToggle}
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      data-testid="theme-toggle"
      className="inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-control)] text-text-muted transition-colors hover:bg-border/50 hover:text-text focus-visible:outline-2 focus-visible:outline-accent"
    >
      {theme === "dark" ? <MoonIcon /> : <SunIcon />}
    </button>
  );
}
