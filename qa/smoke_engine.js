// Quick logic check of the engine in Node (no browser). The Playwright suite will test the real UI.
// Run: node qa/smoke_engine.js
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { createSurvey } = require("../survey/engine.js");
const config = JSON.parse(fs.readFileSync(path.join(__dirname, "../config/questionnaire.json"), "utf8"));

const MATRIX = { price: "3", variety: "4", regional: "5", quality: "4", adfree: "2" };

// Feeds scripted answers and records which questions were shown, their text and their options.
function run(answers) {
  const s = createSurvey(config, { seed: 1 });
  const visited = [], texts = {}, options = {};
  while (s.state.status === "in_progress") {
    const view = s.present();
    visited.push(view.id);
    texts[view.id] = view.text;
    options[view.id] = view.options.map((o) => o.id);
    assert.ok(view.id in answers, `no scripted answer for ${view.id}`);
    const r = s.submit(answers[view.id]);
    assert.ok(!r.error, `${view.id}: ${r.error}`);
  }
  return { s, visited, texts, options };
}

let passed = 0;
function test(name, fn) { fn(); passed++; console.log("PASS", name); }

test("under 18 is terminated at S1", () => {
  const { s, visited } = run({ S1: 17 });
  assert.deepStrictEqual(visited, ["S1"]);
  assert.strictEqual(s.state.status, "screened_out");
  assert.strictEqual(s.state.terminateReason, "age_out_of_range");
});
test("over 45 is terminated at S1", () => {
  assert.strictEqual(run({ S1: 46 }).s.state.terminateReason, "age_out_of_range");
});
test("living outside India is terminated at S2", () => {
  const { s, visited } = run({ S1: 25, S2: "outside_india" });
  assert.deepStrictEqual(visited, ["S1", "S2"]);
  assert.strictEqual(s.state.terminateReason, "outside_india");
});
test("non-streamer skips platform questions and gets Q10", () => {
  const { s, visited } = run({ S1: 25, S2: "metro", Q1: "no", Q10: "cost" });
  assert.deepStrictEqual(visited, ["S1", "S2", "Q1", "Q10"]);
  assert.strictEqual(s.state.status, "complete");
});
test("Netflix + Other user sees Q3 and Q6; Q4 options and Q5 text are piped", () => {
  const { s, visited, texts, options } = run({
    S1: 22, S2: "tier2", Q1: "yes", Q2: ["netflix", "other"], Q3: "Aha", Q4: "netflix",
    Q5: "2to5", Q6: "own", Q7: ["friends"], Q8: MATRIX, Q9: "100to199",
  });
  assert.deepStrictEqual(visited, ["S1", "S2", "Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9"]);
  assert.deepStrictEqual([...options.Q4].sort(), ["netflix", "other"]);
  assert.ok(texts.Q5.includes("Netflix"), texts.Q5);
  assert.strictEqual(s.state.status, "complete");
});
test("Prime-only user skips Q3 and Q6", () => {
  const { visited } = run({
    S1: 30, S2: "metro", Q1: "yes", Q2: ["prime"], Q4: "prime",
    Q5: "lt2", Q7: ["none"], Q8: MATRIX, Q9: "lt100",
  });
  assert.ok(!visited.includes("Q3") && !visited.includes("Q6"));
});
test("anchored options stay last when randomised", () => {
  const { options } = run({ S1: 30, S2: "metro", Q1: "yes", Q2: ["prime"], Q4: "prime", Q5: "lt2", Q7: ["none"], Q8: MATRIX, Q9: "lt100" });
  assert.strictEqual(options.Q2[options.Q2.length - 1], "other");
  assert.strictEqual(options.Q7[options.Q7.length - 1], "none");
});
test("validation blocks missing, non-integer and partial answers", () => {
  const s = createSurvey(config, { seed: 1 });
  assert.ok(s.submit(undefined).error);
  assert.ok(s.submit(17.5).error);
  assert.ok(s.submit(200).error);
  assert.strictEqual(s.state.idx, 0); // still on S1
});
test("a full quota ends the survey as quota_full", () => {
  const s = createSurvey(config, { seed: 1, quotaFull: (g) => g === "age_band" });
  assert.strictEqual(s.submit(22).status, "quota_full");
});
console.log(`\n${passed} tests passed`);
