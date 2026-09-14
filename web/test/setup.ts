import "@testing-library/jest-dom/vitest";

// jsdom does not implement matchMedia. Components that react to the OS color
// scheme (components/charts/chartTheme.ts) call it during subscribe.
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}
