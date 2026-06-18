from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .db import collect_runtime_status, init_db_path, open_db
from .llm import ask_archive, format_ask_response
from .memory import (
    apply_pending_update,
    clear_pending_update,
    latest_pending_update,
    load_memory_state,
    looks_like_confirmation,
    looks_like_rejection,
    queue_alias_update,
    recent_control_turns,
    record_control_turn,
)
from .tracing import (
    finish_trace,
    get_trace,
    list_telemetry,
    list_traces,
    record_telemetry,
    snapshot_memory,
    trace_context,
    trace_event,
)


def export_trace_as_eval_case(config: AppConfig, trace_id: str) -> Path:
    trace = get_trace(config, trace_id)
    if trace is None:
        raise FileNotFoundError(trace_id)
    output_dir = Path(__file__).resolve().parents[2] / 'eval' / 'generated'
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = ''.join(ch.lower() if ch.isalnum() else '-' for ch in (trace.get('question') or trace_id)).strip('-') or trace_id
    slug = '-'.join(part for part in slug.split('-') if part)[:80]
    path = output_dir / f"{slug}.json"
    answer = str(trace.get('final_answer') or '').strip()
    case = {
        'name': 'generated-trace-export',
        'description': 'Console-exported trace stub. Fill expected assertions by hand.',
        'cases': [
            {
                'id': slug,
                'question': str(trace.get('question') or ''),
                'tags': ['generated', 'console-export'],
                'notes': f"Exported from trace {trace_id}.",
                'enabled': False,
                'score_case': False,
                'min_evidence': 0,
                'require_citation': '[' in answer and ']' in answer,
                'answer_contains_any': [answer[:240]] if answer else [],
            }
        ]
    }
    path.write_text(json.dumps(case, indent=2, ensure_ascii=False) + '\n')
    return path


HTML = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beeper Bot Console</title>
<style>
:root {
  --bg: #071018;
  --bg2: #0a1622;
  --panel: #0e1b27;
  --panel2: #132332;
  --line: #20384a;
  --fg: #d9f6ff;
  --muted: #7fa7b9;
  --cyan: #5be7ff;
  --green: #6dff9d;
  --amber: #ffcf5a;
  --red: #ff6b7d;
}
* { box-sizing: border-box; }
* {
  scrollbar-width: thin;
  scrollbar-color: #4b7488 #08111a;
}
*::-webkit-scrollbar { width: 12px; height: 12px; }
*::-webkit-scrollbar-track { background: #08111a; }
*::-webkit-scrollbar-thumb { background: #274557; border-radius: 999px; border: 2px solid #08111a; }
*::-webkit-scrollbar-thumb:hover { background: #3a6880; }
body {
  margin: 0;
  min-height: 100vh;
  overflow: auto;
  background: radial-gradient(circle at top, #10253a 0, var(--bg) 35%, #04090e 100%);
  color: var(--fg);
  font: 14px/1.45 Inter, ui-sans-serif, system-ui, sans-serif;
}
header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--line);
  background: rgba(5, 12, 18, 0.88);
  position: sticky; top: 0; z-index: 50;
  backdrop-filter: blur(8px);
}
header h1 { margin: 0; font-size: 20px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--cyan); }
header .sub { margin-top: 4px; color: var(--muted); }
.hidden { display: none !important; }
main {
  padding: 14px;
  min-height: calc(100vh - 85px);
  display: grid;
  grid-template-columns: 320px minmax(540px, 1.35fr) 360px;
  gap: 14px;
  overflow: visible;
}
.panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: linear-gradient(180deg, rgba(19,35,50,.95), rgba(12,23,34,.95));
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: 0 10px 30px rgba(0,0,0,.22), inset 0 1px 0 rgba(255,255,255,.03);
  overflow: hidden;
}
.panel h2 {
  margin: 0; padding: 10px 12px; font-size: 12px; letter-spacing: .14em; text-transform: uppercase;
  color: var(--cyan); border-bottom: 1px solid var(--line); background: rgba(0,0,0,.12);
}
.panel .body { padding: 12px; min-height: 0; }
.stack { display: grid; gap: 14px; min-height: 0; overflow: visible; }
.stack.left { grid-template-rows: auto minmax(0, 1fr); }
.stack.center { grid-template-rows: auto minmax(0, 1fr); }
.stack.right { grid-template-rows: auto minmax(0, 1fr); }
.kpis { display:grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.kpi { background: var(--bg2); border: 1px solid var(--line); border-radius: 10px; padding: 10px; }
.kpi .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
.kpi .value { font-size: 22px; color: var(--green); margin-top: 4px; }
.trace-list { max-height: 76vh; overflow: auto; display: grid; gap: 8px; }
.trace-item { background: var(--bg2); border: 1px solid var(--line); border-radius: 10px; padding: 10px; cursor: pointer; }
.trace-item:hover { border-color: var(--cyan); }
.trace-item.active { border-color: var(--green); box-shadow: 0 0 0 1px rgba(109,255,157,.3) inset; }
.trace-item .q { font-weight: 600; }
.trace-item .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
.trace-item .badge { float:right; padding: 2px 6px; border-radius: 999px; font-size: 11px; }
.badge.ok { background: rgba(109,255,157,.12); color: var(--green); }
.badge.running { background: rgba(91,231,255,.12); color: var(--cyan); }
.badge.error { background: rgba(255,107,125,.14); color: var(--red); }
.chat-box { display:grid; gap:10px; }
textarea, pre, code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
textarea {
  width: 100%; min-height: 76px; resize: vertical; background: #08111a; color: var(--fg);
  border: 1px solid var(--line); border-radius: 10px; padding: 10px;
}
button {
  background: linear-gradient(180deg, #0d3440, #0b2530); color: var(--fg); border: 1px solid #24515d;
  padding: 10px 14px; border-radius: 10px; cursor: pointer; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
}
button:hover { border-color: var(--cyan); color: var(--cyan); }
.chat-actions { display:flex; align-items:center; justify-content:space-between; gap: 10px; }
.output, .scroller {
  background: #07111a; border: 1px solid var(--line); border-radius: 10px; padding: 10px; overflow: auto;
}
.output { min-height: 96px; max-height: 220px; white-space: pre-wrap; }
#answerOut { min-height: 84px; max-height: 160px; }
#finalAnswer { min-height: 84px; max-height: 140px; }
.trace-list { min-height: 0; max-height: none; }
.timeline { display:flex; flex-direction:column; gap: 10px; min-height: 0; max-height: none; overflow-y:auto; overflow-x:hidden; scrollbar-gutter: stable both-edges; padding-right: 4px; }
.timeline-group { display:grid; gap: 8px; }
.timeline-group-head {
  position: sticky;
  top: 0;
  z-index: 2;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap: 10px;
  padding: 6px 8px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(10,22,34,.96);
}
.timeline-group-title { color: var(--cyan); font-size: 11px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.timeline-group-meta { color: var(--muted); font-size: 11px; }
.event {
  flex: 0 0 auto;
  border: 1px solid var(--line);
  border-left: 4px solid var(--cyan);
  background: rgba(7,17,26,.72);
  border-radius: 10px;
  overflow: hidden;
}
.event.planner { border-left-color: var(--cyan); }
.event.retrieval { border-left-color: var(--green); }
.event.answer { border-left-color: var(--amber); }
.event.verification { border-left-color: #ff9e5a; }
.event.memory { border-left-color: #c78bff; }
.event.bridge, .event.console, .event.ask, .event.control { border-left-color: #7cb7ff; }
.event.error { border-left-color: var(--red); }
.event-toggle {
  display: block;
  width: 100%;
  cursor: pointer;
  padding: 10px 12px;
  background: rgba(255,255,255,.03);
}
.event-toggle:hover { background: rgba(255,255,255,.05); }
.event-head { display: grid; gap: 4px; }
.event-row { display:flex; align-items:center; justify-content:space-between; gap: 10px; }
.event-left { display:flex; align-items:center; gap: 8px; min-width: 0; }
.event-stage {
  display:inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
  border: 1px solid var(--line);
  color: var(--cyan);
  background: rgba(91,231,255,.08);
}
.event-stage.planner { color: var(--cyan); }
.event-stage.retrieval { color: var(--green); background: rgba(109,255,157,.08); }
.event-stage.answer { color: var(--amber); background: rgba(255,207,90,.08); }
.event-stage.verification { color: #ff9e5a; background: rgba(255,158,90,.08); }
.event-stage.memory { color: #c78bff; background: rgba(199,139,255,.08); }
.event-stage.bridge { color: #7cb7ff; background: rgba(124,183,255,.08); }
.event .ek { color: var(--amber); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; font-weight: 700; white-space: nowrap; }
.event .ts { color: var(--muted); font-size: 12px; white-space: nowrap; }
.event .delta { color: var(--muted); font-size: 11px; white-space: nowrap; }
.event .preview { color: var(--fg); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.event-body { padding: 10px 12px 12px; display:none; gap: 10px; }
.event.open .event-body { display:grid; }
.event-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.event-block { display:grid; gap: 6px; }
.event-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
.event-kv { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.event-kv-item { background:#07111a; border:1px solid var(--line); border-radius:8px; padding:8px 10px; }
.event-kv-item .k { color: var(--muted); font-size: 11px; text-transform: uppercase; }
.event-kv-item .v { margin-top: 3px; word-break: break-word; }
.event-cards { display:grid; gap:8px; }
.event-card { background:#07111a; border:1px solid var(--line); border-radius:8px; padding:9px 10px; }
.event-card.result { border-left: 3px solid var(--green); }
.event-card.evidence { border-left: 3px solid var(--amber); }
.event-card.turn { border-left: 3px solid #7cb7ff; }
.event-card.fact { border-left: 3px solid #c78bff; }
.event-card .title { font-weight: 700; }
.event-card .meta { color: var(--muted); font-size: 12px; margin-top: 3px; }
.event-card .text { margin-top: 6px; white-space: pre-wrap; word-break: break-word; }
.event-card .chips { display:flex; flex-wrap:wrap; gap:6px; margin-top: 6px; }
.event-card .chip { display:inline-block; padding:2px 8px; border:1px solid var(--line); border-radius:999px; color:var(--muted); font-size:11px; }
.event pre {
  margin: 0;
  background: #07111a;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  white-space: pre-wrap;
  word-break: break-word;
  overflow:auto;
  max-height: 220px;
}
.ops-grid { display:grid; gap: 10px; }
.ops-top { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.ops-meta { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.ops-line { color: var(--muted); font-size: 12px; }
.chart-grid { display:grid; gap:10px; }
.chart-card { display:grid; gap:6px; }
svg { width:100%; height:72px; background:#061019; border:1px solid var(--line); border-radius:10px; }
.small { color: var(--muted); font-size: 12px; }
.mem-columns { display:grid; gap:10px; min-height:0; overflow:auto; }
.listish { display:grid; gap:6px; }
.pill { display:inline-block; margin:2px 4px 2px 0; padding:2px 8px; border:1px solid var(--line); border-radius:999px; color:var(--cyan); }
.trace-meta-grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px; }
.trace-meta-card { background: var(--bg2); border:1px solid var(--line); border-radius:10px; padding:8px 10px; }
.trace-meta-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing:.08em; }
.trace-meta-card .value { margin-top:4px; font-weight:600; }
.memory-card .scroller, .memory-card .output { max-height: 160px; }
.trace-overview-panel .body { display:grid; gap:10px; }
.trace-timeline-panel .body { padding-top: 8px; display:grid; grid-template-rows: auto minmax(0, 1fr); min-height: 0; }
.trace-toolbar { display:flex; align-items:center; justify-content:space-between; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.filter-row { display:flex; gap: 6px; flex-wrap: wrap; }
.filter-chip {
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #0b1722;
  color: var(--muted);
  cursor: pointer;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .08em;
}
.filter-chip:hover { border-color: #4d738a; color: var(--fg); }
.filter-chip.active {
  color: #041018;
  background: linear-gradient(180deg, #7ef0ff, #54dff9);
  border-color: #7ef0ff;
  box-shadow: 0 0 0 1px rgba(91,231,255,.15), 0 0 18px rgba(91,231,255,.18);
}
.toolbar-actions { display:flex; gap: 8px; flex-wrap: wrap; }
.mini-btn {
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #0b1722;
  color: var(--fg);
  cursor: pointer;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .08em;
}
.mini-btn:hover { border-color: var(--cyan); color: var(--cyan); }
.toast {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 100;
  max-width: 420px;
  padding: 10px 12px;
  border: 1px solid #2e5a6b;
  border-radius: 10px;
  background: linear-gradient(180deg, rgba(8,26,36,.98), rgba(7,17,26,.98));
  color: var(--fg);
  box-shadow: 0 10px 30px rgba(0,0,0,.28), 0 0 20px rgba(91,231,255,.12);
}
.toast.ok { border-color: #2f7c55; box-shadow: 0 10px 30px rgba(0,0,0,.28), 0 0 20px rgba(109,255,157,.12); }
.toast.error { border-color: #7b3040; box-shadow: 0 10px 30px rgba(0,0,0,.28), 0 0 20px rgba(255,107,125,.12); }
.trace-overview-panel { position: sticky; top: 0; }
@media (max-width: 1280px) { main { grid-template-columns: 300px 1fr; } .right { grid-column: span 2; } .event-grid, .event-kv { grid-template-columns: 1fr; } .toolbar-actions { width: 100%; } }
@media (max-width: 980px) { body { overflow: auto; height: auto; } main { min-height: auto; height: auto; grid-template-columns: 1fr; overflow: visible; } .stack { overflow: visible; } .right { grid-column: auto; } }
</style>
</head>
<body>
<header>
  <h1>Beeper Bot Operator Console</h1>
  <div class="sub">Live traces, prompts, evidence, memory, and GPU telemetry.</div>
</header>
<main>
  <section class="stack left">
    <div class="panel"><h2>Console chat</h2><div class="body chat-box">
      <textarea id="question" placeholder="Ask the bot something. Example: What address did Taylor send?"></textarea>
      <div class="chat-actions"><button id="sendBtn">Run query</button><div class="small">Ctrl/Cmd+Enter sends</div></div>
      <div class="output" id="answerOut">No query yet.</div>
    </div></div>
    <div class="panel"><h2>Recent traces</h2><div class="body trace-list" id="traceList"></div></div>
  </section>

  <section class="stack center">
    <div class="panel trace-overview-panel"><h2>Trace overview</h2><div class="body">
      <div class="trace-meta-grid" id="traceStats"></div>
      <div class="small" id="traceMeta">Pick a trace on the left.</div>
      <div class="output" id="finalAnswer"></div>
    </div></div>
    <div class="panel trace-timeline-panel"><h2>Trace timeline</h2><div class="body">
      <div class="trace-toolbar">
        <div>
          <div class="filter-row" id="filterRow"></div>
          <div class="filter-row" id="presetRow" style="margin-top:6px"></div>
        </div>
        <div class="toolbar-actions">
          <button class="mini-btn" id="copyAnswerBtn" type="button">Copy answer</button>
          <button class="mini-btn" id="copyTraceBtn" type="button">Copy trace JSON</button>
          <button class="mini-btn" id="copyPromptBtn" type="button">Copy prompts</button>
          <button class="mini-btn" id="exportEvalBtn" type="button">Export eval case</button>
        </div>
      </div>
      <div class="timeline" id="timeline"></div>
    </div></div>
  </section>

  <section class="stack right">
    <div class="panel"><h2>System and telemetry</h2><div class="body ops-grid">
      <div class="ops-top kpis">
        <div class="kpi"><div class="label">GPU util</div><div class="value" id="gpuUtil">--</div></div>
        <div class="kpi"><div class="label">VRAM used</div><div class="value" id="vramUsed">--</div></div>
        <div class="kpi"><div class="label">Trace count</div><div class="value" id="traceCount">--</div></div>
        <div class="kpi"><div class="label">Pending updates</div><div class="value" id="pendingCount">--</div></div>
      </div>
      <div class="ops-meta">
        <div class="ops-line" id="statusLine"></div>
        <div class="ops-line" id="telemetryLine"></div>
      </div>
      <div class="chart-grid">
        <div class="chart-card"><div class="small">GPU util</div><svg id="gpuChart"></svg></div>
        <div class="chart-card"><div class="small">VRAM used / total</div><svg id="vramChart"></svg></div>
        <div class="chart-card"><div class="small">GPU temperature</div><svg id="tempChart"></svg></div>
      </div>
    </div></div>
    <div class="panel"><h2>Memory and aliases</h2><div class="body mem-columns" id="memoryPane"></div></div>
  </section>
</main>
<div class="toast hidden" id="toast"></div>
<script>
const state = { traces: [], activeTraceId: null, activeTrace: null, openByTrace: {}, timelineScrollByTrace: {}, stageFilters: new Set(), activePreset: 'all' };

function esc(s) {
  return String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
function pretty(x) { return JSON.stringify(x, null, 2); }
async function jget(url, opts) { const r = await fetch(url, opts); if (!r.ok) throw new Error(await r.text()); return r.json(); }

function pathLine(values, maxValue) {
  if (!values.length) return '';
  const w = 500, h = 120;
  return values.map((v, i) => {
    const x = (i / Math.max(1, values.length - 1)) * w;
    const y = h - ((Number(v || 0) / Math.max(1, maxValue)) * (h - 10)) - 5;
    return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
}
function renderChart(id, values, maxValue, color) {
  const svg = document.getElementById(id);
  const d = pathLine(values, maxValue);
  svg.innerHTML = `<path d="${d}" fill="none" stroke="${color}" stroke-width="3" />`;
}

function showToast(text, tone='ok') {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.classList.remove('hidden', 'ok', 'error');
  el.classList.add(tone);
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.add('hidden'), 2200);
}

async function copyText(text, label) {
  await navigator.clipboard.writeText(text);
  showToast(`${label} copied`, 'ok');
}

function renderTraceList() {
  const el = document.getElementById('traceList');
  el.innerHTML = state.traces.map(t => `
    <div class="trace-item ${t.trace_id === state.activeTraceId ? 'active' : ''}" data-id="${t.trace_id}">
      <div><span class="badge ${esc(t.status)}">${esc(t.status)}</span></div>
      <div class="q">${esc(t.question || '(no question)')}</div>
      <div class="meta">${esc(t.source)} · ${esc(t.created_at)}</div>
    </div>`).join('');
  for (const node of el.querySelectorAll('.trace-item')) {
    node.addEventListener('click', () => loadTrace(node.dataset.id));
  }
  document.getElementById('traceCount').textContent = String(state.traces.length);
}

function toneForEvent(kind) {
  if (kind.includes('error')) return 'error';
  if (kind.startsWith('planner.')) return 'planner';
  if (kind.startsWith('retrieval.') || kind.startsWith('evidence.')) return 'retrieval';
  if (kind.startsWith('answer.')) return 'answer';
  if (kind.startsWith('verification.')) return 'verification';
  if (kind.startsWith('memory.')) return 'memory';
  if (kind.startsWith('bridge.') || kind.startsWith('console.') || kind.startsWith('ask.') || kind.startsWith('control.')) return 'bridge';
  return 'bridge';
}

function stageLabel(kind) {
  const tone = toneForEvent(kind);
  return tone === 'bridge' ? 'flow' : tone;
}

function stageOrder() {
  return ['flow', 'planner', 'retrieval', 'answer', 'verification', 'memory', 'error'];
}

function presetMap() {
  return {
    all: [],
    prompts: ['planner', 'answer', 'verification'],
    retrieval: ['retrieval'],
    model: ['planner', 'answer', 'verification'],
    flow: ['flow'],
  };
}

function renderFilters() {
  const row = document.getElementById('filterRow');
  row.innerHTML = stageOrder().map(stage => `<button type="button" class="filter-chip ${state.stageFilters.has(stage) ? 'active' : ''}" data-stage="${stage}">${stage}</button>`).join('');
  for (const node of row.querySelectorAll('.filter-chip')) {
    node.addEventListener('click', () => {
      state.activePreset = 'custom';
      const stage = node.dataset.stage;
      if (state.stageFilters.has(stage)) state.stageFilters.delete(stage);
      else state.stageFilters.add(stage);
      renderFilters();
      renderTraceDetail();
    });
  }
  const presetRow = document.getElementById('presetRow');
  presetRow.innerHTML = Object.keys(presetMap()).map(name => `<button type="button" class="filter-chip ${state.activePreset === name ? 'active' : ''}" data-preset="${name}">${name}</button>`).join('');
  for (const node of presetRow.querySelectorAll('.filter-chip')) {
    node.addEventListener('click', () => {
      const preset = node.dataset.preset;
      state.activePreset = preset;
      state.stageFilters = new Set(presetMap()[preset] || []);
      renderFilters();
      renderTraceDetail();
    });
  }
}

function stageForEvent(kind) {
  return stageLabel(kind);
}

function parseTs(value) {
  const ms = Date.parse(value || '');
  return Number.isFinite(ms) ? ms : null;
}

function fmtDelta(ms) {
  if (ms == null) return '';
  if (ms < 1000) return `+${ms}ms`;
  return `+${(ms / 1000).toFixed(2)}s`;
}

function shortText(value, limit=140) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
}

function payloadPreview(kind, payload) {
  if (kind === 'control.context') {
    const turns = Array.isArray(payload.control_turns) ? payload.control_turns.length : 0;
    const facts = Array.isArray(payload.memory_state?.facts) ? payload.memory_state.facts.length : 0;
    return `control turns=${turns} · memory facts=${facts}`;
  }
  if (kind.endsWith('.request') && payload.payload?.model) {
    return `model=${payload.payload.model} · max_tokens=${payload.payload.max_tokens}`;
  }
  if (payload.search_queries) return `queries: ${payload.search_queries.join(' | ')}`;
  if (payload.queries) return `queries: ${payload.queries.join(' | ')}`;
  if (payload.results) return `results: ${payload.results.length}`;
  if (payload.items) return `evidence: ${payload.items.length}`;
  if (payload.answer) return shortText(payload.answer);
  if (payload.rendered) return shortText(payload.rendered);
  if (payload.raw_text) return shortText(payload.raw_text);
  if (payload.question) return shortText(payload.question);
  if (payload.prompt) return shortText(payload.prompt);
  if (payload.count != null) return `${payload.count} item(s)`;
  return shortText(pretty(payload), 140);
}

function renderResultCards(items, labelKey='text', kind='result') {
  return `<div class="event-cards">${items.map(item => `
    <div class="event-card ${kind}">
      <div class="title">${esc(item.chat_name || item.citation_id || 'item')} · ${esc(item.sender_name || '')}</div>
      <div class="meta">${esc(item.timestamp || '')}${item.score != null ? ` · score=${Number(item.score).toFixed(1)}` : ''}</div>
      <div class="chips">
        ${item.citation_id ? `<span class="chip">${esc(item.citation_id)}</span>` : ''}
        ${item.message_id ? `<span class="chip">msg ${esc(String(item.message_id).slice(0, 8))}</span>` : ''}
      </div>
      <div class="text">${esc(item[labelKey] || item.excerpt || item.text || '')}</div>
    </div>`).join('')}</div>`;
}

function renderKv(payload) {
  const pairs = [];
  for (const [key, value] of Object.entries(payload)) {
    if (value == null || value === '' || Array.isArray(value) || typeof value === 'object') continue;
    pairs.push(`<div class="event-kv-item"><div class="k">${esc(key)}</div><div class="v">${esc(String(value))}</div></div>`);
  }
  return pairs.length ? `<div class="event-kv">${pairs.join('')}</div>` : '';
}

function eventBlocks(payload) {
  const blocks = [];
  const textKeys = [
    ['question', 'Question'],
    ['effective_question', 'Effective question'],
    ['prompt', 'Prompt'],
    ['raw_text', 'Raw model output'],
    ['draft_answer', 'Draft answer'],
    ['answer', 'Answer'],
    ['rendered', 'Rendered response'],
    ['person_context', 'Person context'],
    ['control_context', 'Control context'],
  ];
  for (const [key, label] of textKeys) {
    if (payload[key]) blocks.push(`<div class="event-block"><div class="event-label">${esc(label)}</div><pre>${esc(payload[key])}</pre></div>`);
  }
  if (payload.search_queries || payload.queries) {
    const values = payload.search_queries || payload.queries;
    blocks.push(`<div class="event-block"><div class="event-label">Queries</div><pre>${esc(values.join('\n'))}</pre></div>`);
  }
  if (payload.results) {
    blocks.push(`<div class="event-block"><div class="event-label">Results</div>${renderResultCards(payload.results.slice(0, 8), 'text', 'result')}</div>`);
  }
  if (payload.items) {
    blocks.push(`<div class="event-block"><div class="event-label">Evidence items</div>${renderResultCards(payload.items, 'excerpt', 'evidence')}</div>`);
  }
  if (Array.isArray(payload.control_turns)) {
    blocks.push(`<div class="event-block"><div class="event-label">Control turns</div><div class="event-cards">${payload.control_turns.slice(-6).map(item => `<div class="event-card turn"><div class="title">${esc(item.role || 'turn')}</div><div class="text">${esc(item.content || '')}</div></div>`).join('')}</div></div>`);
  }
  if (Array.isArray(payload.memory_state?.facts)) {
    blocks.push(`<div class="event-block"><div class="event-label">Memory facts</div><div class="event-cards">${payload.memory_state.facts.slice(0, 6).map(item => `<div class="event-card fact"><div class="title">${esc(item.subject || '')}</div><div class="meta">${esc(item.predicate || '')}</div><div class="text">${esc(item.object || '')}</div></div>`).join('')}</div></div>`);
  }
  const compact = { ...payload };
  for (const key of ['question','effective_question','prompt','raw_text','draft_answer','answer','rendered','person_context','control_context','search_queries','queries','results','items','control_turns','memory_state']) delete compact[key];
  const kv = renderKv(compact);
  if (kv) blocks.push(`<div class="event-block"><div class="event-label">Metadata</div>${kv}</div>`);
  const leftovers = { ...compact };
  for (const [key, value] of Object.entries(leftovers)) {
    if (value == null || value === '' || (!Array.isArray(value) && typeof value !== 'object')) delete leftovers[key];
  }
  if (Object.keys(leftovers).length) {
    blocks.push(`<div class="event-block"><div class="event-label">Raw metadata</div><pre>${esc(pretty(leftovers))}</pre></div>`);
  }
  return `<div class="event-grid">${blocks.join('')}</div>`;
}

function eventKey(ev) {
  return `${ev.seq_no || 0}:${ev.event_kind}:${ev.created_at}`;
}

function buildTimelineGroups(events) {
  const ordered = [];
  const byStage = new Map();
  let traceStart = null;
  let prev = null;
  for (const ev of events) {
    const stage = stageForEvent(ev.event_kind);
    const ts = parseTs(ev.created_at);
    if (traceStart == null && ts != null) traceStart = ts;
    ev._stage = stage;
    ev._delta = ts != null && traceStart != null ? ts - traceStart : null;
    ev._stepDelta = ts != null && prev != null ? ts - prev : null;
    prev = ts ?? prev;
    if (!byStage.has(stage)) {
      const group = { stage, events: [] };
      byStage.set(stage, group);
      ordered.push(group);
    }
    byStage.get(stage).events.push(ev);
  }
  ordered.sort((a, b) => stageOrder().indexOf(a.stage) - stageOrder().indexOf(b.stage));
  return ordered;
}

function renderTraceDetail() {
  const t = state.activeTrace;
  if (!t) return;
  document.getElementById('traceMeta').textContent = `${t.trace_kind} · ${t.source} · ${t.status} · ${t.created_at}`;
  document.getElementById('traceStats').innerHTML = `
    <div class="trace-meta-card"><div class="label">Status</div><div class="value">${esc(t.status)}</div></div>
    <div class="trace-meta-card"><div class="label">Source</div><div class="value">${esc(t.source)}</div></div>
    <div class="trace-meta-card"><div class="label">Events</div><div class="value">${(t.events || []).length}</div></div>
    <div class="trace-meta-card"><div class="label">Finished</div><div class="value">${esc(t.finished_at || 'running')}</div></div>`;
  document.getElementById('finalAnswer').textContent = t.final_answer || t.error_text || '(running)';
  const timeline = document.getElementById('timeline');
  const openKeys = state.openByTrace[t.trace_id] || [];
  const openSet = new Set(openKeys);
  const priorScroll = state.timelineScrollByTrace[t.trace_id] || 0;
  const filters = state.stageFilters;
  const groups = buildTimelineGroups([...(t.events || [])]).filter(group => !filters.size || filters.has(group.stage));
  timeline.innerHTML = groups.map(group => {
    const first = group.events[0];
    const last = group.events[group.events.length - 1];
    const span = (last?._delta ?? 0) - (first?._delta ?? 0);
    return `
      <div class="timeline-group ${group.stage}">
        <div class="timeline-group-head">
          <div class="timeline-group-title">${esc(group.stage)}</div>
          <div class="timeline-group-meta">${group.events.length} event(s) · ${esc(fmtDelta(span))}</div>
        </div>
        ${group.events.map((ev, idx) => {
          const key = eventKey(ev);
          const shouldOpen = openSet.size ? openSet.has(key) : (group.stage === 'flow' ? idx < 2 : idx === 0);
          return `
            <div class="event ${toneForEvent(ev.event_kind)} ${shouldOpen ? 'open' : ''}" data-event-key="${esc(key)}">
              <div class="event-toggle">
                <div class="event-head">
                  <div class="event-row"><div class="event-left"><div class="event-stage ${toneForEvent(ev.event_kind)}">${esc(stageLabel(ev.event_kind))}</div><div class="ek">${esc(ev.event_kind)}</div></div><div class="event-left"><div class="delta">${esc(fmtDelta(ev._delta))}</div><div class="ts">${esc(ev.created_at)}</div></div></div>
                  <div class="preview">${esc(payloadPreview(ev.event_kind, ev.payload || {}))}</div>
                </div>
              </div>
              <div class="event-body">${eventBlocks(ev.payload || {})}</div>
            </div>`;
        }).join('')}
      </div>`;
  }).join('');
  for (const node of timeline.querySelectorAll('.event-toggle')) {
    node.addEventListener('click', () => {
      const card = node.parentElement;
      card.classList.toggle('open');
      state.openByTrace[t.trace_id] = [...timeline.querySelectorAll('.event.open')].map(item => item.dataset.eventKey);
    });
  }
  timeline.onscroll = () => {
    state.timelineScrollByTrace[t.trace_id] = timeline.scrollTop;
  };
  timeline.scrollTop = priorScroll;
}

function collectPromptText(trace) {
  if (!trace || !trace.events) return '';
  const items = [];
  for (const ev of trace.events) {
    const payload = ev.payload || {};
    if (payload.prompt) items.push(`${ev.event_kind}\n${payload.prompt}`);
  }
  return items.join('\n\n---\n\n');
}

function renderMemory(mem) {
  const facts = (mem.memory_state && mem.memory_state.facts) || [];
  const turns = mem.control_turns || [];
  const people = mem.people || [];
  const pending = mem.pending_updates || [];
  document.getElementById('pendingCount').textContent = String(pending.filter(x => x.status === 'pending').length);
  document.getElementById('memoryPane').innerHTML = `
    <div class="memory-card">
      <div class="small">Rolling summary</div>
      <div class="output">${esc((mem.memory_state && mem.memory_state.control_summary) || '(none)')}</div>
    </div>
    <div class="memory-card">
      <div class="small">Recent control turns</div>
      <div class="scroller listish">${turns.map(t => `<div><span class="pill">${esc(t.role)}</span> ${esc(t.content)}</div>`).join('') || '(none)'}</div>
    </div>
    <div class="memory-card">
      <div class="small">Structured facts</div>
      <div class="scroller listish">${facts.map(f => `<div>${esc(f.subject)} <span class="pill">${esc(f.predicate)}</span> ${esc(f.object)}</div>`).join('') || '(none)'}</div>
    </div>
    <div class="memory-card">
      <div class="small">People and aliases</div>
      <div class="scroller listish">${people.map(p => `<div><strong>${esc(p.canonical_name)}</strong> ${(p.aliases||[]).map(a => `<span class="pill">${esc(a)}</span>`).join('')}</div>`).join('') || '(none)'}</div>
    </div>
    <div class="memory-card">
      <div class="small">Pending updates</div>
      <div class="scroller listish">${pending.map(p => `<div><span class="pill">${esc(p.status)}</span> ${esc(p.update_kind)} ${esc(JSON.stringify(p.payload))}</div>`).join('') || '(none)'}</div>
    </div>`;
}

async function refreshStatus() {
  const [telemetry, traces, memory, status] = await Promise.all([
    jget('/api/telemetry?limit=120'),
    jget('/api/traces?limit=40'),
    jget('/api/memory'),
    jget('/api/status'),
  ]);
  const latest = telemetry.samples[telemetry.samples.length - 1] || {};
  document.getElementById('gpuUtil').textContent = latest.gpu_util == null ? '--' : `${Math.round(latest.gpu_util)}%`;
  document.getElementById('vramUsed').textContent = latest.vram_used_mb == null ? '--' : `${Math.round(latest.vram_used_mb)} MB`;
  document.getElementById('statusLine').textContent = `model=${status.llm_model} · schema=${status.schema_version} · messages=${status.message_count} · people=${status.people_count}`;
  document.getElementById('telemetryLine').textContent = latest.gpu_temp_c == null ? 'telemetry idle' : `temp=${Math.round(latest.gpu_temp_c)}°C · vram-total=${Math.round(latest.vram_total_mb || 0)} MB`;
  state.traces = traces.traces;
  if (!state.activeTraceId && state.traces.length) state.activeTraceId = state.traces[0].trace_id;
  renderTraceList();
  renderMemory(memory);
  renderChart('gpuChart', telemetry.samples.map(x => x.gpu_util || 0), 100, '#5be7ff');
  renderChart('vramChart', telemetry.samples.map(x => x.vram_used_mb || 0), Math.max(1, ...telemetry.samples.map(x => x.vram_total_mb || 1)), '#6dff9d');
  renderChart('tempChart', telemetry.samples.map(x => x.gpu_temp_c || 0), 100, '#ffcf5a');
  if (state.activeTraceId) await loadTrace(state.activeTraceId, true);
}

async function loadTrace(id, silent=false) {
  const changedTrace = state.activeTraceId !== id;
  state.activeTraceId = id;
  state.activeTrace = await jget(`/api/traces/${id}`);
  if (changedTrace && !state.openByTrace[id]) state.openByTrace[id] = [];
  renderTraceList();
  renderTraceDetail();
  if (!silent) document.getElementById('answerOut').textContent = state.activeTrace.final_answer || '(no final answer yet)';
}

async function sendQuestion() {
  const question = document.getElementById('question').value.trim();
  if (!question) return;
  document.getElementById('answerOut').textContent = 'Running...';
  const res = await jget('/api/chat', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({question}) });
  document.getElementById('answerOut').textContent = res.rendered;
  await refreshStatus();
  if (res.trace_id) await loadTrace(res.trace_id);
}

document.getElementById('sendBtn').addEventListener('click', sendQuestion);
document.getElementById('copyAnswerBtn').addEventListener('click', async () => {
  try {
    if (!state.activeTrace) return;
    await copyText(state.activeTrace.final_answer || '', 'Answer');
  } catch (err) { showToast(`Copy failed: ${err}`, 'error'); }
});
document.getElementById('copyTraceBtn').addEventListener('click', async () => {
  try {
    if (!state.activeTrace) return;
    await copyText(JSON.stringify(state.activeTrace, null, 2), 'Trace JSON');
  } catch (err) { showToast(`Copy failed: ${err}`, 'error'); }
});
document.getElementById('copyPromptBtn').addEventListener('click', async () => {
  try {
    if (!state.activeTrace) return;
    await copyText(collectPromptText(state.activeTrace), 'Prompt set');
  } catch (err) { showToast(`Copy failed: ${err}`, 'error'); }
});
document.getElementById('exportEvalBtn').addEventListener('click', async () => {
  try {
    if (!state.activeTrace) return;
    const res = await jget(`/api/traces/${state.activeTrace.trace_id}/export-eval`, { method: 'POST' });
    showToast(`Eval exported: ${res.path}`, 'ok');
  } catch (err) { showToast(`Export failed: ${err}`, 'error'); }
});
document.getElementById('question').addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') sendQuestion();
});
renderFilters();
refreshStatus();
setInterval(refreshStatus, 3000);
</script>
</body>
</html>
'''


class ConsoleServer:
    def __init__(self, config: AppConfig, host: str, port: int, sample_seconds: int = 2):
        self.config = config
        self.host = host
        self.port = port
        self.sample_seconds = max(1, int(sample_seconds))
        self._stop = threading.Event()

    def _start_sampler(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    record_telemetry(self.config)
                except Exception:
                    pass
                self._stop.wait(self.sample_seconds)
        thread = threading.Thread(target=loop, name="telemetry-sampler", daemon=True)
        thread.start()

    def serve(self) -> None:
        init_db_path(self.config.archive.path)
        self._start_sampler()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, body: str, status: int = 200) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", "0") or "0")
                data = self.rfile.read(length) if length > 0 else b"{}"
                return json.loads(data.decode("utf-8") or "{}")

            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    return self._send_html(HTML)
                if parsed.path == "/api/traces":
                    qs = parse_qs(parsed.query)
                    limit = int((qs.get("limit") or ["40"])[0])
                    return self._send_json({"traces": list_traces(server.config, limit=limit)})
                if parsed.path.startswith("/api/traces/"):
                    trace_id = parsed.path.rsplit("/", 1)[-1]
                    payload = get_trace(server.config, trace_id)
                    if payload is None:
                        return self._send_json({"error": "not found"}, status=404)
                    return self._send_json(payload)
                if parsed.path == "/api/telemetry":
                    qs = parse_qs(parsed.query)
                    limit = int((qs.get("limit") or ["180"])[0])
                    return self._send_json({"samples": list_telemetry(server.config, limit=limit)})
                if parsed.path == "/api/memory":
                    return self._send_json(snapshot_memory(server.config))
                if parsed.path == "/api/status":
                    status = collect_runtime_status(server.config)
                    return self._send_json({
                        "schema_version": status.database.schema_version,
                        "message_count": status.database.message_count,
                        "people_count": status.database.people_count,
                        "llm_model": server.config.llm.model,
                    })
                return self._send_json({"error": "not found"}, status=404)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/chat":
                    body = self._read_json()
                    question = str(body.get("question") or "").strip()
                    if not question:
                        return self._send_json({"error": "question is required"}, status=400)

                    with trace_context(server.config, "ask", question=question, source="console") as handle:
                        trace_event("console.request", {"question": question})
                        record_control_turn(server.config, "user", question, chat_id="console")
                        pending = latest_pending_update(server.config)
                        if pending and looks_like_confirmation(question):
                            answer = apply_pending_update(server.config, pending)
                            trace_event("memory.update.applied", {"update_id": pending.update_id, "answer": answer})
                        elif pending and looks_like_rejection(question):
                            clear_pending_update(server.config, pending.update_id, status="cancelled")
                            answer = "Okay. I did not save that memory update."
                            trace_event("memory.update.cancelled", {"update_id": pending.update_id, "answer": answer})
                        else:
                            response = ask_archive(
                                server.config,
                                question,
                                control_turns=recent_control_turns(server.config, limit=8),
                                memory_state=load_memory_state(server.config),
                            )
                            answer = format_ask_response(response)
                            if question.casefold().startswith("remember that ") and "Please confirm before I save it." in response.answer:
                                import re as _re
                                match = _re.match(r"remember that\s+(.+?)\s+is\s+(.+?)\.?$", question.strip(), _re.IGNORECASE)
                                if match:
                                    queued = queue_alias_update(server.config, match.group(1).strip(), match.group(2).strip(), source_text=question)
                                    trace_event("memory.update.queued", {"update_id": queued.update_id, "payload": queued.payload})
                        record_control_turn(server.config, "assistant", answer, chat_id="console")
                        trace_event("console.response", {"rendered": answer})
                        finish_trace(handle, status="ok", final_answer=answer)
                        return self._send_json({"trace_id": handle.trace_id, "rendered": answer})

                if parsed.path.startswith('/api/traces/') and parsed.path.endswith('/export-eval'):
                    trace_id = parsed.path.split('/')[-2]
                    path = export_trace_as_eval_case(server.config, trace_id)
                    return self._send_json({"ok": True, "path": str(path)})

                return self._send_json({"error": "not found"}, status=404)

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        try:
            httpd.serve_forever()
        finally:
            self._stop.set()
            httpd.server_close()


def serve_console(config: AppConfig, host: str = "127.0.0.1", port: int = 8765, sample_seconds: int = 2) -> None:
    ConsoleServer(config, host, port, sample_seconds=sample_seconds).serve()
