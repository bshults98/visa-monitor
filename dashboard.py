"""Static dashboard renderer for the BD visa dropdown monitor.

Writes a single self-contained `index.html` (no JS frameworks, no CDN) plus
`status.json`, so the output can be served by any static host -- GitHub
Pages, Cloudflare Pages, a plain nginx directory, or just opened locally.

Design choices:
  * Data is inlined into the HTML at render time. The page works even if the
    host cannot reach SQLite/JSONL at request time (i.e. everywhere static).
  * Pure vanilla HTML/CSS/JS, no external requests -- nothing to break, and
    nothing to leak if the page is password-gated behind a proxy.
  * Colour is never the only signal: every state also carries a text label
    and an icon, so the page is readable in greyscale / by colour-blind users.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>BD Visa Slot Monitor</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
    --fg:#e6e9ef; --dim:#98a2b3; --ok:#2ecc71; --bad:#e74c3c;
    --warn:#f39c12; --acc:#4da3ff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:1080px;margin:0 auto;padding:24px 16px 60px}
  header{display:flex;flex-wrap:wrap;gap:14px;align-items:center;
    justify-content:space-between;margin-bottom:22px}
  h1{font-size:20px;margin:0;font-weight:650;letter-spacing:.2px}
  h1 span{color:var(--dim);font-weight:450;font-size:14px;margin-left:8px}
  .pill{display:inline-flex;align-items:center;gap:8px;padding:7px 13px;
    border-radius:999px;font-weight:650;font-size:13.5px;border:1px solid}
  .pill.on{background:rgba(46,204,113,.13);color:var(--ok);border-color:rgba(46,204,113,.45)}
  .pill.off{background:rgba(231,76,60,.13);color:var(--bad);border-color:rgba(231,76,60,.45)}
  .pill.unk{background:rgba(243,156,18,.13);color:var(--warn);border-color:rgba(243,156,18,.45)}
  .dot{width:9px;height:9px;border-radius:50%;background:currentColor}
  .summary{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--acc);
    border-radius:10px;padding:15px 17px;margin-bottom:20px;font-size:14.5px}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin-bottom:24px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:15px}
  .stat .k{font-size:11.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px}
  .stat .v{font-size:26px;font-weight:680;margin-top:5px;line-height:1.15}
  .stat .s{font-size:12.5px;color:var(--dim);margin-top:3px}
  section{background:var(--panel);border:1px solid var(--line);border-radius:11px;
    padding:17px 18px;margin-bottom:18px}
  h2{font-size:14.5px;margin:0 0 14px;font-weight:640;letter-spacing:.2px;
    display:flex;justify-content:space-between;align-items:center;gap:10px}
  h2 em{font-style:normal;color:var(--dim);font-weight:450;font-size:12.5px}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  th{text-align:left;color:var(--dim);font-weight:550;font-size:11.5px;
    text-transform:uppercase;letter-spacing:.6px;padding:0 8px 8px;border-bottom:1px solid var(--line)}
  td{padding:9px 8px;border-bottom:1px solid rgba(42,47,58,.6)}
  tr:last-child td{border-bottom:none}
  .tag{display:inline-block;padding:2px 9px;border-radius:5px;font-size:11.5px;font-weight:660}
  .tag.on{background:rgba(46,204,113,.15);color:var(--ok)}
  .tag.off{background:rgba(231,76,60,.15);color:var(--bad)}
  .tag.unk{background:rgba(243,156,18,.15);color:var(--warn)}
  .bars{display:flex;align-items:flex-end;gap:3px;height:96px;padding-top:6px}
  .bar{flex:1;border-radius:3px 3px 0 0;background:var(--bad);min-width:5px;position:relative;
    transition:opacity .15s}
  .bar:hover{opacity:.75}
  .bar i{position:absolute;bottom:100%;left:50%;transform:translateX(-50%);
    background:#000;color:#fff;font-size:11px;padding:3px 7px;border-radius:4px;
    white-space:nowrap;opacity:0;pointer-events:none;transition:opacity .12s;z-index:5}
  .bar:hover i{opacity:1}
  .axis{display:flex;justify-content:space-between;color:var(--dim);
    font-size:11.5px;margin-top:7px}
  .heat{display:grid;grid-template-columns:repeat(12,1fr);gap:5px}
  .cell{aspect-ratio:1.7;border-radius:5px;display:flex;align-items:center;
    justify-content:center;font-size:11px;font-weight:640;color:#0f1115}
  .note{color:var(--dim);font-size:12.5px;margin-top:9px}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
  .empty{color:var(--dim);font-style:italic;padding:14px 4px}
  footer{color:var(--dim);font-size:12.5px;text-align:center;margin-top:26px;
    padding-top:16px;border-top:1px solid var(--line)}
  @media(max-width:640px){ .heat{grid-template-columns:repeat(6,1fr)} .stat .v{font-size:22px} }
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>BD Visa Slot Monitor<span id="field"></span></h1>
  <div id="statusPill" class="pill unk"><span class="dot"></span><span>loading…</span></div>
</header>

<div class="summary" id="summary">Loading…</div>

<div class="grid" id="stats"></div>

<section>
  <h2>Availability trend <em id="trendRange"></em></h2>
  <div class="bars" id="trend"></div>
  <div class="axis"><span id="trendFirst"></span><span>uptime % per day (BST)</span><span id="trendLast"></span></div>
  <div class="note">Each bar = one calendar day in BST. Hover a bar for exact figures.</div>
</section>

<section>
  <h2>Which hours does BD tend to open? <em>BST hour-of-day, last __DAYS__ days</em></h2>
  <div class="heat" id="heat"></div>
  <div class="note">Brighter = higher share of checks where Bangladesh was present. Grey = no data yet.</div>
</section>

<section>
  <h2>State changes <em>exact online / offline times</em></h2>
  <div id="transitions"></div>
</section>

<section>
  <h2>Recent checks <em>last __SAMPLES__ polls</em></h2>
  <div id="samples"></div>
</section>

<footer id="foot"></footer>

</div>

<script id="payload" type="application/json">__DATA__</script>
<script>
(function(){
  var D = JSON.parse(document.getElementById('payload').textContent);

  function esc(s){ return String(s==null?'':s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function tz(ts){
    if(!ts) return '—';
    try {
      var d = new Date(ts);
      return d.toLocaleString(undefined,{year:'numeric',month:'short',day:'2-digit',
        hour:'2-digit',minute:'2-digit',second:'2-digit'});
    } catch(e){ return esc(ts); }
  }
  function cls(s){ return s==='ONLINE'?'on':(s==='OFFLINE'?'off':'unk'); }
  function label(s){ return s==='ONLINE'?'ONLINE':(s==='OFFLINE'?'OFFLINE':'UNKNOWN'); }
  function icon(s){ return s==='ONLINE'?'●':(s==='OFFLINE'?'○':'◐'); }

  var cur = D.current;
  document.getElementById('field').textContent = D.field ? (" · "+D.field) : '';

  // --- status pill + plain-English summary
  var pill = document.getElementById('statusPill');
  if (cur) {
    var c = cls(cur.state);
    pill.className = 'pill ' + c;
    pill.innerHTML = '<span class="dot"></span><span>' + icon(cur.state)+' '+label(cur.state)+'</span>';

    var head = cur.state==='ONLINE'
      ? 'Bangladesh <b>is currently present</b> in the country dropdown'
      : (cur.state==='OFFLINE'
        ? 'Bangladesh <b>is currently absent</b> from the country dropdown'
        : 'Monitor state is <b>unknown</b> — the site could not be read');

    document.getElementById('summary').innerHTML =
      head + ' — ' + esc(cur.streak_human) + ' ' +
      (cur.state==='ONLINE' ? 'continuously available' : 'in this state') +
      ' (since ' + esc(tz(cur.since)) + '). ' +
      'Over the last ' + esc(D.window_days) + ' days, BD was present in ' +
      '<b>' + esc(D.totals.uptime_pct) + '%</b> of successful checks.';
  }

  // --- stat tiles
  var t = D.totals, tiles = [
    ['Uptime ('+D.window_days+'d)', t.uptime_pct+'%', t.online+' online / '+t.offline+' offline'],
    ['Current streak', cur ? cur.streak_human : '—', cur ? 'since '+tz(cur.since) : 'no data'],
    ['Longest ONLINE', D.longest_online_human, 'in the last '+D.window_days+' days'],
    ['Flips today', String(D.flips_today), 'BST calendar day'],
    ['Checks in window', String(t.samples), t.unknown ? (t.unknown+' unknown') : 'no outages'],
    ['Generated', tz(D.generated_at), 'all times shown in your local zone']
  ];
  document.getElementById('stats').innerHTML = tiles.map(function(x){
    return '<div class="stat"><div class="k">'+esc(x[0])+'</div>'+
           '<div class="v">'+esc(x[1])+'</div><div class="s">'+esc(x[2])+'</div></div>';
  }).join('');

  // --- trend bars
  var trend = D.trend || [];
  document.getElementById('trendRange').textContent = trend.length ? (trend.length+' days') : '';
  if (trend.length) {
    document.getElementById('trendFirst').textContent = trend[0].date;
    document.getElementById('trendLast').textContent  = trend[trend.length-1].date;
    document.getElementById('trend').innerHTML = trend.map(function(d){
      var pct = d.uptime_pct;
      var h = Math.max(3, pct);
      var col = pct >= 99 ? 'var(--ok)' : (pct >= 40 ? 'var(--warn)' : 'var(--bad)');
      return '<div class="bar" style="height:'+h+'%;background:'+col+'">'+
             '<i>'+esc(d.date)+'\\n'+pct+'% up\\n'+d.online+' on / '+d.offline+' off'+
             (d.unknown?' / '+d.unknown+' unknown':'')+'</i></div>';
    }).join('');
  } else {
    document.getElementById('trend').innerHTML =
      '<div class="empty">Not enough data yet — trend appears after the first full day of checks.</div>';
  }

  // --- hour-of-day heatmap
  var heat = D.heatmap || [];
  if (heat.length) {
    document.getElementById('heat').innerHTML = heat.map(function(h){
      if (!h.online && !h.offline) {
        return '<div class="cell" style="background:#20242d;color:#5b6472" title="'+
               String(h.hour).padStart(2,'0')+':00 — no data">—</div>';
      }
      var p = h.uptime_pct;
      var a = 0.14 + (p/100)*0.86;
      var txt = p >= 55 ? '#0f1115' : '#e6e9ef';
      return '<div class="cell" style="background:rgba(46,204,113,'+a.toFixed(2)+');color:'+txt+'" '+
             'title="'+String(h.hour).padStart(2,'0')+':00 BST — '+p+'% online ('+
             h.online+' on / '+h.offline+' off)">'+String(h.hour).padStart(2,'0')+'</div>';
    }).join('');
  } else {
    document.getElementById('heat').innerHTML = '<div class="empty">No heatmap data yet.</div>';
  }

  // --- transitions table
  var tr = D.transitions || [];
  if (tr.length) {
    document.getElementById('transitions').innerHTML =
      '<table><thead><tr><th>Time (local)</th><th>Change</th><th>Previous state lasted</th><th>Started</th></tr></thead><tbody>'+
      tr.map(function(x){
        return '<tr><td>'+esc(tz(x.ts))+'</td>'+
          '<td><span class="tag '+cls(x.from)+'">'+label(x.from)+'</span> → '+
          '<span class="tag '+cls(x.to)+'">'+label(x.to)+'</span></td>'+
          '<td>'+esc(x.duration_human)+'</td><td class="mono">'+esc(tz(x.started))+'</td></tr>';
      }).join('')+'</tbody></table>';
  } else {
    document.getElementById('transitions').innerHTML =
      '<div class="empty">No state changes recorded yet. '+
      'This table fills in as Bangladesh appears and disappears.</div>';
  }

  // --- recent checks table
  var sm = D.latest_samples || [];
  if (sm.length) {
    document.getElementById('samples').innerHTML =
      '<table><thead><tr><th>Time (local)</th><th>State</th><th>Options</th><th>Latency</th><th>Error</th></tr></thead><tbody>'+
      sm.slice().reverse().map(function(s){
        return '<tr><td>'+esc(tz(s.ts))+'</td>'+
          '<td><span class="tag '+cls(s.state)+'">'+label(s.state)+'</span></td>'+
          '<td>'+(s.option_count==null?'—':esc(s.option_count))+'</td>'+
          '<td>'+(s.latency_ms==null?'—':esc(s.latency_ms)+' ms')+'</td>'+
          '<td class="mono">'+(s.error?esc(s.error.slice(0,70)):'—')+'</td></tr>';
      }).join('')+'</tbody></table>';
  } else {
    document.getElementById('samples').innerHTML = '<div class="empty">No checks recorded yet.</div>';
  }

  document.getElementById('foot').textContent =
    'Generated ' + tz(D.generated_at) + ' · ' +
    (D.field || 'target field') + ' · all times local';
})();
</script>
</body>
</html>
"""


def render(analytics: Any, out_dir: Path, days: int = 30) -> dict[str, Any]:
    """Render dashboard assets into `out_dir`. Returns the rollup used."""
    data = analytics.rollup(days=days)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Inline data as JSON inside a <script type="application/json"> tag, which
    # is safer than interpolating into executable JS. `</` is escaped so the
    # payload can never terminate the script element early.
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace(
        "</", "<\\/"
    )

    page = (
        PAGE_TEMPLATE.replace("__DATA__", payload)
        .replace("__DAYS__", str(days))
        .replace("__SAMPLES__", str(len(data.get("latest_samples", []))))
    )

    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / "status.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8"
    )

    # Plain-text status for cheap consumers (curl, curl|jq, chatbots).
    plain = _plain_text(data)
    (out_dir / "status.txt").write_text(plain + "\n", encoding="utf-8")
    return data


def _plain_text(d: dict[str, Any]) -> str:
    cur = d.get("current")
    t = d.get("totals", {})
    lines = [
        "BD VISA SLOT MONITOR",
        "====================",
        f"Generated     : {d.get('generated_at', 'n/a')}",
        f"Window        : last {d.get('window_days', '?')} days",
        "",
        f"Current state : {cur.get('state', 'n/a') if cur else 'n/a'}"
        + (
            f"  (for {cur.get('streak_human', '?')}, since {cur.get('since_bst', '?')})"
            if cur
            else ""
        ),
        f"Uptime        : {t.get('uptime_pct', 0)}%",
        (
            f"Checks        : {t.get('samples', 0)} "
            f"({t.get('online', 0)} online / {t.get('offline', 0)} offline / "
            f"{t.get('unknown', 0)} unknown)"
        ),
        f"Flips today   : {d.get('flips_today', 0)}",
        f"Longest ONLINE: {d.get('longest_online_human', 'n/a')}",
        "",
        "Last changes:",
    ]
    tr = d.get("transitions") or []
    if not tr:
        lines.append("  (none recorded yet)")
    for x in tr[:10]:
        lines.append(
            f"  {x.get('ts_bst', '?')}  {x.get('from', '?')} -> {x.get('to', '?')}  "
            f"(previous held {x.get('duration_human', '?')})"
        )
    lines += ["", "Daily uptime % (BST):"]
    trend = d.get("trend") or []
    if not trend:
        lines.append("  (no full days yet)")
    for x in trend[-14:]:
        lines.append(
            f"  {x.get('date', '?')}  {x.get('uptime_pct', 0):>6}%  ({x.get('online', 0)} on / {x.get('offline', 0)} off)"
        )
    return "\n".join(lines)


def heat_color(uptime: float) -> str:
    """Standalone helper kept for tests / reuse."""
    a = 0.14 + max(0.0, min(100.0, uptime)) / 100.0 * 0.86
    return f"rgba(46,204,113,{a:.2f})"


__all__ = ["heat_color", "render"]
