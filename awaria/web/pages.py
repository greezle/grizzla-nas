"""Server-rendered HTML: CSS, layout chrome and every page/partial."""
import html
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta

from awaria.config import OFFSITE_STAMP, OFFSITE_MAX_AGE_S
from awaria.db import db_lock, open_db, now_str, to_epoch
from awaria.services.printers import is_online
from awaria.services.telemetry import (FINE_EVERY_S, FINE_KEEP_S, live_of,
                                       is_overheated, HISTORY, live_lock)
from awaria.services.catalog import SEVERITY_NAMES, LABEL_MAX_B, \
    QUESTION_MAX_B, ANSWER_MAX_B


CSS = """
body { font-family: system-ui, sans-serif; margin: 0; background: #f2f2f2; color: #111; }
header { background: #1a1a1a; color: #fff; padding: 10px 20px; display: flex; align-items: center; gap: 16px;
  position: sticky; top: 0; z-index: 15; box-shadow: 0 2px 8px rgba(0,0,0,.3); }
header h1 { font-size: 20px; margin: 0; }
header a { color: #ffb700; text-decoration: none; }
#burger { background: none; border: none; color: #fff; font-size: 22px; cursor: pointer; padding: 2px 6px;
  transition: transform .2s ease; }
#burger:hover { transform: scale(1.2); }
#drawer { position: fixed; top: 0; left: 0; bottom: 0; width: 240px; background: #1a1a1a; color: #fff;
  transform: translateX(-100%); transition: transform .28s cubic-bezier(.22,.9,.32,1); z-index: 20; padding-top: 10px; }
#drawer.open { transform: translateX(0); box-shadow: 3px 0 14px rgba(0,0,0,.45); }
#drawer a { display: block; color: #fff; text-decoration: none; padding: 12px 20px; font-size: 15px;
  transition: background .15s ease, padding-left .15s ease; }
#drawer a:hover { background: #333; padding-left: 28px; }
#drawer .brand { color: #ffb700; font-weight: 700; padding: 10px 20px 16px; border-bottom: 1px solid #333; margin-bottom: 6px; }
#drawer .shutdown { position: absolute; bottom: 10px; left: 0; right: 0; border-top: 1px solid #333;
  color: #999; font-size: 13.5px; }
#drawer .shutdown:hover { color: #fff; }
#overlay { position: fixed; inset: 0; background: rgba(0,0,0,.35); z-index: 19;
  opacity: 0; visibility: hidden; transition: opacity .25s ease, visibility .25s; }
#overlay.show { opacity: 1; visibility: visible; }
main { animation: page-in .28s ease; }
main.no-anim { animation: none; }
@keyframes page-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.hidden { display: none; }
.sec-head { display: flex; justify-content: space-between; align-items: center; margin-top: 22px; }
.sec-head h2 { margin: 0; }
.view-toggle { display: flex; border: 1px solid #ccc; border-radius: 6px; overflow: hidden; }
.view-toggle .tab { background: #fff; border: none; padding: 5px 14px; cursor: pointer; font-size: 13px;
  transition: background .15s ease, color .15s ease; }
.view-toggle .tab.active { background: #1a1a1a; color: #ffb700; font-weight: 700; }
table tr { transition: background .15s ease; }
tbody tr:hover td, table tr:hover td { background: #fafafa; }
a { transition: color .15s ease; }
input[type=submit] { cursor: pointer; transition: transform .12s ease; }
input[type=submit]:hover { transform: translateY(-1px); }
.farm-map { display: flex; align-items: flex-start; background: #fff; padding: 16px;
  box-shadow: 0 1px 2px rgba(0,0,0,.15); overflow-x: auto; }
.zone { display: flex; gap: 8px; }             /* tiny spacing between sections */
.zone + .zone { margin-left: 42px; }           /* large spacing ~ half a section */
.sec-label { text-align: center; font-weight: 700; margin-bottom: 4px; }
.sec-grid { display: grid; grid-template-columns: repeat(2, 34px); grid-auto-rows: 34px; gap: 4px; }
.sq { display: flex; align-items: center; justify-content: center; border-radius: 5px; font-size: 12px;
  font-weight: 600; color: #fff; text-decoration: none; transition: transform .12s ease, box-shadow .12s ease;
  position: relative; overflow: hidden; }
.sq .prog { position: absolute; left: 3px; right: 3px; bottom: 2px; height: 4px;
  background: rgba(0,0,0,.30); border-radius: 2px; }
.sq .prog i { display: block; height: 100%; background: #fff; border-radius: 2px;
  animation: prog-pulse 1.6s ease-in-out infinite; }
@keyframes prog-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }
.map-legend { margin-top: 10px; color: #666; font-size: 13px; display: flex; align-items: center; gap: 6px; }
.map-legend .sq.mini { width: 18px; height: 18px; display: inline-flex; margin-left: 14px; cursor: default; }
.map-legend .sq.mini:first-child { margin-left: 0; }
.map-legend .sq.mini:hover { transform: none; box-shadow: none; }
.sq:hover { transform: scale(1.18); box-shadow: 0 2px 8px rgba(0,0,0,.35); }
.sq.off { background: #c9c9c9; color: #666; }
.sq.ok { background: #2e7d32; }
.sq.degraded { background: #f2c200; color: #111; }
.sq.blocked { background: #d32f2f; animation: pulse 1.8s infinite; }
#tip { position: absolute; background: #1a1a1a; color: #fff; padding: 8px 12px; border-radius: 6px;
  font-size: 13px; box-shadow: 0 4px 14px rgba(0,0,0,.4); opacity: 0; visibility: hidden;
  transition: opacity .15s ease; z-index: 30; pointer-events: none; max-width: 260px; }
#tip.show { opacity: 1; visibility: visible; }
.stats-top { display: flex; gap: 30px; align-items: center; background: #fff; padding: 18px;
  box-shadow: 0 1px 2px rgba(0,0,0,.15); }
.donut { width: 170px; height: 170px; border-radius: 50%; position: relative; flex-shrink: 0; }
.donut-hole { position: absolute; inset: 32px; background: #fff; border-radius: 50%;
  display: flex; flex-direction: column; align-items: center; justify-content: center; }
.donut-hole b { font-size: 26px; }
.donut-hole span { color: #777; font-size: 12px; }
.legend p { margin: 8px 0; font-size: 14.5px; color: #444; }
.legend p .muted, .legend p.muted { font-size: 12.5px; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 8px;
  vertical-align: middle; }
.usage-row { display: flex; align-items: center; gap: 12px; padding: 6px 10px; border-radius: 6px;
  transition: opacity .2s ease, background .15s ease; }
.usage-row:hover { background: #f6f6f6; }
.usage-row.excluded { opacity: .35; }
.usage-row .host, .usage-row a.host { min-width: 42px; font-size: 14px; font-weight: 600; }
.usage-row a.host:hover { text-decoration: underline; }
.usage-row > .muted { min-width: 150px; font-size: 12px; text-align: right; white-space: nowrap; }
.usage-row input[type=checkbox] { width: 15px; height: 15px; accent-color: #1a1a1a; flex-shrink: 0; }
.ubar { flex: 1; height: 12px; border-radius: 6px; overflow: hidden; display: flex; background: #ececec; }
.ubar div { transition: width .4s ease; }
.sec-group + .sec-group { margin-top: 20px; }
.sec-toggle { display: flex; align-items: center; gap: 8px; padding: 0 10px 6px; margin-bottom: 4px;
  border-bottom: 1px solid #e6e6e6; cursor: pointer; font-size: 11.5px; text-transform: uppercase;
  letter-spacing: .09em; color: #888; user-select: none; }
.sec-toggle b { font-size: 11.5px; font-weight: 700; color: #666; }
.sec-toggle .muted { font-size: 11.5px; }
.sec-toggle input[type=checkbox] { width: 14px; height: 14px; accent-color: #1a1a1a; }
.sec-toggle:hover { color: #444; }
.donut { transition: background .3s ease; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; align-items: flex-start; }
.card { background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.15); padding: 12px 16px; flex: 1; min-width: 320px; }
.card h3 { margin: 2px 0 8px; font-size: 14px; text-transform: uppercase; color: #666; }
table.plain td { padding: 4px 8px; }
.chip { display: inline-flex; align-items: center; padding: 2px 10px; border-radius: 10px; color: #fff;
  font-size: 12px; font-weight: 700; margin: 0 3px 3px 0; border: none; vertical-align: middle; }
.chip-x { background: none; border: none; color: #fff; cursor: pointer; font-weight: 700; padding: 0 0 0 6px;
  font-size: 13px; opacity: .75; transition: opacity .15s ease; }
.chip-x:hover { opacity: 1; }
.chip.ghost { opacity: .65; border: 1px dashed rgba(255,255,255,.8); transition: opacity .15s ease; }
.chip.ghost:hover { opacity: 1; }
.chip-btn { background: none; border: none; color: #fff; font-weight: 700; font-size: 12px; cursor: pointer; padding: 0; }
.chips { margin: 6px 0; }
.swatch { width: 22px; height: 22px; border-radius: 5px; border: 1px solid #bbb; cursor: pointer; padding: 0;
  transition: transform .12s ease; }
.swatch:hover { transform: scale(1.2); }
.inline-form { display: flex; gap: 6px; align-items: center; margin: 8px 0; flex-wrap: wrap; }
.inline-form input[type=color] { width: 34px; height: 26px; padding: 1px; border: 1px solid #ccc; }
.checks { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 4px; }
label.check { display: block; padding: 3px 6px; border-radius: 4px; transition: background .15s ease; }
label.check:hover { background: #f0f0f0; }
td.drag { width: 26px; cursor: grab; color: #999; font-size: 18px; text-align: center; user-select: none; }
td.drag:active { cursor: grabbing; }
tr.ghost td { background: #fff3cd; }
tr.drag-active { box-shadow: 0 4px 14px rgba(0,0,0,.25); }
.b-block.alive { animation: pulse 1.8s infinite; }
@keyframes pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(211,47,47,.55); } 60% { box-shadow: 0 0 0 8px rgba(211,47,47,0); } }
.counts { margin-left: auto; font-size: 15px; }
#bell-wrap { position: relative; margin-left: 14px; }
.counts + #bell-wrap { margin-left: 14px; }
header > #bell-wrap:nth-last-child(1):nth-child(3) { margin-left: auto; } /* no counts -> push right */
#bell { background: none; border: none; color: #fff; cursor: pointer; padding: 4px; position: relative;
  transition: transform .15s ease; }
#bell:hover { transform: scale(1.15); }
#bell-badge { position: absolute; top: -4px; right: -6px; background: #d32f2f; color: #fff;
  font-size: 11px; font-weight: 700; border-radius: 9px; padding: 1px 5px; min-width: 10px; }
#notif-clip { position: absolute; right: -20px; top: 34px; width: 440px; max-width: 94vw;
  overflow: hidden; padding: 0 20px 30px; pointer-events: none; z-index: 40; }
#notif-panel { position: relative; background: #fff; color: #111; border-radius: 0 0 8px 8px;
  box-shadow: 0 6px 24px rgba(0,0,0,.35); pointer-events: auto;
  transform: translateY(calc(-100% - 40px)); visibility: hidden;
  transition: transform .3s cubic-bezier(.22,.9,.32,1), visibility .3s; }
#notif-panel.open { transform: translateY(0); visibility: visible; }
#notif-list { max-height: 60vh; overflow-y: auto; }
.notif { display: flex; align-items: flex-start; border-left: 4px solid #999; border-bottom: 1px solid #eee;
  overflow: hidden; transition: transform .24s ease, opacity .24s ease,
  height .2s ease .16s, border-bottom-width .2s ease .16s; }
.notif.going { transform: translateX(-110%); opacity: 0; height: 0 !important;
  border-bottom-width: 0; pointer-events: none; }
.notif a, .notif > span { flex: 1; padding: 8px 10px; text-decoration: none; color: #111; font-size: 13px; }
.notif a:hover { background: #f6f6f6; }
.notif small { display: block; color: #888; }
.notif .nx { background: none; border: none; color: #999; font-size: 17px; cursor: pointer; padding: 8px 10px;
  transition: color .15s ease; }
.notif .nx:hover { color: #d32f2f; }
.nk-failure { border-left-color: #d32f2f; }
.nk-repair { border-left-color: #2e7d32; }
.nk-overheat { border-left-color: #e65100; }
.nk-gcode_update { border-left-color: #1565c0; }
.nk-note { border-left-color: #f2c200; }
.nempty { padding: 16px; color: #888; text-align: center; }
#notif-clear { display: none; width: 100%; border: none; background: #f2f2f2; padding: 9px; cursor: pointer;
  font-weight: 600; border-radius: 0 0 8px 8px; transition: background .15s ease; }
#notif-clear:hover { background: #e4e4e4; }
.sq .hot { position: absolute; top: 1px; right: 1px; background: #b71c1c; color: #fff; font-size: 9px;
  line-height: 1; padding: 2px 3px; border-radius: 3px; animation: pulse 1.8s infinite; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 9px; font-weight: 600; font-size: 13px; }
.b-block { background: #d32f2f; color: #fff; }
.b-degr { background: #f2c200; color: #111; }
.b-ok { background: #2e7d32; color: #fff; }
main { padding: 16px 20px 260px; max-width: 1200px; margin: 0 auto; }
h2 { font-size: 16px; margin: 22px 0 8px; }
table { border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.15); }
th { text-align: left; font-size: 12px; text-transform: uppercase; color: #666; padding: 7px 10px; border-bottom: 2px solid #ddd; }
td { padding: 8px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
tr.blocked td { background: #fdecea; }
tr.degraded td { background: #fdf7e0; }
td.host, a.host { font-weight: 700; font-size: 17px; text-decoration: none; color: #111; }
.detail { color: #555; white-space: pre-line; font-size: 13px; }
.age { white-space: nowrap; }
.muted { color: #888; font-size: 13px; }
form.note { display: flex; gap: 6px; margin-top: 4px; }
form.note input[type=text] { flex: 1; padding: 4px 6px; }
.empty { padding: 30px; text-align: center; color: #777; background: #fff; }
.warnbar { background: #f2c200; color: #111; padding: 9px 14px; font-weight: 600;
  border-radius: 6px; margin-bottom: 10px; box-shadow: 0 1px 2px rgba(0,0,0,.15); }
form.wizard { background: #fff; padding: 14px 18px; box-shadow: 0 1px 2px rgba(0,0,0,.15); }
form.wizard fieldset { margin: 12px 0; border: 1px solid #ccc; }
form.wizard input, form.wizard select { padding: 3px 5px; }
form.wizard table td { border: none; padding: 3px 6px; }
"""


def display_name(path):
    """File name of a print-session path; sessions store the full library
    path since fw 11244, but the UI shows just the name."""
    return str(path or "").rsplit("/", 1)[-1]


def fmt_age(start, end=None):
    try:
        t0 = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "?"
    t1 = datetime.strptime(end, "%Y-%m-%d %H:%M:%S") if end else datetime.now()
    mins = max(0, int((t1 - t0).total_seconds() // 60))
    if mins < 60:
        return f"{mins} min"
    if mins < 48 * 60:
        return f"{mins // 60} h {mins % 60} min"
    return f"{mins // (24 * 60)} d {(mins // 60) % 24} h"


def e(s):
    return html.escape(str(s if s is not None else ""))


def page(title, body, refresh=None):
    # soft refresh: re-fetch the page and swap <main> + header counts in place
    # (a meta refresh reloaded and "blinked" the whole page); inline scripts in
    # the fresh content are re-created so they execute again
    soft_refresh = f"""<script>
    async function softRefresh() {{
      try {{
        const r = await fetch(location.pathname + location.search);
        if (!r.ok) return;
        const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
        const fresh = doc.querySelector('main');
        const old = document.querySelector('main');
        if (fresh && old) {{
          fresh.classList.add('no-anim');
          old.replaceWith(fresh);
          fresh.querySelectorAll('script').forEach(s => {{
            const n = document.createElement('script');
            if (s.src) {{ n.src = s.src; }} else {{ n.textContent = s.textContent; }}
            s.replaceWith(n);
          }});
        }}
        const nc = doc.querySelector('.counts'), oc = document.querySelector('.counts');
        if (nc && oc) {{ oc.replaceWith(nc); }}
      }} catch (err) {{ /* offline - keep the current view */ }}
    }}
    setInterval(softRefresh, {int(refresh) * 1000});
    </script>""" if refresh else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title><style>{CSS}</style></head><body>
<div id="overlay" onclick="drawer(false)"></div>
<nav id="drawer">
  <div class="brand">GRIZZLA</div>
  <a href="/awaria/">Panel serwisowy</a>
  <a href="/awaria/history">Historia</a>
  <a href="/awaria/stats">Statystyki</a>
  <a href="/awaria/netlog">Log sieci</a>
  <a href="/awaria/defs">Katalog błędów</a>
  <a href="/awaria/components">Części zamienne</a>
  <a href="#" class="shutdown" onclick="shutdownServer(); return false">&#9211; Wyłącz serwer</a>
</nav>
<header><button id="burger" onclick="drawer()" title="Menu" aria-label="Menu"
  aria-expanded="false" aria-controls="drawer">&#9776;</button>
<h1><a href="/awaria/">GRIZZLA — panel serwisowy</a></h1>{body[0]}
<div id="bell-wrap">
  <button id="bell" onclick="notifPanel()" title="Powiadomienia" aria-label="Powiadomienia"
    aria-haspopup="true" aria-expanded="false" aria-controls="notif-panel">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>
      <path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>
    <span id="bell-badge" class="hidden"></span>
  </button>
  <div id="notif-clip"><div id="notif-panel">
    <div id="notif-list"></div>
    <button id="notif-clear" onclick="clearNotifs()">Wyczyść wszystkie</button>
  </div></div>
</div></header>
<main>{body[1]}</main>
<script>
function drawer(open) {{
  const d = document.getElementById('drawer'), o = document.getElementById('overlay');
  const on = open === undefined ? !d.classList.contains('open') : open;
  d.classList.toggle('open', on); o.classList.toggle('show', on);
  if (on) {{ notifPanel(false); }}
  document.getElementById('burger').setAttribute('aria-expanded', on);
}}
async function shutdownServer() {{
  if (!confirm('Wyłączyć serwer (przenosiny / serwis)?\\n\\nDrukarki będą kolejkować zgłoszenia i wstrzymają aktualizacje do jego powrotu. Odłącz zasilanie dopiero gdy zielona dioda zgaśnie (~20 s).')) {{ return; }}
  try {{ await fetch('/awaria/api/shutdown', {{method: 'POST'}}); }} catch (err) {{}}
  document.body.innerHTML = '<div style="padding:48px;font:17px system-ui;max-width:34em">' +
    'Serwer się wyłącza. Poczekaj aż zielona dioda na Raspberry Pi przestanie migać (ok. 20 s), potem można odłączyć zasilanie.<br><br>' +
    'Po podłączeniu w nowym miejscu (zasilanie + ethernet) panel wróci pod tym samym adresem po ok. minucie.</div>';
}}
function escText(s) {{ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }}
function notifPanel(open) {{
  const p = document.getElementById('notif-panel');
  const on = open === undefined ? !p.classList.contains('open') : open;
  p.classList.toggle('open', on);
  if (on) {{ drawer(false); }}
  document.getElementById('bell').setAttribute('aria-expanded', on);
}}
// current-standard dismissal: click anywhere outside closes the panel,
// Escape closes both the panel and the drawer
document.addEventListener('click', ev => {{
  if (!ev.target.closest('#bell-wrap')) {{ notifPanel(false); }}
}});
document.addEventListener('keydown', ev => {{
  if (ev.key === 'Escape') {{ notifPanel(false); drawer(false); }}
}});
async function loadNotifs() {{
  try {{
    const d = await (await fetch('/awaria/api/notifications.json')).json();
    const badge = document.getElementById('bell-badge');
    badge.textContent = d.count > 99 ? '99+' : d.count;
    badge.classList.toggle('hidden', !d.count);
    document.getElementById('notif-clear').style.display = d.count ? 'block' : 'none';
    document.getElementById('notif-list').innerHTML = d.items.length ? d.items.map(n => {{
      const inner = '<small>' + n.created_at.slice(5, 16) + '</small>' + escText(n.text);
      const body = n.link ? '<a href="' + n.link + '">' + inner + '</a>'
                          : '<span>' + inner + '</span>';
      return '<div class="notif nk-' + n.kind + '">' + body +
             '<button class="nx" title="Usuń" onclick="dismissNotif(' + n.id + ', this)">&times;</button></div>';
    }}).join('') : '<p class="nempty">Brak powiadomień</p>';
  }} catch (err) {{}}
}}
async function dismissNotifs(payload) {{
  await fetch('/awaria/api/notifications/dismiss', {{method: 'POST',
    headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload)}});
  loadNotifs();
}}
function dismissNotif(id, btn) {{
  const row = btn.closest('.notif');
  row.style.height = row.offsetHeight + 'px';
  void row.offsetHeight; // lock the height before animating, or it can't collapse
  row.classList.add('going');
  fetch('/awaria/api/notifications/dismiss', {{method: 'POST',
    headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{id}})}});
  setTimeout(loadNotifs, 480); // re-sync badge + empty state after the exit
}}
function clearNotifs() {{ dismissNotifs({{all: true}}); }}
loadNotifs();
setInterval(loadNotifs, 60000); // slow fallback - SSE nudges are the fast path
// live nudges from the server (EventSource reconnects on its own); pages
// with a soft refresh react to farm changes instantly
(function() {{
  let t = null;
  new EventSource('/awaria/api/stream').onmessage = ev => {{
    let kind = '';
    try {{ kind = JSON.parse(ev.data).kind; }} catch (err) {{ return; }}
    if (kind === 'notifications') {{ loadNotifs(); }}
    if ((kind === 'failures' || kind === 'printers') && typeof softRefresh === 'function') {{
      clearTimeout(t);
      t = setTimeout(softRefresh, 250); // coalesce bursts into one re-fetch
    }}
  }};
}})();
</script>
{soft_refresh}
</body></html>"""


def state_badge_of(f):
    """Current state of a failure row: repaired failures are green, never
    'BLOKADA' (that read as the printer's live state - field confusion on E6)."""
    if f["closed_at"]:
        return '<span class="badge b-ok">NAPRAWIONA</span>'
    return ('<span class="badge b-block">BLOKADA</span>'
            if f["blocking"] else '<span class="badge b-degr">DZIAŁA</span>')


def failure_row(f, show_host=True):
    cls = "" if f["closed_at"] else (
        "blocked" if f["blocking"] else "degraded")
    host_cell = (
        f'<td class="host"><a class="host" href="/awaria/printer/'
        f'{urllib.parse.quote(f["hostname"])}">{e(f["hostname"])}</a></td>'
        if show_host else "")
    closed = f'<div class="muted">naprawiona {e(f["closed_at"])} ({e(f["closed_by"] or "")}), po {fmt_age(f["opened_at"], f["closed_at"])}</div>' if f[
        "closed_at"] else ""
    note = f'<div class="muted">🛠 {e(f["repair_note"])}</div>' if f[
        "repair_note"] else ""
    return f"""<tr class="{cls}">{host_cell}
      <td>{state_badge_of(f)}</td>
      <td><a href="/awaria/failure/{f['id']}"><b>{e(f['label'])}</b></a>
          <div class="detail">{e(f['detail'])}</div>{closed}{note}</td>
      <td class="age">{e(f['opened_at'])}<br><b>{fmt_age(f['opened_at'], f['closed_at'])}</b></td>
    </tr>"""


def flag_chips(db, host, removable=False):
    # each removable chip IS a delete form: a <form> nested in a <span>/<p> is
    # invalid HTML and browsers break it apart (the original "cannot delete
    # RND" bug) - as the chip element itself it is valid and the x works
    chips = []
    for f in db.execute(
            "SELECT * FROM printer_flags WHERE hostname=? ORDER BY id",
        (host, )):
        if removable:
            chips.append(
                f'<form method="post" action="/awaria/printer/{urllib.parse.quote(host)}/flag_del"'
                f' class="chip" style="background:{e(f["color"])}">'
                f'<input type="hidden" name="id" value="{f["id"]}">{e(f["text"])}'
                f'<button class="chip-x" title="Usuń oznaczenie">×</button></form>'
            )
        else:
            chips.append(
                f'<span class="chip" style="background:{e(f["color"])}">{e(f["text"])}</span>'
            )
    return " ".join(chips)


def flag_suggestions(db, host):
    """One-click chips of recently used tags (text+color) not yet on this
    printer, plus recent color swatches for the add-flag form."""
    own = {
        (r["text"], r["color"])
        for r in db.execute(
            "SELECT text, color FROM printer_flags WHERE hostname=?", (host, ))
    }
    recent = db.execute(
        "SELECT text, color, MAX(id) m FROM printer_flags"
        " GROUP BY text, color ORDER BY m DESC LIMIT 12").fetchall()
    quoted = urllib.parse.quote(host)
    tags = "".join(
        f'<form method="post" action="/awaria/printer/{quoted}/flag_add" class="chip ghost"'
        f' style="background:{e(r["color"])}">'
        f'<input type="hidden" name="text" value="{e(r["text"])}">'
        f'<input type="hidden" name="color" value="{e(r["color"])}">'
        f'<button class="chip-btn" title="Dodaj to oznaczenie">+ {e(r["text"])}</button></form>'
        for r in recent if (r["text"], r["color"]) not in own)[:2000]

    colors = db.execute("SELECT color, MAX(id) m FROM printer_flags"
                        " GROUP BY color ORDER BY m DESC LIMIT 8").fetchall()
    swatches = "".join(
        f'<button type="button" class="swatch" style="background:{e(c["color"])}"'
        f' title="{e(c["color"])}" onclick="document.getElementById(\'flagcolor\').value=\'{e(c["color"])}\'"></button>'
        for c in colors)
    return tags, swatches


def render_map(db):
    """Farm layout: 4 zones of sections (left to right N M L | A B C D E |
    G H I | J K), each section = 2 columns x 3 rows, numbered row by row
    (1-2 / 3-4 / 5-6). Hostname = section letter + number, e.g. E6."""
    zones = [["N", "M", "L"], ["A", "B", "C", "D", "E"], ["G", "H", "I"],
             ["J", "K"]]

    info = {}
    for p in db.execute(
            "SELECT hostname FROM printers UNION SELECT DISTINCT hostname FROM events"
    ):
        h = p["hostname"]
        opens = db.execute(
            "SELECT COUNT(*) c, MAX(blocking) b FROM failures"
            " WHERE hostname=? AND closed_at IS NULL", (h, )).fetchone()
        state, cls = (("BLOKADA", "blocked") if opens["b"] else
                      (("USZKODZONA", "degraded") if opens["c"] else
                       ("SPRAWNA", "ok")))
        last = db.execute(
            "SELECT closed_at, label FROM failures WHERE hostname=?"
            " AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1",
            (h, )).fetchone()
        online = is_online(h)
        info[h] = {
            "state":
            state,
            "cls":
            cls if online else "off",
            "open":
            opens["c"],
            "online":
            online,
            "repair":
            f"{last['closed_at'][:10]} — {last['label'] or '?'}"
            if last else None
        }
        if live := live_of(h):
            v = live[0]
            if isinstance(v.get("print_filename"),
                          str) and v["print_filename"]:
                progress = f" ({v['print_progress']:.0f}%)" if isinstance(
                    v.get("print_progress"), float) else ""
                info[h]["file"] = v["print_filename"] + progress
                info[h]["prog"] = v["print_progress"] if isinstance(
                    v.get("print_progress"), float) else 0.0
            temps = []
            if isinstance(v.get("temp_noz"), float):
                temps.append(f"dysza {v['temp_noz']:.0f}°")
            if isinstance(v.get("temp_bed"), float):
                temps.append(f"stół {v['temp_bed']:.0f}°")
            if isinstance(v.get("temp_brd"), float):
                temps.append(f"xBuddy {v['temp_brd']:.0f}°")
            if is_overheated(h):
                mcu = v.get("temp_mcu")
                brd = v.get("temp_brd")
                info[h]["hot"] = True
                temps.append("UWAGA: elektronika " + "/".join(
                    f"{x:.0f}°" for x in (mcu, brd) if isinstance(x, float)))
            if temps:
                info[h]["temps"] = ", ".join(temps)

    def cell(host, n):
        p = info.get(host)
        cls = p["cls"] if p else "off"
        bar = hot = ""
        if p and cls != "off" and "prog" in p:
            width = max(p["prog"], 10)  # a sliver stays visible at 0-10%
            bar = f'<span class="prog"><i style="width:{width:.0f}%"></i></span>'
        if p and p.get("hot"):
            hot = '<b class="hot" title="Wysoka temperatura elektroniki">H</b>'
        return (f'<a class="sq {cls}" href="/awaria/printer/{host}"'
                f' data-host="{host}">{n}{bar}{hot}</a>')

    zone_html = []
    for zone in zones:
        sections = []
        for s in zone:
            cells = "".join(cell(f"{s}{n}", n) for n in range(1, 7))
            sections.append(
                f'<div class="section"><div class="sec-label">{s}</div>'
                f'<div class="sec-grid">{cells}</div></div>')
        zone_html.append(f'<div class="zone">{"".join(sections)}</div>')

    legend = """<div class="map-legend">
      <span class="sq mini ok"></span> sprawna
      <span class="sq mini ok"><span class="prog"><i style="width:55%"></i></span></span> drukuje
      <span class="sq mini degraded"></span> uszkodzona
      <span class="sq mini" style="background:#d32f2f"></span> blokada
      <span class="sq mini off"></span> offline
    </div>"""

    return f"""<div class="farm-map">{''.join(zone_html)}</div>{legend}<div id="tip"></div>
    <script>
    (function() {{
      const P = {json.dumps(info, ensure_ascii=False).replace("<", "\\u003c")};
      const tip = document.getElementById('tip');
      document.querySelectorAll('.sq').forEach(el => {{
        el.addEventListener('mouseenter', () => {{
          const h = el.dataset.host, p = P[h];
          let text = '<b>' + h + '</b><br>';
          if (!p) {{
            text += 'Niepodłączona do sieci (brak zgłoszeń)';
          }} else {{
            text += (p.online ? '🟢 online' : '⚪ offline') + '<br>Stan: ' + p.state
                 + (p.open ? ' (' + p.open + ' otw.)' : '')
                 + (p.file ? '<br>Drukuje: ' + escText(p.file) : '')
                 + (p.temps ? '<br>' + p.temps : '')
                 + '<br>Ostatnia naprawa: ' + escText(p.repair || 'brak');
          }}
          tip.innerHTML = text;
          const r = el.getBoundingClientRect();
          tip.style.left = Math.min(r.left + window.scrollX + 22, window.scrollX + document.documentElement.clientWidth - 280) + 'px';
          tip.style.top = (r.bottom + window.scrollY + 6) + 'px';
          tip.classList.add('show');
        }});
        el.addEventListener('mouseleave', () => tip.classList.remove('show'));
      }});
    }})();
    </script>"""


def offsite_backup_warning():
    """Yellow bar on the home page when the off-device backup goes stale."""
    try:
        with open(OFFSITE_STAMP) as f:
            age = time.time() - float(f.read().strip())
    except (OSError, ValueError):
        age = None
    if age is not None and age < OFFSITE_MAX_AGE_S:
        return ""
    detail = (f"ostatni {age / 86400:.1f} dn. temu"
              if age is not None else "jeszcze nigdy nie wykonany")
    return ('<div class="warnbar">&#9888; Backup off-site nieaktualny — '
            f'{detail} (sprawdź gcode-nas-backup)</div>')


def render_home(db):
    open_f = db.execute("SELECT * FROM failures WHERE closed_at IS NULL"
                        " ORDER BY blocking DESC, opened_at ASC").fetchall()
    n_block = sum(1 for f in open_f if f["blocking"])
    n_degr = len(open_f) - n_block

    counts = (
        f'<div class="counts"><span class="badge b-block{" alive" if n_block else ""}">{n_block} BLOKAD</span> '
        f'<span class="badge b-degr">{n_degr} USZKODZONYCH</span></div>')

    if open_f:
        rows = "\n".join(failure_row(f) for f in open_f)
        failures_html = (
            f'<table><tr><th>Drukarka</th><th>Stan</th>'
            f'<th>Awaria</th><th>Zgłoszona / czas</th></tr>{rows}</table>')
    else:
        failures_html = '<div class="empty">Brak aktywnych awarii 🎉</div>'

    # printers overview: last event + open counts + 30-day blocked downtime
    printers = db.execute(
        "SELECT DISTINCT hostname FROM events ORDER BY hostname").fetchall()
    cutoff = (datetime.now() -
              timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    prows = []
    for p in printers:
        h = p["hostname"]
        opens = db.execute(
            "SELECT COUNT(*) c, MAX(blocking) b FROM failures"
            " WHERE hostname=? AND closed_at IS NULL", (h, )).fetchone()
        last = db.execute(
            "SELECT received_at FROM events WHERE hostname=?"
            " ORDER BY id DESC LIMIT 1", (h, )).fetchone()
        downtime = 0
        for f in db.execute(
                "SELECT opened_at, closed_at FROM failures WHERE hostname=?"
                " AND blocking=1 AND (closed_at IS NULL OR closed_at>?)",
            (h, cutoff)):
            t0 = max(f["opened_at"], cutoff)
            t1 = f["closed_at"] or now_str()
            try:
                downtime += max(0,
                                (datetime.strptime(t1, "%Y-%m-%d %H:%M:%S") -
                                 datetime.strptime(
                                     t0, "%Y-%m-%d %H:%M:%S")).total_seconds())
            except ValueError:
                pass
        state = ('<span class="badge b-block">BLOKADA</span>'
                 if opens["b"] else
                 ('<span class="badge b-degr">USZKODZONA</span>'
                  if opens["c"] else '<span class="badge b-ok">OK</span>'))
        fw = gc = "—"
        if live := live_of(h):
            v = live[0]
            fw = e(v["fw_version"]) if isinstance(
                v.get("fw_version"), str) and v["fw_version"] else "—"
            gc = e(v["gcode_release"]) if isinstance(
                v.get("gcode_release"), str) and v["gcode_release"] else "—"
        prows.append(
            f"""<tr><td class="host"><a class="host" href="/awaria/printer/{urllib.parse.quote(h)}">{e(h)}</a>
            {flag_chips(db, h)}</td>
            <td>{state}</td><td>{opens['c']}</td>
            <td>{downtime / 3600:.1f} h</td>
            <td class="muted">{fw}</td><td class="muted">{gc}</td>
            <td class="muted">{e(last['received_at'] if last else '-')}</td></tr>"""
        )
    printers_html = ('<table><tr><th>Drukarka</th><th>Stan</th><th>Otwarte awarie</th>'
                     '<th>Przestój (30 dni)</th><th>Firmware</th><th>G-code</th><th>Ostatnie zdarzenie</th></tr>'
                     + "\n".join(prows) + "</table>") if prows else \
        '<div class="empty">Żadna drukarka jeszcze nic nie zgłosiła.</div>'

    body = f"""{offsite_backup_warning()}<h2>Aktywne awarie</h2>{failures_html}
    <div class="sec-head"><h2>Drukarki</h2>
      <div class="view-toggle">
        <button class="tab" data-view="map" onclick="setView('map')">Mapa</button>
        <button class="tab" data-view="list" onclick="setView('list')">Lista</button>
      </div>
    </div>
    <div id="view-map">{render_map(db)}</div>
    <div id="view-list">{printers_html}</div>
    <script>
    function setView(v) {{
      localStorage.setItem('printer_view', v);
      document.getElementById('view-map').classList.toggle('hidden', v !== 'map');
      document.getElementById('view-list').classList.toggle('hidden', v !== 'list');
      document.querySelectorAll('.view-toggle .tab').forEach(t =>
        t.classList.toggle('active', t.dataset.view === v));
    }}
    setView(localStorage.getItem('printer_view') || 'map');
    </script>"""
    return page("GRIZZLA — panel serwisowy", (counts, body), refresh=90)


def render_telemetry(host):
    """Inner HTML of the live-telemetry card; also served as a partial for
    the in-place background refresh on the printer page."""
    live = live_of(host)
    if not live:
        return (
            '<p class="muted">Brak danych — drukarka nie wysyła metryk '
            '(wymaga firmware ≥ 11240 albo ręcznego włączenia w Settings → Metrics).</p>'
        )
    v, updated = live
    rows = []
    if isinstance(v.get("print_filename"), str) and v["print_filename"]:
        rows.append(("Drukowany plik", e(v["print_filename"])))
        if isinstance(v.get("print_progress"), float):
            rows.append(("Postęp", f"{v['print_progress']:.0f}%"))
    else:
        rows.append(
            ("Drukowany plik", '<span class="muted">nic nie drukuje</span>'))
    if isinstance(v.get("temp_noz"), float):
        target = f" / {v['ttemp_noz']:.0f}°C" if isinstance(
            v.get("ttemp_noz"), float) and v["ttemp_noz"] else ""
        rows.append(("Dysza", f"{v['temp_noz']:.1f}°C{target}"))
    if isinstance(v.get("temp_bed"), float):
        target = f" / {v['ttemp_bed']:.0f}°C" if isinstance(
            v.get("ttemp_bed"), float) and v["ttemp_bed"] else ""
        rows.append(("Stół", f"{v['temp_bed']:.1f}°C{target}"))
    if isinstance(v.get("temp_brd"), float):
        rows.append(("Płyta xBuddy", f"{v['temp_brd']:.1f}°C"))
    if isinstance(v.get("temp_mcu"), float):
        rows.append(("MCU", f"{v['temp_mcu']:.0f}°C"))
    if isinstance(v.get("fw_version"), str) and v["fw_version"]:
        rows.append(("Firmware", e(v["fw_version"])))
    if isinstance(v.get("gcode_release"), str) and v["gcode_release"]:
        rows.append(("Wydanie g-code", e(v["gcode_release"])))
    if isinstance(v.get("gcode_check"), str) and v["gcode_check"]:
        rows.append(("Kontrola aktualizacji", e(v["gcode_check"])))
    return (
        "<table class='plain'>" +
        "".join(f"<tr><td class='muted'>{k}</td><td><b>{val}</b></td></tr>"
                for k, val in rows) +
        f"</table><p class='muted'>aktualizacja {int(time.time() - updated)} s temu</p>"
    )


def render_printer(db, host):
    quoted = urllib.parse.quote(host)
    printer = db.execute("SELECT * FROM printers WHERE hostname=?",
                         (host, )).fetchone()
    opens = db.execute(
        "SELECT COUNT(*) c, MAX(blocking) b FROM failures"
        " WHERE hostname=? AND closed_at IS NULL", (host, )).fetchone()
    state = ('<span class="badge b-block">BLOKADA</span>' if opens["b"] else
             ('<span class="badge b-degr">USZKODZONA</span>'
              if opens["c"] else '<span class="badge b-ok">SPRAWNA</span>'))

    # components with the date of their last replacement on this printer
    comp_rows = []
    components = db.execute(
        "SELECT c.id, c.name, MAX(m.done_at) last FROM components c"
        " LEFT JOIN maintenance m ON m.component_id = c.id AND m.hostname = ?"
        " GROUP BY c.id ORDER BY c.position, c.id", (host, )).fetchall()
    for c in components:
        comp_rows.append(
            f"<tr><td>{e(c['name'])}</td>"
            f"<td>{e(c['last'][:10]) if c['last'] else '<span class=muted>—</span>'}</td></tr>"
        )
    comp_options = "".join(f'<option value="{c["id"]}">{e(c["name"])}</option>'
                           for c in components)

    suggested_tags, color_swatches = flag_suggestions(db, host)
    built_on = printer["built_on"] if printer and printer["built_on"] else ""
    info_html = f"""
    <div class="cards">
      <div class="card">
        <h3>Stan</h3>
        <p style="font-size:18px">{state}</p>
        <div class="chips">Oznaczenia: {flag_chips(db, host, removable=True) or '<span class="muted">brak</span>'}</div>
        {f'<div class="chips muted">Ostatnio używane: {suggested_tags}</div>' if suggested_tags else ''}
        <form method="post" action="/awaria/printer/{quoted}/flag_add" class="inline-form">
          <input name="text" size="10" maxlength="16" placeholder="np. RND" required>
          <input type="color" name="color" id="flagcolor" value="#607d8b" title="Kolor">
          {color_swatches}
          <input type="submit" value="Dodaj oznaczenie">
        </form>
        <form method="post" action="/awaria/printer/{quoted}/update" class="inline-form">
          <label>Data budowy: <input type="date" name="built_on" value="{e(built_on)}"></label>
          <label>Adres IP: <input name="last_ip" size="14" placeholder="auto (mDNS)"
                 value="{e(printer['last_ip'] if printer and printer['last_ip'] else '')}"></label>
          <input type="submit" value="Zapisz">
        </form>
        <p class="muted">{'🟢 online' if is_online(host) else '⚪ offline / brak IP'} —
           IP wykrywane automatycznie (skan sieci + mDNS co ok. 4 min); wpis ręczny nadpisuje.</p>
      </div>
      <div class="card">
        <h3>Telemetria (na żywo)</h3>
        <div id="telemetry-body">{render_telemetry(host)}</div>
      </div>
      <div class="card" style="flex-basis:100%">
        <h3>Temperatury (ostatnie 30 min)</h3>
        <div id="tchart"><p class="muted">Zbieranie danych...</p></div>
      </div>
      <div class="card">
        <h3>Komponenty — ostatnia wymiana / konserwacja</h3>
        <table class="plain">{''.join(comp_rows)}</table>
        <form method="post" action="/awaria/printer/{quoted}/maintenance" class="inline-form">
          <select name="component_id">{comp_options}</select>
          <input type="date" name="done_at" value="{datetime.now().strftime('%Y-%m-%d')}">
          <input type="submit" value="Odnotuj wymianę">
        </form>
      </div>
    </div>"""

    failures = db.execute(
        "SELECT * FROM failures WHERE hostname=?"
        " ORDER BY (closed_at IS NULL) DESC, opened_at DESC LIMIT 200",
        (host, )).fetchall()
    events = db.execute(
        "SELECT * FROM events WHERE hostname=? ORDER BY id DESC LIMIT 300",
        (host, )).fetchall()

    frows = "\n".join(failure_row(f, show_host=False) for f in failures) or ""
    failures_html = (
        f'<table><tr><th>Stan</th><th>Awaria</th><th>Zgłoszona / czas</th></tr>{frows}</table>'
        if failures else '<div class="empty">Brak awarii w historii.</div>')
    erows = "\n".join(
        f"<tr><td class='muted'>{e(ev['received_at'])}</td><td>{e(ev['action'])}</td>"
        f"<td><b>{e(ev['label'])}</b><div class='detail'>{e(ev['detail'])}</div></td></tr>"
        for ev in events)
    events_html = (
        f"<table><tr><th>Odebrano</th><th>Zdarzenie</th><th>Opis</th></tr>{erows}</table>"
        if events else '<div class="empty">Brak zdarzeń.</div>')
    # background refresh of just the telemetry card + chart (a full-page swap
    # would wipe half-filled forms on this page)
    chart_js = f"""
    <link rel="stylesheet" href="/awaria/static/uPlot.min.css">
    <script src="/awaria/static/uPlot.iife.min.js"></script>
    <script>
    (function() {{
      const host = encodeURIComponent({json.dumps(host).replace("<", "\\u003c")});
      let chart = null;
      async function tick() {{
        try {{
          const body = await (await fetch('/awaria/partial/telemetry/' + host)).text();
          document.getElementById('telemetry-body').innerHTML = body;
          const data = await (await fetch('/awaria/api/history/' + host)).json();
          if (data[0] && data[0].length > 1) {{
            const el = document.getElementById('tchart');
            if (!chart) {{
              el.innerHTML = '';
              chart = new uPlot({{
                width: el.clientWidth || 800, height: 260,
                series: [ {{}},
                  {{label: 'Dysza', stroke: '#d32f2f', width: 2}},
                  {{label: 'Dysza cel', stroke: '#d32f2f', dash: [6, 6]}},
                  {{label: 'Stół', stroke: '#1565c0', width: 2}},
                  {{label: 'Stół cel', stroke: '#1565c0', dash: [6, 6]}},
                  {{label: 'Płyta xBuddy', stroke: '#2e7d32'}} ],
              }}, data, el);
              window.addEventListener('resize',
                () => chart && chart.setSize({{width: el.clientWidth, height: 260}}));
            }} else {{
              chart.setData(data);
            }}
          }}
        }} catch (err) {{ /* server unreachable - keep last view */ }}
      }}
      tick();
      setInterval(tick, 5000);
    }})();
    </script>"""

    body = (f"<h2>Drukarka {e(host)}</h2>{info_html}"
            f"<h2>Dziennik awarii</h2>{failures_html}"
            f"<h2>Dziennik zdarzeń</h2>{events_html}{chart_js}")
    return page(f"GRIZZLA — {host}", ("", body))


def render_failure(db, fid):
    f = db.execute("SELECT * FROM failures WHERE id=?", (fid, )).fetchone()
    if not f:
        return None
    quoted_host = urllib.parse.quote(f["hostname"])

    done = db.execute(
        "SELECT m.*, c.name FROM maintenance m LEFT JOIN components c ON c.id = m.component_id"
        " WHERE m.failure_id=? ORDER BY m.id", (fid, )).fetchall()
    done_html = "".join(
        f"<li>{e(m['done_at'][:10])} — <b>{e(m['name'] or m['action'] or '?')}</b>"
        f"{(' <span class=muted>(' + e(m['action']) + ')</span>') if m['name'] and m['action'] else ''}</li>"
        for m in done) or '<li class="muted">nic jeszcze nie odnotowano</li>'

    checkboxes = "".join(
        f'<label class="check"><input type="checkbox" name="component" value="{c["id"]}"> {e(c["name"])}</label>'
        for c in db.execute(
            "SELECT id, name FROM components ORDER BY position, id"))

    session = db.execute(
        "SELECT file, material FROM print_log WHERE id=?",
        (f["print_session_id"], )).fetchone() \
        if f["print_session_id"] else None
    session_info = (
        f'<p>Podczas wydruku: <b title="{e(session["file"])}">{e(display_name(session["file"]))}</b>'
        f'{" (" + e(session["material"]) + ")" if session["material"] else ""}</p>'
        if session else "")

    status = state_badge_of(f)
    closed_info = (
        f'<p>Naprawiona: <b>{e(f["closed_at"])}</b> ({e(f["closed_by"] or "?")}) '
        f'— czas awarii: <b>{fmt_age(f["opened_at"], f["closed_at"])}</b></p>'
        if f["closed_at"] else
        f'<p>Otwarta od: <b>{e(f["opened_at"])}</b> — trwa <b>{fmt_age(f["opened_at"])}</b></p>'
    )
    close_box = "" if f["closed_at"] else \
        '<p><label class="check"><input type="checkbox" name="close" checked> zamknij awarię (naprawa zakończona)</label></p>'

    body = f"""
    <p><a href="/awaria/printer/{quoted_host}">&larr; Drukarka {e(f['hostname'])}</a></p>
    <h2>Awaria: {e(f['label'])} {status}</h2>
    <div class="card">
      <div class="detail" style="font-size:15px">{e(f['detail'])}</div>
      {session_info}
      {closed_info}
    </div>
    <h2>Naprawa</h2>
    <div class="card">
      <p><b>Wykonane czynności:</b></p><ul>{done_html}</ul>
      <form method="post" action="/awaria/failure/{fid}/repair">
        <p><b>Notatka serwisowa:</b><br>
           <textarea name="note" rows="3" style="width:100%">{e(f['repair_note'] or '')}</textarea></p>
        <p><b>Wymienione części / wykonana konserwacja:</b></p>
        <div class="checks">{checkboxes}</div>
        <p>Inne czynności: <input name="action" size="40" maxlength="120"
           placeholder="np. czyszczenie ekstrudera"></p>
        {close_box}
        <p><input type="submit" value="Zapisz naprawę"></p>
      </form>
    </div>"""
    return page(f"GRIZZLA — awaria {f['hostname']}", ("", body))


def render_components(db):
    rows = []
    for c in db.execute(
            "SELECT c.id, c.name, c.position, COUNT(m.id) used FROM components c"
            " LEFT JOIN maintenance m ON m.component_id = c.id"
            " GROUP BY c.id ORDER BY c.position, c.id"):
        delete = (
            "" if c["used"] else
            f'<form method="post" action="/awaria/components/del" class="inline-form">'
            f'<input type="hidden" name="id" value="{c["id"]}">'
            f'<input type="submit" value="usuń"></form>')
        rows.append(f"""<tr data-id="{c['id']}">
            <td class="drag" title="Przeciągnij, aby zmienić kolejność">&#8801;</td>
            <td><form method="post" action="/awaria/components/edit" class="inline-form">
                <input type="hidden" name="id" value="{c['id']}">
                <input name="name" value="{e(c['name'])}" size="30" maxlength="40" required>
                <input type="submit" value="Zapisz">
            </form></td>
            <td class='muted'>{c['used']} wpisów</td><td>{delete}</td></tr>""")
    body = f"""
    <h2>Części zamienne i czynności serwisowe</h2>
    <table id="components"><thead><tr><th></th><th>Nazwa (edytuj i zapisz)</th><th>Użycia</th><th></th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
    <form method="post" action="/awaria/components/add" class="inline-form" style="margin-top:10px">
      <input name="name" size="34" maxlength="40" placeholder="nowa część / czynność" required>
      <input type="submit" value="Dodaj">
    </form>
    <p class="muted">Kolejność listy (przeciągnij za &#8801;) obowiązuje we wszystkich formularzach
    napraw. Pozycje z historią wpisów nie mogą być usunięte.</p>
    <script src="/awaria/static/Sortable.min.js"></script>
    <script>
    new Sortable(document.querySelector('#components tbody'), {{
      handle: '.drag', animation: 180, easing: 'cubic-bezier(.22,.9,.32,1)',
      ghostClass: 'ghost', chosenClass: 'drag-active',
      onEnd: () => {{
        const ids = [...document.querySelectorAll('#components tbody tr[data-id]')].map(r => +r.dataset.id);
        fetch('/awaria/components/reorder', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
               body: JSON.stringify({{ids}})}}).then(r => {{ if (!r.ok) location.reload(); }});
      }},
    }});
    </script>"""
    return page("GRIZZLA — części zamienne", ("", body))


def effective_seconds(t0, t1, include_weekends):
    """Seconds between two epochs, optionally counting Mon-Fri only."""
    if t1 <= t0:
        return 0
    if include_weekends:
        return t1 - t0
    total = 0
    cur = t0
    while cur < t1:
        d = datetime.fromtimestamp(cur)
        day_end = int(datetime(d.year, d.month, d.day).timestamp()) + 86400
        seg_end = min(day_end, t1)
        if d.weekday() < 5:
            total += seg_end - cur
        cur = seg_end
    return total


def merge_intervals(intervals):
    """Overlapping (start, end) epochs merged, so time is never counted twice."""
    merged = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def printer_usage(db, host, t_from, t_to, include_weekends):
    """(printing_s, down_s, active) for one printer in the window; active is
    False when there is nothing to count (not connected, no data) -> skip."""
    now = int(time.time())
    from_str = datetime.fromtimestamp(t_from).strftime("%Y-%m-%d %H:%M:%S")
    to_str = datetime.fromtimestamp(t_to).strftime("%Y-%m-%d %H:%M:%S")

    prints = [
        (max(to_epoch(r["started_at"]), t_from),
         min(to_epoch(r["ended_at"]) if r["ended_at"] else now, t_to))
        for r in db.execute(
            "SELECT started_at, ended_at FROM print_log WHERE hostname=?"
            " AND kind='prod'"
            " AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)", (
                host, to_str, from_str))
    ]
    fails = [(
        max(to_epoch(r["opened_at"]), t_from),
        min(to_epoch(r["closed_at"]) if r["closed_at"] else now, t_to)
    ) for r in db.execute(
        "SELECT opened_at, closed_at FROM failures WHERE hostname=? AND blocking=1"
        " AND opened_at <= ? AND (closed_at IS NULL OR closed_at >= ?)", (
            host, to_str, from_str))]

    if not prints and not fails and not is_online(host):
        return 0, 0, False
    printing = sum(
        effective_seconds(a, b, include_weekends)
        for a, b in merge_intervals(prints))
    down = sum(
        effective_seconds(a, b, include_weekends)
        for a, b in merge_intervals(fails))
    return printing, down, True


def fmt_hours(seconds):
    return f"{seconds / 3600:.1f} h"


def render_stats(db, query):
    include_weekends = bool(query.get("weekends")) or not query  # default: on
    rng = (query.get("range") or ["7"])[0]
    now = int(time.time())
    custom_from = (query.get("from") or [""])[0]
    custom_to = (query.get("to") or [""])[0]
    if rng == "custom" and custom_from and custom_to:
        try:
            t_from = int(
                datetime.strptime(custom_from, "%Y-%m-%d").timestamp())
            t_to = int(datetime.strptime(custom_to,
                                         "%Y-%m-%d").timestamp()) + 86400
        except ValueError:
            t_from, t_to = now - 7 * 86400, now
    else:
        days = {"7": 7, "30": 30, "365": 365}.get(rng, 7)
        rng = str(days)
        t_from, t_to = now - days * 86400, now
    t_to = min(t_to, now)

    hosts = {
        r["hostname"]: r["telemetry_since"]
        for r in db.execute(
            "SELECT hostname, telemetry_since FROM printers"
            " UNION SELECT hostname, NULL FROM events WHERE hostname NOT IN (SELECT hostname FROM printers)"
        )
    }

    rows = []
    farm_print = farm_down = farm_total = 0
    for h, telemetry_since in sorted(hosts.items()):
        # only printers that are connected AND currently report their printing
        # status (fw >= 11240 streaming print_filename) enter the statistics -
        # older-firmware printers have no print data and would look 100% idle
        live = live_of(h)
        reports_printing = bool(live) and isinstance(
            live[0].get("print_filename"), str)
        if not is_online(h) or not reports_printing:
            continue
        # the printer's window starts when it began reporting - time before
        # that would otherwise be counted as (fake) idle
        h_from = max(t_from,
                     to_epoch(telemetry_since)) if telemetry_since else t_from
        window = effective_seconds(h_from, t_to, include_weekends)
        if window <= 0:
            continue
        printing, down, _ = printer_usage(db, h, h_from, t_to,
                                          include_weekends)
        printing = min(printing, window)
        down = min(down, max(0, window - printing))
        idle = max(0, window - printing - down)
        farm_print += printing
        farm_down += down
        farm_total += window
        rows.append((h, printing, down, idle))

    def percentages(printing, down, total):
        if total <= 0:
            return 0.0, 0.0, 100.0
        p = printing * 100.0 / total
        d = down * 100.0 / total
        return p, d, max(0.0, 100.0 - p - d)

    donut = f"""
    <div class="stats-top">
      <div class="donut" id="donut">
        <div class="donut-hole"><b id="donut-pct">0%</b><span>druku</span></div>
      </div>
      <div class="legend">
        <p><span class="dot" style="background:#2e7d32"></span> Druk: <b id="lg-print"></b></p>
        <p><span class="dot" style="background:#d32f2f"></span> Awarie (blokada): <b id="lg-down"></b></p>
        <p><span class="dot" style="background:#f2c200"></span> Bezczynność: <b id="lg-idle"></b></p>
        <p class="muted"><span id="lg-count"></span> z {len(rows)} drukarek w sumie — tylko podłączone
        i raportujące status druku; czas każdej liczony od początku jej raportowania</p>
      </div>
    </div>"""

    # group by section (hostname letter prefix), numerically within a section
    def host_key(hostname):
        m = re.match(r"^([A-Za-z]+)(\d*)", hostname)
        return (m.group(1).upper(), int(m.group(2) or 0)) if m else (hostname,
                                                                     0)

    sections = {}
    for h, printing, down, idle in rows:
        sections.setdefault(host_key(h)[0], []).append(
            (h, printing, down, idle))

    groups = []
    for section in sorted(sections):
        printer_rows = []
        for h, printing, down, idle in sorted(sections[section],
                                              key=lambda r: host_key(r[0])):
            p, d, i = percentages(printing, down, printing + down + idle)
            printer_rows.append(
                f"""<div class="usage-row" data-print="{printing}" data-down="{down}" data-idle="{idle}">
              <input type="checkbox" class="p-check" checked title="Uwzględnij w sumie">
              <a class="host" href="/awaria/printer/{urllib.parse.quote(h)}">{e(h)}</a>
              <div class="ubar">
                <div style="width:{p:.2f}%;background:#2e7d32" title="Druk {p:.1f}% ({fmt_hours(printing)})"></div>
                <div style="width:{d:.2f}%;background:#d32f2f" title="Awarie {d:.1f}% ({fmt_hours(down)})"></div>
                <div style="width:{i:.2f}%;background:#f2c200" title="Bezczynność {i:.1f}% ({fmt_hours(idle)})"></div>
              </div>
              <span class="muted">{p:.0f}% druku ({fmt_hours(printing)}){f' · {d:.0f}% awarie' if d >= 0.5 else ''}</span>
            </div>""")
        groups.append(f"""<div class="sec-group">
          <label class="sec-toggle"><input type="checkbox" class="sec-check" checked>
            <b>Sekcja {e(section)}</b> <span class="muted">({len(printer_rows)})</span></label>
          {''.join(printer_rows)}
        </div>""")
    bars_html = "".join(
        groups) or '<div class="empty">Brak danych w wybranym okresie.</div>'

    recalc_js = """
    <script>
    (function() {
      const fmtH = s => (s / 3600).toFixed(1) + ' h';
      function recalc() {
        let p = 0, d = 0, i = 0, n = 0;
        document.querySelectorAll('.usage-row').forEach(row => {
          const on = row.querySelector('.p-check').checked;
          row.classList.toggle('excluded', !on);
          if (!on) { return; }
          p += +row.dataset.print; d += +row.dataset.down; i += +row.dataset.idle; n++;
        });
        const total = p + d + i;
        const pp = total ? p * 100 / total : 0, pd = total ? d * 100 / total : 0;
        document.getElementById('donut').style.background = 'conic-gradient(#2e7d32 0 ' + pp
          + '%, #d32f2f ' + pp + '% ' + (pp + pd) + '%, #f2c200 ' + (pp + pd) + '% 100%)';
        document.getElementById('donut-pct').textContent = pp.toFixed(0) + '%';
        document.getElementById('lg-print').textContent = pp.toFixed(1) + '% (' + fmtH(p) + ')';
        document.getElementById('lg-down').textContent = pd.toFixed(1) + '% (' + fmtH(d) + ')';
        document.getElementById('lg-idle').textContent = (total ? 100 - pp - pd : 100).toFixed(1) + '% (' + fmtH(i) + ')';
        document.getElementById('lg-count').textContent = n;
        // section checkbox states follow their printers (incl. indeterminate)
        document.querySelectorAll('.sec-group').forEach(g => {
          const boxes = [...g.querySelectorAll('.p-check')];
          const checked = boxes.filter(b => b.checked).length;
          const sec = g.querySelector('.sec-check');
          sec.checked = checked === boxes.length;
          sec.indeterminate = checked > 0 && checked < boxes.length;
        });
      }
      document.querySelectorAll('.p-check').forEach(b => b.addEventListener('change', recalc));
      document.querySelectorAll('.sec-check').forEach(sec => sec.addEventListener('change', () => {
        sec.closest('.sec-group').querySelectorAll('.p-check').forEach(b => { b.checked = sec.checked; });
        recalc();
      }));
      recalc();
    })();
    </script>"""

    range_options = "".join(
        f'<option value="{v}"{" selected" if v == rng else ""}>{label}</option>'
        for v, label in (("7", "1 tydzień"), ("30", "1 miesiąc"),
                         ("365", "1 rok"), ("custom", "własny zakres")))
    custom_display = "inline-flex" if rng == "custom" else "none"
    body = f"""
    <div class="sec-head"><h2>Statystyki wykorzystania</h2>
      <form method="get" action="/awaria/stats" class="inline-form">
        <select name="range" onchange="document.getElementById('custom-dates').style.display
            = this.value === 'custom' ? 'inline-flex' : 'none'">{range_options}</select>
        <span id="custom-dates" class="inline-form" style="display:{custom_display}">
          <input type="date" name="from" value="{e(custom_from)}"> —
          <input type="date" name="to" value="{e(custom_to)}"></span>
        <label><input type="checkbox" name="weekends" value="1"
               {"checked" if include_weekends else ""}> uwzględnij weekendy</label>
        <input type="submit" value="Pokaż">
      </form>
    </div>
    {donut}
    <h2>Drukarki</h2>
    <div class="card">{bars_html}</div>
    <p class="muted">Druk = sesje z dziennika wydruków (telemetria, od fw 11240); awarie = czas
    blokad z panelu; reszta okna = bezczynność. Najedź na pasek, aby zobaczyć godziny;
    odznacz drukarki lub całe sekcje, aby przeliczyć sumę na bieżąco.</p>
    {recalc_js}"""
    return page("GRIZZLA — statystyki", ("", body))


NET_EVENT_BADGE = {
    "offline": "b-block",
    "offline_mid_print": "b-block",
    "telemetry_lost_mid_print": "b-block",
    "online": "b-ok",
    "rediscovered": "b-degr",
    "ip_change": "b-degr",
    "print_session_reopened": "b-degr",
}


def render_netlog(db, query):
    """Connectivity audit viewer: transitions, IP changes, re-discoveries
    and telemetry silences - the material for judging network stability."""
    host = (query.get("host") or [""])[0][:32]
    where, args = ("WHERE hostname=?", [host]) if host else ("", [])
    rows = db.execute(
        f"SELECT * FROM net_log {where} ORDER BY id DESC LIMIT 500",
        args).fetchall()

    day_ago = int(time.time()) - 86400
    summary = db.execute(
        "SELECT hostname, COUNT(*) c FROM net_log"
        " WHERE event LIKE 'offline%' AND at_ts > ?"
        " GROUP BY hostname ORDER BY c DESC LIMIT 12", (day_ago, )).fetchall()
    chips = " ".join(
        f'<a class="chip" style="background:#607d8b" '
        f'href="/awaria/netlog?host={urllib.parse.quote(r["hostname"])}">'
        f'{e(r["hostname"])}: {r["c"]}&times;</a>' for r in summary)
    summary_html = (f'<div class="card"><h3>Najczęściej offline (24 h)</h3>'
                    f'<div class="chips">{chips}</div></div>'
                    if summary else "")

    trs = "".join(
        f"<tr><td class='age'>{e(r['at'])}</td>"
        f"<td class='host'><a class='host' href='/awaria/netlog?host="
        f"{urllib.parse.quote(r['hostname'])}'>{e(r['hostname'])}</a></td>"
        f"<td><span class='badge "
        f"{NET_EVENT_BADGE.get(r['event'], 'b-degr')}'>{e(r['event'])}"
        f"</span></td><td class='muted'>{e(r['detail'] or '')}</td></tr>"
        for r in rows)
    table = (f"<table><tr><th>Kiedy</th><th>Drukarka</th><th>Zdarzenie</th>"
             f"<th>Szczegóły</th></tr>{trs}</table>" if rows else
             '<div class="empty">Brak zdarzeń sieciowych.</div>')

    filter_note = (f'<p class="muted">Filtr: <b>{e(host)}</b> — '
                   f'<a href="/awaria/netlog">pokaż wszystkie</a></p>'
                   if host else "")
    body = f"""<h2>Log sieci</h2>
    <p class="muted">Rozłączenia (osobno oznaczone gdy przerwały druk), powroty,
    zmiany adresów IP i automatyczne ponowne wykrycia (mDNS), oraz utraty
    telemetrii w trakcie druku. Ostatnie 500 zdarzeń.</p>
    {summary_html}{filter_note}{table}"""
    return page("GRIZZLA — log sieci", ("", body))


def kind_badge(p):
    """Small marker on non-production sessions (visible with all=1)."""
    kind = p["kind"] if "kind" in p.keys() else "prod"
    if kind == "prod":
        return ""
    label = "serwis" if kind == "service" else "test"
    return f' <span class="badge b-degr">{label}</span>'


def render_history(db, query):
    host = (query.get("host") or [""])[0][:32]
    range_h = (query.get("range") or ["24"])[0]
    range_h = range_h if range_h in ("6", "24", "48", "96") else "24"
    try:
        t_from = int((query.get("from") or ["0"])[0])
        t_to = int((query.get("to") or ["0"])[0])
    except ValueError:
        t_from = t_to = 0
    if not t_from or not t_to:
        t_to = int(time.time())
        t_from = t_to - int(range_h) * 3600

    hosts = [
        r["hostname"] for r in db.execute(
            "SELECT hostname FROM printers UNION SELECT DISTINCT hostname FROM events ORDER BY 1"
        )
    ]
    host_options = '<option value="">— wybierz drukarkę —</option>' + "".join(
        f'<option value="{e(h)}"{" selected" if h == host else ""}>{e(h)}</option>'
        for h in hosts)
    range_options = "".join(
        f'<option value="{v}"{" selected" if v == range_h else ""}>{label}</option>'
        for v, label in (("6", "6 godzin"), ("24", "24 godziny"),
                         ("48", "2 dni"), ("96", "4 dni")))

    if host:
        chart_html = f"""
        <div class="card" style="flex-basis:100%"><h3>Temperatury — {e(host)}</h3>
          <div id="hchart"><p class="muted">Ładowanie...</p></div></div>
        <link rel="stylesheet" href="/awaria/static/uPlot.min.css">
        <script src="/awaria/static/uPlot.iife.min.js"></script>
        <script>
        (async function() {{
          const data = await (await fetch('/awaria/api/samples/{urllib.parse.quote(host)}?from={t_from}&to={t_to}')).json();
          const el = document.getElementById('hchart');
          if (!data[0] || data[0].length < 2) {{ el.innerHTML = '<p class="muted">Brak zapisanych danych w tym okresie.</p>'; return; }}
          el.innerHTML = '';
          new uPlot({{
            width: el.clientWidth || 900, height: 300,
            series: [ {{}},
              {{label: 'Dysza', stroke: '#d32f2f', width: 2}},
              {{label: 'Dysza cel', stroke: '#d32f2f', dash: [6, 6]}},
              {{label: 'Stół', stroke: '#1565c0', width: 2}},
              {{label: 'Stół cel', stroke: '#1565c0', dash: [6, 6]}},
              {{label: 'Płyta xBuddy', stroke: '#2e7d32'}} ],
          }}, data, el);
        }})();
        </script>"""
    else:
        chart_html = '<div class="empty">Wybierz drukarkę, aby zobaczyć wykres temperatur.</div>'

    week_ago = (datetime.now() -
                timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    show_all = bool(query.get("all"))
    kind_filter = "" if show_all else " AND kind='prod'"
    if host:
        prints = db.execute(
            f"SELECT * FROM print_log WHERE started_at>=? AND hostname=?"
            f"{kind_filter} ORDER BY started_at DESC LIMIT 200",
            (week_ago, host)).fetchall()
    else:
        prints = db.execute(
            f"SELECT * FROM print_log WHERE started_at>=?"
            f"{kind_filter} ORDER BY started_at DESC LIMIT 200",
            (week_ago, )).fetchall()
    prows = []
    for p in prints:
        start_e = to_epoch(p["started_at"])
        end_e = to_epoch(p["ended_at"]) if p["ended_at"] else int(time.time())
        link = (f'/awaria/history?host={urllib.parse.quote(p["hostname"])}'
                f'&from={start_e - 300}&to={end_e + 300}')
        duration = fmt_age(p["started_at"], p["ended_at"]) if p["ended_at"] else \
            f'<span class="badge b-ok">w trakcie</span> {fmt_age(p["started_at"])}'
        prows.append(f"""<tr>
            <td class="host"><a class="host" href="/awaria/printer/{urllib.parse.quote(p['hostname'])}">{e(p['hostname'])}</a></td>
            <td title="{e(p['file'])}"><b>{e(display_name(p['file']))}</b>{kind_badge(p)}</td>
            <td class="age">{e(p['started_at'])}</td><td>{duration}</td>
            <td><a href="{link}">wykres</a></td></tr>""")
    prints_html = (
        f'<table><tr><th>Drukarka</th><th>Plik</th><th>Start</th><th>Czas</th><th></th></tr>'
        f'{"".join(prows)}</table>' if prows else
        '<div class="empty">Brak zarejestrowanych wydruków w ostatnim tygodniu.</div>'
    )

    body = f"""
    <div class="sec-head"><h2>Historia telemetrii</h2>
      <form method="get" action="/awaria/history" class="inline-form">
        <select name="host">{host_options}</select>
        <select name="range">{range_options}</select>
        <input type="submit" value="Pokaż">
      </form>
    </div>
    {chart_html}
    <h2>Wydruki (ostatnie 7 dni)
      <a style="float:right;font-weight:400;font-size:13px"
         href="/awaria/history?all={'' if show_all else '1'}{'&host=' + urllib.parse.quote(host) if host else ''}">
         {'ukryj testy i serwisowe' if show_all else 'pokaż też testy i serwisowe'}</a></h2>{prints_html}
    <p class="muted">Historia temperatur: 1 próbka / {FINE_EVERY_S} s na drukarkę, przechowywana
    {FINE_KEEP_S // 86400} dni; długie zakresy są uśredniane do ~3600 punktów — zawęź zakres
    (albo kliknij "wykres" przy wydruku), aby zobaczyć pełny detal. Wydruki wykrywane z
    telemetrii (wymaga firmware ≥ 11240).</p>"""
    return page("GRIZZLA — historia", ("", body))


def render_defs_list(db):
    seq = db.execute(
        "SELECT value FROM meta WHERE key='catalog_seq'").fetchone()
    rows = []
    for d in db.execute("SELECT * FROM error_defs ORDER BY position, id"):
        try:
            n_questions = len(json.loads(d["questions"]))
        except json.JSONDecodeError:
            n_questions = 0
        badges = []
        if d["print_ctx"]:
            badges.append("plik+podłoże")
        if d["hidden"]:
            badges.append("ukryty")
        rows.append(
            f"""<tr data-id="{d['id']}"{' style="opacity:.5"' if d['hidden'] else ''}>
            <td class="drag" title="Przeciągnij, aby zmienić kolejność">&#8801;</td>
            <td><b>{e(d['label'])}</b></td>
            <td>{e(SEVERITY_NAMES.get(d['severity'], '?'))}</td>
            <td>{n_questions or '-'}</td><td class="muted">{e(', '.join(badges))}</td>
            <td><a href="/awaria/defs/{d['id']}">edytuj</a></td></tr>""")
    body = f"""
    <h2>Katalog błędów (wersja {e(seq['value'] if seq else '1')})
        <a style="float:right;font-weight:400" href="/awaria/defs/new">+ Zdefiniuj nowy błąd</a></h2>
    <table id="defs"><thead><tr><th></th><th>Nazwa</th><th>Ważność</th><th>Pytania</th><th></th><th></th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
    <p class="muted">Kolejność listy = kolejność w menu drukarki — przeciągnij wiersze za uchwyt &#8801;.
    Drukarki pobierają katalog przy każdej synchronizacji g-code, przy starcie oraz z menu
    Settings → "Aktualizuj listę awarii". Błędów nie można usuwać (ich numery są zapisane
    w drukarkach) — zamiast tego oznacz je jako ukryte. Limity znaków wynikają z pamięci drukarki.</p>
    <script src="/awaria/static/Sortable.min.js"></script>
    <script>
    new Sortable(document.querySelector('#defs tbody'), {{
      handle: '.drag',
      animation: 180,          // the other rows glide up/down while dragging
      easing: 'cubic-bezier(.22,.9,.32,1)',
      ghostClass: 'ghost',
      chosenClass: 'drag-active',
      onEnd: () => {{
        const ids = [...document.querySelectorAll('#defs tbody tr[data-id]')].map(r => +r.dataset.id);
        fetch('/awaria/defs/reorder', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
               body: JSON.stringify({{ids}})}}).then(r => {{ if (!r.ok) location.reload(); }});
      }},
    }});
    </script>"""
    return page("GRIZZLA — katalog błędów", ("", body))


def render_def_form(db, def_row):
    d = def_row or {
        "id": "",
        "label": "",
        "severity": 1,
        "print_ctx": 0,
        "hidden": 0,
        "position": 100,
        "questions": "[]"
    }
    try:
        questions = json.loads(d["questions"])
    except json.JSONDecodeError:
        questions = []

    def sev_select(name, value, allow_none):
        options = [
            '<option value="-"%s>— bez zmiany —</option>' %
            (" selected" if value is None else "")
        ] if allow_none else []
        for k, label in SEVERITY_NAMES.items():
            options.append('<option value="%d"%s>%s</option>' %
                           (k, " selected" if value == k else "", e(label)))
        return '<select name="%s">%s</select>' % (name, "".join(options))

    q_blocks = []
    for qi in range(2):
        q = questions[qi] if qi < len(questions) else {}
        answers = q.get("answers", [])
        a_rows = []
        for ai in range(3):
            a = answers[ai] if ai < len(answers) else {}
            a_rows.append(f"""<tr><td>Odpowiedź {ai + 1}</td>
                <td><input name="q{qi}_a{ai}_text" maxlength="19" size="22"
                     value="{e(a.get('text', ''))}" placeholder="maks. {ANSWER_MAX_B} bajtów"></td>
                <td>ważność po tej odpowiedzi: {sev_select(f'q{qi}_a{ai}_sev', a.get('severity'), True)}</td></tr>"""
                          )
        q_blocks.append(
            f"""<fieldset><legend>Pytanie {qi + 1} (opcjonalne, min. 2 odpowiedzi)</legend>
            <input name="q{qi}_text" size="52" maxlength="47" value="{e(q.get('text', ''))}"
                   placeholder="treść pytania, maks. {QUESTION_MAX_B} bajtów">
            <table>{''.join(a_rows)}</table></fieldset>""")

    body = f"""
    <h2>{'Edycja błędu #%s' % e(d['id']) if def_row else 'Nowy błąd'}</h2>
    <form method="post" action="/awaria/defs/save" class="wizard">
      <input type="hidden" name="id" value="{e(d['id'])}">
      <p><label>Nazwa (na liście zgłaszania i na ekranie AWARIA):<br>
         <input name="label" size="52" maxlength="39" required value="{e(d['label'])}"></label></p>
      <p><label>Ważność: {sev_select('severity', d['severity'], False)}</label></p>
      <p><label><input type="checkbox" name="print_ctx" {'checked' if d['print_ctx'] else ''}>
         dołącz informacje o wydruku (plik + print sheet)</label><br>
         <label><input type="checkbox" name="hidden" {'checked' if d['hidden'] else ''}>
         ukryty (nie pokazuj w menu zgłaszania)</label></p>
      {''.join(q_blocks)}
      <p>Uwaga: odpowiedzi są przyciskami na ekranie drukarki — im krótsze, tym lepiej.
         Odpowiedź może zmienić ważność zgłoszenia (np. "Nie działa" → krytyczna).</p>
      <p><input type="submit" value="Zapisz i opublikuj nową wersję katalogu">
         <a href="/awaria/defs">anuluj</a></p>
    </form>"""
    return page("AWARIA — definicja błędu", ("", body))
