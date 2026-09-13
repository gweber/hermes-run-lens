/**
 * Render the run-lens bundle for real, off the dashboard.
 *
 * The bundle is a plain IIFE with no build step; `node --check` proves only that it
 * parses. What breaks a tab is a ReferenceError on a render or click path, so this
 * mounts every tab with stubbed SDK components against fixtures dumped from a real
 * run-lens database (tests/dump_fixtures.py), then clicks through: open a run, switch
 * its panels, stop it, expand and acknowledge a finding.
 *
 *   python tests/dump_fixtures.py --demo /tmp/fixtures.json
 *   RUN_LENS_FIXTURES=/tmp/fixtures.json node hermes-plugin/dashboard/render_check.js
 *
 */
const path = require("path");
const fs = require("fs");

// React and jsdom: $RUN_LENS_NODE_MODULES, a local `npm install` in this repo, or the
// Hermes install's own node_modules — nothing has to be installed on a Hermes machine.
const NM = [
  process.env.RUN_LENS_NODE_MODULES,
  path.join(__dirname, "..", "..", "node_modules"),
  path.join(__dirname, "node_modules"),
  process.env.HERMES_HOME && path.join(process.env.HERMES_HOME, "hermes-agent/node_modules"),
  path.join(process.env.HOME || "", ".hermes/hermes-agent/node_modules"),
].filter(Boolean).find((d) => fs.existsSync(path.join(d, "react")) && fs.existsSync(path.join(d, "jsdom")));
if (!NM) { console.error("No react/react-dom/jsdom found: `npm install react react-dom jsdom` in the repo root"); process.exit(2); }
const React = require(path.join(NM, "react"));
const { renderToStaticMarkup } = require(path.join(NM, "react-dom/server"));

const FIX = process.env.RUN_LENS_FIXTURES;
if (!FIX || !fs.existsSync(FIX)) { console.error("set RUN_LENS_FIXTURES (tests/dump_fixtures.py writes it)"); process.exit(2); }
const fixtures = JSON.parse(fs.readFileSync(FIX, "utf8"));

// ── stub SDK ───────────────────────────────────────────────────────────
const used = new Set();
const passthrough = (name, tag = "div") => function Stub(props) {
  used.add(name);
  const { children, ...rest } = props || {};
  const safe = {};
  for (const k of ["className", "style", "id", "value", "disabled", "type", "placeholder"]) if (rest[k] !== undefined) safe[k] = rest[k];
  for (const k of Object.keys(rest)) if (k.startsWith("on") && typeof rest[k] === "function") safe[k] = rest[k];
  if (name === "Button" || name === "TabsTrigger") safe["data-stub"] = name;
  return React.createElement(tag, safe, children);
};
const components = {
  Card: passthrough("Card"), CardHeader: passthrough("CardHeader"), CardTitle: passthrough("CardTitle"),
  CardContent: passthrough("CardContent"), Badge: passthrough("Badge", "span"), Button: passthrough("Button", "button"),
  Input: passthrough("Input", "input"), Select: passthrough("Select", "select"), SelectOption: passthrough("SelectOption", "option"),
  TabsList: passthrough("TabsList"), TabsTrigger: passthrough("TabsTrigger", "button"),
  Tabs: function Tabs(props) {
    used.add("Tabs");
    if (typeof props.children !== "function") throw new Error("Tabs was passed " + typeof props.children + ", not a render function");
    const [active, setActive] = React.useState(Tabs.active || props.defaultValue);
    return React.createElement("div", null, props.children(active, setActive));
  },
};

const calls = [];
function fetchJSON(url, init) {
  const u = String(url).replace("/api/plugins/run-lens", "");
  const clean = u.split("?")[0];
  calls.push({ url: u, method: (init && init.method) || "GET" });
  if (init && init.method === "POST") return Promise.resolve({ ok: true, sessions: 1, root: "x", report: {} });
  if (clean.startsWith("/run/")) {
    const id = decodeURIComponent(clean.slice(5));
    const d = fixtures["/run"][id] || Object.values(fixtures["/run"])[0];
    return Promise.resolve(d);
  }
  if (fixtures[clean]) return Promise.resolve(fixtures[clean]);
  return Promise.reject(new Error("no fixture for " + clean));
}

const registered = {};
global.window = {
  __HERMES_PLUGIN_SDK__: {
    React, components, fetchJSON,
    hooks: { useState: React.useState, useEffect: React.useEffect, useCallback: React.useCallback, useMemo: React.useMemo, useRef: React.useRef },
    utils: { cn: (...a) => a.filter(Boolean).join(" ") },
  },
  __HERMES_PLUGINS__: { register: (name, c) => { registered[name] = c; } },
  confirm: () => true,
  scrollTo: () => {},
};
global.document = { hidden: false };
new Function(fs.readFileSync(path.join(__dirname, "dist/index.js"), "utf8"))();
if (!registered["run-lens"]) { console.error("FAIL: bundle registered nothing"); process.exit(1); }

let failed = 0;
const TABS = ["overview", "runs", "jobs", "models", "findings"];
for (const tab of TABS) {
  components.Tabs.active = tab;
  try {
    const html = renderToStaticMarkup(React.createElement(registered["run-lens"]));
    if (!html || html.length < 20) throw new Error("rendered empty");
    console.log(`  ok   ${tab.padEnd(10)} first paint`);
  } catch (e) { failed++; console.error(`  FAIL ${tab.padEnd(10)} ${e.message}`); }
}

(async () => {
  const { JSDOM } = require(path.join(NM, "jsdom"));
  const dom = new JSDOM("<!doctype html><div id=root></div>", { pretendToBeVisual: true });
  // Node 21+ has a read-only global navigator; define over it instead of assigning.
  for (const [k, v] of Object.entries({
    document: dom.window.document, navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement,
    Element: dom.window.Element, SVGElement: dom.window.SVGElement, Node: dom.window.Node,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) Object.defineProperty(global, k, { value: v, configurable: true, writable: true });
  const { createRoot } = require(path.join(NM, "react-dom/client"));
  const { act } = React;
  const errors = [];
  const realError = console.error;
  console.error = (...a) => { const t = String(a[0] || ""); if (!/not wrapped in act|unrecognized|Invalid DOM property|non-boolean|unique "key"/i.test(t)) errors.push(t.slice(0, 300)); };
  const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 30)); });
  const click = (el) => act(async () => { el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })); });
  const check = (ok, label) => { if (ok) realError.call(console, `  ok   ${label}`); else { failed++; realError.call(console, `  FAIL ${label}`); } };

  for (const tab of TABS) {
    components.Tabs.active = tab;
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    try {
      await act(async () => { root.render(React.createElement(registered["run-lens"])); });
      await settle();
      const text = el.textContent || "";
      check(text.length > 50 && !/Loading…$/.test(text.trim()), `${tab} mounted with data (${text.length} chars)`);
      if (tab === "overview") check(el.querySelectorAll("svg rect").length > 0, "overview charts drew bars");
      if (tab === "jobs") check(el.querySelectorAll("tbody tr").length > 0, "jobs table has rows");
      if (tab === "runs") {
        // Open the run with the most calls, so the call chart has something to draw.
        const detailIds = Object.keys(fixtures["/run"]);
        const heavy = detailIds.find((id) => (fixtures["/run"][id].calls || []).length > 0);
        const rowsEls = [...el.querySelectorAll("tbody tr.cursor-pointer")];
        const row = rowsEls.find((r) => heavy && r.textContent.includes(heavy)) || rowsEls[0];
        check(!!row, "runs table has clickable rows");
        if (row) {
          await click(row);
          await settle();
          check(/Call by call/.test(el.textContent), "clicking a run opens its detail");
          check(el.querySelectorAll("svg rect").length > 0, "run detail chart drew calls");
          const toolsTab = [...el.querySelectorAll("button")].find((b) => /^Tools/.test(b.textContent));
          check(!!toolsTab, "run detail has a Tools panel trigger");
          if (toolsTab) { await click(toolsTab); await settle(); check(/arguments/.test(el.textContent), "Tools panel renders"); }
          const back = [...el.querySelectorAll("button")].find((b) => /back/.test(b.textContent));
          if (back) { await click(back); await settle(); check(/running only/.test(el.textContent), "back returns to the runs list"); }
        }
      }
      if (tab === "findings") {
        const head = el.querySelector(".cursor-pointer");
        check(!!head, "findings list renders entries");
        if (head) {
          await click(head);
          await settle();
          const ack = [...el.querySelectorAll("button")].find((b) => /Acknowledge|Reopen/.test(b.textContent));
          check(!!ack, "expanded finding offers state buttons");
          if (ack) { await click(ack); await settle(); check(calls.some((c) => c.method === "POST" && /\/findings\/\d+\/state/.test(c.url)), "acknowledge POSTs"); }
        }
      }
    } catch (e) { failed++; realError.call(console, `  FAIL ${tab} mount: ${e.message}`); if (process.env.VERBOSE) realError.call(console, e.stack); }
    await act(async () => root.unmount());
  }
  if (errors.length) { failed += errors.length; errors.slice(0, 5).forEach((e) => realError.call(console, "  console.error: " + e)); }
  realError.call(console, `\ncomponents used: ${[...used].sort().join(", ")}`);
  realError.call(console, failed ? `\n${failed} FAILED` : "\nall render checks passed");
  process.exit(failed ? 1 : 0);
})();
