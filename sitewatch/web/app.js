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
    tr.append(cell(nightlyControl(site)));
    const inspect = node('button', '↗', 'filter'); inspect.setAttribute('aria-label', `Inspect ${site.id}`); inspect.addEventListener('click', e => {e.stopPropagation(); showDetail(site);});
    tr.append(cell(inspect)); tr.addEventListener('click', () => showDetail(site)); body.append(tr);
  }
  if (!sites.length) { const tr = node('tr'); const td = node('td', snapshot ? 'No sites match this view.' : 'No snapshot available. Check your file or SSH configuration.', 'empty'); td.colSpan = 7; tr.append(td); body.append(tr); }
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
const selected = new Set();
const key = (site, item) => `${site.id}|${item.type}|${item.name}`;
const label = item => item.type === 'core' ? 'WordPress core' : item.name;
async function postUpdate(site, item, restoreDatabase) {
  // One update; returns its result (or {result:'failed', message}) and re-renders with the reply.
  try {
    const response = await fetch('/api/update', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Site-Watch-Token': payload.refresh_token},
      body: JSON.stringify({site: site.id, kind: item.type, slug: item.type === 'core' ? null : item.name, restore_database: item.type === 'core' ? true : restoreDatabase})});
    const data = await response.json();
    if ('status' in data) render(data);
    return data.results?.[data.results.length - 1] || {result: 'failed', message: data.error || 'The update request failed'};
  } catch (_) {
    return {result: 'failed', message: 'The update request failed. Check the update log before trying again'};
  }
}
const ok = r => ['updated', 'unchanged'].includes(r.result);
async function runUpdate(site, item) {
  // Core always restores the database: update-db upgrades it, and old core files on it are no rollback.
  const answer = await ask({title: `Update ${label(item)} on ${site.id}?`, ok: 'Update', option: item.type !== 'core',
    text: `${item.version || '?'} → ${item.update_version}. A backup of its files and the database is taken first. If the site fails its health check afterwards, the backup is restored automatically.`});
  if (!answer) return;
  updating = true; renderUpdates();
  showBanner(`Updating ${label(item)} on ${site.id}. This can take a few minutes; keep this page open.`, false);
  const r = await postUpdate(site, item, answer.restoreDatabase);
  selected.delete(key(site, item));
  updating = false; renderUpdates();
  showBanner(`${site.id} · ${label(item)}: ${r.message}.${r.result === 'updated' ? ' The inventory refreshes within a minute.' : ''}`, !ok(r));
  setTimeout(() => load(true), 60000);
}
function selectedItems() {
  return grouped().flatMap(([, , entries]) => entries).filter(({site, item}) => selected.has(key(site, item)));
}
async function runSelected() {
  const chosen = selectedItems();
  if (!chosen.length) return;
  const sites = new Set(chosen.map(({site}) => site.id));
  const answer = await ask({title: `Update ${chosen.length} item${chosen.length > 1 ? 's' : ''} on ${sites.size} site${sites.size > 1 ? 's' : ''}?`, ok: 'Update all',
    option: chosen.some(({item}) => item.type !== 'core'),
    text: 'They run one after another, each with its own backup, health check and automatic rollback. If one fails on a site, the rest of that site’s selection is skipped; other sites continue. Keep this page open.'});
  if (!answer) return;
  updating = true; renderUpdates();
  const done = [], failed = new Set();
  for (const [index, {site, item}] of chosen.entries()) {
    if (failed.has(site.id)) { done.push({site, item, r: {result: 'skipped'}}); continue; }
    showBanner(`Updating ${index + 1} of ${chosen.length}: ${label(item)} on ${site.id}…`, false);
    const r = await postUpdate(site, item, answer.restoreDatabase);
    selected.delete(key(site, item));
    if (!ok(r)) failed.add(site.id);
    done.push({site, item, r});
  }
  updating = false; renderUpdates();
  const count = name => done.filter(d => d.r.result === name).length;
  const problems = done.filter(d => !ok(d.r) && d.r.result !== 'skipped').map(d => `${label(d.item)} on ${d.site.id}: ${d.r.message}`);
  const summary = [`${count('updated')} updated`, count('unchanged') && `${count('unchanged')} unchanged`, count('rolled_back') && `${count('rolled_back')} rolled back`,
    count('rollback_failed') && `${count('rollback_failed')} ROLLBACK FAILED`, count('refused') && `${count('refused')} refused`, count('failed') && `${count('failed')} failed`,
    count('skipped') && `${count('skipped')} skipped`].filter(Boolean).join(', ');
  showBanner(`${summary}.${problems.length ? ' ' + problems.join(' · ') + '.' : ''} The inventory refreshes within a minute.`, !!problems.length);
  setTimeout(() => load(true), 60000);
}
function ask({title, text, ok, option = false}) {
  // One dialog for confirmations; resolves to null (cancel) or {restoreDatabase}.
  return new Promise(resolve => {
    const dialog = $('confirm'), box = $('confirm-restore-db');
    $('confirm-title').textContent = title; $('confirm-text').textContent = text; $('confirm-ok').textContent = ok;
    $('confirm-option').hidden = !option; box.checked = true;
    const finish = value => { dialog.close(); $('confirm-ok').onclick = $('confirm-cancel').onclick = dialog.oncancel = null; resolve(value); };
    $('confirm-ok').onclick = () => finish({restoreDatabase: box.checked});
    $('confirm-cancel').onclick = () => finish(null);
    dialog.oncancel = e => { e.preventDefault(); finish(null); };
    dialog.showModal();
  });
}
function nightlyOn(site) { return !!payload?.nightly?.[site.id]?.enabled; }
function nightlyControl(site) {
  if (site.kind !== 'wordpress') return node('span', '—');
  const on = nightlyOn(site), unknown = payload?.nightly == null;
  const label = unknown ? 'Unknown' : on ? 'On' : 'Off';
  if (!payload?.capabilities?.updates) return badge(label, on ? 'good' : unknown ? 'warn' : 'neutral');
  const button = node('button', label, `filter nightly-toggle${on ? ' on' : ''}`);
  button.setAttribute('aria-pressed', String(on)); button.setAttribute('aria-label', `Nightly updates for ${site.id}: ${label.toLowerCase()}`);
  button.addEventListener('click', e => { e.stopPropagation(); setNightly(site, !on); });
  return button;
}
async function setNightly(site, enabled) {
  const answer = await ask(enabled
    ? {title: `Turn on nightly updates for ${site.id}?`, ok: 'Turn on',
       text: 'Every night at about 03:30–03:50 the plugin and theme updates for this site, and new releases of the installed WordPress branch (for example 6.8.2 to 6.8.3, which is how security fixes ship), are installed one at a time, each with a backup, a health check and automatic rollback. A new WordPress major version stays a manual update. Results appear under Recent updates.'}
    : {title: `Turn off nightly updates for ${site.id}?`, ok: 'Turn off', text: 'Updates for this site will only run when you press Update.'});
  if (!answer) return;
  try {
    const response = await fetch('/api/nightly', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Site-Watch-Token': payload.refresh_token},
      body: JSON.stringify({site: site.id, enabled})});
    const data = await response.json();
    if ('status' in data) render(data);
    showBanner(response.ok ? `Nightly updates for ${site.id} are ${enabled ? 'on' : 'off'}.` : data.error || 'The change failed.', !response.ok);
  } catch (_) {
    showBanner('The change failed. Check the SSH connection.', true);
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
const GROUPS = [['core', 'WordPress core'], ['plugin', 'Plugins'], ['theme', 'Themes']];
function grouped() {
  return GROUPS.map(([type, title]) => [type, title, (snapshot?.sites || []).flatMap(site => updates(site).filter(item => item.type === type).map(item => ({site, item})))]);
}
function checkbox(checked, labelText, onChange) {
  const box = node('input'); box.type = 'checkbox'; box.checked = checked; box.disabled = updating || !!payload?.update_running;
  box.setAttribute('aria-label', labelText); box.addEventListener('change', () => onChange(box.checked)); return box;
}
function renderUpdates() {
  const list = $('update-list'); list.replaceChildren();
  const enabled = !!payload?.capabilities?.updates, busy = updating || !!payload?.update_running;
  for (const [type, title, entries] of grouped()) {
    if (!entries.length) continue;
    const head = node('div', undefined, 'update-group-head'), heading = node('h3', `${title} `);
    heading.append(node('span', String(entries.length), 'count'));
    if (enabled) {
      const all = entries.every(({site, item}) => selected.has(key(site, item)));
      const box = checkbox(all, `Select all ${title.toLowerCase()}`, on => { for (const {site, item} of entries) on ? selected.add(key(site, item)) : selected.delete(key(site, item)); renderUpdates(); });
      const pick = node('label', undefined, 'select-all'); pick.append(box, node('span', 'Select all')); head.append(heading, pick);
    } else head.append(heading);
    list.append(head);
    for (const {site, item} of entries) {
      const row = node('div', undefined, 'update-item'), name = node('div', undefined, 'update-name'), side = node('div', undefined, 'update-actions');
      if (enabled) name.append(checkbox(selected.has(key(site, item)), `Select ${label(item)} on ${site.id}`, on => { on ? selected.add(key(site, item)) : selected.delete(key(site, item)); renderSelection(); }));
      const text = node('div', label(item)); text.append(node('small', `${site.id} · ${item.source || 'wordpress.org'}`)); name.append(text);
      side.append(node('span', `${item.version || '?'} → ${item.update_version}`, 'version-change'));
      if (canUpdate(item)) {
        const button = node('button', 'Update', 'filter update-button'); button.disabled = busy;
        button.setAttribute('aria-label', `Update ${label(item)} on ${site.id}`); button.addEventListener('click', () => runUpdate(site, item)); side.append(button);
      }
      row.append(name, side); list.append(row);
    }
  }
  const server = serverUpdates(snapshot?.server);
  if (server.length) {
    const head = node('div', undefined, 'update-group-head'), heading = node('h3', 'Server packages ');
    heading.append(node('span', String(server.length), 'count')); head.append(heading, node('span', 'Installed by Ubuntu, not from here', 'detail-note')); list.append(head);
    for (const item of server) {
      const row = node('div', undefined, 'update-item'), name = node('div', item.name);
      name.append(node('small', `${snapshot.server.hostname || 'server'} · ${item.type} · ${item.source}`));
      row.append(name, node('span', `${item.version || '?'} → ${item.update_version}`, 'version-change')); list.append(row);
    }
  }
  if (!list.children.length) list.append(node('p', 'No updates reported in this snapshot. Unavailable or unmapped vendor checks may still need attention.', 'detail-note'));
  // Forget selections whose update is no longer reported.
  const present = new Set(grouped().flatMap(([, , entries]) => entries.map(({site, item}) => key(site, item))));
  for (const k of [...selected]) if (!present.has(k)) selected.delete(k);
  renderSelection();
}
function renderSelection() {
  const bar = $('selection-bar'), enabled = !!payload?.capabilities?.updates;
  bar.hidden = !enabled;
  $('selection-count').textContent = selected.size ? `${selected.size} selected` : 'Select updates to run several in one go';
  $('run-selected').disabled = !selected.size || updating || !!payload?.update_running;
  $('clear-selected').disabled = !selected.size || updating;
}
function showDetail(site) {
  $('detail-title').textContent = site.id; const body = $('detail-body'); body.replaceChildren();
  const flags = node('div', undefined, 'detail-flags');
  for (const [name, value] of Object.entries(site.flags || {})) flags.append(badge(`${name}: ${value === null ? 'unknown' : String(value)}`, value === null ? 'warn' : 'neutral'));
  body.append(flags);
  if (site.kind === 'wordpress') {
    const setting = payload?.nightly?.[site.id];
    body.append(node('p', payload?.nightly == null ? 'Nightly updates: unknown (the switch file could not be read).' : setting?.enabled ? `Nightly updates are on (since ${date(setting.changed_at)}${setting.by ? `, ${setting.by}` : ''}).` : 'Nightly updates are off.', 'detail-note'));
  }
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
$('run-selected').addEventListener('click', runSelected);
$('clear-selected').addEventListener('click', () => { selected.clear(); renderUpdates(); });
load();
setInterval(() => load(), 60000);
