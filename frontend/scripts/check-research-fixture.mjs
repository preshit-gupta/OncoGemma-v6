import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const fixturePath = path.join(__dirname, "..", "lib", "mock", "research.json");

if (!fs.existsSync(fixturePath)) {
  console.error("Missing fixture file: " + fixturePath);
  process.exit(1);
}

const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
const run1 = fixture.runs.find((r) => r.id === "run_01");

if (!run1) {
  console.error("Missing run_01 in fixture");
  process.exit(1);
}

const ns_m = run1.headline.ns_m;
const ns_g = run1.headline.ns_g;

if (!ns_m || !ns_g) {
  console.error("Missing headline metrics in run_01");
  process.exit(1);
}

// Assert values match contract
function assertEqual(actual, expected, label) {
  if (actual !== expected) {
    console.error(`Assertion failed for ${label}: expected ${expected}, got ${actual}`);
    process.exit(1);
  }
}

assertEqual(ns_m.value, 0.71, "ns_m.value");
assertEqual(ns_m.ci_low, 0.66, "ns_m.ci_low");
assertEqual(ns_m.ci_high, 0.76, "ns_m.ci_high");
assertEqual(ns_m.n, 30, "ns_m.n");
assertEqual(ns_m.status, "final", "ns_m.status");

assertEqual(ns_g.value, 0.58, "ns_g.value");
assertEqual(ns_g.ci_low, 0.49, "ns_g.ci_low");
assertEqual(ns_g.ci_high, 0.67, "ns_g.ci_high");
assertEqual(ns_g.n, 104, "ns_g.n");
assertEqual(ns_g.status, "provisional", "ns_g.status");

// Formatting helper check
function formatMetric(m) {
  if (!m || m.value === null) return "—";
  return `${m.value.toFixed(2)} [${m.ci_low?.toFixed(2)}, ${m.ci_high?.toFixed(2)}] (n=${m.n})`;
}

const nsmFormatted = formatMetric(ns_m);
const nsgFormatted = formatMetric(ns_g);

assertEqual(nsmFormatted, "0.71 [0.66, 0.76] (n=30)", "nsmFormatted");
assertEqual(nsgFormatted, "0.58 [0.49, 0.67] (n=104)", "nsgFormatted");

console.log("check-research-fixture passed! All headline numbers match fixture.");
