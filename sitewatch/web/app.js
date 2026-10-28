'use strict';
const $ = id => document.getElementById(id);
let snapshot = null, payload = null, filter = 'all', query = '', currentView = 'overview', updating = false;
const node = (tag, text, className) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; };
const badge = (text, kind = 'neutral') => node('span', text, `badge ${kind}`);
const bytes = n => typeof n === 'number' ? `${(n / 1073741824).toFixed(1)} GB` : 'Unknown';
const date = value => { const d = new Date(value); return Number.isNaN(d.getTime()) ? 'Unknown' : d.toLocaleString(); };
function updates(site) {
  const items = [];
  if (site.core?.update_version) items.push({...site.core, name: 'WordPress core', type: 'core'});
  for (const type of ['plugins', 'themes']) for (const item of site[type] || []) if (item.update_version) items.push({...item, type: type === 'plugins' ? 'plugin' : 'theme'});
  return items;
}
function attention(site) { return site.state !== 'ok' || !!site.errors?.length; }
function healthy(site) { return site.health?.length > 0 && site.health.every(h => h.ok); }
function cell(child) { const td = node('td'); td.append(child); return td; }
function renderSites() {
  const sites = (snapshot?.sites || []).filter(s => s.id.toLowerCase().includes(query) && (filter === 'all' || (filter === 'updates' ? updates(s).length : attention(s))));
  const body = $('sites'); body.replaceChildren();
  for (const site of sites) {
    const tr = node('tr'); tr.dataset.site = site.id;
    const identity = node('div', undefined, 'site-cell');
    identity.append(node('span', site.id[0].toUpperCase(), 'site-avatar'));
    const label = node('div'); label.append(node('div', site.id, 'site-name'), node('div', site.kind === 'wordpress' ? 'WordPress' : 'Web application', 'site-kind')); identity.append(label); tr.append(cell(identity));
    const first = site.health?.[0];
    tr.append(cell(badge(healthy(site) ? `${first?.response_ms ?? '—'} ms` : (site.health?.length ? 'Check failed' : 'Unknown'), healthy(site) ? 'good' : 'warn')));
    tr.append(cell(node('span', site.kind === 'wordpress' ? site.core?.version || 'Unknown' : '—')));
    const count = updates(site).length;
    tr.append(cell(badge(count ? `${count} available` : 'None reported', count ? 'warn' : 'neutral')));
    const flag = site.flags?.DISALLOW_FILE_MODS;
    tr.append(cell(badge(site.kind !== 'wordpress' ? '—' : flag === true ? 'Locked' : flag === false ? 'Allowed' : 'Unknown', flag === true ? 'good' : 'neutral')));
    const inspect = node('button', '↗', 'filter'); inspect.setAttribute('aria-label', `Inspect ${site.id}`); inspect.addEventListener('click', e => {e.stopPropagation(); showDetail(site);});
    tr.append(cell(inspect)); tr.addEventListener('click', () => showDetail(site)); body.append(tr);
  }
  if (!sites.length) { const tr = node('tr'); const td = node('td', snapshot ? 'No sites match this view.' : 'No snapshot available. Check your file or SSH configuration.', 'empty'); td.colSpan = 6; tr.append(td); body.append(tr); }
  $('table-count').textContent = sites.length;
  $('table-foot').textContent = `Showing ${sites.length} of ${snapshot?.sites.length || 0} sites`;
}
function meter(parent, label, value, ratio) {
  const line = node('div', undefined, 'server-line'); line.append(node('span', label), node('strong', value)); parent.append(line);
  if (Number.isFinite(ratio)) {const bar = node('div', undefined, 'meter'), fill = node('div', undefined, 'meter-fill'); fill.style.width = `${Math.max(0, Math.min(100, ratio * 100))}%`; bar.append(fill); parent.append(bar);}
}
function phpText(s) {
  const r = s.php_release;
  if (!s.php_version) return 'Unknown';
  if (!r) return `${s.php_version} · latest unknown`;
  return r.update_available ? `${s.php_version} · ${r.latest} available${r.security_release ? ' (security)' : ''}` : `${s.php_version} · current`;
}
function aptText(apt) {
  if (!apt) return 'Unknown';
  if (!apt.upgradable) return 'None pending';
  return `${apt.upgradable} pending${apt.security ? ` · ${apt.security} security` : ''}`;
}
function serverUpdates(s) {
  const items = [];
  if (s?.php_release?.update_available) items.push({name: 'PHP', type: 'runtime', source: 'php.net', version: s.php_version, update_version: s.php_release.latest});
  for (const p of s?.apt?.packages || []) items.push({name: p.name, type: p.security ? 'security package' : 'package', source: 'apt', version: p.from, update_version: p.version});
  return items;
}
function renderServer() {
  const s = snapshot?.server || {}, disk = s.disk, memory = s.memory;
  $('server-name').textContent = s.hostname || 'Server health';
  for (const id of ['server-compact', 'server-details']) {
    const parent = $(id); parent.replaceChildren(); parent.classList.add('server-lines');
    meter(parent, 'Disk usage', disk ? `${bytes(disk.used_bytes)} / ${bytes(disk.total_bytes)}` : 'Unknown', disk ? disk.used_bytes / disk.total_bytes : undefined);
    meter(parent, 'Memory usage', memory ? `${bytes(memory.total_bytes - memory.available_bytes)} / ${bytes(memory.total_bytes)}` : 'Unknown', memory ? (memory.total_bytes - memory.available_bytes) / memory.total_bytes : undefined);
    meter(parent, 'PHP runtime', phpText(s));
    meter(parent, 'OS updates', aptText(s.apt));
    if (id === 'server-details') {
      meter(parent, 'Load · 1 / 5 / 15 minutes', s.load?.map(v => Number(v).toFixed(2)).join(' / ') || 'Unknown');
      meter(parent, 'Failed systemd units', Array.isArray(s.failed_units) ? s.failed_units.length ? s.failed_units.join(', ') : 'None' : 'Unknown');
      meter(parent, 'Package lists refreshed', s.apt ? (s.apt.lists_refreshed_at ? date(s.apt.lists_refreshed_at) : 'No successful apt update recorded') : 'Unknown');
      if (s.apt && !s.apt.lists_refreshed_at) parent.append(node('p', 'Pending updates are only as current as the last successful apt update. Enable apt-daily.timer or run apt update.', 'detail-error'));
      if (s.php_release?.branch_supported === false) parent.append(node('p', `PHP ${s.php_release.branch} no longer receives security fixes from php.net.`, 'detail-error'));
      for (const error of s.errors || []) parent.append(node('p', error, 'detail-error'));
      parent.append(node('p', 'Mail, certificates, firewall, FPM and remote backup verification are planned for a later collector release.', 'detail-note'));
    }
  }
}
const RESULT_BADGES = {updated: ['Updated', 'good'], unchanged: ['No change', 'neutral'], refused: ['Refused', 'neutral'], rolled_back: ['Rolled back', 'warn'], rollback_failed: ['Rollback failed', 'bad']};
function canUpdate(item) { return !!payload?.capabilities?.updates && ['plugin', 'theme', 'core'].includes(item.type); }
function itemLabel(r) { return r.kind === 'core' ? 'WordPress core' : r.kind === 'translations' ? 'Translations' : r.slug; }
function showBanner(text, error) { $('banner').textContent = text; $('banner').hidden = false; $('banner').classList.toggle('error', error); $('banner').scrollIntoView({block: 'nearest', behavior: 'smooth'}); }
async function runUpdate(site, item) {
  const what = item.type === 'core' ? 'WordPress core' : item.name;
  if (!confirm(`Update ${what} on ${site.id} from ${item.version || '?'} to ${item.update_version}?\n\nA backup of its files and the database is taken first. If the site fails its health check afterwards, the backup is restored automatically.`)) return;
  updating = true; renderUpdates();
  showBanner(`Updating ${what} on ${site.id}. This can take a few minutes; keep this page open.`, false);
  try {
    const response = await fetch('/api/update', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Site-Watch-Token': payload.refresh_token},
      body: JSON.stringify({site: site.id, kind: item.type, slug: item.type === 'core' ? null : item.name})});
    const data = await response.json();
    if ('status' in data) render(data);
    const r = data.results?.[data.results.length - 1];
    showBanner(r ? `${site.id} · ${what}: ${r.message}.${r.result === 'updated' ? ' The inventory refreshes within a minute.' : ''}` : data.error || 'The update request failed.', !r || !['updated', 'unchanged'].includes(r.result));
  } catch (_) {
    showBanner('The update request failed. Check the update log before trying again.', true);
  } finally {
    updating = false; renderUpdates(); setTimeout(() => load(true), 60000);
  }
}
function renderHistory() {
  const list = $('update-history'); list.replaceChildren();
  const history = payload?.history || [];
  $('history-panel').hidden = !payload?.capabilities?.updates && !history.length;
  for (const r of history) {
    const row = node('div', undefined, 'update-item'), name = node('div', itemLabel(r));
    name.append(node('small', `${r.site} · ${date(r.time)} · ${r.user || 'unknown user'}`), node('small', r.message));
    const [text, kind] = RESULT_BADGES[r.result] || [r.result, 'neutral'], side = node('div', undefined, 'update-actions');
    if (r.from && r.to && r.from !== r.to) side.append(node('span', `${r.from} → ${r.to}`, 'version-change'));
    side.append(badge(text, kind)); row.append(name, side); list.append(row);
  }
  if (!history.length) list.append(node('p', 'No updates have been run from Site Watch yet.', 'detail-note'));
}
function renderUpdates() {
  const list = $('update-list'); list.replaceChildren();
  for (const site of snapshot?.sites || []) for (const item of updates(site)) {
    const row = node('div', undefined, 'update-item'), name = node('div', item.name), side = node('div', undefined, 'update-actions');
    name.append(node('small', `${site.id} · ${item.type} · ${item.source || 'wordpress.org'}`));
    side.append(node('span', `${item.version || '?'} → ${item.update_version}`, 'version-change'));
    if (canUpdate(item)) {
      const button = node('button', 'Update', 'filter update-button'); button.disabled = updating || !!payload.update_running;
      button.setAttribute('aria-label', `Update ${item.name} on ${site.id}`); button.addEventListener('click', () => runUpdate(site, item)); side.append(button);
    }
    row.append(name, side); list.append(row);
  }
  for (const item of serverUpdates(snapshot?.server)) {
    const row = node('div', undefined, 'update-item'), name = node('div', item.name);
    name.append(node('small', `${snapshot.server.hostname || 'server'} · ${item.type} · ${item.source}`));
    row.append(name, node('span', `${item.version || '?'} → ${item.update_version}`, 'version-change')); list.append(row);
  }
  if (!list.children.length) list.append(node('p', 'No updates reported in this snapshot. Unavailable or unmapped vendor checks may still need attention.', 'detail-note'));
}
function showDetail(site) {
  $('detail-title').textContent = site.id; const body = $('detail-body'); body.replaceChildren();
  const flags = node('div', undefined, 'detail-flags');
  for (const [name, value] of Object.entries(site.flags || {})) flags.append(badge(`${name}: ${value === null ? 'unknown' : String(value)}`, value === null ? 'warn' : 'neutral'));
  body.append(flags);
  body.append(node('h3', 'Origin checks'));
  for (const h of site.health || []) {const row = node('div', undefined, 'update-item'); row.append(node('span', h.path), badge(`${h.status || 'Failed'} · ${h.response_ms ?? '—'} ms`, h.ok ? 'good' : 'bad')); body.append(row);}
  for (const key of ['plugins', 'themes']) {
    if (!(site[key] || []).length) continue;
    body.append(node('h3', `${key === 'plugins' ? 'Plugins' : 'Themes'} (${site[key].length})`));
    const table = node('table', undefined, 'detail-table');
    const head = node('tr'); for (const title of ['COMPONENT', 'INSTALLED', 'UPDATE', 'SOURCE']) head.append(node('th', title)); table.append(head);
    for (const item of site[key]) {const row = node('tr'); row.append(cell(node('span', item.name)), cell(node('span', `${item.version || '?'} · ${item.status}`)), cell(badge(item.update_version || (item.check_state === 'unknown' ? 'Unknown' : 'None reported'), item.update_version ? 'warn' : 'neutral')), cell(node('span', item.source || 'wordpress.org'))); table.append(row);}
    const scroll = node('div', undefined, 'table-scroll'); scroll.append(table); body.append(scroll);
  }
  body.append(node('h3', 'Backup visibility'));
  const backup = site.backup;
  body.append(node('p', backup?.state === 'observed' ? `Local archive observed ${date(backup.last_modified)} · ${bytes(backup.size_bytes)}. Restore has not been verified.` : 'Backup source not configured or unavailable. This is not a confirmed backup.', 'detail-note'));
  for (const error of site.errors || []) body.append(node('p', error, 'detail-error'));
  for (const note of site.notes || []) body.append(node('p', note, 'detail-note'));
  $('detail').showModal();
}
function render(data) {
  payload = data; snapshot = data.status;
  const sites = snapshot?.sites || [], count = sites.reduce((n,s) => n + updates(s).length,0), wp = sites.filter(s => s.kind === 'wordpress').length;
  $('total').textContent = snapshot ? sites.length : '—'; $('updates').textContent = snapshot ? count : '—'; $('healthy').textContent = snapshot ? `${sites.filter(healthy).length}/${sites.length}` : '—'; $('attention').textContent = snapshot ? sites.filter(attention).length : '—'; $('nav-count').textContent = count;
  $('site-breakdown').textContent = `${wp} WordPress · ${sites.length - wp} web applications`;
  $('collected').textContent = snapshot ? `Collected ${date(snapshot.collected_at)}` : 'No snapshot available'; $('source').textContent = data.source;
  $('connection').textContent = snapshot?.demo ? 'Demo workspace' : data.error ? 'Connection needs attention' : data.freshness?.stale ? 'Snapshot is stale' : 'Snapshot available';
  const messages = [];
  if (snapshot?.demo) messages.push('DEMO WORKSPACE · All versions, checks and metrics shown here are sample data.');
  if (data.error) messages.push(`${data.error}${snapshot ? ' Showing the last successful snapshot.' : ''}`);
  if (data.freshness?.stale) messages.push(data.freshness.clock_skew ? 'Snapshot time is in the future. Check the server clock.' : `Snapshot is stale since ${date(snapshot.collected_at)}. Refresh reads the latest file; it does not trigger the server collector.`);
  $('banner').textContent = messages.join(' '); $('banner').hidden = !messages.length; $('banner').classList.toggle('error', !!data.error);
  const enabled = !!data.capabilities?.updates;
  $('update-mode').textContent = enabled ? 'Updates enabled' : 'Read only'; $('version-mode').textContent = enabled ? 'VERSION 0.2 · UPDATES ON' : 'VERSION 0.2 · READ ONLY';
  $('update-note').textContent = enabled ? 'Enabled for this dashboard. Open Updates to run one.' : 'Read only. Set "updates": true in the viewer config once site-update is installed on the server.';
  renderSites(); renderServer(); renderUpdates(); renderHistory();
}
async function load(refresh = false) {
  $('refresh').disabled = true;
  try {
    const options = refresh ? {method:'POST', headers:{'X-Site-Watch-Token':payload.refresh_token}} : {};
    const response = await fetch(refresh ? '/api/refresh' : '/api/status', options);
    if (!response.ok) throw new Error('Request failed'); render(await response.json());
  } catch (_) {$('banner').textContent = 'The local dashboard is unavailable. Check that the Python app is still running.'; $('banner').hidden = false; $('banner').classList.add('error');}
  finally {$('refresh').disabled = false;}
}
$('refresh').addEventListener('click', () => load(!!payload));
$('search').addEventListener('input', e => {query = e.target.value.toLowerCase(); renderSites();});
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {filter = button.dataset.filter; document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('selected', b === button)); renderSites();}));
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {currentView = button.dataset.view; for (const id of ['overview','updates','server']) $(`${id}-view`).hidden = id !== currentView; document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b === button)); $('breadcrumb').textContent = {overview:'Overview',updates:'Updates',server:'Server health'}[currentView]; $('page-title').textContent = {overview:'A clear view of your sites.',updates:'Know what needs updating.',server:'Keep an eye on your server.'}[currentView];}));
$('close-detail').addEventListener('click', () => $('detail').close());
load();
setInterval(() => load(), 60000);
