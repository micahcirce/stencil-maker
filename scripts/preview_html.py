"""Interactive, self-contained HTML preview for a stencil run (no network, no libraries).

Per layer: show/hide, color picker, opacity slider (default 100 %), nearest paint from a chart.
Views: painted result (layers stacked over a surface color) or any single stencil sheet.
"Copy settings" gives JSON to paste into --params (layer_colors / layer_opacity / surface_color),
so choices made in the browser feed back into the deterministic engine.
"""
import json

def _d(geom, dx=0.0, dy=0.0, nd=2):
    parts = list(geom.geoms) if hasattr(geom, "geoms") else ([geom] if not geom.is_empty else [])
    out = []
    for p in parts:
        for r in [p.exterior] + list(p.interiors):
            c = list(r.coords)[:-1]
            if len(c) >= 3:
                out.append("M" + " ".join(f"{x + dx:.{nd}f},{y + dy:.{nd}f}" for x, y in c) + "Z")
    return "".join(out)

def write_html(path, layers, SW, SH, M, DW, DH, size_text, chart=None, surface="#ffffff", title="Stencil preview"):
    data = dict(
        sheet=[round(SW, 3), round(SH, 3)], margin=M, design=[round(DW, 3), round(DH, 3)], size=size_text,
        surface=surface, chart=chart or [],
        layers=[dict(n=i + 1, color=L["color"], cluster=L.get("cluster_color", L["color"]),
                     opacity=L.get("opacity", 100), cut=_d(L["cut"]), reg=_d(L["reg"]),
                     bridges="".join(_d(b) for b in L["bridge_marks"]),
                     bridge_ids=[dict(id=b["id"], x=round((b["p"][0] + b["q"][0]) / 2 + M, 2),
                                      y=round((b["p"][1] + b["q"][1]) / 2 + M, 2)) for b in L["bridges"]],
                     floating=[dict(id=f["id"], x=round(f["x"] + M, 2), y=round(f["y"] + M, 2))
                               for f in L.get("floating", [])])
                for i, L in enumerate(layers)],
    )
    html = TEMPLATE.replace("__TITLE__", title).replace("__DATA__", json.dumps(data, separators=(",", ":")))
    with open(path, "w") as f:
        f.write(html)

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f5f2;--panel:#ffffff;--ink:#1d1d1b;--muted:#6b6a66;--line:#dedcd6;--accent:#c2452d;--sheet:#e9dcc0;--bridge:#e0202088}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecebe7;--muted:#a3a19b;--line:#34332f;--accent:#ff7a5c;--sheet:#5a5140}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{display:grid;grid-template-columns:340px 1fr;min-height:100vh}
@media (max-width:860px){.wrap{grid-template-columns:1fr}}
aside{background:var(--panel);border-right:1px solid var(--line);padding:16px;overflow:auto}
main{padding:16px;display:flex;flex-direction:column;gap:10px;min-width:0}
h1{font-size:16px;margin:0 0 4px}.muted{color:var(--muted);font-size:12px}
.row{display:grid;grid-template-columns:22px 36px 1fr 46px;gap:8px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.row input[type=color]{width:36px;height:28px;border:1px solid var(--line);border-radius:6px;padding:0;background:none}
.row input[type=range]{width:100%}.row .num{width:46px}
.paint{grid-column:2/5;font-size:12px;color:var(--muted)}
.ctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}
button,select{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
textarea{width:100%;height:110px;font:12px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);padding:8px}
.stage{flex:1;min-height:360px;background:var(--panel);border:1px solid var(--line);border-radius:12px;display:flex;align-items:center;justify-content:center;padding:12px}
svg{max-width:100%;max-height:78vh;width:auto;height:auto}
label{user-select:none}
</style></head><body>
<div class="wrap">
<aside>
  <h1>Stencil preview</h1>
  <div class="muted" id="size"></div>
  <div class="ctl">
    <label>View <select id="view"></select></label>
    <label>Surface <input type="color" id="surface"></label>
  </div>
  <div id="rows"></div>
  <div class="ctl">
    <label class="muted">Paint chart CSV (name,hex[,brand]) <input type="file" id="chartfile" accept=".csv,text/csv"></label>
    <button id="snap">Snap all to nearest paint</button>
    <button id="reset">Reset colors</button>
  </div>
  <div class="ctl"><button class="primary" id="copy">Copy settings</button><span class="muted" id="copied"></span></div>
  <textarea id="out" readonly></textarea>
  <div class="muted">Paste into a params file to make these choices part of the run. Colors and opacity affect previews, PDF swatches and the report only, never the cut.</div>
</aside>
<main>
  <div class="stage"><svg id="svg" xmlns="http://www.w3.org/2000/svg"></svg></div>
  <div class="muted" id="legend"></div>
</main>
</div>
<script>
const D = __DATA__;
const S = {colors: D.layers.map(l => l.color), opacity: D.layers.map(l => l.opacity), visible: D.layers.map(() => true),
           surface: D.surface, view: "paint", chart: D.chart.slice()};
const $ = id => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
// ---- color science: sRGB -> CIE Lab (D65), delta E 76
function lab(hex){let h=hex.replace('#','');let c=[0,2,4].map(i=>parseInt(h.substr(i,2),16)/255).map(v=>v<=0.04045?v/12.92:Math.pow((v+0.055)/1.055,2.4));
 let X=(c[0]*0.4124+c[1]*0.3576+c[2]*0.1805)/0.95047,Y=(c[0]*0.2126+c[1]*0.7152+c[2]*0.0722),Z=(c[0]*0.0193+c[1]*0.1192+c[2]*0.9505)/1.08883;
 const f=t=>t>0.008856?Math.cbrt(t):7.787*t+16/116;return [116*f(Y)-16,500*(f(X)-f(Y)),200*(f(Y)-f(Z))];}
function dE(a,b){const p=lab(a),q=lab(b);return Math.hypot(p[0]-q[0],p[1]-q[1],p[2]-q[2]);}
function nearest(hex){let best=null;for(const p of S.chart){const d=dE(hex,p.hex);if(!best||d<best.d)best={...p,d};}return best;}
function parseCSV(t){const out=[];for(const line of t.split(/\r?\n/)){if(!line.trim()||line.trim().startsWith('#'))continue;
 const c=line.split(',').map(s=>s.trim());if(c[0].toLowerCase()==='name')continue;let hx=c[1]||'';if(!hx.startsWith('#'))hx='#'+hx;
 if(/^#[0-9a-fA-F]{6}$/.test(hx))out.push({name:c[0],hex:hx.toLowerCase(),brand:c[2]||''});}return out;}
// ---- UI
function buildRows(){const rows=$('rows');rows.innerHTML='';D.layers.forEach((l,i)=>{const r=document.createElement('div');r.className='row';
 r.innerHTML=`<input type="checkbox" ${S.visible[i]?'checked':''} title="show layer ${l.n}">
 <input type="color" value="${S.colors[i]}" title="layer ${l.n} color">
 <div><div><b>Layer ${l.n}</b> <span class="muted">paint order ${l.n}</span></div><input type="range" min="0" max="100" value="${S.opacity[i]}"></div>
 <input class="num" type="number" min="0" max="100" value="${S.opacity[i]}"><div class="paint"></div>`;
 const [vis,col]=r.querySelectorAll('input[type=checkbox],input[type=color]');const rng=r.querySelector('input[type=range]');const num=r.querySelector('.num');
 vis.onchange=()=>{S.visible[i]=vis.checked;draw();};
 col.oninput=()=>{S.colors[i]=col.value;draw();paintLabel(r,i);};
 rng.oninput=()=>{S.opacity[i]=+rng.value;num.value=rng.value;draw();};
 num.onchange=()=>{let v=Math.max(0,Math.min(100,+num.value||0));S.opacity[i]=v;rng.value=v;num.value=v;draw();};
 rows.appendChild(r);paintLabel(r,i);});}
function paintLabel(r,i){const el=r.querySelector('.paint');if(!S.chart.length){el.textContent='';return;}
 const p=nearest(S.colors[i]);el.innerHTML=`nearest paint: <b>${p.name}</b>${p.brand?' ('+p.brand+')':''} ${p.hex} · ΔE ${p.d.toFixed(1)}`;}
function buildViews(){const v=$('view');v.innerHTML='<option value="paint">Painted result</option>'+D.layers.map(l=>`<option value="${l.n}">Stencil layer ${l.n}</option>`).join('');v.value=S.view;v.onchange=()=>{S.view=v.value;draw();};}
function el(tag,attrs){const e=document.createElementNS(NS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);return e;}
function draw(){const svg=$('svg');svg.innerHTML='';const [SW,SH]=D.sheet,M=D.margin,[DW,DH]=D.design;
 if(S.view==='paint'){svg.setAttribute('viewBox',`${M} ${M} ${DW} ${DH}`);svg.setAttribute('width',DW+'mm');svg.setAttribute('height',DH+'mm');
  svg.appendChild(el('rect',{x:M,y:M,width:DW,height:DH,fill:S.surface}));
  D.layers.forEach((l,i)=>{if(!S.visible[i])return;svg.appendChild(el('path',{d:l.cut,fill:S.colors[i],'fill-rule':'evenodd','fill-opacity':S.opacity[i]/100}));});
  $('legend').textContent='Simulated paint: each layer sprayed through its stencil in order, over the surface color. Bridges show as gaps where no later layer covers them.';
 } else {const i=+S.view-1,l=D.layers[i];svg.setAttribute('viewBox',`0 0 ${SW} ${SH}`);svg.setAttribute('width',SW+'mm');svg.setAttribute('height',SH+'mm');
  const sheet=getComputedStyle(document.documentElement).getPropertyValue('--sheet').trim()||'#e9dcc0';
  svg.appendChild(el('rect',{x:0,y:0,width:SW,height:SH,fill:sheet,stroke:'#000','stroke-width':0.4}));
  svg.appendChild(el('path',{d:l.cut+l.reg,fill:'#222','fill-rule':'evenodd'}));
  if(l.bridges)svg.appendChild(el('path',{d:l.bridges,fill:'#e02020'}));
  const fs=Math.max(3,SW/70);
  l.bridge_ids.forEach(b=>{const t=el('text',{x:b.x+1,y:b.y-1,'font-size':fs,fill:'#e02020','font-family':'system-ui'});t.textContent=b.id.split('-')[1];svg.appendChild(t);});
  l.floating.forEach(f=>{const t=el('text',{x:f.x,y:f.y,'font-size':fs,fill:'#0050ff','font-family':'system-ui','text-anchor':'middle'});t.textContent=f.id;svg.appendChild(t);});
  $('legend').textContent=`Layer ${l.n} stencil: dark = cut out, red = bridge (Bn labels), blue In = floating island.`;}
 $('out').value=JSON.stringify({layer_colors:S.colors,layer_opacity:S.opacity,surface_color:S.surface},null,1);}
$('surface').value=S.surface;$('surface').oninput=e=>{S.surface=e.target.value;draw();};
$('chartfile').onchange=e=>{const f=e.target.files[0];if(!f)return;f.text().then(t=>{S.chart=parseCSV(t);buildRows();draw();});};
$('snap').onclick=()=>{if(!S.chart.length){alertless('Load a paint chart first');return;}S.colors=S.colors.map(c=>nearest(c).hex);buildRows();draw();};
$('reset').onclick=()=>{S.colors=D.layers.map(l=>l.cluster);buildRows();draw();};
$('copy').onclick=()=>{const t=$('out').value;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>alertless('Copied'),()=>{$('out').select();alertless('Select-all done: copy with Ctrl/Cmd+C');});};
function alertless(m){$('copied').textContent=m;setTimeout(()=>$('copied').textContent='',2500);}
$('size').textContent=D.size;buildViews();buildRows();draw();
</script></body></html>
"""
