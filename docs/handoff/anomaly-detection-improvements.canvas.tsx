import {
  Callout,
  Code,
  CollapsibleSection,
  Divider,
  Grid,
  H1,
  H2,
  H3,
  Pill,
  Row,
  Stack,
  Stat,
  Table,
  Text,
} from "cursor/canvas";

const accuracyMetric =
  "0.5 × clean specificity + 0.5 × mean detection recall on injected faults (spike, noise, two-channel level shift). Measured on native_july_clearsky.csv with a tenant-calibrated noise baseline.";

const phase1Rows: string[][] = [
  [
    "P1.1",
    "Upload: add MAD spike detector",
    "24 h rolling z cannot see +6 °C (z ≈ 1.26 vs gate 5.5). MAD c=8, floor 0.5 °C caught 12/12 spikes with 0 clean false hits.",
    "backend + ml",
    "Must",
  ],
  [
    "P1.2",
    "Upload: enable robust LSTM gate",
    "Turn on COLD_RUN_ROBUST_GATE_ENABLED so the 99.9th percentile stops flagging 0.1 % of every clean file.",
    "backend",
    "Must",
  ],
  [
    "P1.3",
    "Upload: align validation bounds to OOB",
    "Raise CO₂ / temperature chunk bounds to match OOB_LIMITS so a 1050 ppm reading is labelled, not discarded.",
    "backend",
    "Must",
  ],
  [
    "P1.4",
    "Pipeline: score the last timestep only",
    "Enrichment is about the current reading. any(flags) on a 20-step window drives the 99.7 % clean false-alarm rate.",
    "backend",
    "Must",
  ],
  [
    "P1.5",
    "Pipeline: tenant-calibrated noise baseline",
    "Fixed NOISE_BASELINE_VARIANCE is ~70× too low for humidity on real data. Median high-pass variance from tenant history + ratio 10–20 → FPR 3.6–9.6 % with 100 % noise recall.",
    "ml + backend",
    "Must",
  ],
  [
    "P1.6",
    "Pipeline: precedence rewrite",
    "Order: OOB / dropout → BOCPD change-point → spike MAD → noise → dual bias → diurnal → Mahalanobis → bands. Spike before noise; BOCPD before noise so a level shift is not labelled accuracy_decline.",
    "backend",
    "Must",
  ],
  [
    "P1.7",
    "BOCPD: any flag in the window",
    "Reading only flags[-1] missed 40/40 mid-window level shifts. any(flags) caught 40/40.",
    "ml + backend",
    "Must",
  ],
  [
    "P1.8",
    "Label fixes",
    "Emit out_of_bounds (not spike_fault). Map psychrometric_inconsistency to a CHECK-allowed type or extend migration 019 (closes BACKLOG-20).",
    "backend",
    "Must",
  ],
];

const phase1Measured: string[][] = [
  ["Current comprehensive path", "99.7 %", "~0 % on spike/bias/level-shift labels", "~0.2 %"],
  ["Proposed Phase 1 bundle (prototyped)", "4.2 %", "100 % detect, 100 % label on spike/noise/level-shift", "97.9 %"],
  ["Upload MAD only (c=8, floor 0.5)", "0 false hits / week", "12/12 single-sample +6 °C spikes", "≈100 % on that slice"],
];

const neverDetected: string[][] = [
  [
    "Short spikes / holds on CSV upload",
    "0/24 at +6…+20 °C",
    "P1.1 MAD on upload path",
    "Prototyped: 12/12 at +6 °C",
  ],
  [
    "Bias step (+3 °C lasting days) on upload",
    "0/1",
    "P2: hour-of-day residual or persistent BiasEMA across portions",
    "HoD residual catches onset; needs FPR tuning",
  ],
  [
    "Slow drift (0→+4 °C / 5 days) on upload",
    "0/1",
    "P2: stateful CUSUM on upload portions with reset + seeded EMA variance",
    "Not yet above 85 % alone",
  ],
  [
    "Two-channel level shift → calibration_drift",
    "0/40 labels (BOCPD last-step)",
    "P1.7 + P1.6",
    "Prototyped: 40/40 correct",
  ],
  [
    "out_of_bounds label",
    "Never emitted",
    "P1.8",
    "Code-only",
  ],
  [
    "thermodynamic_contradiction / humidity_fault",
    "Unreachable or DB-forbidden",
    "P1.8 + BACKLOG-20",
    "Code-only",
  ],
  [
    "SCADA flatline on 20-step windows",
    "17/40 (exact var == 0)",
    "P1.9: variance ≤ 1e-4; identical_steps ≤ SEQ_LEN",
    "Prototyped: 40/40 at eps 1e-4",
  ],
  [
    "Pattern classifier → fault_type",
    "Never reaches fault_type when ensemble bias_flag is always on",
    "P1.10: remove midpoint BiasOffsetDetector from ensemble; denorm inputs",
    "Broken flag 100/100 → drop the check",
  ],
];

const phase2Rows: string[][] = [
  ["P2.1", "Seed EMA variance and reset CUSUM after alarm", "Stationary alarm fraction 32 % → 3 % with min_std + reset; still early — needs warm-up samples before arming."],
  ["P2.2", "Hour-of-day residual on upload for bias/drift", "Catches +3 °C bias onset; clean FPR still high — combine with MAD and require persistence."],
  ["P2.3", "Exclude light from stuck / spike night votes", "Zero light at night trips stuck pre-check; light is diurnal, not a frozen sensor."],
  ["P2.4", "Mahalanobis against tenant history, not the 20-step window", "In-window fit only finds outliers relative to themselves."],
  ["P2.5", "Wire climate_context on live enrichment", "Over-heating / under-heating / HD rules never see live setpoints today."],
  ["P2.6", "Refresh ANOMALY_CATALOG against the new precedence", "17 doc mismatches from the audit stay open until this lands."],
];

const designRows: string[][] = [
  [
    "D1",
    "One scoring path for operators",
    "CSV upload, live enrichment, and /api/inference/comprehensive use three different detector sets. Operators only see upload + live. Either route upload through the comprehensive pipeline (windowed) or document upload as a deliberately smaller detector set and stop implying catalog Parts 1–4 apply to it.",
  ],
  [
    "D2",
    "LSTM as a score, not a false-alarm machine",
    "On upload the 99.9th percentile guarantees ~0.1 % alerts on every file. Use a fixed or robust threshold (median + z·MAD) calibrated on clean history, and keep the LSTM as one ensemble member with a real vote threshold of 0.8 (today members vote at half the documented value).",
  ],
  [
    "D3",
    "Normalise once, denormalise for every physical rule",
    "BACKLOG-23 (train/serve skew) is still open. Ensemble bias/correlation already compare normalised values to physical ranges. Fix: denormalise before BiasOffset, Correlation, Stuck, and any range check; keep normalised tensors only for the LSTM-VAE.",
  ],
  [
    "D4",
    "Vector / embedding use is thin",
    "The VAE latent is computed and mostly unused for detection. Near-term: nearest-neighbour / Mahalanobis in latent space against a clean tenant gallery. Medium-term: replace the broken midpoint bias flag with latent residual growth. Do not invent a second unsupervised stack beside Isolation Forest until the gallery exists.",
  ],
  [
    "D5",
    "Stateful detectors need a session, not a fresh pipeline",
    "Live enrichment constructs a new ComprehensiveInferencePipeline per call, so CUSUM/EMA never accumulate. Persist LearningState (already serialisable) per tenant scope across enrichment calls, and reset only on acknowledged alarms.",
  ],
  [
    "D6",
    "Fault-type fusion should be scored, not first-wins",
    "next(iter(rule_votes)) makes gate insertion order the product taxonomy. Replace with an explicit priority table (short-circuit > change-point > spike > noise > bias > band) and unit tests that lock the table.",
  ],
  [
    "D7",
    "Accuracy gate in CI",
    "Promote the /tmp harness into tests/models/test_detection_accuracy.py: clean-week FPR ceiling, injected-fault recall floor (≥85 % composite), and upload MAD spike recall. Fail the build when a detector change regresses them.",
  ],
  [
    "D8",
    "Single-replica + provenance stay as-is",
    "Do not couple detection work to multi-replica live scoring (BACKLOG-7) or CSV source collisions. Those are separate.",
  ],
];

const targetRows: string[][] = [
  ["Clean specificity", "≥ 90 % (FPR ≤ 10 %)", "95.8 % on held-out half-week with calibrated noise"],
  ["Injected detection recall", "≥ 85 % mean", "100 % on spike / noise / level-shift bundle"],
  ["Label accuracy (spike, noise, level-shift)", "≥ 85 %", "100 % with BOCPD-before-noise precedence"],
  ["Upload short-spike recall", "≥ 85 %", "100 % (12/12) with MAD c=8 floor 0.5"],
  ["Stretch target after Phase 1", "≥ 95 % composite", "97.9 % prototyped on the three-fault bundle"],
];

export default function AnomalyDetectionImprovements() {
  return (
    <Stack gap={24}>
      <Stack gap={6}>
        <H1>Anomaly detection improvement plan</H1>
        <Text tone="secondary">
          Grounded in the main audit and /tmp/anomaly_audit prototypes. No repo changes yet — awaiting approval to dispatch agents.
        </Text>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value="97.9 %" label="Prototyped composite accuracy (Phase 1 bundle)" tone="success" />
        <Stat value="4.2 %" label="Clean false-alarm rate after Phase 1" tone="success" />
        <Stat value="12/12" label="Upload +6 °C spikes caught by MAD" tone="success" />
        <Stat value="85 %" label="Minimum acceptance bar (stretch 95 %)" tone="info" />
      </Grid>

      <Callout tone="info" title="Accuracy definition used below">
        {accuracyMetric} Bias steps and multi-day drift are Phase 2 — they need stateful detectors the enrichment path does not keep today.
      </Callout>

      <Stack gap={10}>
        <H2>Target</H2>
        <Table
          headers={["Metric", "Acceptance", "Prototyped"]}
          rows={targetRows}
          columnAlign={["left", "left", "left"]}
        />
        <Text size="small" tone="secondary">
          85 % is achievable; 95 % is the better Phase 1 exit bar for the spike / noise / level-shift slice. Do not claim 85 % over all catalog types until Phase 2 bias/drift work lands.
        </Text>
      </Stack>

      <Divider />

      <Stack gap={10}>
        <H2>1. Phase 1 — reach ≥ 85 % (and close never-detected gaps)</H2>
        <Row gap={8} wrap>
          <Pill tone="warning">Must ship together</Pill>
          <Pill tone="info">Owners: backend-engineer + ml-engineer + qa-engineer</Pill>
        </Row>
        <Table
          headers={["ID", "Change", "Why / evidence", "Owner", "Priority"]}
          rows={phase1Rows}
        />
        <H3>Measured effect of the Phase 1 bundle</H3>
        <Table
          headers={["Configuration", "Clean FPR", "Fault detection / labels", "Composite accuracy"]}
          rows={phase1Measured}
          rowTone={["danger", "success", "success"]}
        />
        <Text size="small" tone="secondary">
          Also include P1.9 flatline eps ≤ 1e-4 (40/40 stuck windows) and P1.10 remove ensemble midpoint bias (always-on today) as part of the same PR train — they are small and unblock correct labels.
        </Text>
      </Stack>

      <Stack gap={10}>
        <H2>2. Never-detected faults — close list</H2>
        <Table
          headers={["Gap", "Audit result", "Fix", "Status"]}
          rows={neverDetected}
          rowTone={["danger", "warning", "warning", "success", "warning", "warning", "success", "warning"]}
        />
      </Stack>

      <Stack gap={10}>
        <H2>3. Phase 2 — bias, drift, live context</H2>
        <Table headers={["ID", "Change", "Notes"]} rows={phase2Rows} />
      </Stack>

      <Divider />

      <Stack gap={10}>
        <H2>4. LSTM, vector analysis, and design improvements</H2>
        <Text tone="secondary" size="small">
          These are the structural items behind the detector bugs. Phase 1 can land without a redesign; D1–D7 should be scheduled immediately after so the fix does not rot.
        </Text>
        <Table headers={["ID", "Theme", "Recommendation"]} rows={designRows} />
      </Stack>

      <CollapsibleSection title="Suggested dispatch order (after you approve)" defaultOpen>
        <Stack gap={6}>
          <Text>1. ml-engineer — noise baseline calibration API, BOCPD any-in-window, MAD floors, flatline eps, CUSUM seed/reset.</Text>
          <Text>2. backend-engineer — pipeline last-step + precedence table, label/CHECK fixes, upload MAD + robust gate + validation bounds, ensemble denorm / drop midpoint bias.</Text>
          <Text>3. qa-engineer — accuracy harness promoted into tests with ≥85 % composite gate; upload spike recall; clean-week FPR ceiling.</Text>
          <Text>4. code-reviewer — read-only pass against guardrails.</Text>
          <Text>5. backend-engineer — catalog refresh (P2.6) once behaviour matches.</Text>
        </Stack>
      </CollapsibleSection>

      <Callout tone="warning" title="Not started in the repo">
        Prototypes and results live only under <Code>/tmp/anomaly_audit</Code>. Say which phases to dispatch and whether the acceptance bar should be 85 % or the 95 % stretch for Phase 1.
      </Callout>
    </Stack>
  );
}
