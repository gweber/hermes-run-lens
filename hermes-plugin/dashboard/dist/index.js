/**
 * run-lens — Hermes dashboard plugin.
 *
 * Plain IIFE, no build step. React and every UI primitive come from
 * window.__HERMES_PLUGIN_SDK__; charts are hand-drawn SVG (nothing is bundled).
 * Colours use the host's CSS variables where one fits, and a fixed categorical
 * palette chosen to stay readable on both the light and the dark theme.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const { React } = SDK;
  const { useState, useEffect, useCallback, useMemo } = SDK.hooks;
  const C = SDK.components;
  const h = React.createElement;

  const API = "/api/plugins/run-lens";
  const get = (path) => SDK.fetchJSON(API + path);
  const post = (path, body) => SDK.fetchJSON(API + path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  });

  // ── formatting ─────────────────────────────────────────────────────
  const num = (v) => {
    if (v === null || v === undefined) return "–";
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (a >= 1e4) return Math.round(v / 1e3) + "k";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "k";
    return Number.isInteger(v) ? String(v) : v.toFixed(1);
  };
  const dur = (s) => {
    if (s === null || s === undefined) return "–";
    if (s < 1) return Math.round(s * 1000) + "ms";
    if (s < 60) return s.toFixed(1) + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    if (s < 86400) return (s / 3600).toFixed(1) + "h";
    return (s / 86400).toFixed(1) + "d";
  };
  const clock = (ts) => {
    if (!ts) return "–";
    const d = new Date(ts * 1000);
    const recent = Date.now() / 1000 - ts < 20 * 3600;
    const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: recent ? "2-digit" : undefined });
    return recent ? hm : d.toLocaleDateString([], { day: "2-digit", month: "2-digit" }) + " " + hm;
  };
  const ago = (ts) => {
    if (!ts) return "never";
    const s = Date.now() / 1000 - ts;
    return s < 1 ? "just now" : dur(s) + " ago";  // clocks of two processes can disagree by a moment
  };

  const PALETTE = ["#4f7cff", "#1fa37a", "#e0902c", "#c94f7c", "#8a63d2", "#2aa3b8", "#b8a02a", "#7a8699"];
  const SOURCE_COLOR = { cron: "#e0902c", kanban: "#4f7cff", telegram: "#1fa37a", cli: "#8a63d2", tui: "#c94f7c",
                         api_server: "#2aa3b8", subagent: "#b8a02a", external: "#7a8699" };
  const colorFor = (key, i) => SOURCE_COLOR[key] || PALETTE[i % PALETTE.length];
  const SEV = {
    critical: { label: "critical", color: "#d64545", weight: 3 },
    high: { label: "high", color: "#e06a3a", weight: 2 },
    warn: { label: "warn", color: "#d99a1e", weight: 1 },
    info: { label: "info", color: "#2aa3b8", weight: 0 },
  };
  const STATUS_COLOR = { running: "#1fa37a", capped: "#d64545", open: "#d99a1e", done: "var(--color-muted-foreground)" };

  // ── small building blocks ──────────────────────────────────────────
  function useAsync(fn, deps) {
    const [state, setState] = useState({ loading: true, error: null, data: null });
    const run = useCallback(() => {
      let alive = true;
      setState((s) => ({ ...s, loading: true }));
      fn().then(
        (data) => alive && setState({ loading: false, error: null, data }),
        (err) => alive && setState({ loading: false, error: String((err && err.message) || err), data: null }),
      );
      return () => { alive = false; };
    }, deps); // eslint-disable-line react-hooks/exhaustive-deps
    useEffect(run, [run]);
    return [state, run];
  }

  function useInterval(fn, ms) {
    useEffect(() => {
      if (!ms) return undefined;
      const id = setInterval(() => { if (!document.hidden) fn(); }, ms);
      return () => clearInterval(id);
    }, [fn, ms]);
  }

  const Muted = (props) => h("span", { className: "text-muted-foreground " + (props.className || "") }, props.children);
  const Mono = (props) => h("span", { className: "font-mono text-xs " + (props.className || ""), title: props.title }, props.children);

  function Note({ message, tone }) {
    if (!message) return null;
    const cls = tone === "error" ? "border-destructive text-destructive" : "text-muted-foreground";
    return h("div", { className: "mb-4 rounded-md border p-3 text-sm whitespace-pre-wrap " + cls }, message);
  }

  function Loading() { return h("p", { className: "text-sm text-muted-foreground" }, "Loading…"); }

  function SevBadge({ sev }) {
    if (!sev) return null;
    const s = SEV[sev] || SEV.info;
    return h("span", {
      className: "inline-block rounded px-1.5 py-0.5 text-xs font-medium",
      style: { color: s.color, border: "1px solid " + s.color, whiteSpace: "nowrap" },
    }, s.label);
  }

  function StatusDot({ status }) {
    return h("span", { className: "inline-flex items-center gap-1 text-xs", style: { whiteSpace: "nowrap" } },
      h("span", { style: { width: 8, height: 8, borderRadius: 4, display: "inline-block",
                           background: STATUS_COLOR[status] || "var(--color-muted-foreground)" } }),
      status);
  }

  function Stat({ label, value, sub, tone }) {
    return h(C.Card, null,
      h(C.CardContent, { className: "p-4" },
        h("div", { className: "text-2xl font-semibold", style: tone ? { color: tone } : null }, value),
        h("div", { className: "text-xs text-muted-foreground" }, label),
        sub ? h("div", { className: "mt-1 text-xs text-muted-foreground" }, sub) : null));
  }

  function Section({ title, right, children }) {
    return h(C.Card, { className: "mb-4" },
      h(C.CardHeader, null,
        h("div", { className: "flex items-center justify-between gap-2" },
          h(C.CardTitle, null, title), right || null)),
      h(C.CardContent, null, children));
  }

  function Table({ columns, rows, onRow, empty }) {
    if (!rows || rows.length === 0) return h(Muted, { className: "text-sm" }, empty || "Nothing here.");
    return h("div", { style: { overflowX: "auto" } },
      h("table", { className: "w-full text-sm", style: { borderCollapse: "collapse" } },
        h("thead", null, h("tr", { className: "text-left text-xs text-muted-foreground" },
          columns.map((c) => h("th", { key: c.key, className: "px-2 py-1 font-medium",
                                        style: { textAlign: c.right ? "right" : "left", whiteSpace: "nowrap" } }, c.label)))),
        h("tbody", null, rows.map((r, i) => h("tr", {
          key: r.__key || i, className: "border-t" + (onRow ? " cursor-pointer" : ""),
          style: { borderColor: "var(--color-border)" },
          onClick: onRow ? () => onRow(r) : undefined,
        }, columns.map((c) => h("td", { key: c.key, className: "px-2 py-1 align-top",
                                        style: { textAlign: c.right ? "right" : "left",
                                                 whiteSpace: c.wrap ? "normal" : "nowrap",
                                                 maxWidth: c.max || undefined, overflow: "hidden",
                                                 textOverflow: "ellipsis" } },
          c.render ? c.render(r) : r[c.key])))))));
  }

  function SinceSelect({ value, onChange, options }) {
    const opts = options || [["1h", "1 hour"], ["6h", "6 hours"], ["24h", "24 hours"], ["3d", "3 days"], ["7d", "7 days"], ["14d", "14 days"]];
    return h(C.Select, { value, onChange: (e) => onChange(e && e.target ? e.target.value : e) },
      opts.map(([v, l]) => h(C.SelectOption, { key: v, value: v }, l)));
  }

  // ── charts ─────────────────────────────────────────────────────────
  function StackedBars({ timeline, metric, height }) {
    const H = height || 140;
    const W = 900;
    const series = Object.entries((timeline && timeline.series) || {})
      .sort((a, b) => sum(b[1][metric]) - sum(a[1][metric]));
    const buckets = (timeline && timeline.buckets) || [];
    if (!buckets.length || !series.length) return h(Muted, { className: "text-sm" }, "No calls in this window.");
    const totals = buckets.map((_, i) => series.reduce((acc, [, s]) => acc + (s[metric][i] || 0), 0));
    const top = Math.max(1, ...totals);
    const bw = W / buckets.length;
    const bars = [];
    buckets.forEach((b, i) => {
      let y = H;
      series.forEach(([key, s], si) => {
        const v = s[metric][i] || 0;
        if (!v) return;
        const hh = (v / top) * (H - 14);
        y -= hh;
        bars.push(h("rect", { key: key + i, x: i * bw + 0.5, y, width: Math.max(1, bw - 1), height: hh,
                              fill: colorFor(key, si) },
          h("title", null, `${clock(b)} · ${key}: ${num(v)}`)));
      });
    });
    const tickEvery = Math.max(1, Math.ceil(buckets.length / 8));
    return h("div", null,
      h("svg", { viewBox: `0 0 ${W} ${H + 16}`, width: "100%", height: H + 16, preserveAspectRatio: "none",
                 className: "text-muted-foreground" },
        h("text", { x: 2, y: 10, fontSize: 10, fill: "currentColor" }, num(top)),
        bars,
        buckets.map((b, i) => (i % tickEvery === 0 ? h("text", { key: "t" + i, x: i * bw + 2, y: H + 12, fontSize: 10,
                                                                 fill: "currentColor" }, clock(b)) : null))),
      h("div", { className: "mt-2 flex flex-wrap gap-3 text-xs" },
        series.map(([key, s], si) => h("span", { key, className: "inline-flex items-center gap-1" },
          h("span", { style: { width: 10, height: 10, background: colorFor(key, si), display: "inline-block", borderRadius: 2 } }),
          `${key} ${num(sum(s[metric]))}`))));
  }

  const sum = (arr) => (arr || []).reduce((a, b) => a + (b || 0), 0);

  function CallChart({ calls, tools }) {
    const W = 900, H = 170, PAD = 18;
    if (!calls || calls.length === 0) return h(Muted, { className: "text-sm" }, "No per-call records for this run.");
    const tin = calls.map((c) => c.input_tokens || 0);
    const tout = calls.map((c) => c.output_tokens || 0);
    const top = Math.max(1, ...tin);
    const topOut = Math.max(1, ...tout);
    const bw = W / calls.length;
    const t0 = calls[0].started_at || calls[0].ended_at;
    const t1 = calls[calls.length - 1].ended_at || t0 + 1;
    const span = Math.max(1, t1 - t0);
    const outPath = tout.map((v, i) => `${i ? "L" : "M"}${(i + 0.5) * bw},${PAD + (H - PAD * 2) * (1 - v / topOut)}`).join(" ");
    const strip = (tools || []).filter((t) => t.ended_at || t.started_at);
    return h("div", null,
      h("svg", { viewBox: `0 0 ${W} ${H + 26}`, width: "100%", height: H + 26, preserveAspectRatio: "none",
                 className: "text-muted-foreground" },
        calls.map((c, i) => {
          const hh = ((c.input_tokens || 0) / top) * (H - PAD * 2);
          const err = c.status === "error";
          return h("rect", { key: i, x: i * bw + 0.25, y: H - PAD - hh, width: Math.max(0.8, bw - 0.5), height: hh,
                             fill: err ? "#d64545" : "#4f7cff", opacity: 0.55 },
            h("title", null, `#${c.seq || i + 1} ${clock(c.ended_at)} · in ${num(c.input_tokens)} · out ${num(c.output_tokens)} · ${dur(c.latency_s)}${c.served_model ? " · " + c.served_model : ""}`));
        }),
        h("path", { d: outPath, fill: "none", stroke: "#e0902c", strokeWidth: 1.5 }),
        h("text", { x: 2, y: 11, fontSize: 10, fill: "currentColor" }, `prompt ≤ ${num(top)}`),
        h("text", { x: W - 2, y: 11, fontSize: 10, fill: "#e0902c", textAnchor: "end" }, `completion ≤ ${num(topOut)}`),
        strip.map((t, i) => {
          const x = (((t.ended_at || t.started_at) - t0) / span) * W;
          const col = t.status === "error" ? "#d64545" : t.status === "blocked" ? "#e0902c" : "currentColor";
          return h("rect", { key: "s" + i, x: Math.min(W - 1, Math.max(0, x)), y: H + 4, width: 1.2, height: 12, fill: col,
                             opacity: t.status === "ok" ? 0.35 : 0.95 },
            h("title", null, `${t.name} · ${t.status} · ${t.args_preview || ""}`));
        })),
      h("div", { className: "mt-1 flex flex-wrap gap-3 text-xs text-muted-foreground" },
        h("span", null, "bars: prompt tokens per call (red = failed call)"),
        h("span", null, "orange line: completion tokens"),
        h("span", null, "strip: tool calls over time (red = failed)")));
  }

  function RangeBar({ b, value }) {
    // p50–p99 of normal runs as a band, this group's largest run as a tick.
    if (!b || b.calls.n < 5) return h(Muted, null, "–");
    // Scaled per row: one 2,000-call outlier must not flatten every other group's band.
    const W = 140, H = 12;
    const top = Math.max(value || 0, b.calls.p99 * 1.25, 1);
    const scale = (v) => Math.min(W, (v / top) * W);
    return h("svg", { width: W, height: H, style: { verticalAlign: "middle" } },
      h("rect", { x: 0, y: 5, width: W, height: 2, fill: "var(--color-border)" }),
      h("rect", { x: scale(b.calls.p50), y: 2, width: Math.max(2, scale(b.calls.p99) - scale(b.calls.p50)), height: 8,
                  fill: "#1fa37a", opacity: 0.6 }),
      h("rect", { x: Math.min(W - 3, Math.max(0, scale(value) - 1)), y: 0, width: 3, height: H,
                  fill: value > 3 * b.calls.p99 + 10 ? "#d64545" : "currentColor" }));
  }

  // ── Overview ───────────────────────────────────────────────────────
  function Overview({ openRun, go }) {
    const [since, setSince] = useState("24h");
    const [{ loading, error, data }, reload] = useAsync(() => get(`/overview?since=${since}`), [since]);
    useInterval(reload, 30000);
    if (loading && !data) return h(Loading);
    if (error) return h(Note, { tone: "error", message: error });
    const o = data;
    const sev = o.open_findings || {};
    const liveAge = o.last_hook ? Date.now() / 1000 - o.last_hook : null;
    return h("div", null,
      h("div", { className: "mb-4 flex flex-wrap items-center gap-2" },
        h(SinceSelect, { value: since, onChange: setSince }),
        h(C.Button, { variant: "outline", size: "sm", onClick: () => post("/refresh").then(reload) }, "Refresh now"),
        h(Muted, { className: "text-xs" },
          `live capture: ${o.last_hook ? "last call " + ago(o.last_hook) : "no hook rows yet — enable the plugin and restart the gateway"}`)),
      h("div", { className: "grid gap-3 mb-4", style: { gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))" } },
        h(Stat, { label: "runs", value: num(o.runs), sub: `${o.running.length} running now` }),
        h(Stat, { label: "LLM calls (Hermes)", value: num(o.calls), sub: o.external_calls ? `+ ${num(o.external_calls)} by other clients` : null }),
        h(Stat, { label: "prompt tokens", value: num(o.input_tokens), sub: `${num(o.output_tokens)} completion` }),
        h(Stat, { label: "heaviest 3 runs", value: o.top3_token_share != null ? Math.round(o.top3_token_share * 100) + "%" : "–",
                  sub: "of all prompt tokens", tone: o.top3_token_share > 0.5 ? "#d64545" : null }),
        h(Stat, { label: "open findings", value: num((sev.critical || 0) + (sev.high || 0)),
                  sub: `${sev.critical || 0} critical · ${sev.high || 0} high · ${sev.warn || 0} warn`,
                  tone: sev.critical ? "#d64545" : sev.high ? "#e06a3a" : null })),
      o.running.length ? h(Section, { title: "Running now" },
        h(RunsTable, { runs: o.running, openRun })) : null,
      h(Section, { title: "LLM calls over time", right: h(Muted, { className: "text-xs" }, "by caller") },
        h(StackedBars, { timeline: o.timeline, metric: "calls" })),
      h(Section, { title: "Prompt tokens over time" },
        h(StackedBars, { timeline: o.timeline, metric: "tokens", height: 110 })),
      h(Section, { title: "Findings", right: h(C.Button, { variant: "outline", size: "sm", onClick: () => go("findings") }, "All findings") },
        h(FindingsList, { findings: o.findings, openRun, compact: true, onChanged: reload })),
      h(Section, { title: "Heaviest runs" }, h(RunsTable, { runs: o.top_runs, openRun })),
      h("p", { className: "mt-2 text-xs text-muted-foreground font-mono" },
        `${o.db} · ${num(o.db_bytes)}B · page refreshed ${ago(o.now)}`));
  }

  function RunsTable({ runs, openRun, showId }) {
    return h(Table, {
      rows: (runs || []).map((r) => ({ ...r, __key: r.root_id })),
      onRow: (r) => openRun(r.root_id),
      columns: [
        { key: "started_at", label: "started", render: (r) => clock(r.started_at) },
        { key: "status", label: "status", render: (r) => h(StatusDot, { status: r.status }) },
        { key: "label", label: "run", max: 320, render: (r) => h("span", { title: r.root_id }, r.label) },
        { key: "profile", label: "profile" },
        { key: "source", label: "source" },
        { key: "calls", label: "calls", right: true, render: (r) => num(r.calls) },
        { key: "tool_calls", label: "tools", right: true, render: (r) => num(r.tool_calls) },
        { key: "input_tokens", label: "prompt tok", right: true, render: (r) => num(r.input_tokens) },
        { key: "wall_s", label: "wall", right: true, render: (r) => dur(r.wall_s) },
        { key: "worst_finding", label: "", render: (r) => h(SevBadge, { sev: r.worst_finding }) },
      ].concat(showId ? [{ key: "root_id", label: "id", render: (r) => h(Mono, null, r.root_id) }] : []),
    });
  }

  // ── Runs ───────────────────────────────────────────────────────────
  function Runs({ openRun }) {
    const [since, setSince] = useState("24h");
    const [source, setSource] = useState("");
    const [sort, setSort] = useState("started");
    const [active, setActive] = useState(false);
    const [q, setQ] = useState("");
    const [{ loading, error, data }, reload] = useAsync(
      () => get(`/runs?since=${since}&source=${source}&sort=${sort}&active=${active}&limit=500`), [since, source, sort, active]);
    useInterval(reload, active ? 10000 : 0);
    const rows = useMemo(() => {
      const all = (data && data.runs) || [];
      if (!q) return all;
      const needle = q.toLowerCase();
      return all.filter((r) => [r.label, r.root_id, r.profile, r.source, r.title].join(" ").toLowerCase().includes(needle));
    }, [data, q]);
    const sources = ["", "cron", "kanban", "telegram", "cli", "tui", "api_server", "subagent"];
    return h("div", null,
      h("div", { className: "mb-4 flex flex-wrap items-center gap-2" },
        h(SinceSelect, { value: since, onChange: setSince }),
        h(C.Select, { value: source, onChange: (e) => setSource(e && e.target ? e.target.value : e) },
          sources.map((s) => h(C.SelectOption, { key: s, value: s }, s || "all sources"))),
        h(C.Select, { value: sort, onChange: (e) => setSort(e && e.target ? e.target.value : e) },
          [["started", "newest"], ["calls", "most calls"], ["tokens", "most tokens"], ["wall", "longest"]]
            .map(([v, l]) => h(C.SelectOption, { key: v, value: v }, l))),
        h(C.Button, { variant: active ? "default" : "outline", size: "sm", onClick: () => setActive(!active) },
          active ? "running only ✓" : "running only"),
        h(C.Input, { placeholder: "filter by name, id, profile…", value: q, onChange: (e) => setQ(e.target.value),
                     style: { maxWidth: 260 } }),
        h(Muted, { className: "text-xs" }, loading ? "loading…" : `${rows.length} runs`)),
      error ? h(Note, { tone: "error", message: error }) : null,
      h(RunsTable, { runs: rows, openRun, showId: true }));
  }

  // ── Run detail ─────────────────────────────────────────────────────
  function RunDetail({ runId, back }) {
    const [{ loading, error, data }, reload] = useAsync(() => get(`/run/${encodeURIComponent(runId)}`), [runId]);
    const [stopping, setStopping] = useState("");
    const [tab, setTab] = useState("calls");
    const live = data && data.run && data.sessions && data.sessions.some((s) => !s.ended_at) &&
      Date.now() / 1000 - (data.run.last_at || 0) < 900;
    useInterval(reload, live ? 8000 : 0);
    if (loading && !data) return h(Loading);
    if (error) return h("div", null, h(C.Button, { variant: "outline", size: "sm", onClick: back }, "← back"), h(Note, { tone: "error", message: error }));
    const r = data.run || {};
    const b = data.baseline;
    const lastTurn = (data.turns || []).filter((t) => t.exit_reason).slice(-1)[0];
    const stop = () => {
      if (!window.confirm(`Stop ${r.label}? The owning process interrupts it at its next LLM or tool call.`)) return;
      setStopping("requesting…");
      post(`/stop/${encodeURIComponent(r.root_id)}`, { reason: "stopped from the dashboard" })
        .then((res) => { setStopping(`stop requested for ${res.sessions} session id(s)`); reload(); },
              (err) => setStopping("stop failed: " + err));
    };
    const errs = (data.tools || []).filter((t) => t.status !== "ok");
    return h("div", null,
      h("div", { className: "mb-3 flex flex-wrap items-center gap-2" },
        h(C.Button, { variant: "outline", size: "sm", onClick: back }, "← back"),
        h("h2", { className: "text-lg font-semibold" }, r.label || runId),
        h(StatusDot, { status: live ? "running" : lastTurn && /max_iterations|guardrail/.test(lastTurn.exit_reason) ? "capped" : "done" }),
        live ? h(C.Button, { variant: "destructive", size: "sm", onClick: stop }, "Stop this run") : null,
        stopping ? h(Muted, { className: "text-xs" }, stopping) : null,
        data.stop ? h(Muted, { className: "text-xs" }, `stop requested ${ago(data.stop.requested_at)} by ${data.stop.requested_by}` +
                      (data.stop.applied_at ? `, applied ${ago(data.stop.applied_at)}` : ", not yet applied")) : null),
      h("p", { className: "mb-3 text-sm text-muted-foreground" },
        [r.profile, r.source, r.model, "started " + clock(r.started_at), "wall " + dur(r.wall_s),
         `${(data.sessions || []).length} session(s)`, lastTurn ? "ended: " + lastTurn.exit_reason : null]
          .filter(Boolean).join(" · "),
        h("br"), h(Mono, null, r.root_id)),
      h("div", { className: "grid gap-3 mb-4", style: { gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))" } },
        h(Stat, { label: "LLM calls", value: num(r.calls),
                  sub: b && b.calls.n >= 5 ? `normal p50 ${num(b.calls.p50)} · p99 ${num(b.calls.p99)}` : "no baseline yet",
                  tone: b && b.calls.n >= 5 && r.calls > 3 * b.calls.p99 + 10 ? "#d64545" : null }),
        h(Stat, { label: "tool calls", value: num(r.tool_calls), sub: errs.length ? `${errs.length} failed` : null,
                  tone: errs.length > 10 ? "#d64545" : null }),
        h(Stat, { label: "prompt tokens", value: num(r.input_tokens),
                  sub: b && b.calls.n >= 5 ? `normal p95 ${num(b.input_tokens.p95)}` : null }),
        h(Stat, { label: "completion tokens", value: num(r.output_tokens) }),
        r.job ? h(Stat, { label: "cron job", value: r.job.name, sub: r.job.schedule }) : null),
      (data.findings || []).length ? h(Section, { title: "Findings" },
        h(FindingsList, { findings: data.findings, compact: true, onChanged: reload })) : null,
      h(Section, { title: "Call by call" }, h(CallChart, { calls: data.calls, tools: data.tools })),
      (data.repeats || []).length && data.repeats[0].count > 1 ? h(Section, { title: "Most repeated tool calls" },
        h(Table, {
          rows: data.repeats, columns: [
            { key: "count", label: "times", right: true },
            { key: "errors", label: "failed", right: true, render: (x) => h("span", { style: { color: x.errors ? "#d64545" : null } }, x.errors) },
            { key: "name", label: "tool" },
            { key: "args", label: "arguments", wrap: true, render: (x) => h(Mono, null, x.args) },
          ] })) : null,
      h(C.Tabs, { defaultValue: tab }, (active, setActive) => {
        const pick = active || tab;
        const panels = {
          calls: () => h(Table, {
            rows: data.calls, empty: "No per-call rows.", columns: [
              { key: "ended_at", label: "at", render: (c) => clock(c.ended_at) },
              { key: "seq", label: "#", right: true },
              { key: "model", label: "model" },
              { key: "served_model", label: "served by", render: (c) => c.served_model || h(Muted, null, "–") },
              { key: "input_tokens", label: "in", right: true, render: (c) => num(c.input_tokens) },
              { key: "output_tokens", label: "out", right: true, render: (c) => num(c.output_tokens) },
              { key: "latency_s", label: "latency", right: true, render: (c) => dur(c.latency_s) },
              { key: "ttft_s", label: "ttft", right: true, render: (c) => (c.ttft_s ? dur(c.ttft_s) : "") },
              { key: "status", label: "status", render: (c) => h("span", { style: { color: c.status === "error" ? "#d64545" : null } }, c.status || "") },
              { key: "origin", label: "source", render: (c) => h(Muted, { className: "text-xs" }, c.origin) },
            ] }),
          tools: () => h(Table, {
            rows: data.tools, empty: "No tool calls.", columns: [
              { key: "ended_at", label: "at", render: (t) => clock(t.ended_at || t.started_at) },
              { key: "name", label: "tool" },
              { key: "status", label: "status", render: (t) => h("span", { style: { color: t.status === "error" ? "#d64545" : t.status === "blocked" ? "#e0902c" : null } }, t.status || "") },
              { key: "duration_ms", label: "took", right: true, render: (t) => (t.duration_ms != null ? dur(t.duration_ms / 1000) : "") },
              { key: "args_preview", label: "arguments", wrap: true, max: 420, render: (t) => h(Mono, null, t.args_preview) },
              { key: "result_preview", label: "result", wrap: true, max: 420, render: (t) => h(Mono, { className: t.status !== "ok" ? "text-destructive" : "" }, t.result_preview) },
            ] }),
          turns: () => h(Table, {
            rows: data.turns, empty: "No turn records.", columns: [
              { key: "ended_at", label: "ended", render: (t) => clock(t.ended_at) },
              { key: "exit_reason", label: "exit reason" },
              { key: "api_calls", label: "calls", right: true },
              { key: "max_iterations", label: "cap", right: true },
              { key: "session_id", label: "session", render: (t) => h(Mono, null, t.session_id) },
            ] }),
          events: () => h(Table, {
            rows: data.events, empty: "No events.", columns: [
              { key: "at", label: "at", render: (e) => clock(e.at) },
              { key: "kind", label: "kind" },
              { key: "detail", label: "detail", wrap: true, render: (e) => h(Mono, null, e.detail) },
            ] }),
          sessions: () => h(Table, {
            rows: data.sessions, columns: [
              { key: "id", label: "session", render: (s) => h(Mono, null, s.id) },
              { key: "started_at", label: "started", render: (s) => clock(s.started_at) },
              { key: "ended_at", label: "ended", render: (s) => clock(s.ended_at) },
              { key: "end_reason", label: "end reason" },
              { key: "api_calls", label: "calls", right: true },
              { key: "input_tokens", label: "prompt tok", right: true, render: (s) => num(s.input_tokens) },
            ] }),
        };
        const TABS = [["calls", `Calls (${(data.calls || []).length})`], ["tools", `Tools (${(data.tools || []).length})`],
                      ["turns", "Turns"], ["events", "Events"], ["sessions", "Sessions"]];
        return [
          h(C.TabsList, { key: "tabs" }, TABS.map(([k, l]) => h(C.TabsTrigger, {
            key: k, value: k, active: pick === k, onClick: () => { setActive(k); setTab(k); } }, l))),
          h("div", { key: "panel", className: "mt-3" }, (panels[pick] || panels.calls)()),
        ];
      }));
  }

  // ── Jobs ───────────────────────────────────────────────────────────
  function Jobs({ go, openRun }) {
    const [since, setSince] = useState("7d");
    const [{ loading, error, data }] = useAsync(() => get(`/jobs?since=${since}`), [since]);
    if (loading && !data) return h(Loading);
    if (error) return h(Note, { tone: "error", message: error });
    const gs = data.groups || [];
    return h("div", null,
      h("div", { className: "mb-4 flex flex-wrap items-center gap-2" },
        h(SinceSelect, { value: since, onChange: setSince }),
        h(Muted, { className: "text-xs" }, "Each cron job, each profile's kanban workers and each chat surface, measured against its own normal runs.")),
      h(Table, {
        rows: gs.map((g) => ({ ...g, __key: g.group })), columns: [
          { key: "name", label: "group", max: 260 },
          { key: "profile", label: "profile" },
          { key: "source", label: "source" },
          { key: "runs", label: "runs", right: true },
          { key: "p50", label: "calls p50", right: true, render: (g) => (g.baseline && g.baseline.calls.n >= 5 ? num(g.baseline.calls.p50) : "–") },
          { key: "p99", label: "p99", right: true, render: (g) => (g.baseline && g.baseline.calls.n >= 5 ? num(g.baseline.calls.p99) : "–") },
          { key: "range", label: "normal band · largest run", render: (g) => h(RangeBar, { b: g.baseline, value: g.max_calls }) },
          { key: "max_calls", label: "max", right: true, render: (g) => num(g.max_calls) },
          { key: "tokens_per_day", label: "prompt tok/day", right: true, render: (g) => num(g.tokens_per_day) },
          { key: "max_turns", label: "cap", right: true, render: (g) => {
              const cap = g.max_turns || (g.source === "cron" ? 500 : null);
              const warn = g.suggested_cap && cap && cap > 4 * g.suggested_cap;
              return h("span", { style: { color: warn ? "#d99a1e" : null }, title: g.max_turns ? "agent.max_turns" : "unset (cron default 500)" },
                cap ? (g.max_turns ? cap : cap + "*") : "–");
            } },
          { key: "suggested_cap", label: "suggest", right: true, render: (g) => g.suggested_cap || "–" },
          { key: "hard_stop", label: "hard stop", render: (g) => h("span", { style: { color: g.hard_stop ? null : "#d99a1e" } }, g.hard_stop ? "on" : "off") },
          { key: "high_findings", label: "", render: (g) => (g.high_findings ? h("span", { className: "text-xs", style: { color: "#e06a3a" } }, `${g.high_findings} high`) : null) },
        ] }),
      h("p", { className: "mt-3 text-xs text-muted-foreground" },
        "Green band: p50–p99 of normal runs (runs with a loop or cap finding are left out). Tick: this group's largest run, red when it is far outside. ",
        "cap: agent.max_turns of the profile; * = unset, cron uses 500. suggest: 1.5 × p99 — a cap far above it decides only how long a loop may burn."));
  }

  // ── Models ─────────────────────────────────────────────────────────
  function Models() {
    const [since, setSince] = useState("24h");
    const [{ loading, error, data }] = useAsync(() => get(`/models?since=${since}`), [since]);
    if (loading && !data) return h(Loading);
    if (error) return h(Note, { tone: "error", message: error });
    const ms = data.models || [];
    return h("div", null,
      h("div", { className: "mb-4 flex flex-wrap items-center gap-2" },
        h(SinceSelect, { value: since, onChange: setSince }),
        h(Muted, { className: "text-xs" }, "Every call LiteLLM saw, attributed to the Hermes run that made it where possible.")),
      ms.length === 0 ? h(Muted, { className: "text-sm" }, "No calls in this window.") : null,
      ms.map((m) => h(Section, {
        key: m.model, title: m.model,
        right: h(Muted, { className: "text-xs" },
          `${num(m.calls)} calls · ${num(m.input_tokens)} prompt tok · latency p50 ${dur(m.latency_p50)} / p95 ${dur(m.latency_p95)} · ttft p50 ${dur(m.ttft_p50)}` +
          (m.errors ? ` · ${m.errors} errors` : "")),
      },
        h("div", { className: "mb-2 flex h-4 w-full overflow-hidden rounded", style: { background: "var(--color-border)" } },
          m.by_caller.map((c, i) => h("div", {
            key: c.caller, title: `${c.caller}: ${num(c.calls)} calls (${Math.round((c.calls / m.calls) * 100)}%)`,
            style: { width: (c.calls / m.calls) * 100 + "%", background: colorFor(c.caller.replace("hermes:", ""), i) },
          }))),
        h(Table, {
          rows: m.by_caller.slice(0, 10), columns: [
            { key: "caller", label: "caller" },
            { key: "calls", label: "calls", right: true, render: (c) => num(c.calls) },
            { key: "share", label: "share", right: true, render: (c) => Math.round((c.calls / m.calls) * 100) + "%" },
            { key: "input_tokens", label: "prompt tok", right: true, render: (c) => num(c.input_tokens) },
          ] }),
        m.served.length ? h("p", { className: "mt-2 text-xs text-muted-foreground" },
          "served by: " + m.served.map((s) => `${s.model} (${num(s.calls)})`).join(", ") +
          (m.served.length > 1 ? " — more than one deployment answered this alias" : "")) : null)));
  }

  // ── Findings ───────────────────────────────────────────────────────
  function FindingsList({ findings, openRun, compact, onChanged }) {
    const [open, setOpen] = useState(null);
    const setState = (f, state) => post(`/findings/${f.id}/state`, { state }).then(() => onChanged && onChanged());
    if (!findings || findings.length === 0) return h(Muted, { className: "text-sm" }, "Nothing open. Good.");
    return h("div", { className: "flex flex-col gap-1" }, findings.map((f) => {
      const ev = typeof f.evidence === "string" ? safeJSON(f.evidence) : f.evidence;
      const expanded = open === f.id;
      return h("div", { key: f.id, className: "rounded-md border px-3 py-2", style: { borderColor: "var(--color-border)" } },
        h("div", { className: "flex flex-wrap items-center gap-2 cursor-pointer", onClick: () => setOpen(expanded ? null : f.id) },
          h(SevBadge, { sev: f.severity }),
          h("span", { className: "text-sm" }, f.title),
          h(Muted, { className: "text-xs ml-auto" }, `${clock(f.last_seen)}${f.count > 1 ? " · seen " + f.count + "×" : ""}${f.state !== "open" ? " · " + f.state : ""}`)),
        expanded ? h("div", { className: "mt-2 text-sm" },
          f.detail ? h("p", { className: "mb-2 text-muted-foreground" }, f.detail) : null,
          f.suggestion ? h("p", { className: "mb-2" }, "→ ", f.suggestion) : null,
          ev ? h("pre", { className: "mb-2 overflow-x-auto rounded p-2 text-xs", style: { background: "var(--color-muted)" } },
            JSON.stringify(ev, null, 2)) : null,
          h("div", { className: "flex flex-wrap gap-2" },
            f.session_id && openRun ? h(C.Button, { size: "sm", variant: "outline", onClick: () => openRun(f.session_id) }, "Open run") : null,
            f.state !== "acked" ? h(C.Button, { size: "sm", variant: "outline", onClick: () => setState(f, "acked") }, "Acknowledge") : null,
            f.state !== "resolved" ? h(C.Button, { size: "sm", variant: "outline", onClick: () => setState(f, "resolved") }, "Resolve") : null,
            f.state !== "open" ? h(C.Button, { size: "sm", variant: "outline", onClick: () => setState(f, "open") }, "Reopen") : null)) : null);
    }));
  }

  const safeJSON = (s) => { try { return JSON.parse(s); } catch (e) { return null; } };

  function Findings({ openRun }) {
    const [state, setStateFilter] = useState("open");
    const [severity, setSeverity] = useState("warn");
    const [kind, setKind] = useState("");
    const [{ loading, error, data }, reload] = useAsync(() => get(`/findings?state=${state}&severity=${severity}`), [state, severity]);
    const all = (data && data.findings) || [];
    const kinds = Array.from(new Set(all.map((f) => f.kind))).sort();
    const rows = kind ? all.filter((f) => f.kind === kind) : all;
    return h("div", null,
      h("div", { className: "mb-4 flex flex-wrap items-center gap-2" },
        h(C.Select, { value: state, onChange: (e) => setStateFilter(e && e.target ? e.target.value : e) },
          [["open", "open"], ["acked", "acknowledged"], ["resolved", "resolved"], ["all", "all"]].map(([v, l]) => h(C.SelectOption, { key: v, value: v }, l))),
        h(C.Select, { value: severity, onChange: (e) => setSeverity(e && e.target ? e.target.value : e) },
          [["info", "info and up"], ["warn", "warn and up"], ["high", "high and up"], ["critical", "critical"]].map(([v, l]) => h(C.SelectOption, { key: v, value: v }, l))),
        h(C.Select, { value: kind, onChange: (e) => setKind(e && e.target ? e.target.value : e) },
          [h(C.SelectOption, { key: "", value: "" }, "all kinds")].concat(kinds.map((k) => h(C.SelectOption, { key: k, value: k }, k)))),
        h(Muted, { className: "text-xs" }, loading ? "loading…" : `${rows.length} findings`)),
      error ? h(Note, { tone: "error", message: error }) : null,
      h(FindingsList, { findings: rows, openRun, onChanged: reload }));
  }

  // ── Page ───────────────────────────────────────────────────────────
  const TABS = [
    ["overview", "Overview", Overview],
    ["runs", "Runs", Runs],
    ["jobs", "Jobs", Jobs],
    ["models", "Models", Models],
    ["findings", "Findings", Findings],
  ];

  function RunLensPage() {
    const [runId, setRunId] = useState(null);
    const [tab, setTab] = useState(TABS[0][0]);
    const openRun = useCallback((id) => { setRunId(id); window.scrollTo && window.scrollTo(0, 0); }, []);
    if (runId) return h("div", { className: "p-4" }, h(RunDetail, { runId, back: () => setRunId(null) }));
    // C.Tabs takes a RENDER FUNCTION (active, setActive) — passing an element array
    // blanks the page ("e is not a function"), as honcho-lens learned.
    return h("div", { className: "p-4" },
      h(C.Tabs, { defaultValue: tab }, (active, setActive) => {
        const current = active || tab;
        const entry = TABS.find((t) => t[0] === current) || TABS[0];
        const go = (key) => { setActive(key); setTab(key); };
        return [
          h(C.TabsList, { key: "tabs" }, TABS.map(([key, label]) => h(C.TabsTrigger, {
            key, value: key, active: current === key, onClick: () => go(key) }, label))),
          h("div", { key: "panel", className: "mt-4" }, h(entry[2], { openRun, go })),
        ];
      }));
  }

  window.__HERMES_PLUGINS__.register("run-lens", RunLensPage);
})();
