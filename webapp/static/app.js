// Browser side of the web app: talks only to /api (webapp/app.py). No measurement logic lives here.
'use strict';
const $ = (id) => document.getElementById(id);
const S = { health: null, cfg: null, cfgName: null, preview: null, rows: [], seq: 0, es: null, roots: [] };
const COLORS = { site: '#b9b9b4', path: '#2459c9', before: '#55555c', measured: '#1f8a4c', failed: '#c0392b',
                 other: '#b7791f', origin: '#000000' };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts,
                                body: opts.body ? JSON.stringify(opts.body) : undefined });
  const t = await r.text();
  const v = t ? JSON.parse(t) : null;
  if (!r.ok) throw new Error((v && v.detail) || r.statusText);
  return v;
}
// Plots are optional: without Plotly the page still plans, runs and lists rows, and says why there is no figure.
function plot(div, traces, layout) {
  if (!window.Plotly) { $(div).textContent = 'Figure unavailable: plotly.min.js did not load (pip install plotly).'; return; }
  Plotly.react(div, traces, layout, { responsive: true });
}
function purge(div) { if (window.Plotly) Plotly.purge(div); else $(div).textContent = ''; }
const fmt = (v) => {
  if (typeof v !== 'number') return v ?? '';
  if (Number.isInteger(v) && Math.abs(v) < 1e6) return String(v);
  const a = Math.abs(v);
  return a !== 0 && (a < 1e-3 || a >= 1e5) ? v.toExponential(3) : v.toPrecision(4);
};
const axis = (text, extra = {}) => ({ title: { text }, ...extra });   // plotly.js 3: titles are objects
const plotLayout = (extra) => Object.assign({
  margin: { l: 70, r: 20, t: 10, b: 50 }, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
  font: { color: getComputedStyle(document.body).color }, legend: { orientation: 'h', y: -0.2 } }, extra);

// ---------- tabs ----------
document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === b));
  document.querySelectorAll('.tab-page').forEach((p) => { p.hidden = p.id !== `tab-${b.dataset.tab}`; });
  if (b.dataset.tab === 'data') loadRoots();
}));

// ---------- config + options ----------
function currentCfg() {
  try { return JSON.parse($('cfg-text').value); } catch (e) { throw new Error(`Config JSON: ${e.message}`); }
}
function options() {
  const o = { only: $('opt-only').value.trim(), columns: $('opt-columns').value.trim(),
              L_values: $('opt-L').value.trim(), limit: $('opt-limit').value.trim(),
              origin: $('opt-origin').value.trim(), origin_offset: $('opt-offset').value.trim(),
              keep_order: $('opt-keep').checked };
  Object.keys(o).forEach((k) => { if (o[k] === '' || o[k] === false) delete o[k]; });
  return o;
}
function showErrors(list) { $('errors').innerHTML = list.map((e) => `<li>${escapeHtml(e)}</li>`).join(''); }
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

async function loadConfigList() {
  const names = await api('/api/configs');
  $('cfg-select').innerHTML = names.map((n) => `<option>${n}</option>`).join('');
  const pick = names.includes('lc500_idvg.json') ? 'lc500_idvg.json' : names[0];
  if (pick) { $('cfg-select').value = pick; await loadConfig(pick); }
}
async function loadConfig(name) {
  S.cfgName = name;
  $('cfg-text').value = JSON.stringify(await api(`/api/configs/${encodeURIComponent(name)}`), null, 2);
  invalidatePlan();
}
function invalidatePlan() { S.preview = null; $('btn-start').disabled = true; }
$('cfg-select').addEventListener('change', (e) => loadConfig(e.target.value));
['cfg-text', 'opt-only', 'opt-columns', 'opt-L', 'opt-limit', 'opt-origin', 'opt-offset', 'opt-keep', 'opt-live']
  .forEach((id) => $(id).addEventListener('input', invalidatePlan));
$('opt-live').addEventListener('change', () => {
  $('btn-start').textContent = $('opt-live').checked ? 'Start LIVE run' : 'Start dry-run';
  $('btn-start').classList.toggle('danger', $('opt-live').checked);
  $('btn-start').classList.toggle('primary', !$('opt-live').checked);
});

$('btn-preview').addEventListener('click', async () => {
  showErrors([]);
  try {
    const cfg = currentCfg();
    const v = await api('/api/validate', { method: 'POST', body: cfg });
    if (!v.ok) { showErrors(v.errors); invalidatePlan(); return; }
    S.preview = await api('/api/preview', { method: 'POST', body: { cfg, options: options(), live: $('opt-live').checked } });
    S.rows = []; renderRows();
    const p = S.preview;
    const steps = p.plan.reduce((a, d) => a + Math.hypot(d.dx, d.dy), 0);
    $('plan-summary').textContent = `${v.experiment} on ${v.smu_tool}: ${p.plan.length} devices to visit out of `
      + `${p.sites.length} on the map, origin ${p.origin}, total travel ${(steps / 1000).toFixed(2)} mm`
      + (p.measured.length ? `, ${p.measured.length} already measured (skipped)` : '') + '.';
    renderMap();
    $('btn-start').disabled = !p.plan.length || isActive();
  } catch (e) { showErrors([e.message]); invalidatePlan(); }
});

// ---------- map ----------
function renderMap() {
  const p = S.preview;
  if (!p) return;
  const done = {};
  S.rows.forEach((r) => { done[r.site] = r.row.status === 'measured' ? 'measured' : (r.row.status === 'failed' ? 'failed' : 'other'); });
  const bySite = Object.fromEntries(p.sites.map((s) => [s.site, s]));
  const pts = (filter) => p.sites.filter(filter);
  const sc = (list, name, color, extra = {}) => ({ type: 'scatter', mode: 'markers', name, x: list.map((s) => s.x),
    y: list.map((s) => s.y), text: list.map((s) => `${s.site}<br>L ${fmt(s.L)} µm`), hoverinfo: 'text',
    marker: { color, size: 8, ...extra } });
  const before = new Set(p.measured);
  const traces = [
    sc(pts((s) => !before.has(s.site)), 'device', COLORS.site, { symbol: 'circle-open' }),
    { type: 'scatter', mode: 'lines', name: 'planned path', hoverinfo: 'skip', line: { color: COLORS.path, width: 1 },
      x: [0, ...p.plan.map((d) => d.tx)], y: [0, ...p.plan.map((d) => d.ty)] },
  ];
  if (before.size) traces.push(sc(pts((s) => before.has(s.site)), 'measured earlier', COLORS.before));
  for (const k of ['measured', 'failed', 'other']) {
    const list = Object.keys(done).filter((s) => done[s] === k && bySite[s]).map((s) => bySite[s]);
    if (list.length) traces.push(sc(list, k === 'other' ? 'not measured (screened out)' : k, COLORS[k]));
  }
  traces.push(sc(pts((s) => s.origin), `origin ${p.origin} (probes start here)`, COLORS.origin, { symbol: 'star', size: 13 }));
  plot('map', traces, plotLayout({ xaxis: axis('stage x from origin (µm)', { zeroline: false }),
    yaxis: axis('stage y from origin (µm)', { scaleanchor: 'x', zeroline: false }) }));
}

// ---------- runs ----------
const isActive = () => ['starting', 'running', 'awaiting_confirm', 'stopping'].includes($('run-state').textContent);
function setState(state) {
  const el = $('run-state'); el.textContent = state; el.className = `state ${state}`;
  $('btn-stop').disabled = !isActive();
  $('btn-start').disabled = isActive() || !S.preview || !S.preview.plan.length;
  if (state !== 'awaiting_confirm') $('confirm').hidden = true;
}
$('btn-start').addEventListener('click', async () => {
  showErrors([]);
  const live = $('opt-live').checked;
  if (live && !window.confirm('Start a LIVE run on the real instruments? You will still confirm the landing and the alignment.')) return;
  try {
    S.rows = []; renderRows(); $('log').textContent = '';
    await api('/api/runs', { method: 'POST', body: { cfg: currentCfg(), options: options(), live, config_name: S.cfgName } });
    renderMap();
  } catch (e) { showErrors([e.message]); }
});
$('btn-stop').addEventListener('click', () => api('/api/runs/stop', { method: 'POST' }).catch((e) => showErrors([e.message])));
$('btn-confirm').addEventListener('click', async () => {
  try { await api('/api/runs/confirm', { method: 'POST', body: { text: $('confirm-text').value } }); $('confirm-text').value = ''; }
  catch (e) { showErrors([e.message]); }
});
$('btn-decline').addEventListener('click', () => api('/api/runs/decline', { method: 'POST' }).catch((e) => showErrors([e.message])));

function onEvent(e) {
  S.seq = Math.max(S.seq, e.seq);
  if (e.type === 'log') {
    const log = $('log'); const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    log.textContent += e.line + '\n'; if (atEnd) log.scrollTop = log.scrollHeight;
  } else if (e.type === 'state') {
    if (e.state === 'starting') { S.rows = []; renderRows(); $('log').textContent = ''; }
    setState(e.state);
    if (e.error) showErrors([e.error]);
  } else if (e.type === 'confirm') {
    $('confirm').hidden = false; $('confirm-prompt').textContent = e.prompt; $('confirm-expect').textContent = e.expect;
    $('confirm-text').value = ''; $('confirm-text').focus();
  } else if (e.type === 'row') {
    S.rows.push(e); renderRows(); renderMap(); showLatest(e.row);
  }
}
function connectStream() {
  if (S.es) S.es.close();
  S.es = new EventSource(`/api/runs/stream?since=${S.seq}`);
  S.es.onmessage = (m) => onEvent(JSON.parse(m.data));
}

const ROW_FIRST = ['key', 'status', 'reason', 'W_um', 'L_um', 'Vt_V', 'on_off', 'flags'];
const ROW_SKIP = new Set(['time', 'sample', 'die_idx', 'x_um', 'y_um', 'col']);
function renderRows() {
  const rows = S.rows.map((r) => r.row);
  const extra = [...new Set(rows.flatMap((r) => Object.keys(r).filter((k) => r[k] !== '' && r[k] !== null)))]
    .filter((k) => !ROW_FIRST.includes(k) && !ROW_SKIP.has(k) && !k.endsWith('_file'));
  const cols = [...ROW_FIRST, ...extra];
  $('rows').innerHTML = rows.length ? `<tr>${cols.map((c) => `<th>${c}</th>`).join('')}</tr>` + rows.map((r) => `<tr>${
    cols.map((c) => `<td class="${c === 'status' ? (r.status === 'measured' ? 'measured' : r.status === 'failed' ? 'failed' : 'other') : ''}">${escapeHtml(fmt(r[c]))}</td>`).join('')}</tr>`).join('') : '';
}

// ---------- files shown from a row ----------
function rootOf(path) {
  const p = String(path).replace(/\\/g, '/').replace(/^\.\//, '');
  const roots = [...S.roots].sort((a, b) => b.length - a.length);
  for (const r of roots) if (p.startsWith(r + '/')) return [r, p.slice(r.length + 1)];
  return null;
}
async function showLatest(row) {
  const f = row.main_file || row.scan_file || Object.keys(row).filter((k) => k.endsWith('_file') && row[k]).map((k) => row[k])[0];
  if (!f) return;
  let rp = rootOf(f);
  if (!rp) { S.roots = await api('/api/data/roots'); rp = rootOf(f); }   // the dry-run folder appears with the first run
  if (!rp) return;
  try {
    const t = await api(`/api/data/table?root=${encodeURIComponent(rp[0])}&path=${encodeURIComponent(rp[1])}`);
    plotTable('latest', t, t.axes.x, t.axes.y, t.axes.logy, `${row.key}: ${t.name}`);
  } catch (e) { /* the curve is a convenience; the row is already shown */ }
}
function plotTable(div, t, x, ys, logy, title) {
  if (!x || !ys.length) { purge(div); return; }
  const traces = ys.map((y) => ({ type: 'scattergl', mode: 'lines+markers', name: logy ? `|${y}|` : y, x: t.data[x],
    y: logy ? t.data[y].map((v) => (v === null ? null : Math.abs(v))) : t.data[y], marker: { size: 3 } }));
  plot(div, traces, plotLayout({ title: { text: title, font: { size: 12 } }, margin: { l: 70, r: 20, t: 30, b: 50 },
    xaxis: axis(unitLabel(x)),
    yaxis: axis(ys.length === 1 ? unitLabel(ys[0]) : (ys.some((y) => /Current/.test(y)) ? 'current (A)' : ''),
                { type: logy ? 'log' : 'linear', exponentformat: 'e' }) }));
}
const unitLabel = (c) => (/Voltage|_V$/.test(c) ? `${c} (V)` : /Current|_A$/.test(c) ? `${c} (A)` : /time|_s$/i.test(c) ? `${c} (s)` : c);

// ---------- data tab ----------
async function loadRoots() {
  S.roots = await api('/api/data/roots');
  const sel = $('data-root');
  if (!sel.options.length || sel.options.length !== S.roots.length) {
    sel.innerHTML = S.roots.map((r) => `<option>${r}</option>`).join('');
    if (S.roots.length) browse(S.roots[0], '');
  }
}
$('data-root').addEventListener('change', (e) => browse(e.target.value, ''));
async function browse(root, path) {
  const l = await api(`/api/data/list?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`);
  const parts = path ? path.split('/') : [];
  $('crumbs').innerHTML = [`<a data-p="">${root}</a>`, ...parts.map((p, i) => `<a data-p="${parts.slice(0, i + 1).join('/')}">${p}</a>`)].join(' / ');
  $('crumbs').querySelectorAll('a').forEach((a) => a.addEventListener('click', () => browse(root, a.dataset.p)));
  const j = (n) => (path ? `${path}/${n}` : n);
  $('listing').innerHTML = l.dirs.map((d) => `<li>📁 <a data-d="${j(d)}">${d}</a></li>`).join('')
    + l.files.map((f) => `<li><a data-f="${j(f.name)}">${f.name}</a> <span class="muted">${(f.size / 1024).toFixed(1)} kB</span></li>`).join('');
  $('listing').querySelectorAll('a[data-d]').forEach((a) => a.addEventListener('click', () => browse(root, a.dataset.d)));
  $('listing').querySelectorAll('a[data-f]').forEach((a) => a.addEventListener('click', () => openFile(root, a.dataset.f)));
}
async function openFile(root, path) {
  const q = `root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`;
  $('file-title').textContent = path; $('file-img').hidden = true; $('file-text').hidden = true; $('axes').hidden = true;
  $('file-table').innerHTML = ''; purge('file-plot');
  if (path.endsWith('.png')) { $('file-img').src = `/api/data/image?${q}`; $('file-img').hidden = false; return; }
  if (path.endsWith('.log')) {
    const r = await fetch(`/api/data/log?${q}`); $('file-text').textContent = await r.text(); $('file-text').hidden = false; return;
  }
  const t = await api(`/api/data/table?${q}`);
  const opt = (c) => `<option>${c}</option>`;
  $('ax-x').innerHTML = t.numeric.map(opt).join(''); $('ax-y').innerHTML = t.numeric.map(opt).join('');
  if (t.axes.x) $('ax-x').value = t.axes.x;
  [...$('ax-y').options].forEach((o) => { o.selected = t.axes.y.includes(o.value); });
  $('ax-logy').checked = t.axes.logy; $('axes').hidden = !t.numeric.length;
  const draw = () => plotTable('file-plot', t, $('ax-x').value, [...$('ax-y').selectedOptions].map((o) => o.value), $('ax-logy').checked, t.name);
  ['ax-x', 'ax-y', 'ax-logy'].forEach((id) => { $(id).onchange = draw; });
  draw();
  const n = Math.min(t.n_rows, 200);
  $('file-table').innerHTML = `<tr>${t.columns.map((c) => `<th>${c}</th>`).join('')}</tr>`
    + Array.from({ length: n }, (_, i) => `<tr>${t.columns.map((c) => `<td>${escapeHtml(fmt(t.data[c][i]))}</td>`).join('')}</tr>`).join('')
    + (t.n_rows > n ? `<tr><td colspan="${t.columns.length}" class="muted">first ${n} of ${t.n_rows} rows</td></tr>` : '');
}

// ---------- start ----------
(async function init() {
  S.health = await api('/api/health');
  const m = $('mode');
  m.textContent = S.health.allow_live ? 'LIVE RUNS ALLOWED on this PC' : `Dry-run only (fake instruments, data in ${S.health.dryrun_data_dir})`;
  m.classList.toggle('live', S.health.allow_live);
  $('opt-live').disabled = !S.health.allow_live;
  S.roots = await api('/api/data/roots');
  await loadConfigList();
  const st = await api('/api/runs/status');
  setState(st.state);
  connectStream();
})().catch((e) => showErrors([e.message]));
