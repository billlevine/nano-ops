"""Primary dashboard presentation layer.

The data aggregation and shared rendering primitives live in ``bin/dashboard``;
this module owns only the primary page's layout, interaction, and styling.
"""
from __future__ import annotations

import json

import brief_manifest
import dashboard_estate

PRIMARY_CSS = r"""
.estate-ops-link{font-size:11px;color:var(--info);text-decoration:none;border:1px solid var(--edge);border-radius:7px;padding:6px 9px}
/* Primary-page additions, scoped to their owning components. */
.digest-tabs{display:flex;gap:6px;overflow-x:auto;padding-bottom:10px;
  margin-bottom:12px;border-bottom:1px solid var(--hair);scrollbar-width:thin}
.digest-tab{font:inherit;font-size:10.5px;color:var(--faint);white-space:nowrap;
  border:1px solid var(--edge);background:var(--surface-2);border-radius:7px;
  padding:6px 9px;cursor:pointer}
.digest-tab:hover{color:var(--ink);border-color:var(--dim)}
.digest-tab.active{color:var(--accent);border-color:var(--accent);
  background:var(--accent-soft)}
.digest-report{display:flex;flex-direction:column;gap:10px}
.digest-intro{font-size:11.5px;color:var(--dim);line-height:1.55;padding:0 2px}
.digest-section{border:1px solid var(--hair);border-radius:10px;
  background:var(--surface-2);overflow:hidden}
.digest-section>summary{display:flex;align-items:center;gap:8px;cursor:pointer;
  list-style:none;padding:10px 12px;color:var(--dim);font-size:10.5px;
  font-weight:600;letter-spacing:.12em;text-transform:uppercase}
.digest-section>summary::-webkit-details-marker{display:none}
.digest-section>summary::before{content:'›';font-size:17px;color:var(--faint);
  line-height:1;transition:transform .12s}
.digest-section[open]>summary::before{transform:rotate(90deg)}
.digest-section.priority{border-color:rgba(242,171,53,.35)}
.digest-section.priority>summary{color:var(--accent)}
.digest-content{padding:2px 12px 12px;border-top:1px solid var(--hair)}
.digest-content p{font-family:var(--sans);font-size:12.5px;line-height:1.55;
  color:var(--dim);margin:10px 0}
.digest-content h3{font-family:var(--sans);font-size:13px;line-height:1.4;
  color:var(--ink);margin:14px 0 7px}
.digest-content a{color:var(--info);text-decoration:underline;
  text-decoration-color:transparent;transition:text-decoration-color .12s}
.digest-content a:hover{text-decoration-color:currentColor}
.digest-activity{padding-top:10px}
.digest-repo{padding:0 0 12px;margin-bottom:12px;border-bottom:1px solid var(--hair)}
.digest-repo:last-child{padding-bottom:0;margin-bottom:0;border-bottom:0}
.digest-repo h3{margin:0 0 8px}
.digest-repo-label{display:inline-block;min-width:58px;color:var(--faint);
  font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase}
.digest-pr-list{margin:6px 0 10px;padding-left:20px;color:var(--dim)}
.digest-pr-list li{font-family:var(--sans);font-size:12.5px;line-height:1.5;
  margin:4px 0;padding-left:2px}
.digest-opened{font-family:var(--sans);font-size:12.5px;line-height:1.55;color:var(--dim)}
.digest-check{display:grid;grid-template-columns:18px 1fr;gap:8px;align-items:start;
  padding:8px 0;border-bottom:1px solid var(--hair);font-family:var(--sans);
  font-size:12.5px;line-height:1.5;color:var(--ink)}
.digest-check:last-child{border-bottom:0}
.digest-check input{appearance:none;width:15px;height:15px;margin:2px 0 0;
  border:1px solid var(--dim);border-radius:4px;background:var(--surface);cursor:pointer}
.digest-check input:checked{border-color:var(--good);background:var(--good)}
.digest-check input:checked::after{content:'✓';display:block;color:#07130a;
  font:bold 12px/13px var(--sans);text-align:center}
.digest-check:has(input:checked) .digest-item{color:var(--faint);
  text-decoration:line-through;text-decoration-color:var(--faint)}
.digest-empty{padding:22px 10px;text-align:center;color:var(--faint);font-size:12px}
.focus-project-link{text-decoration:underline;text-decoration-color:transparent;
  text-underline-offset:3px;transition:color .12s,text-decoration-color .12s}
.focus-project-link:hover{color:var(--info);text-decoration-color:currentColor}
.digest-toggle{font:inherit;font-size:10px;line-height:1;color:var(--faint);
  border:1px solid var(--edge);background:var(--surface-2);border-radius:6px;
  padding:5px 8px;cursor:pointer}
.digest-toggle:hover{color:var(--ink);border-color:var(--dim)}
.digest-toggle::before{content:'▾';display:inline-block;transition:transform .12s}
.digest-panel.collapsed .digest-toggle::before{transform:rotate(-90deg)}
.digest-panel.collapsed>.pbody{display:none}
.digest-panel.collapsed>.phead{border-bottom:0}
.vital.jumpable{cursor:pointer;transition:border-color .15s,transform .15s,background .15s}
.vital.jumpable:hover{border-color:var(--accent);background:var(--raise);transform:translateY(-1px)}
.vital.jumpable:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.panel.jump-target{animation:jump-target 1.2s ease-out}
@keyframes jump-target{0%,35%{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft),var(--shadow)}
  100%{border-color:var(--edge);box-shadow:var(--shadow)}}
#sec-focus .obs li{font-size:13px;line-height:1.55;margin:4px 0 0;
  padding:8px 10px 8px 18px;border-radius:7px;color:var(--dim);
  transition:color .12s,background .12s}
#sec-focus .obs li::before{left:7px;top:8px}
#sec-focus .obs li:hover{color:var(--ink);background:var(--raise)}
#sec-focus .obs li:hover a{color:#fff}
:root[data-theme="light"] #sec-focus .obs li:hover a{color:var(--ink)}
"""

PRIMARY_CSS += dashboard_estate.PROPOSAL_CSS + r"""
.panel.proposal-review #sec-mech{max-height:none}
.proposal-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:12px 0}
.proposal-toolbar .filter{color:var(--info);border:1px solid var(--edge);background:var(--surface-2);border-radius:7px;padding:7px 10px;font:inherit;cursor:pointer}
.proposal-toolbar .filter.active{color:var(--accent);border-color:var(--accent)}
.proposal-toolbar .search{flex:1;min-width:180px;background:var(--surface-2);color:var(--ink);border:1px solid var(--edge);border-radius:7px;padding:8px 10px}
#sec-mech .estate-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:2px}
#sec-mech .estate-stat{background:var(--surface-2);border:1px solid var(--hair);border-radius:9px;padding:13px}
#sec-mech .estate-stat b{display:block;font-size:24px}
#sec-mech .estate-stat span,#sec-mech .label{color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.1em}
#sec-mech .stage{font-family:var(--mono);font-size:10.5px;white-space:nowrap;border:1px solid currentColor;border-radius:99px;padding:1px 6px;color:var(--dim)}
#sec-mech .stage.approved-backlog{color:var(--good)}#sec-mech .stage.pending-review{color:var(--warn)}#sec-mech .stage.rejected,#sec-mech .stage.stopped{color:var(--crit)}
#sec-mech .id,#sec-mech .time{color:var(--faint);font-family:var(--mono)}
#sec-mech .kv{display:grid;grid-template-columns:120px 1fr;gap:4px 12px;margin:6px 0}
#sec-mech .hist{margin-top:9px;border-top:1px solid var(--hair);padding-top:7px}
#sec-mech .history-entry{padding:5px 0;border-bottom:1px solid var(--hair)}
@media(max-width:800px){.review-grid{grid-template-columns:1fr}.proposal>summary{grid-template-columns:70px 1fr}.proposal-title,.proposal-recommendation{grid-column:2}.proposal-body{padding-left:12px}}
"""


PRIMARY_JS = r"""
(function(){
  var mast=document.querySelector('.masthead'),theme=document.getElementById('themebtn');
  if(mast&&!document.querySelector('.estate-ops-link')){var link=document.createElement('a');link.className='estate-ops-link';link.href='estate.html';link.textContent='Estate operations';mast.insertBefore(link,theme);}
  /* Recompose the shared panels for the primary information hierarchy.
     Moving the existing nodes preserves IDs, listeners, scroll state, and
     the shared live updater. */
  var grid=document.querySelector('.grid');
  if(grid){
    [
      ['sec-focus','col-12'],
      ['sec-pr','col-12'],
      ['sec-brief','col-12'],
      ['sec-loops','col-7'],
      ['sec-night','col-5'],
      ['sec-dispatch-sessions','col-12'],
      ['sec-ledger','col-7'],
      ['sec-mech','col-12']
    ].forEach(function(spec){
      var body=document.getElementById(spec[0]);
      var panel=body&&body.closest('section.panel');
      if(!panel)return;
      panel.classList.remove('col-5','col-7','col-12');
      panel.classList.add(spec[1]);
      grid.appendChild(panel);
    });
  }

  var vitalTargets={
    'loops live':'sec-loops',
    'needs you':'sec-pr',
    'going astray':'sec-pr',
    'queued':'sec-night',
    'proposals':'sec-mech'
  };
  function jumpTo(id){
    var body=document.getElementById(id),panel=body&&body.closest('section.panel');if(!panel)return;
    panel.classList.remove('jump-target');void panel.offsetWidth;panel.classList.add('jump-target');
    panel.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
    window.setTimeout(function(){panel.classList.remove('jump-target');},1300);
  }
  function wireVitals(){
    var rack=document.getElementById('vitals');if(!rack)return;
    Array.prototype.forEach.call(rack.querySelectorAll('.vital'),function(tile){
      var label=tile.querySelector('.l'),key=label&&label.textContent.trim().toLowerCase();
      var target=key&&vitalTargets[key];
      if(!target||tile.dataset.jumpTarget)return;
      if(key==='queued')label.textContent='nightshift';
      tile.dataset.jumpTarget=target;tile.classList.add('jumpable');tile.tabIndex=0;
      tile.setAttribute('role','link');tile.setAttribute('aria-label','Jump to '+label.textContent.trim());
      tile.addEventListener('click',function(){jumpTo(target);});
      tile.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();jumpTo(target);}});
    });
  }
  var vitalsRoot=document.getElementById('vitals');
  if(vitalsRoot)new MutationObserver(wireVitals).observe(vitalsRoot,{childList:true});
  wireVitals();

  var root=document.getElementById('sec-brief');
  if(!root)return;
  var STORE='ops-digest-checks-v1', COLLAPSE_STORE='ops-digest-collapsed-v1';
  var DAYS=30, selected=null, reports=[], projectUrls={};
  /* t-390. This page OWNS the digest panel — `sections.brief` is unmanaged
     here — so everything the shared renderer puts above the brief was landing
     on a surface nobody serves. That included the panel's own read warrant and
     the live-board divergence, which was `true` in dashboard.json while the
     string that says so appeared on none of the four rendered artifacts.
     Injected as the PRE-RENDERED block rather than rebuilt in JS, so this page
     and every other one say it in the same bytes. */
  var PANELSTATE='';

  var digestPanel=root.closest('section.panel');
  if(digestPanel){
    digestPanel.classList.add('digest-panel');
    var head=digestPanel.querySelector('.phead');
    var toggle=document.createElement('button');toggle.type='button';toggle.className='digest-toggle';
    toggle.setAttribute('aria-label','Collapse Daily Digest');toggle.setAttribute('aria-expanded','true');
    if(head)head.appendChild(toggle);
    var collapsed=false;try{collapsed=localStorage.getItem(COLLAPSE_STORE)==='1';}catch(e){}
    function setCollapsed(value){collapsed=value;digestPanel.classList.toggle('collapsed',value);
      toggle.setAttribute('aria-expanded',String(!value));
      toggle.setAttribute('aria-label',(value?'Expand':'Collapse')+' Daily Digest');
      try{localStorage.setItem(COLLAPSE_STORE,value?'1':'0');}catch(e){}}
    setCollapsed(collapsed);
    toggle.addEventListener('click',function(){setCollapsed(!collapsed);});
  }

  // `https://` only, and deliberately no allowlist of hosts: the tracker a
  // project lives in is an installation's choice, and the guard here is about
  // refusing to linkify something that is not an absolute external URL.
  function linkProjects(){
    var focus=document.getElementById('sec-focus');if(!focus)return;
    Array.prototype.forEach.call(focus.querySelectorAll('span'),function(title){
      var url=projectUrls[title.textContent.trim()];
      if(!url||url.indexOf('https://')!==0||title.parentElement.closest('.focus-project-link'))return;
      var link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noopener';
      link.className='focus-project-link';title.parentNode.insertBefore(link,title);link.appendChild(title);
    });
  }
  var focusRoot=document.getElementById('sec-focus');
  if(focusRoot)new MutationObserver(linkProjects).observe(focusRoot,{childList:true,subtree:true});

  function esc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
  function inline(s){
    var safe=esc(s);
    safe=safe.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    safe=safe.replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>');
    safe=safe.replace(/\*([^*\n]+)\*/g,'<em>$1</em>');
    safe=safe.replace(/`([^`\n]+)`/g,'<code>$1</code>');
    return safe;
  }
  function hash(s){var h=2166136261;for(var i=0;i<s.length;i++){
    h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return (h>>>0).toString(36);}
  function load(){try{var x=JSON.parse(localStorage.getItem(STORE)||'{}');
    var cutoff=Date.now()-DAYS*86400000, changed=false;
    Object.keys(x).forEach(function(k){if(!x[k]||x[k]<cutoff){delete x[k];changed=true;}});
    if(changed)localStorage.setItem(STORE,JSON.stringify(x));return x;
  }catch(e){return {};}}
  function save(x){try{localStorage.setItem(STORE,JSON.stringify(x));}catch(e){}}

  function parse(body){
    var lines=(body||'').split(/\r?\n/), intro=[], sections=[], section=null, sub=null;
    function flushSub(){if(sub&&section){section.blocks.push(sub);sub=null;}}
    lines.forEach(function(line){
      if(/^# /.test(line))return;
      if(/^## /.test(line)){flushSub();section={title:line.slice(3).trim(),blocks:[]};
        sections.push(section);return;}
      if(/^### /.test(line)){flushSub();if(!section){section={title:'Details',blocks:[]};sections.push(section);}
        sub={type:'heading',text:line.slice(4).trim()};section.blocks.push(sub);sub=null;return;}
      var bullet=line.match(/^[-*] (.+)$/);
      if(bullet){if(!section){section={title:'Summary',blocks:[]};sections.push(section);}
        section.blocks.push({type:'item',text:bullet[1]});return;}
      if(!line.trim())return;
      var target=section?section.blocks:intro;
      var last=target[target.length-1];
      /* Keep authored line boundaries. Ordinary prose still collapses this
         whitespace in HTML; structured sections can use it as a delimiter. */
      if(last&&last.type==='text')last.text+='\n'+line.trim();
      else target.push({type:'text',text:line.trim()});
    });
    return {intro:intro,sections:sections};
  }
  // Digest sections that render expanded, because they are the ones a reader
  // has to act on rather than skim. Which headings those are is a property of
  // the brief an installation writes, not of this renderer, so the list is an
  // override point: set window.DIGEST_PRIORITY_SECTIONS to your own lowercase
  // substrings before this script runs. The default matches the estate's own
  // attention vocabulary and nothing else.
  var PRIORITY_SECTIONS=window.DIGEST_PRIORITY_SECTIONS||['needs you','attention','blocked'];
  function isPriority(title){var t=String(title||'').toLowerCase();
    return PRIORITY_SECTIONS.some(function(p){return t.indexOf(p)>=0;});}
  function activityHtml(sec){
    var lines=[];sec.blocks.forEach(function(b){
      if(b.type==='text')lines=lines.concat(b.text.split('\n'));
    });
    var repos=[], repo=null;
    lines.forEach(function(line){
      var head=line.match(/^\*\*([^*]+)\*\*\s*(.*)$/);
      if(head){repo={name:head[1],lines:[]};repos.push(repo);if(head[2])repo.lines.push(head[2]);return;}
      if(repo&&line.trim())repo.lines.push(line.trim());
    });
    if(!repos.length)return '';
    return '<div class="digest-activity">'+repos.map(function(r){
      var rows=r.lines.map(function(line){
        var merged=line.match(/^Merged\s+(\d+):\s*(.*)$/i);
        if(merged){
          var items=merged[2].replace(/[.]$/,'').split(/\s+·\s+/).filter(Boolean);
          return '<div><span class="digest-repo-label">Merged '+esc(merged[1])+'</span>'+
            '<ul class="digest-pr-list">'+items.map(function(item){return '<li>'+inline(item)+'</li>';}).join('')+'</ul></div>';
        }
        var opened=line.match(/^Opened\s+(\d+):\s*(.*)$/i);
        if(opened)return '<div class="digest-opened"><span class="digest-repo-label">Opened '+
          esc(opened[1])+'</span>'+inline(opened[2])+'</div>';
        return '<p>'+inline(line)+'</p>';
      }).join('');
      return '<section class="digest-repo"><h3>'+inline(r.name)+'</h3>'+rows+'</section>';
    }).join('')+'</div>';
  }
  function sectionHtml(sec,date,checks){
    var priority=isPriority(sec.title), open=priority?' open':'';
    var activity=/what landed\s*\/\s*opened/i.test(sec.title)?activityHtml(sec):'';
    var blocks=activity||sec.blocks.map(function(b){
      if(b.type==='heading')return '<h3>'+inline(b.text)+'</h3>';
      if(b.type==='text')return '<p>'+inline(b.text)+'</p>';
      var id=hash(date+'\n'+sec.title+'\n'+b.text), checked=checks[id]?' checked':'';
      return '<label class="digest-check"><input type="checkbox" data-check="'+id+'"'+checked+
        '><span class="digest-item">'+inline(b.text)+'</span></label>';
    }).join('');
    return '<details class="digest-section'+(priority?' priority':'')+'"'+open+'><summary>'+
      esc(sec.title)+'</summary><div class="digest-content">'+blocks+'</div></details>';
  }
  /* P-11: what the SELECTED report's gatherers could see. Per report, not per
     page — switching to yesterday's tab has to show yesterday's provenance, or
     the strip becomes a claim about the wrong morning. Rendered from the
     manifest the brief itself wrote; the freshness classes are re-derived by
     the shared source ticker after this innerHTML swap. */
  /* `esc` is textContent round-tripping, which leaves quotes alone — fine for
     element text, not for an attribute holding a source's `why` (a file path,
     or whatever `gh` printed to stderr). */
  function attr(s){return esc(s).replace(/"/g,'&quot;');}
  function sourcesHtml(report){
    var m=report.manifest||{}, src=m.sources||{};
    var labels=window.OPS_SOURCE_LABELS||{}, order=window.OPS_SOURCE_ORDER||[];
    var keys=Object.keys(src);
    if(m.legacy||!keys.length)
      return '<div class="srcstrip legacy" title="'+attr(m.why||window.OPS_SOURCE_LEGACY||'')+
        '">no source manifest — nothing recorded what this brief could see</div>';
    var seen={}, ordered=[];
    order.forEach(function(k){if(src[k]){ordered.push(k);seen[k]=1;}});
    keys.sort().forEach(function(k){if(!seen[k])ordered.push(k);});
    var bad=[];
    var chips=ordered.map(function(k){
      var e=src[k]||{}, st=e.staleness||{}, ok=e.status==='completed'&&!e.degraded;
      if(!ok)bad.push((labels[k]||k)+' ('+(e.outcome||'?')+')');
      return '<span class="srcchip s-'+attr(e.status||'')+' f-'+attr(st.state||'unknown')+
        (e.degraded?' deg':'')+'" title="'+attr((e.status||'')+(e.why?' · '+e.why:''))+
        '" data-asof="'+attr(st.as_of||'')+'" data-maxage="'+attr(String(st.max_age_seconds||0))+
        '"><span class="srcdot"></span><span class="srcname">'+esc(labels[k]||k)+'</span>'+
        (e.count==null?'':'<span class="srcn">'+esc(String(e.count))+'</span>')+
        '<span class="srcage">—</span></span>';
    }).join('');
    var out='<div class="srcstrip">'+chips+'</div>';
    if(bad.length)out+='<div class="srcwarn">'+bad.length+
      ' source(s) could not answer for themselves — an empty section below is not '+
      'evidence of a quiet morning: '+esc(bad.join(', '))+'</div>';
    return out;
  }

  function render(){
    /* The panel state goes above the tabs and outside the per-report strip: it
       describes THIS PANEL's read and the newest brief, not whichever morning
       is selected. "No briefs available yet" is a claim only a successful read
       may make, so the block precedes it too. */
    if(!reports.length){root.innerHTML=PANELSTATE+
      '<div class="digest-empty">No briefs available yet.</div>';return;}
    var report=reports.find(function(r){return r.date===selected;})||reports[0];selected=report.date;
    var checks=load(), parsed=parse(report.body);
    var tabs='<div class="digest-tabs" role="tablist" aria-label="Morning brief date">'+
      reports.map(function(r,i){var label=i===0?'Today':r.date;
        return '<button class="digest-tab'+(r.date===selected?' active':'')+'" type="button" data-date="'+
          esc(r.date)+'" role="tab" aria-selected="'+(r.date===selected)+'">'+esc(label)+'</button>';}).join('')+'</div>';
    var intro=parsed.intro.map(function(b){return '<p>'+inline(b.text)+'</p>';}).join('');
    root.innerHTML=PANELSTATE+tabs+sourcesHtml(report)+'<div class="digest-report">'+
      (intro?'<div class="digest-intro">'+intro+'</div>':'')+
      parsed.sections.map(function(s){return sectionHtml(s,report.date,checks);}).join('')+'</div>';
    if(window.OPS_TICK_SOURCES)window.OPS_TICK_SOURCES();
  }
  root.addEventListener('click',function(e){var tab=e.target.closest('[data-date]');
    if(tab){selected=tab.getAttribute('data-date');render();}});
  root.addEventListener('change',function(e){var id=e.target.getAttribute('data-check');if(!id)return;
    var x=load();if(e.target.checked)x[id]=Date.now();else delete x[id];save(x);});
  function refresh(){fetch('dashboard.json?t='+Math.floor(Date.now()/1000),{cache:'no-store'})
    .then(function(r){if(!r.ok)throw 0;return r.json();}).then(function(d){
      PANELSTATE=(d.panel_state_html&&d.panel_state_html.brief)||'';
      reports=(d.brief&&d.brief.reports)||[];
      projectUrls={};((d.focus&&d.focus.projects)||[]).forEach(function(p){if(p.url)projectUrls[p.name]=p.url;});
      linkProjects();
      if(!selected||!reports.some(function(r){return r.date===selected;}))selected=reports[0]&&reports[0].date;
      render();}).catch(function(){});}
  refresh();setInterval(refresh,30000);
})();
"""


def source_config() -> str:
    """The P-11 source vocabulary, handed to the client rather than restated.

    The digest panel here renders one report at a time from `dashboard.json`,
    so its source strip is built in the browser — but the labels and the order
    still come from lib/brief_manifest, so the two panels cannot end up calling
    one gatherer by two names.
    """
    return ("window.OPS_SOURCE_LABELS="
            + json.dumps(brief_manifest.SOURCE_LABEL, separators=(",", ":"))
            + ";window.OPS_SOURCE_ORDER="
            + json.dumps(list(brief_manifest.SOURCES), separators=(",", ":"))
            + ";window.OPS_SOURCE_LEGACY="
            + json.dumps(brief_manifest.LEGACY_WHY) + ";\n")


def proposal_review_js() -> str:
    """V1 adapter around the estate page's proposal-review primitives."""
    buttons = ''.join(
        f'<button class="filter{" active" if key == "pending-review" else ""}" '
        f'data-proposal-stage="{key}">{label}</button>'
        for key, label in (("pending-review", "Needs decision"),
                           ("approved-backlog", "Approved"),
                           ("rejected", "Rejected"), ("stopped", "Stopped"),
                           ("resolved", "Resolved"), ("all", "All")))
    adapter_head = r"""
(function(){
var DATA={},INDEX={},EVENT_INDEX={},PROPOSAL_STAGE='pending-review',OPEN_PROPOSALS={},DECISION_NOTES={},DECISION_CHOICES={};
function e(v){var d=document.createElement('div');d.textContent=v==null?'':String(v);return d.innerHTML}
function age(v){if(!v)return '—';var n=typeof v==='number'?v*1000:Date.parse(v),s=Math.max(0,(Date.now()-n)/1000);if(s<90)return Math.round(s)+'s ago';if(s<5400)return Math.round(s/60)+'m ago';if(s<172800)return Math.round(s/3600)+'h ago';return Math.round(s/86400)+'d ago'}
function stamp(v){if(!v)return '—';var s=String(v),utc=/(\+00:00|Z)$/.test(s);return s.replace('T',' ').slice(0,16)+(utc?'Z':'')}
function rows(a,fn,msg){return a&&a.length?'<div class="estate-list">'+a.map(fn).join('')+'</div>':'<div class="empty">'+msg+'</div>'}
function stats(c,names){return '<div class="estate-grid">'+names.map(function(k){return '<div class="estate-stat"><b>'+e((c||{})[k]||0)+'</b><span>'+e(k)+'</span></div>'}).join('')+'</div>'}
function kv(label,value){return value==null||value===''?'':'<span class="label">'+e(label)+'</span><span>'+e(value)+'</span>'}
function task(id){return INDEX[id]}
function who(v){var a=v.actor||'unknown';return e(v.session_id?a+' · '+v.session_id:a)}
function historyLine(v){return '<div class="history-entry"><div class="history-meta"><span class="time">'+e(stamp(v.ts))+'</span> <b>'+e(v.summary)+'</b> · '+who(v)+(v.kind?' · '+e(v.kind):'')+(v.phase?' · '+e(v.phase):'')+'</div></div>'}
function history(list,msg){return '<div class="hist"><span class="label">history ('+e(list.length)+')</span>'+(list.length?list.map(historyLine).join(''):'<div>'+e(msg)+'</div>')+'</div>'}
"""
    adapter_tail = r"""
function install(){
  var body=document.getElementById('sec-mech'),panel=body&&body.closest('section.panel');if(!body||!panel)return;
  panel.classList.add('proposal-review');
  panel.querySelector('.phead').innerHTML='<span class="k">Proposal review</span><span class="grow"></span><span class="tick" id="proposal-count"></span><span class="tick">decide here · audited by estate</span>';
  /* t-390: `sec-mech` is unmanaged here too — this panel is rendered entirely
     from `estate_activity`, so an estate store that could not be opened used
     to show an empty proposal list and nothing else. */
  body.className='pbody';body.innerHTML='<div id="proposal-state"></div><div id="proposal-stats"></div><div class="proposal-toolbar">__BUTTONS__<input class="search" id="proposal-search" placeholder="Filter by id, kind or text…"></div><div id="proposals"></div>';
  document.querySelectorAll('[data-proposal-stage]').forEach(function(b){b.onclick=function(){PROPOSAL_STAGE=b.dataset.proposalStage;document.querySelectorAll('[data-proposal-stage]').forEach(function(x){x.classList.toggle('active',x===b)});drawProposals()}});
  document.getElementById('proposal-search').oninput=drawProposals;
  /* t-1794: the option cards are a SELECTION now, not only a note
     prefill, so this page has to wire them too — approving a
     decision-classified proposal is refused without one, and a card
     nothing listens to would make that refusal unanswerable here. */
  body.onclick=function(ev){var b=ev.target.closest('[data-decision-id]');if(b){decideProposal(b);return}var card=ev.target.closest('[data-decision-card]');if(card)prefillDecision(card)};
  body.onkeydown=function(ev){var card=ev.target.closest('[data-decision-card]');if(card&&(ev.key==='Enter'||ev.key===' ')){ev.preventDefault();prefillDecision(card)}};
  installDetailWiring();
}
install();
document.addEventListener('ops-dashboard-data',function(ev){
  document.querySelectorAll('[data-decision-note]').forEach(function(v){DECISION_NOTES[v.dataset.decisionNote]=v.value});
  OPEN_PROPOSALS={};document.querySelectorAll('[data-proposal][open]').forEach(function(v){OPEN_PROPOSALS[v.dataset.proposal]=true});
  var state=document.getElementById('proposal-state');
  if(state)state.innerHTML=((ev.detail&&ev.detail.panel_state_html)||{}).estate_activity||'';
  DATA=(ev.detail&&ev.detail.estate_activity)||{};INDEX={};(DATA.tasks||[]).forEach(function(t){INDEX[t.id]=t});
  EVENT_INDEX={};(DATA.events||[]).forEach(function(v){EVENT_INDEX[v.seq]=v});
  reindexDetailEvents();
  drawProposals();
  /* t-1450: a proposal re-rendered open fires no toggle event, so it asks for
     its history here. `estate_activity` no longer embeds one. */
  ensureOpenDetails();
});
})();
""".replace('__BUTTONS__', buttons)
    # Rows, badges, decision transitions and the authenticated write request
    # are owned by dashboard_estate and embedded byte-for-byte here — and
    # since t-1450 so is the drill-down fetch (JS_DETAIL), because a proposal's
    # history is no longer in the snapshot and one implementation of asking for
    # it is the point.
    return (adapter_head + dashboard_estate.JS_DETAIL
            + dashboard_estate.JS_PROPOSALS + adapter_tail)


def render(dashboard, snapshot: dict) -> str:
    """Compose the primary shell through the shared renderer's public hooks."""
    return dashboard.render_shell(
        snapshot,
        extra_css=PRIMARY_CSS,
        extra_js=source_config() + PRIMARY_JS + proposal_review_js(),
        unmanaged_sections=("brief", "mechanic"),
        brief_tick="recent briefs · locally checkable",
    )
