"""Dark, minimalist FastAPI cockpit. Binds 0.0.0.0 so it's reachable across the Tailscale mesh.
Streams live job states + pending human gates + log from the thread-safe STATE."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import config
from .state import STATE

app = FastAPI(title="Job Application Engine")


@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse(STATE.snapshot())


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Engine · Cockpit</title>
<script src="https://cdn.tailwindcss.com/3.4.14"></script>
<style>body{background:#0b0f17}</style></head>
<body class="text-slate-200 font-sans">
<div class="max-w-6xl mx-auto px-4 py-6">
  <div class="flex items-center justify-between mb-6">
    <h1 class="text-xl font-semibold tracking-tight">⚙️ Job Application Engine
      <span class="text-slate-500 text-sm font-normal">· live cockpit</span></h1>
    <div id="clock" class="text-xs text-slate-500"></div>
  </div>
  <div id="alert" class="hidden mb-5 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4"></div>
  <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6" id="stats"></div>
  <div class="rounded-xl border border-slate-800 overflow-hidden mb-6">
    <table class="w-full text-sm"><thead class="bg-slate-900/60 text-slate-400 text-xs uppercase">
      <tr><th class="text-left px-4 py-2">Role</th><th class="text-left px-4 py-2">Company</th>
      <th class="text-left px-4 py-2">Status</th><th class="px-4 py-2">Tier</th>
      <th class="px-4 py-2">ATS</th><th class="text-left px-4 py-2">Note</th></tr></thead>
      <tbody id="rows"></tbody></table>
  </div>
  <h2 class="text-xs uppercase text-slate-500 mb-2">Activity</h2>
  <pre id="log" class="text-xs text-slate-400 bg-slate-900/40 rounded-xl p-4 h-64 overflow-auto whitespace-pre-wrap"></pre>
</div>
<script>
const COLORS={queued:'bg-slate-700',scouting:'bg-sky-700',ranking:'bg-sky-700',tailoring:'bg-indigo-700',
 login:'bg-amber-700',filling:'bg-indigo-700',ats:'bg-fuchsia-700',awaiting_human:'bg-amber-600',
 ready:'bg-emerald-800',submitting:'bg-emerald-700',submitted:'bg-emerald-600',skipped:'bg-slate-700',error:'bg-rose-700'};
function chip(s){return `<span class="px-2 py-0.5 rounded text-xs ${COLORS[s]||'bg-slate-700'}">${s}</span>`}
async function tick(){
 let d; try{d=await (await fetch('/api/state')).json()}catch(e){return}
 document.getElementById('clock').textContent=new Date().toLocaleTimeString();
 const a=document.getElementById('alert');
 if(d.pending && d.pending.length){a.classList.remove('hidden');
   a.innerHTML='⏸ <b>Awaiting your input on Telegram</b> — '+d.pending.map(p=>`${p.company}: ${p.prompt}`).join(' · ');}
 else a.classList.add('hidden');
 const by={}; d.jobs.forEach(j=>by[j.status]=(by[j.status]||0)+1);
 document.getElementById('stats').innerHTML=[['Total',d.jobs.length],['Submitted',by.submitted||0],
   ['Awaiting',(by.awaiting_human||0)+(d.pending?d.pending.length:0)],['Errors',by.error||0]]
   .map(([k,v])=>`<div class="rounded-xl border border-slate-800 p-3"><div class="text-2xl font-semibold">${v}</div><div class="text-xs text-slate-500">${k}</div></div>`).join('');
 document.getElementById('rows').innerHTML=d.jobs.map(j=>`<tr class="border-t border-slate-800">
   <td class="px-4 py-2">${j.role||''}</td><td class="px-4 py-2 text-slate-400">${j.company||''}</td>
   <td class="px-4 py-2">${chip(j.status)}</td><td class="px-4 py-2 text-center">${j.tier??''}</td>
   <td class="px-4 py-2 text-center">${j.ats_score!=null?j.ats_score+'%':''}</td>
   <td class="px-4 py-2 text-slate-500">${j.note||''}</td></tr>`).join('')
   || '<tr><td class="px-4 py-6 text-slate-600" colspan=6>No jobs yet — send a URL to the Telegram bot or run <code>python main.py &lt;url&gt;</code>.</td></tr>';
 document.getElementById('log').textContent=(d.log||[]).join('\\n');
 document.getElementById('log').scrollTop=1e9;
}
tick(); setInterval(tick,1500);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE
