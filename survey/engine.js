/* SurveyOps engine: runs a survey entirely from config/questionnaire.json.
   Part 1 is pure logic (works in the browser and in Node, so the QA suite can import it).
   Part 2 is the browser UI. */
(function (root) {
  "use strict";

  /* ---------- Part 1: logic ---------- */
  const isEmpty = (v) =>
    v === undefined || v === null || v === "" ||
    (Array.isArray(v) && v.length === 0) ||
    (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0);

  // Evaluates the structured conditions used by show_if / terminate_if.
  function evalCond(c, a) {
    if (c.all) return c.all.every((x) => evalCond(x, a));
    if (c.any) return c.any.some((x) => evalCond(x, a));
    if (c.not) return !evalCond(c.not, a);
    const v = a[c.q];
    switch (c.op) {
      case "answered": return !isEmpty(v);
      case "eq": return v === c.value;
      case "ne": return !isEmpty(v) && v !== c.value;
      case "in": return Array.isArray(c.value) && c.value.includes(v);
      case "contains": return Array.isArray(v) && v.includes(c.value);
      case "gt": return typeof v === "number" && v > c.value;
      case "gte": return typeof v === "number" && v >= c.value;
      case "lt": return typeof v === "number" && v < c.value;
      case "lte": return typeof v === "number" && v <= c.value;
      default: throw new Error("Unknown operator: " + c.op);
    }
  }

  // Seeded random numbers, so a run with ?seed=7 always shows the same option order.
  function mulberry32(seed) {
    return function () {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // Shuffles everything except options marked "anchor" (Other, None of these), which stay last.
  function shuffleKeepingAnchors(list, rng) {
    const free = list.filter((o) => !o.anchor);
    const anchored = list.filter((o) => o.anchor);
    for (let i = free.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      [free[i], free[j]] = [free[j], free[i]];
    }
    return free.concat(anchored);
  }

  function validateAnswer(q, v) {
    if (isEmpty(v)) return q.required ? "Please answer this question to continue." : null;
    switch (q.type) {
      case "numeric":
        if (typeof v !== "number" || !Number.isInteger(v)) return "Enter a whole number.";
        if (v < q.min || v > q.max) return `Enter a number between ${q.min} and ${q.max}.`;
        return null;
      case "text":
        return q.max_length && v.length > q.max_length ? `Use ${q.max_length} characters or fewer.` : null;
      case "multi_select":
        return q.min_select && v.length < q.min_select ? `Select at least ${q.min_select}.` : null;
      case "matrix":
        return q.rows.some((r) => !v[r.id]) ? "Rate every row to continue." : null;
      default:
        return null;
    }
  }

  function createSurvey(config, opts) {
    opts = opts || {};
    const qs = config.questions;
    const byId = Object.fromEntries(qs.map((q) => [q.id, q]));
    const rng = mulberry32(opts.seed != null ? opts.seed : Date.now());
    const s = {
      respondentId: "R" + Math.random().toString(36).slice(2, 10),
      answers: {}, idx: -1, status: "in_progress", terminateReason: null,
      startedAt: Date.now(), endedAt: null, shownAt: null, timings: {}, order: {},
    };

    const isVisible = (q) => !q.show_if || evalCond(q.show_if, s.answers);
    function nextFrom(i) {
      for (let k = i + 1; k < qs.length; k++) if (isVisible(qs[k])) return k;
      return -1;
    }

    function labelOf(qid, val) {
      const q = byId[qid];
      const src = q && q.options_from ? byId[q.options_from] : q;
      const o = src && (src.options || []).find((x) => x.id === val);
      return o ? o.label : String(val);
    }
    // Replaces {{Qx}} in question text with the label the respondent chose.
    function pipe(q) {
      return q.text.replace(/\{\{(\w+)\}\}/g, (_, id) => {
        const v = s.answers[id];
        if (v === undefined) return "";
        return Array.isArray(v) ? v.map((x) => labelOf(id, x)).join(", ") : labelOf(id, v);
      });
    }

    function progress() {
      const answered = Object.keys(s.timings).length;
      let remaining = 0;
      for (let k = s.idx + 1; k < qs.length; k++) if (isVisible(qs[k])) remaining++;
      return answered / (answered + 1 + remaining);
    }

    // The current question as the UI should show it (piped text, final option order).
    function present() {
      const q = qs[s.idx];
      if (!q || s.status !== "in_progress") return null;
      if (!s.order[q.id]) {
        let options = q.options || [];
        if (q.options_from) {
          const picked = s.answers[q.options_from] || [];
          options = byId[q.options_from].options.filter((o) => picked.includes(o.id));
        }
        if (q.randomize) options = shuffleKeepingAnchors(options, rng);
        const rows = q.randomize_rows ? shuffleKeepingAnchors(q.rows, rng) : q.rows || [];
        s.order[q.id] = { options, rows };
        s.shownAt = Date.now();
      }
      return {
        id: q.id, type: q.type, text: pipe(q), required: q.required,
        options: s.order[q.id].options, rows: s.order[q.id].rows, scale: q.scale,
        min: q.min, max: q.max, max_length: q.max_length,
        progress: progress(), isLast: nextFrom(s.idx) === -1,
      };
    }

    function end(status, reason) {
      s.status = status;
      s.terminateReason = reason;
      s.endedAt = Date.now();
      return { status, reason };
    }

    // Validates, stores the answer, then applies termination, quota and routing rules.
    function submit(raw) {
      if (s.status !== "in_progress") return { status: s.status };
      const q = qs[s.idx];
      const v = q.type === "text" && typeof raw === "string" ? raw.trim() : raw;
      const error = validateAnswer(q, v);
      if (error) return { status: s.status, error };
      if (!isEmpty(v)) s.answers[q.id] = v;
      s.timings[q.id] = Date.now() - s.shownAt;
      if (q.terminate_if && evalCond(q.terminate_if, s.answers)) return end("screened_out", q.terminate_reason);
      if (q.quota_group && opts.quotaFull && opts.quotaFull(q.quota_group, s.answers)) return end("quota_full", "quota_full");
      const next = nextFrom(s.idx);
      if (next === -1) return end("complete", null);
      s.idx = next;
      return { status: s.status };
    }

    function payload() {
      return {
        respondent_id: s.respondentId, survey_id: config.survey_id, version: config.version,
        status: s.status, terminate_reason: s.terminateReason,
        started_at: new Date(s.startedAt).toISOString(),
        ended_at: s.endedAt ? new Date(s.endedAt).toISOString() : null,
        duration_sec: s.endedAt ? Math.round((s.endedAt - s.startedAt) / 1000) : null,
        answers: s.answers, timings_ms: s.timings,
      };
    }

    s.idx = nextFrom(-1);
    return { state: s, present, submit, payload };
  }

  root.SurveyEngine = { createSurvey, evalCond, validateAnswer, mulberry32 };
  if (typeof module !== "undefined" && module.exports) module.exports = root.SurveyEngine;

  /* ---------- Part 2: browser UI ---------- */
  if (typeof document === "undefined") return;

  const MESSAGES = {
    age_out_of_range: "This survey is for people aged 18 to 45, so we can't continue. Thanks for your time.",
    outside_india: "This survey is only for people living in India, so we can't continue. Thanks for your time.",
    quota_full: "We've already collected all the responses we need from your group. Thanks for your time.",
  };

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    kids.flat().forEach((c) => el.append(c));
    return el;
  }

  function matrix(v) {
    const head = h("tr", {}, h("th", {}, ""), v.scale.map((c) => h("th", { scope: "col" }, c.label)));
    const body = v.rows.map((r) =>
      h("tr", {}, h("th", { scope: "row" }, r.label),
        v.scale.map((c) => h("td", {}, h("input", { type: "radio", name: `${v.id}.${r.id}`, value: c.id, "aria-label": `${r.label}: ${c.label}` })))));
    return h("div", { class: "matrix" }, h("table", {}, h("thead", {}, head), h("tbody", {}, body)));
  }

  function field(v) {
    if (v.type === "numeric") return h("input", { type: "number", class: "text-input", min: v.min, max: v.max, inputmode: "numeric" });
    if (v.type === "text") return h("input", { type: "text", class: "text-input", maxlength: v.max_length });
    if (v.type === "matrix") return matrix(v);
    const kind = v.type === "single_select" ? "radio" : "checkbox";
    return h("div", { class: "options" }, v.options.map((o) =>
      h("label", { class: "option" },
        h("input", { type: kind, name: v.id, value: o.id, "data-exclusive": o.exclusive ? "1" : "" }),
        h("span", {}, o.label))));
  }

  function collect(v, form) {
    if (v.type === "numeric") { const x = form.querySelector("input").value; return x === "" ? undefined : Number(x); }
    if (v.type === "text") return form.querySelector("input").value;
    if (v.type === "single_select") { const c = form.querySelector("input:checked"); return c ? c.value : undefined; }
    if (v.type === "multi_select") return [...form.querySelectorAll("input:checked")].map((i) => i.value);
    const out = {};
    v.rows.forEach((r) => {
      const c = form.querySelector(`input[name="${v.id}.${r.id}"]:checked`);
      if (c) out[r.id] = c.value;
    });
    return out;
  }

  function render(survey) {
    const stage = document.getElementById("stage");
    const view = survey.present();
    if (!view) return finish(survey, stage);
    document.getElementById("bar-fill").style.width = Math.round(view.progress * 100) + "%";

    const error = h("p", { class: "error", role: "alert", "data-testid": "error" });
    const form = h("form", { class: "card", "data-qid": view.id, novalidate: "" },
      h("h1", { class: "question" }, view.text),
      field(view), error,
      h("button", { type: "submit", class: "next", "data-testid": "next" }, view.isLast ? "Finish" : "Continue"));

    // "None of these" style options are exclusive: picking one clears the rest, and vice versa.
    form.addEventListener("change", (e) => {
      const t = e.target;
      if (t.type !== "checkbox" || !t.checked) return;
      form.querySelectorAll("input[type=checkbox]").forEach((o) => {
        if (o !== t && (t.dataset.exclusive || o.dataset.exclusive)) o.checked = false;
      });
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const result = survey.submit(collect(view, form));
      if (result.error) { error.textContent = result.error; return; }
      render(survey);
    });

    stage.replaceChildren(form);
    const first = form.querySelector("input");
    if (first && (view.type === "numeric" || view.type === "text")) first.focus();
  }

  // Sends one response to the server. Resolves true only if it was stored.
  async function save(data) {
    try {
      const res = await fetch("/api/responses", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
      });
      return res.ok;
    } catch (err) {
      return false;
    }
  }

  let finished = false;

  function finish(survey, stage) {
    finished = true;
    const data = survey.payload();
    document.getElementById("bar-fill").style.width = "100%";
    const done = data.status === "complete";
    const saveNote = h("p", { class: "note", "data-testid": "save-status" }, "Saving your responses...");
    stage.replaceChildren(h("section", { class: "card", "data-testid": "end", "data-status": data.status },
      h("h1", { class: "question" }, done ? "Thank you for taking part." : "Survey ended"),
      done ? "" : h("p", { class: "note" }, MESSAGES[data.terminate_reason] || "Thanks for your time."),
      saveNote,
      h("details", {}, h("summary", {}, "Response data"), h("pre", { "data-testid": "payload" }, JSON.stringify(data, null, 2)))));
    save(data).then((ok) => {
      saveNote.textContent = ok ? (done ? "Your responses have been recorded." : "") : "We couldn't record your responses. Please try again later.";
      if (!ok) saveNote.classList.add("error");
      window.dispatchEvent(new CustomEvent("surveyops:finished", { detail: Object.assign({ saved: ok }, data) }));
    });
  }

  // Quota check used by the engine: is the respondent's cell already full on the server?
  function makeQuotaFull(status) {
    return (group, answers) => {
      const g = status.groups[group];
      if (!g) return false;
      const v = answers[g.source];
      const cell = g.cells.find((c) => (c.option !== undefined ? c.option === v : typeof v === "number" && v >= c.min && v <= c.max));
      return Boolean(cell && cell.full);
    };
  }

  // Leaving mid-survey is recorded as "abandoned", which feeds the dropout rate.
  window.addEventListener("pagehide", () => {
    if (finished || !window.survey) return;
    const data = window.survey.payload();
    if (!Object.keys(data.answers).length) return;
    data.status = "abandoned";
    navigator.sendBeacon("/api/responses", new Blob([JSON.stringify(data)], { type: "application/json" }));
  });

  async function boot() {
    const stage = document.getElementById("stage");
    try {
      const config = await (await fetch("../config/questionnaire.json")).json();
      let quotaFull;
      try {
        const res = await fetch("/api/quota-status");
        if (res.ok) quotaFull = makeQuotaFull(await res.json());
      } catch (err) { /* no server running: quotas are not enforced */ }
      const params = new URLSearchParams(location.search);
      const survey = createSurvey(config, { seed: params.has("seed") ? Number(params.get("seed")) : undefined, quotaFull });
      window.survey = survey; // exposed for debugging and the QA suite
      document.getElementById("title").textContent = config.title;
      render(survey);
    } catch (err) {
      stage.replaceChildren(h("p", { class: "error" },
        "Couldn't load the questionnaire. Start the server (python server/app.py) and open /survey/."));
    }
  }
  boot();
})(typeof window !== "undefined" ? window : globalThis);