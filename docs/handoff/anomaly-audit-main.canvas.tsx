import {
  BarChart,
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

const mergedWork: string[][] = [
  ["#98, #99, #100, #102, #101", "BACKLOG-14", "Cold-start Pass 1 was discarding about half the training corpus on a bimodal light channel. Fixed, with Pass-1 metrics and structured warnings in metrics_json, knife-edge regression fixtures, a non-finite guard in register_model, 3-seed shadow scoring and a retraining runbook."],
  ["#104", "Web", "Anomaly jump on Monitoring now pre-selects every channel the row shows (alias-aware) and keeps manual toggles."],
  ["#106", "BACKLOG-19", "The pipeline deep-copies the detector model instead of mutating the instance live scoring shares. Adds 6 tests. Opens BACKLOG-33 and 34."],
  ["#105, #101", "Docs", "Architecture docs are tracked in git: 5 design specs, 14 point-in-time reviews with historical banners, Pass-1 spec corrected against the code."],
  ["#103, #107", "Docs", "Test baseline recorded as 3991. Backlog status index added: 34 items, 5 done, 1 partial, 1 won't-fix, 1 adopted, 26 open."],
];

const docMismatches: string[][] = [
  ["Where detection runs", "Parts 1 to 4 read as the product's detection path", "The rule detectors are imported only by the comprehensive pipeline, reached from /api/inference/* (the web app never calls it) and live enrichment. CSV upload runs LSTM + rolling z-score + wall-clock stuck only.", "Import search"],
  ["Upload fault label", "Not described", "Outside 20-30 C, 50-80 %, 300-500 ppm or 0-1200 W/m2 gives spike_fault, otherwise unknown. Those are the normalisation ranges, not sensor limits.", "fault_classification.py"],
  ["2. Out-of-bounds", "fault_type out_of_bounds", "The pipeline emits spike_fault with root cause sensor_out_of_bounds. Nothing produces out_of_bounds.", "Measured 40/40"],
  ["8. Thermodynamic", "fault_type thermodynamic_contradiction; \"physics has no false positives\"", "Never produced. A Tetens mismatch maps to humidity_fault, which the DB CHECK constraint forbids (BACKLOG-20, open). \"Impossibility\" is a trend heuristic: T and RH both rising.", "Code"],
  ["Precedence", "Rule votes outrank physics; spike can be the label", "A physics-confirmed result overrides rule votes. Gate order is setpoint rules, noise, spike, dual bias, correlation, diurnal, Mahalanobis, change-point, so noise outranks spike.", "Code + measured"],
  ["1. Spike", "A velocity limit runs alongside MAD", "VELOCITY_LIMITS is used only in pattern scoring and SPIKE_DELTA_MAX only in training bootstrap. Neither is in the spike gate.", "Usage search"],
  ["4. Drift (CUSUM)", "ARL0 of about 340 samples", "Alarms at the second sample on stationary noise and stays in alarm 34.5 % of the time. The EMA variance starts at zero and the pipeline never resets the accumulator.", "Measured, 20 runs"],
  ["4. Drift (BOCPD)", "Detects level shifts, filters transients", "The pipeline reads only the last sample's flag, where confirmation is impossible. A mid-window two-channel step got 0 of 40 votes.", "Measured"],
  ["Mahalanobis", "Distance from the tenant's clean history", "Fitted on the 20-sample window itself with a trimmed refit.", "Code"],
  ["Ensemble vote", "Thresholds 0.8 / 0.5 / 3.0 / 0.35", "LSTM and statistical members vote at half the listed value. The ensemble gets normalised data but compares it with physical ranges, so the bias flag is always set and the correlation member always votes.", "Measured 266/266"],
  ["Severity", "Four chips from one client function", "getSeverity returns only normal or warning. Severity is server-assigned with six values including urgent and unclassified.", "severity.ts"],
  ["Band breaches", "Persist for 3 samples; driven by your configuration", "It is the mean of the last 3 samples. A default tomato profile applies when nothing is configured. The VPD-to-temperature mapping constant is unused.", "Code + probe"],
  ["Over-heating", "Only when outdoor is below 25 C and solar does not explain it", "Fires unless solar is above 400 W/m2 and outdoor is above 25 C together.", "Measured 51 % of clean windows"],
  ["3. Flatline", "24 identical steps", "Windows are 20 steps, so 20 are checked while the text says 24. Covers temperature and humidity only. It missed 23 of 40 perfectly constant series because it also requires the computed variance to equal exactly 0.0, and rounding leaves about 1e-10.", "Measured"],
  ["Polling dropout", "5 consecutive missing samples", "The detector matches. The message it prints says \"more than 25 % of steps\". The HTTP schema rejects null and accepts NaN.", "Probe"],
  ["LSTM-VAE", "Falls back to a global baseline without a model", "Raises NoActiveModelError and returns 503.", "Code"],
  ["Config value", "LIGHT_RANGE 0-1000 lux", "(0, 1200) in W/m2.", "Config dump"],
];

const injectedRows: string[][] = [
  ["Spike, +6 C single sample", "spike_fault", "accuracy_decline", "0 %", "0 %"],
  ["Out-of-bounds, RH 104 %", "out_of_bounds", "spike_fault", "0 %", "0 %"],
  ["Stuck, 20 identical samples", "stuck_fault", "stuck_fault", "100 %", "64 %"],
  ["Noise, sigma 1 C", "accuracy_decline", "accuracy_decline", "100 %", "100 %"],
  ["Bias step, +3 C", "bias_offset", "accuracy_decline", "0 %", "0 %"],
  ["Level shift on 2 channels", "calibration_drift", "accuracy_decline", "0 %", "0 %"],
  ["Joint implausible reading", "accuracy_decline", "accuracy_decline (noise root cause)", "100 %", "50 %"],
  ["Polling dropout, 6 NaN rows", "polling_dropout", "polling_dropout", "100 %", "100 %"],
  ["Temp 4 C above day band", "temp_band_high", "temp_band_high or suppressed", "50 %", "0 %"],
];

const uploadRows: string[][] = [
  ["Clean week, no injection", "-", "2 alerts, both LSTM"],
  ["Temperature spike +6 C, single sample", "0 of 12", "2 alerts, unchanged"],
  ["Humidity drop -15 %, single sample", "0 of 12", "2 alerts, unchanged"],
  ["Temperature +6 to +20 C for 5, 15 and 30 min", "0 of 8 in all 12 cases", "2 alerts, unchanged"],
  ["Temperature bias +3 C from day 5", "0 of 1", "2 alerts, unchanged"],
  ["Temperature drift 0 to +4 C over 5 days", "0 of 1", "2 alerts, unchanged"],
  ["Temperature noise sigma 1 C for 24 h", "0 of 1", "2 alerts, unchanged"],
  ["Humidity frozen 8 h", "1 of 1", "stuck_fault raised"],
  ["Temperature frozen 8 h", "1 of 1", "stuck_fault raised"],
  ["One reading RH 101 % or CO2 1050 ppm", "-", "The chunk holding it is discarded: the whole file at the default 5,000-row chunk, 500 rows silently at chunk size 500"],
];

export default function AnomalyAudit() {
  return (
    <Stack gap={24}>
      <Stack gap={6}>
        <H1>Anomaly audit of main</H1>
        <Text tone="secondary">
          main at <Code>4556539</Code> (PR #107, 2026-11-10). Docs checked: <Code>docs/ANOMALY_CATALOG.md</Code>, last verified 2026-10-30. Measured 2026-11-12.
        </Text>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value="3997 / 0" label="Tests passed / failed on main (35 skipped, 3 xfailed)" tone="success" />
        <Stat value="99.7 %" label="Clean real windows the comprehensive pipeline flags (332 of 333)" tone="danger" />
        <Stat value="0 of 24" label="Injected single-sample spikes the CSV upload path caught" tone="danger" />
        <Stat value="17" label="Catalog behaviour claims that do not match the code" tone="warning" />
      </Grid>

      <Callout tone="warning" title="How to read the measurements">
        Clean data is the repo's own <Code>native_july_clearsky.csv</Code>: one real week at 5-minute cadence with no known sensor faults. The LSTM figures use the fixture checkpoint <Code>epoch_0072_trained_baseline.pt</Code>, not a production tenant model. The rule-detector figures do not depend on the model. Nothing in the repo was changed.
      </Callout>

      <Stack gap={10}>
        <H2>What landed on main since the catalog refresh</H2>
        <Text tone="secondary" size="small">
          PRs #98 to #107, 2026-11-08 to 2026-11-10: 71 files, +15,581 / -168 lines. No detector logic changed in this window.
        </Text>
        <Table headers={["PRs", "Area", "What changed"]} rows={mergedWork} />
        <Text size="small" tone="secondary">
          <Code>CLAUDE.md</Code> still records a baseline of 3991 passed. main measures 3997: PR #106 added the 6 tests in <Code>tests/api/test_pipeline_model_isolation.py</Code> after that line was written.
        </Text>
      </Stack>

      <Divider />

      <Stack gap={10}>
        <H2>Measured: the comprehensive pipeline on clean real data</H2>
        <Grid columns="3fr 2fr" gap={20}>
          <Stack gap={6}>
            <H3>Share of 333 clean windows where each rule fired</H3>
            <BarChart
              categories={["Spike (MAD)", "Noise", "Over-heating", "Mahalanobis", "Change-point"]}
              series={[{ name: "Clean windows where the rule fired", data: [91.3, 69.4, 51.4, 17.7, 0] }]}
              valueSuffix=" %"
              yMax={100}
              height={220}
              showValues
            />
            <Text size="small" tone="secondary">
              x: rule detector. y: percent of windows. Source: native_july_clearsky.csv, 20-sample windows every 30 min. Over-heating needs the setpoint context only the HTTP route accepts.
            </Text>
          </Stack>
          <Stack gap={8}>
            <H3>Why almost everything is flagged</H3>
            <Text>
              Labels on the clean week: <Code>accuracy_decline</Code> 231, <Code>spike_fault</Code> 91, <Code>stuck_fault</Code> 7, <Code>bias_offset</Code> 3, not flagged 1.
            </Text>
            <Text>
              The LSTM is not the cause: its score peaks at 0.31 against a threshold of 0.8. The flags come from the spike and noise floors, which a normal diurnal ramp exceeds.
            </Text>
            <Text>
              A missing CO2 sensor is imputed as a constant 400 ppm, and the ensemble's stuck check then trips on 332 of 333 windows. Zero light at night trips it on 67.
            </Text>
            <Text>
              On a quiet synthetic window the pipeline does return normal: 0 of 40 flagged.
            </Text>
          </Stack>
        </Grid>
      </Stack>

      <Stack gap={10}>
        <H2>Measured: does an injected fault get the documented label?</H2>
        <Text tone="secondary" size="small">
          Every injected fault was flagged as an anomaly. The table scores the label. Quiet base: 40 synthetic in-range windows. Real base: 42 windows from the clean week, all of which are already flagged before injection.
        </Text>
        <Table
          headers={["Injected fault", "Catalog label", "Label returned (quiet base)", "Correct, quiet base", "Correct, real base"]}
          rows={injectedRows}
          columnAlign={["left", "left", "left", "right", "right"]}
          rowTone={["danger", "danger", "success", "success", "danger", "danger", "warning", "success", "warning"]}
        />
        <Text size="small" tone="secondary">
          Spike amplitude sweep on temperature: +0.2 C is not flagged, +0.5 to +1.0 C is labelled spike_fault 20 of 20, +2 C and above is labelled accuracy_decline 20 of 20. Half of the band-breach trials were returned as normal because the physics validator suppressed them as diurnal variation.
        </Text>
      </Stack>

      <Stack gap={10}>
        <H2>Measured: the CSV upload path</H2>
        <Row gap={8} wrap>
          <Pill tone="info">Statistical gate: |z| of 5.5 or more against a 24 h window, 3 samples in a row</Pill>
          <Pill tone="info">LSTM threshold: 99.9th percentile of the file's own scores</Pill>
        </Row>
        <Table
          headers={["Scenario on the July week (2,016 rows)", "Injected events caught", "Result"]}
          rows={uploadRows}
          rowTone={["success", "danger", "danger", "danger", "danger", "danger", "danger", "success", "success", "warning"]}
        />
        <Text size="small" tone="secondary">
          The 24 h standard deviation of greenhouse temperature in this week is 4.8 C, so the z gate needs a deviation of about 26 C, which then exceeds the 50 C input-validation bound. The percentile threshold yields about 2 alerts per 2,000 rows whatever the file contains.
        </Text>
      </Stack>

      <Divider />

      <Stack gap={10}>
        <H2>Catalog versus code</H2>
        <Text tone="secondary" size="small">
          17 places where the catalog describes behaviour the code does not have. The last column says whether the row was measured or read from the code.
        </Text>
        <Table headers={["Catalog section", "Catalog says", "Code does", "Evidence"]} rows={docMismatches} striped />
      </Stack>

      <CollapsibleSection title="What the catalog gets right" defaultOpen={false}>
        <Stack gap={6}>
          <Text>Every config constant it names (about 86) exists, and the stated defaults match except LIGHT_RANGE.</Text>
          <Text>The 24 fault types in the quick reference equal the migration 019 constraint list, and the UI label map covers them.</Text>
          <Text>Pattern classification: 7 types, explanation strings and tie-break order all match.</Text>
          <Text>Gas (thresholds, 5-sample minimum, 1.5 IQR fence, both endpoints), the 7 network routes with their constants and 19 features, and the 9 synthetic injection types all match.</Text>
          <Text>Polling dropout and out-of-bounds are the only two short-circuits; band breaches rank last and are dropped when their sensor is already flagged; the CO2 enrichment flag behaves as described.</Text>
          <Text>The Tetens humidity-deficit check produced 0 inconsistencies on 333 clean real windows with the fixture's own measured HD column.</Text>
          <Text>The spec verification vectors pass: 49 passed, 1 xfailed in <Code>tests/models/test_framework_spec_vectors.py</Code>.</Text>
        </Stack>
      </CollapsibleSection>
    </Stack>
  );
}
