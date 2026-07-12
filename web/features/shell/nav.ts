/** Sidebar navigation — mirrors the Streamlit app's page tree. */

export interface NavEntry {
  readonly href: string;
  readonly label: string;
}

export interface NavSection {
  readonly title: string | null;
  readonly entries: readonly NavEntry[];
}

export const NAV_SECTIONS: readonly NavSection[] = [
  { title: null, entries: [{ href: "/", label: "Home" }] },
  {
    title: "Active Monitoring",
    entries: [
      { href: "/monitoring", label: "Monitoring" },
      { href: "/climate", label: "Climate Data" },
      { href: "/gas", label: "Gas Monitoring" },
    ],
  },
  {
    title: "Detection",
    entries: [
      { href: "/anomalies", label: "Anomaly Detection" },
      { href: "/thresholds", label: "Thresholds" },
      { href: "/metrics", label: "System Metrics" },
    ],
  },
  {
    title: "Operations",
    entries: [
      { href: "/operations", label: "Upload & Operations" },
      { href: "/jobs", label: "Job Monitoring" },
    ],
  },
  {
    title: "Feedback",
    entries: [{ href: "/feedback", label: "Operator Feedback" }],
  },
];
