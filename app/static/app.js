/* Slipcast dashboard — vanilla JS client.
 * No eval / no Function() so the strict CSP (script-src 'self') stays intact.
 * All data comes from /api/state; all mutations go through the JSON API. */

'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ---- tiny DOM builder ---- */
function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  (Array.isArray(children) ? children : [children]).forEach((c) => {
    if (c == null || c === false) return;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return node;
}

/* ---- client state ---- */
const state = {
  data: null,
  selected: new Set(),     // selected subscribed-channel urls
  search: '',
  sort: 'added',
  seenJobs: new Set(),     // job ids already toasted
  primed: false,           // suppress toasts for jobs that existed at first load
  epChannel: null,         // channel whose episode modal is open (for re-render)
  fsChannel: null,         // channel whose feed-settings modal is open
};

/* ---- toasts ---- */
function toast(message, kind = 'info') {
  const icon = kind === 'ok' ? '✓' : kind === 'err' ? '!' : 'ℹ';
  const t = el('div', { class: `toast ${kind}`, role: 'status' }, [
    el('span', { class: 't-icon', text: icon }),
    el('span', { text: message }),
  ]);
  $('#toaster').appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    t.addEventListener('animationend', () => t.remove(), { once: true });
  }, kind === 'err' ? 6000 : 3800);
}

/* ---- API helpers ---- */
async function readError(res) {
  try { const j = await res.json(); return j.detail || j.message || res.statusText; }
  catch { return res.statusText || 'Request failed'; }
}

async function postForm(url, formData) {
  const res = await fetch(url, { method: 'POST', body: formData });
  if (!res.ok) throw new Error(await readError(res));
  return res.json().catch(() => ({}));
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json().catch(() => ({}));
}

function fd(obj) {
  const f = new FormData();
  for (const [k, v] of Object.entries(obj)) f.append(k, v);
  return f;
}

/* ---- state loading + polling ---- */
let pollTimer = null;

async function loadState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    state.data = await res.json();
    handleJobs(state.data.jobs || []);
    render();
  } catch { /* transient — next tick retries */ }
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  const active = (state.data?.jobs || []).some((j) => j.status === 'running');
  pollTimer = setTimeout(loadState, active ? 2000 : 9000);
}

function handleJobs(jobs) {
  for (const j of jobs) {
    if (j.status === 'running' || state.seenJobs.has(j.id)) continue;
    state.seenJobs.add(j.id);
    if (state.primed) toast(j.message || j.target, j.status === 'success' ? 'ok' : 'err');
  }
  state.primed = true;
}

/* ---- rendering ---- */
function avatar(name, thumb) {
  if (thumb) return el('span', { class: 'ch-avatar' }, [el('img', { src: thumb, alt: '', loading: 'lazy', onerror: (e) => { e.target.remove(); } })]);
  const letter = (name || '?').trim().charAt(0).toUpperCase();
  return el('span', { class: 'ch-avatar', 'aria-hidden': 'true', text: letter });
}

function epBadge(ch) {
  const n = ch.episodes;
  if (n === 0 || !ch.channel_id) {
    return el('span', { class: `ep-badge${n === 0 ? ' zero' : ''}`, text: n === 0 ? 'no episodes yet' : `${n} episode${n === 1 ? '' : 's'}` });
  }
  return el('button', {
    class: 'ep-badge link', type: 'button', title: 'View episodes',
    text: `${n} episode${n === 1 ? '' : 's'}`,
    onclick: () => openEpisodes(ch),
  });
}

function sizeBadge(ch) {
  if (!ch.bytes) return null;
  return el('span', { class: 'ep-badge', title: 'Audio + thumbnails on disk', text: fmtBytes(ch.bytes) });
}

function lastPollBadge(ch) {
  const lp = ch.last_poll;
  if (!lp) return null;
  const failed = lp.status === 'error';
  return el('span', {
    class: `poll-tag ${failed ? 'err' : 'ok'}`,
    title: failed ? (lp.error || 'Last poll failed') : `Last polled ${fmtAgo(lp.at)}`,
  }, [
    el('span', { class: `pr-dot ${failed ? 'err' : 'ok'}`, 'aria-hidden': 'true' }),
    el('span', { text: failed ? 'poll failed' : `polled ${fmtAgo(lp.at)}` }),
  ]);
}

function subscribedCard(ch) {
  const selected = state.selected.has(ch.url);
  const card = el('div', { class: `card ch-card${selected ? ' selected' : ''}` });

  const checkbox = el('input', {
    type: 'checkbox', 'aria-label': `Select ${ch.name}`,
    onchange: (e) => { e.target.checked ? state.selected.add(ch.url) : state.selected.delete(ch.url); render(); },
  });
  checkbox.checked = selected;
  card.appendChild(el('span', { class: 'ch-select' }, [checkbox]));

  card.appendChild(el('div', { class: 'ch-top' }, [
    avatar(ch.name, ch.thumbnail),
    el('div', { class: 'ch-meta' }, [
      el('div', { class: 'ch-name', title: ch.name, text: ch.name }),
      el('div', { class: 'ch-sub' }, [epBadge(ch), sizeBadge(ch), lastPollBadge(ch)]),
    ]),
  ]));

  const share = el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Share', onclick: () => openShare(ch) });
  if (!ch.feed_url) { share.disabled = true; share.title = 'Feed appears after the first successful poll'; }

  // Same gate as Share: the settings are keyed by channel_id, which only exists
  // once the channel has been polled successfully at least once.
  const settings = el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Feed settings', onclick: () => openFeedSettings(ch) });
  if (!ch.channel_id) { settings.disabled = true; settings.title = 'Available after the first successful poll'; }

  card.appendChild(el('div', { class: 'ch-actions' }, [
    share,
    settings,
    el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Poll', onclick: () => act(() => postForm('/channels/poll', fd({ url: ch.url }))) }),
    el('button', {
      class: 'btn btn-danger-ghost btn-sm', type: 'button', text: 'Remove',
      onclick: () => { if (confirm(`Remove ${ch.name}? Downloaded audio will be deleted.`)) act(() => postForm('/channels/remove', fd({ url: ch.url }))); },
    }),
  ]));
  return card;
}

function oneoffCard(ch) {
  return el('div', { class: 'card ch-card' }, [
    el('div', { class: 'ch-top' }, [
      avatar(ch.name, ch.thumbnail),
      el('div', { class: 'ch-meta' }, [
        el('div', { class: 'ch-name', title: ch.name, text: ch.name }),
        el('div', { class: 'ch-sub' }, [epBadge(ch), sizeBadge(ch)]),
      ]),
    ]),
    el('div', { class: 'ch-actions' }, [
      el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Share', onclick: () => openShare(ch) }),
      el('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Subscribe', onclick: () => act(() => postForm('/channels/subscribe', fd({ channel_id: ch.channel_id, channel_name: ch.name }))) }),
      el('button', {
        class: 'btn btn-danger-ghost btn-sm', type: 'button', text: 'Remove',
        onclick: () => { if (confirm(`Remove ${ch.name}? Downloaded audio will be deleted.`)) act(() => postForm('/channels/remove-unsubscribed', fd({ channel_id: ch.channel_id }))); },
      }),
    ]),
  ]);
}

function fmtBytes(n) {
  if (!n) return '0 MB';
  const mb = n / 1048576;
  return mb >= 1 ? `${mb.toFixed(mb < 10 ? 1 : 0)} MB` : `${Math.round(n / 1024)} KB`;
}

function orphanCard(o) {
  return el('div', { class: 'card ch-card' }, [
    el('div', { class: 'ch-top' }, [
      avatar(o.channel_name, null),
      el('div', { class: 'ch-meta' }, [
        el('div', { class: 'ch-name', title: o.channel_name, text: o.channel_name }),
        el('div', { class: 'ch-sub' }, [
          el('span', { class: 'ep-badge', text: `${o.episode_count} episode${o.episode_count === 1 ? '' : 's'} · ${fmtBytes(o.bytes)}` }),
        ]),
      ]),
    ]),
    el('div', { class: 'ch-actions' }, [
      el('button', {
        class: 'btn btn-danger-ghost btn-sm', type: 'button', text: 'Delete',
        onclick: () => { if (confirm(`Permanently delete orphaned data for "${o.channel_name}"? This cannot be undone.`)) act(() => postForm('/channels/remove-orphan', fd({ channel_id: o.channel_id }))); },
      }),
    ]),
  ]);
}

function emptyCard(emoji, title, hint) {
  return el('div', { class: 'empty' }, [
    el('span', { class: 'empty-emoji', 'aria-hidden': 'true', text: emoji }),
    el('strong', { text: title }),
    el('span', { text: hint }),
  ]);
}

function filteredSubscribed() {
  let list = (state.data?.channels || []).slice();
  const q = state.search.trim().toLowerCase();
  if (q) list = list.filter((c) => c.name.toLowerCase().includes(q));
  if (state.sort === 'name') list.sort((a, b) => a.name.localeCompare(b.name));
  else if (state.sort === 'episodes') list.sort((a, b) => b.episodes - a.episodes);
  else list.sort((a, b) => (a.added_at || '').localeCompare(b.added_at || ''));
  return list;
}

function render() {
  if (!state.data) return;
  const d = state.data;

  // counts + polling panel
  $('#subs-count').textContent = d.channels.length;
  $('#subs-storage').textContent = d.total_bytes ? `${fmtBytes(d.total_bytes)} on disk` : '';
  $('#oneoff-count').textContent = d.unsubscribed.length;
  $('#all-feed-share').hidden = !(d.all_feed_url && d.channels.length);
  renderPolling(d);

  // activity indicator
  const running = (d.jobs || []).filter((j) => j.status === 'running');
  const activity = $('#activity');
  if (running.length) {
    activity.hidden = false;
    $('#activity-text').textContent = running.length === 1 ? (running[0].target || 'Working…') : `${running.length} jobs running`;
  } else { activity.hidden = true; }

  // cookie banner + status card
  renderCookies(d.cookies, d.email);

  // toolbar visibility
  $('#subs-toolbar').hidden = d.channels.length === 0;

  // subscribed grid
  const grid = $('#subs-grid');
  grid.replaceChildren();
  if (d.channels.length === 0) {
    grid.appendChild(emptyCard('📡', 'No channels yet', 'Paste a YouTube channel URL above to start building a podcast feed.'));
  } else {
    const list = filteredSubscribed();
    if (list.length === 0) grid.appendChild(emptyCard('🔍', 'No matches', 'No channels match your search.'));
    else list.forEach((ch) => grid.appendChild(subscribedCard(ch)));
  }

  // bulk bar
  state.selected.forEach((u) => { if (!d.channels.some((c) => c.url === u)) state.selected.delete(u); });
  const bulk = $('#bulk-bar');
  if (state.selected.size > 0) {
    bulk.hidden = false;
    $('#bulk-count').textContent = `${state.selected.size} selected`;
  } else { bulk.hidden = true; }

  // one-off grid
  const og = $('#oneoff-grid');
  og.replaceChildren();
  if (d.unsubscribed.length === 0) og.appendChild(emptyCard('🎯', 'Nothing here', 'One-off video downloads will appear here. Subscribe to keep getting new episodes.'));
  else d.unsubscribed.forEach((ch) => og.appendChild(oneoffCard(ch)));

  // orphaned data — only shown when there's something to clean up
  const orphans = d.orphans || [];
  const orphSection = $('#orphans-section');
  orphSection.hidden = orphans.length === 0;
  if (orphans.length) {
    $('#orphans-count').textContent = orphans.length;
    const ogrid = $('#orphans-grid');
    ogrid.replaceChildren();
    orphans.forEach((o) => ogrid.appendChild(orphanCard(o)));
  }
}

function renderCookies(c, email) {
  // The file carries a hard expiry; YouTube also rotates cookies server-side
  // every few weeks (age-based 'stale'). Whichever fires first wins the banner.
  const expSoon = c.present && c.days_until_expiry != null && c.days_until_expiry <= 7 && !c.expired;
  const banner = $('#cookie-banner');
  if (!c.present) {
    banner.hidden = false; banner.className = 'banner is-error';
    banner.innerHTML = '<strong>No YouTube cookies.</strong> Downloads will be blocked until you upload <code>cookies.txt</code> below.';
  } else if (c.expired) {
    banner.hidden = false; banner.className = 'banner is-error';
    banner.innerHTML = `<strong>Cookies expired ${c.expires_at}.</strong> Polls will fail until you upload a fresh <code>cookies.txt</code> below.`;
  } else if (expSoon) {
    banner.hidden = false; banner.className = 'banner is-warn';
    banner.innerHTML = `<strong>Cookies expire in ${c.days_until_expiry} day${c.days_until_expiry === 1 ? '' : 's'}</strong> (${c.expires_at}). Re-upload <code>cookies.txt</code> to avoid failed polls.`;
  } else if (c.stale) {
    banner.hidden = false; banner.className = 'banner is-warn';
    banner.innerHTML = `<strong>Cookies are ${c.age_days} days old.</strong> YouTube usually rotates cookies after a few weeks — re-upload soon to avoid failed polls.`;
  } else { banner.hidden = true; }

  const dot = !c.present || c.expired ? 'bad' : (expSoon || c.stale) ? 'warn' : 'ok';
  const label = !c.present ? 'No cookies'
    : c.expired ? 'Cookies expired'
    : expSoon ? 'Cookies expiring'
    : c.stale ? 'Cookies aging'
    : 'Cookies active';
  let sub;
  if (!c.present) sub = 'YouTube may block downloads';
  else if (c.expires_at) sub = `expires ${c.expires_at}${c.days_until_expiry != null && !c.expired ? ` (${c.days_until_expiry}d)` : ''} · updated ${c.updated}`;
  else sub = `updated ${c.updated}`;
  $('#cookies-status').replaceChildren(
    el('span', { class: `dot ${dot}` }),
    el('span', { text: label }),
    el('span', { class: 'sub', text: sub }),
  );

  const wrap = $('#cookies-email');
  wrap.replaceChildren();
  if (email.configured) {
    wrap.appendChild(el('span', { text: `✉ Expiry alerts on — ${email.address}` }));
    wrap.appendChild(el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Send test email', onclick: () => act(() => postForm('/auth/test-email', new FormData())) }));
  } else {
    wrap.appendChild(el('span', { text: '✉ Email alerts not configured (set SMTP_* in .env)' }));
  }
}

/* ---- actions ---- */
async function act(fn) {
  try {
    const r = await fn();
    if (r && r.message) toast(r.message, 'ok');
    state.selected.clear();
    await loadState();
  } catch (e) {
    toast(e.message || 'Something went wrong', 'err');
  }
}

/* ---- share modal ---- */
function openShare(ch) {
  const url = ch.feed_url;
  if (!url) return;
  $('#share-name').textContent = ch.name;
  $('#share-url-input').value = url;

  const qrBox = $('#share-qr');
  qrBox.replaceChildren();
  try {
    const qr = qrcode(0, 'M');
    qr.addData(url);
    qr.make();
    qrBox.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
  } catch { qrBox.textContent = ''; }

  const noScheme = url.replace(/^https?:\/\//, '');
  $('#share-apple').href = 'podcast://' + noScheme;
  $('#share-pocketcasts').href = 'pktc://subscribe/' + noScheme;

  const modal = $('#share-modal');
  modal.hidden = false;
  $('#share-copy').focus();
}

function closeShare() { $('#share-modal').hidden = true; }

/* ---- episodes modal ---- */
function fmtDuration(s) {
  if (!s) return '';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}
function fmtSize(b) {
  if (!b) return '';
  const mb = b / 1048576;
  return mb >= 1 ? `${mb.toFixed(mb < 10 ? 1 : 0)} MB` : `${Math.round(b / 1024)} KB`;
}
function fmtDate(iso) {
  // Bare YYYY-MM-DD parses as UTC midnight, which can roll back a day in
  // negative-offset zones — pin it to local midnight so the date is stable.
  const s = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00` : iso;
  const d = new Date(s);
  if (isNaN(d)) return '';
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

/* Compact "12m ago" / "in 1h 59m" style relative time. */
function fmtAgo(iso) {
  if (!iso) return '';
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  const past = secs >= 0;
  const a = Math.abs(secs);
  let txt;
  if (a < 45) txt = 'just now';
  else if (a < 3600) txt = `${Math.round(a / 60)}m`;
  else if (a < 86400) { const h = Math.floor(a / 3600), m = Math.round((a % 3600) / 60); txt = m ? `${h}h ${m}m` : `${h}h`; }
  else txt = `${Math.round(a / 86400)}d`;
  if (txt === 'just now') return txt;
  return past ? `${txt} ago` : `in ${txt}`;
}

function fmtCountdown(secs) {
  if (secs <= 0) return 'now';
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
  if (h >= 1) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m >= 1) return `${m}m`;
  return `${secs}s`;
}

function episodeRow(ep) {
  const meta = [fmtDate(ep.published), fmtDuration(ep.duration), fmtSize(ep.filesize)].filter(Boolean).join(' · ');
  const playWrap = el('div', { class: 'ep-play' });
  playWrap.appendChild(el('button', {
    class: 'btn btn-ghost btn-sm', type: 'button', text: '▶ Play',
    onclick: () => playWrap.replaceChildren(el('audio', { controls: true, autoplay: true, preload: 'none', src: ep.audio_url })),
  }));
  const children = [];
  if (ep.thumbnail) children.push(el('img', { class: 'ep-thumb', src: ep.thumbnail, alt: '', loading: 'lazy', onerror: (e) => e.target.remove() }));
  children.push(el('div', { class: 'ep-body' }, [
    el('div', { class: 'ep-row-title', title: ep.title, text: ep.title }),
    el('div', { class: 'ep-row-meta', text: meta }),
  ]));
  children.push(playWrap);
  children.push(el('div', { class: 'ep-actions' }, [
    el('button', {
      class: 'btn btn-ghost btn-sm', type: 'button', text: 'Re-download',
      title: 'Fetch this episode again, replacing the file on disk',
      onclick: () => act(() => postForm('/episodes/redownload', fd({ episode_id: ep.id }))).then(refreshEpisodes),
    }),
    el('button', {
      class: 'btn btn-danger-ghost btn-sm', type: 'button', text: 'Delete',
      onclick: () => {
        if (confirm(`Delete "${ep.title}"? The audio file will be removed.`)) {
          act(() => postForm('/episodes/delete', fd({ episode_id: ep.id }))).then(refreshEpisodes);
        }
      },
    }),
  ]));
  return el('div', { class: 'ep-row' }, children);
}

async function openEpisodes(ch) {
  // Remembered so an episode action can re-render the open modal: act() calls
  // loadState(), which refreshes the dashboard's counts but knows nothing about
  // the episode list, so a deleted row would otherwise linger on screen.
  state.epChannel = ch;
  $('#ep-title').textContent = ch.name;
  $('#ep-sub').textContent = `${ch.episodes} episode${ch.episodes === 1 ? '' : 's'} being served · newest first`;
  const list = $('#ep-list');
  list.replaceChildren(el('div', { class: 'ep-empty', text: 'Loading…' }));
  $('#ep-modal').hidden = false;
  try {
    const res = await fetch(`/api/channels/${encodeURIComponent(ch.channel_id)}/episodes`);
    if (!res.ok) throw new Error(await readError(res));
    const data = await res.json();
    list.replaceChildren();
    if (!data.episodes.length) { list.appendChild(el('div', { class: 'ep-empty', text: 'No episodes yet.' })); return; }
    data.episodes.forEach((ep) => list.appendChild(episodeRow(ep)));
  } catch (e) {
    list.replaceChildren(el('div', { class: 'ep-empty', text: e.message || 'Failed to load episodes' }));
  }
}

/* Re-fetch and re-render the open episode modal after a delete/re-download. */
async function refreshEpisodes() {
  if (state.epChannel && !$('#ep-modal').hidden) await openEpisodes(state.epChannel);
}

function closeEpisodes() {
  const m = $('#ep-modal');
  m.hidden = true;
  state.epChannel = null;
  $('#ep-list').replaceChildren();  // stop any inline audio playback
}

/* ---- feed settings modal ---- */
function openFeedSettings(ch) {
  if (!ch.channel_id) return;
  state.fsChannel = ch;
  const fs = ch.feed_settings || {};
  $('#fs-name').textContent = ch.name;
  $('#fs-category').value = fs.category || '';
  $('#fs-language').value = fs.language || '';
  $('#fs-explicit').value = fs.explicit || '';
  $('#fs-modal').hidden = false;
  $('#fs-category').focus();
}

function closeFeedSettings() { $('#fs-modal').hidden = true; state.fsChannel = null; }

function submitFeedSettings() {
  const ch = state.fsChannel;
  if (!ch) return;
  // Blank means "use the default" — the server stores NULL for it, which is how
  // a previously-set value gets cleared.
  const body = fd({
    channel_id: ch.channel_id,
    category: $('#fs-category').value,
    language: $('#fs-language').value.trim(),
    explicit: $('#fs-explicit').value,
  });
  act(() => postForm('/channels/feed-settings', body)).then(closeFeedSettings);
}

/* ---- settings / about modal ---- */
function fmtDateTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function changelogEntry(entry) {
  const running = state.data && entry.version === state.data.version;
  const head = el('div', { class: 'cl-head' }, [
    el('span', { class: 'cl-version', text: `v${entry.version}` }),
    running ? el('span', { class: 'cl-badge', text: 'running' }) : null,
    el('span', { class: 'cl-date', text: fmtDate(entry.date) }),
  ]);
  const list = el('ul', { class: 'cl-changes' }, (entry.changes || []).map((c) => el('li', { text: c })));
  return el('div', { class: 'cl-entry' }, [head, list]);
}

async function openSettings() {
  const modal = $('#settings-modal');
  $('#about-version').textContent = state.data ? `v${state.data.version}` : '…';
  $('#about-started').textContent = '…';
  const list = $('#changelog-list');
  list.replaceChildren(el('div', { class: 'ep-empty', text: 'Loading…' }));
  modal.hidden = false;
  try {
    const res = await fetch('/api/changelog');
    if (!res.ok) throw new Error(await readError(res));
    const data = await res.json();
    $('#about-version').textContent = `v${data.version}`;
    $('#about-started').textContent = fmtDateTime(data.started_at) || '—';
    list.replaceChildren();
    (data.entries || []).forEach((e) => list.appendChild(changelogEntry(e)));
    if (!list.children.length) list.appendChild(el('div', { class: 'ep-empty', text: 'No release notes yet.' }));
  } catch (e) {
    list.replaceChildren(el('div', { class: 'ep-empty', text: e.message || 'Failed to load changelog' }));
  }
}

function closeSettings() { $('#settings-modal').hidden = true; }

/* ---- polling panel ---- */
const RING_CIRC = 2 * Math.PI * 37;  // r=37 in the SVG

function runStatus(run) {
  if (run.status === 'error') return { cls: 'err', label: 'failed' };
  if (run.downloaded > 0) return { cls: 'ok', label: `+${run.downloaded} new` };
  return { cls: 'idle', label: 'up to date' };
}

function pollRunRow(run) {
  const s = runStatus(run);
  const right = run.status === 'error'
    ? el('span', { class: 'pr-status err', title: run.error || 'Poll failed', text: 'failed' })
    : el('span', { class: `pr-status ${s.cls}`, text: s.label });
  return el('div', { class: 'pr-row' }, [
    el('span', { class: `pr-dot ${s.cls}`, 'aria-hidden': 'true' }),
    el('span', { class: 'pr-name', title: run.channel_name, text: run.channel_name }),
    el('span', { class: 'pr-time', text: fmtAgo(run.finished_at || run.started_at) }),
    right,
  ]);
}

function renderPolling(d) {
  const p = d.polling;
  const card = $('#poll-card');
  if (!p) { card.hidden = true; return; }
  card.hidden = false;
  state.polling = p;

  // schedule pill
  const iv = p.interval_hours;
  $('#poll-interval').textContent = `every ${iv === 1 ? 'hour' : `${iv}h`}`;

  // health summary from each channel's last run
  const fails = (d.channels || []).filter((c) => c.last_poll && c.last_poll.status === 'error');
  const health = $('#poll-health');
  if (fails.length) {
    health.className = 'poll-health bad';
    health.textContent = `${fails.length} failing`;
  } else if (d.channels && d.channels.length) {
    health.className = 'poll-health good';
    health.textContent = 'all healthy';
  } else {
    health.className = 'poll-health';
    health.textContent = '';
  }

  // facts line
  const facts = $('#poll-facts');
  facts.replaceChildren(
    el('span', {}, [el('strong', { text: 'Last poll ' }), document.createTextNode(p.last_poll_at ? fmtAgo(p.last_poll_at) : '—')]),
  );

  // recent activity
  const runs = $('#poll-runs');
  runs.replaceChildren();
  if (!p.runs || !p.runs.length) {
    runs.appendChild(el('div', { class: 'pr-empty', text: 'No polls yet — they’ll appear here as channels are checked.' }));
  } else {
    p.runs.slice(0, 6).forEach((r) => runs.appendChild(pollRunRow(r)));
  }

  tickPoll();  // paint the gauge immediately
}

function tickPoll() {
  const p = state.polling;
  const ring = $('#poll-ring');
  const num = $('#poll-countdown');
  if (!p || !ring || !num) return;
  if (!p.next_poll_at) { num.textContent = '—'; ring.style.strokeDashoffset = RING_CIRC; return; }

  const remaining = Math.max(0, Math.round((new Date(p.next_poll_at).getTime() - Date.now()) / 1000));
  const interval = (p.interval_hours || 1) * 3600;
  const elapsedFrac = Math.min(1, Math.max(0, 1 - remaining / interval));
  ring.style.strokeDasharray = RING_CIRC;
  ring.style.strokeDashoffset = RING_CIRC * (1 - elapsedFrac);
  num.textContent = fmtCountdown(remaining);
}

/* ---- copy ---- */
async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = el('textarea', {}, []); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch { /* ignore */ }
    ta.remove();
  }
  if (btn) { const old = btn.textContent; btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = old; }, 1500); }
}

/* ---- wiring ---- */
function init() {
  $('#add-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = $('#add-url');
    act(() => postForm('/channels/add', fd({ url: input.value.trim() }))).then(() => { input.value = ''; });
  });

  $('#dl-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = $('#dl-url');
    const sub = $('#dl-subscribe').checked;
    act(() => postForm('/episodes/download', fd({ url: input.value.trim(), subscribe: sub }))).then(() => { input.value = ''; $('#dl-subscribe').checked = false; });
  });

  $('#cookies-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const file = $('#cookies-file').files[0];
    if (!file) return;
    act(() => postForm('/auth/cookies', fd({ file }))).then(() => { $('#cookies-form').reset(); });
  });

  $('#poll-now').addEventListener('click', () => act(() => postForm('/channels/poll-all', new FormData())));
  setInterval(tickPoll, 1000);  // live countdown between state refreshes

  $('#subs-search').addEventListener('input', (e) => { state.search = e.target.value; render(); });
  $('#subs-sort').addEventListener('change', (e) => { state.sort = e.target.value; render(); });

  $('#bulk-poll').addEventListener('click', () => act(() => postJSON('/channels/poll-bulk', { urls: [...state.selected] })));
  $('#bulk-remove').addEventListener('click', () => {
    if (confirm(`Remove ${state.selected.size} channel(s)? Downloaded audio will be deleted.`)) act(() => postJSON('/channels/remove-bulk', { urls: [...state.selected] }));
  });
  $('#bulk-clear').addEventListener('click', () => { state.selected.clear(); render(); });

  // share modal
  $('#share-copy').addEventListener('click', (e) => copyText($('#share-url-input').value, e.target));
  $$('#share-modal [data-close]').forEach((n) => n.addEventListener('click', closeShare));

  // all-channels combined feed — reuses the share modal, which only reads
  // .name and .feed_url.
  $('#all-feed-share').addEventListener('click', () => {
    if (state.data?.all_feed_url) openShare({ name: 'All channels', feed_url: state.data.all_feed_url });
  });

  // episodes modal
  $$('#ep-modal [data-close]').forEach((n) => n.addEventListener('click', closeEpisodes));

  // feed settings modal
  $$('#fs-modal [data-close]').forEach((n) => n.addEventListener('click', closeFeedSettings));
  $('#fs-form').addEventListener('submit', (e) => { e.preventDefault(); submitFeedSettings(); });

  // settings / about modal
  $('#settings-btn').addEventListener('click', openSettings);
  $$('#settings-modal [data-close]').forEach((n) => n.addEventListener('click', closeSettings));

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeShare(); closeEpisodes(); closeSettings(); closeFeedSettings(); } });

  loadState();
}

document.addEventListener('DOMContentLoaded', init);
