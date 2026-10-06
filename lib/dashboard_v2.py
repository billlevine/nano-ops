"""Dashboard v2 — one page, two doors the contract.

An ADDITIVE second presentation of the SAME snapshot the primary page renders.
It fetches `dashboard.json` and nothing else, so every number here is the one
`bin/dashboard --json` already computed on its 45s cadence; there is no second
data path to drift from the first. `/` and `/estate.html` are untouched.

The page is Concept B (Two Doors) folded onto one scroll, toned down to a tool:

  Masthead     the top strip the current dashboard already has — the account
               cap/token line and the vitals rack, both dropped in as the
               server-rendered `usage_html` / `vitals_html`, plus the running
               loops rack as `sections.loops`. Rendering code REUSED verbatim,
               not rebuilt.
  Today        four questions in the operator's own words, each answered from a STORED
               predicate rather than a keyword guess at what an item "is", and
               each one naming its predicate in small type. An item appears in
               exactly one question: the first that claims it, in order.
  Machine room Concept A's machinery summary — dense, subordinate, counted not
               listed, and explicitly not waiting on anybody. The full detail
               it summarises already exists at /estate.html and is linked, not
               rebuilt.

Two rules carried over from the mockups because they are the honest ones:

  * A DEGRADED READ DEGRADES THE SUMMARY. The blindness line above Today is
    driven by the oldest/worst input — a stale loop heartbeat, a spotter item
    that could not be re-read, a brief gatherer that failed, an unreadable
    estate store — even when none of them changes a single count below it.
  * THE PAGE AGES WHILE IT SITS THERE. Every age is computed in the browser
    from the snapshot's own stamps against the current clock, so a tab left
    open on a second monitor says it has gone cold instead of lying quietly.

Read-only. No write endpoint is wired here: the ack marks and the proposal
decision box live on the pages that already own them.
"""
from __future__ import annotations

import html
import json


CSS = r"""
/* v2 — extends the shared dashboard tokens; invents no new visual identity. */
.v2 .masthead{padding-bottom:16px;margin-bottom:16px}
.v2 .eyebrow{letter-spacing:.28em}
.v2 .title{font-size:25px}
.v2-nav{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.v2-link{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim);background:var(--surface);border:1px solid var(--edge);border-radius:20px;
  padding:7px 11px;cursor:pointer}
.v2-link:hover{color:var(--ink);border-color:var(--accent)}
.v2-link.on{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}

/* running loops + fleet, compacted out of the shared .loop rack */
.v2-strip{margin-bottom:14px}
.v2-strip .pbody{padding:4px 8px 8px}
.v2-strip .loop{padding:7px 8px;grid-template-columns:14px 1fr auto}
.v2-strip .loop .persona{font-size:12.5px}
.v2-strip .loop .role{font-size:10.5px}
.v2-strip .chip{font-size:9.5px;padding:2px 6px}
.v2-strip .hb{font-size:10px}

/* what I can and cannot tell you */
.v2-blind{border:1px solid var(--hair);border-left:2px solid var(--edge);border-radius:8px;
  background:var(--surface-2);padding:9px 12px;margin-bottom:16px;font-size:11px;
  color:var(--faint);line-height:1.55}
.v2-blind.deg{border-left-color:var(--warn);color:var(--dim)}
.v2-blind b{color:var(--warn);font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  font-size:9.5px;margin-right:6px}
.v2-blind ul{margin:5px 0 0;padding-left:16px}
.v2-blind li{margin:2px 0}

/* section headings — Today loud, machine room quiet */
.v2-h{display:flex;align-items:baseline;gap:12px;margin:0 0 12px;padding-top:4px}
.v2-h .hk{font-size:13px;font-weight:700;letter-spacing:.2em;text-transform:uppercase;color:var(--ink)}
.v2-h .hs{font-size:11px;color:var(--faint);line-height:1.5}
.v2-h .grow{flex:1}
.v2-h a{font-size:10.5px;color:var(--info);border-bottom:1px solid transparent}
.v2-h a:hover{border-bottom-color:currentColor}
#machine .v2-h .hk{font-size:11px;color:var(--dim);letter-spacing:.24em}

/* Today — one question per block */
.q{background:var(--surface);border:1px solid var(--edge);border-radius:12px;
  box-shadow:var(--shadow);margin-bottom:12px;overflow:hidden}
.qhead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;padding:12px 14px 10px;
  border-bottom:1px solid var(--hair)}
.qhead .qn{font-size:19px;font-weight:700;line-height:1;color:var(--accent);
  font-variant-numeric:tabular-nums;min-width:26px}
.qhead .qq{font-size:14px;font-weight:600;color:var(--ink)}
.qhead .grow{flex:1}
.qhead .qsrc{font-size:9.5px;letter-spacing:.06em;color:var(--faint);text-align:right}
.qhead .qnote{flex-basis:100%;font-size:10.5px;color:var(--faint);margin-top:2px}
.q.calm .qhead .qn{color:var(--faint)}
.qrows,.qrest{padding:2px 6px 4px}
.it{display:grid;grid-template-columns:88px 1fr auto;gap:10px;align-items:baseline;
  padding:7px 8px;border-bottom:1px solid var(--hair)}
.it:last-child{border-bottom:0}
.it:hover{background:var(--surface-2)}
.it .iid{font-size:11px;color:var(--accent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.it .ittl{font-size:12.5px;color:var(--ink);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;line-height:1.45}
.it:hover .ittl{white-space:normal}
.it .ittl a{border-bottom:1px solid transparent}
.it:hover .ittl a{border-bottom-color:var(--edge)}
.it .iage{font-size:10.5px;color:var(--faint);white-space:nowrap;font-variant-numeric:tabular-nums}
/* Optional second line: why the row is here at all, spanning the title and age
   columns under the title. Only the PR rows carry one today (the board's `why`,
   written by pr-tracker's engine); every other item kind omits it and the row
   stays exactly one line tall. */
.it .iwhy{grid-column:2/4;font-size:11px;color:var(--dim);line-height:1.4;
  margin-top:3px;padding-left:14px;position:relative}
.it .iwhy::before{content:"→";position:absolute;left:0;top:0;color:var(--warn)}
.tagx{font-size:9px;letter-spacing:.05em;padding:1px 5px;margin-left:6px;border-radius:4px;
  border:1px solid var(--edge);color:var(--dim);white-space:nowrap;text-transform:lowercase}
.tagx.hot{color:var(--crit);border-color:rgba(242,89,76,.45);background:var(--crit-soft)}
.tagx.act{color:var(--warn);border-color:rgba(242,171,53,.45);background:var(--warn-soft)}
.tagx.on{color:var(--info);border-color:rgba(90,169,255,.45);background:var(--info-soft)}
.qempty{padding:14px;font-size:11.5px;color:var(--faint)}
.more{display:block;width:calc(100% - 12px);margin:4px 6px 8px;font:inherit;font-size:10px;
  letter-spacing:.12em;text-transform:uppercase;color:var(--faint);background:var(--surface-2);
  border:1px solid var(--edge);border-radius:7px;padding:7px 9px;cursor:pointer}
.more:hover{color:var(--ink);border-color:var(--dim)}

/* Machine room — dense, subordinate, counted rather than listed */
#machine{margin-top:26px;padding-top:18px;border-top:1px solid var(--edge)}
.mr{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media (max-width:900px){.mr{grid-template-columns:1fr 1fr}}
@media (max-width:560px){.mr{grid-template-columns:1fr}}
.mcard{background:var(--surface-2);border:1px solid var(--hair);border-radius:9px;padding:9px 11px}
.mcard .mk{font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--faint);
  display:block;margin-bottom:6px}
.mrow{display:flex;align-items:baseline;gap:8px;font-size:11px;color:var(--dim);
  padding:2px 0;line-height:1.45}
.mrow .ml{color:var(--faint);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.mrow .mv{color:var(--ink);font-variant-numeric:tabular-nums;white-space:nowrap}
.mrow .mv.bad{color:var(--crit)} .mrow .mv.warn{color:var(--warn)} .mrow .mv.ok{color:var(--good)}
.mrow .mv.mute{color:var(--faint)}
.mchips{display:flex;flex-wrap:wrap;gap:4px;padding-top:4px}
.mchip{font-size:9.5px;color:var(--dim);background:var(--surface);border:1px solid var(--edge);
  border-radius:5px;padding:1px 5px;white-space:nowrap;font-variant-numeric:tabular-nums}
.mchip b{color:var(--ink);font-weight:600}
.mnote{font-size:10px;color:var(--faint);line-height:1.5;padding-top:5px}
.mled{font-size:10.5px;color:var(--dim);line-height:1.45;padding:3px 0;
  border-bottom:1px solid var(--hair);display:flex;gap:7px}
.mled:last-child{border-bottom:0}
.mled .mt{color:var(--faint);font-variant-numeric:tabular-nums;white-space:nowrap}
.mled .ms{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mled:hover .ms{white-space:normal}
.mcard.wide{grid-column:span 2}
@media (max-width:560px){.mcard.wide{grid-column:1}}
"""


# The client. ONE fetch of dashboard.json per cycle feeds the masthead, Today
# and the machine room — the same payload, the same numbers, rendered three
# ways. `usage_html`, `vitals_html` and `sections.loops` are dropped in as-is:
# they are the primary page's own render_usage / render_vitals / render_loops
# output, so the top strip here cannot disagree with the top strip there.
JS = r"""
(function(){
var D={},EA={},PR={},TASK={},SEEN={},OPEN={},genEpoch=0,lastOk=0,everOk=false;
var CAP=6;

function e(v){var d=document.createElement('div');d.textContent=v==null?'':String(v);return d.innerHTML}
function since(v){if(v==null||v==='')return null;
  var n=typeof v==='number'?v*1000:Date.parse(String(v));
  if(isNaN(n))return null;return Math.max(0,(Date.now()-n)/1000)}
function human(s){s=Math.max(0,Math.floor(s));
  return s<90?s+'s':s<5400?Math.round(s/60)+'m':s<172800?Math.round(s/3600)+'h':Math.round(s/86400)+'d'}
function ago(v){var s=since(v);return s==null?'—':human(s)+' ago'}
function dayAge(v){var s=since(v);return s==null?null:s/86400}
function num(v){return typeof v==='number'?v:0}
function tag(t,cls){return t?'<span class="tagx '+(cls||'')+'">'+e(t)+'</span>':''}
function put(id,h){var el=document.getElementById(id);if(el&&h!=null&&el.innerHTML!==h)el.innerHTML=h}
function setText(id,t){var el=document.getElementById(id);if(el)el.textContent=t}

/* ── Today ─────────────────────────────────────────────────────────────────
   Every bucket is a STORED predicate. The estate does not record whether an
   item wants a decision or is work that has not come back — so nothing here
   guesses from title text; each question is answered by the field that
   already means that, and the header says which field. */

function claim(id){if(!id||SEEN[id])return false;SEEN[id]=1;return true}

function dueTag(t){
  if(!t.due_state||t.due_state==='undated')return '';
  var cls=(t.due_state==='overdue'||t.due_state==='unreadable')?'hot':'act';
  var left=t.days_left==null?'':' '+Math.abs(Math.round(t.days_left))+'d';
  return tag(t.due_state.replace('_',' ')+left,cls);
}
function taskItem(t,label,cls){
  return {id:t.id,title:t.title,
    tags:tag(label,cls)+dueTag(t)+(t.blocked_by&&t.blocked_by.length?tag('blocked by '+t.blocked_by.join(' '),'hot'):''),
    age:ago(t.updated_at),when:'updated '+(t.updated_at||'unknown')};
}
function byUpdated(a,b){return String(a.updated_at||'').localeCompare(String(b.updated_at||''))}
function ids(list){return (list||[]).map(function(i){return TASK[i]}).filter(Boolean)}

/* Q1 — parked ON THE OWNER by the store itself: status=needs-owner, plus a
   mechanic/charter proposal still at review stage pending-review. */
function decide(){
  var out=[];
  ids(EA.attention).forEach(function(t){
    if(t.status!=='needs-owner'||!claim(t.id))return;
    out.push(taskItem(t,'needs-owner','hot'));
  });
  ids(EA.proposals).filter(function(t){return t.stage==='pending-review'})
    .sort(byUpdated).forEach(function(t){
      if(!claim(t.id))return;
      out.push(taskItem(t,'pending review','act'));
    });
  return out;
}

/* Q2 — halted until the owner acts on it: the spotter's own act-first list
   where the ball is in their court or the PR has gone astray, and any
   night-shift item that stopped at needs-you. */
function stopped(){
  var out=[];
  (((D.night||{}).queue)||[]).forEach(function(q){
    if(q.status!=='needs-you')return;
    out.push({id:q.id,title:q.title,url:q.review_url||null,
      tags:tag('night shift','hot')+tag(q.kind),age:'stopped'});
  });
  (PR.act_first||[]).forEach(function(r){
    var mine=r.waiting_on==='me'||r.waiting_on==='my_review';
    if(!mine&&!r.astray)return;
    if(r.estate_task&&!claim(r.estate_task))return;
    out.push({id:r.number?'#'+r.number:r.key,title:r.title,url:r.url,
      tags:tag((r.repo||'').split('/').pop())
        +(r.astray?tag('astray','hot'):'')
        +tag(r.waiting_on==='my_review'?'your review':'waiting on you','act')
        +(r.unobserved?tag('held','act'):'')+(r.unseen?tag('new activity','on'):''),
      age:(r.idle_days==null?'—':r.idle_days.toFixed(1)+'d idle'),
      why:r.why||'',
      when:r.key+' · last observed '+(r.last_successful_observation_at||'unknown')});
  });
  return out;
}
/* The board carries `my_review` as a COUNT and never as rows — the spotter's
   act-first list is astray + waiting-on-you only (see build_pr_board in
   bin/dashboard). Saying so is the difference between a number the owner can
   chase and one that silently is not on this page. */
function stoppedNote(){
  var n=num((PR.buckets||{}).my_review);
  return n?(n+' more PR'+(n===1?' is':'s are')+' in your review queue — the board keeps only '+
            'the count for those, not the rows'):'';
}

/* Q3 — asked for and not back: the hub mirrors every self-DM message as an
   `inbox-message` task and closes it when the request really resolves
   (t-296), and a dispatched worker is work in flight with a task behind it. */
function asked(){
  var out=[];
  ids(EA.inbox_messages).forEach(function(t){
    if(!claim(t.id))return;
    var m=t.message||{};
    out.push({id:t.id,title:t.title,
      tags:tag(t.status,t.status==='needs-owner'?'hot':'on')+tag(m.inbox)+(m.threaded?tag('thread'):''),
      age:ago(t.created_at),when:'asked '+(t.created_at||'unknown')});
  });
  (D.dispatch_sessions||[]).forEach(function(w){
    var t=w.task_id?TASK[w.task_id]:null;
    if(w.task_id)claim(w.task_id);
    out.push({id:w.task_id||w.slug,title:(t&&t.title)||w.ref||w.slug,
      tags:tag(w.kind)+tag(w.status,w.status==='running'?'on':'act')+tag('dispatched'),
      age:(w.age==null?'—':human(w.age)+' in flight'),when:w.path||''});
  });
  return out;
}

/* Q4 — everything else still waiting on a person (docs/attention-contract),
   oldest movement first. Nothing from the attention set is dropped: what the
   questions above did not claim lands here rather than nowhere. */
function quiet(){
  return ids(EA.attention).filter(function(t){return claim(t.id)})
    .sort(byUpdated)
    .map(function(t){
      var f=t.followup||{};
      return taskItem(t,f.source?'via '+f.source:t.kind);
    });
}

function row(o){
  var title=o.url?'<a href="'+e(o.url)+'" target="_blank" rel="noopener">'+e(o.title)+'</a>':e(o.title);
  return '<div class="it"><span class="iid" title="'+e(o.id)+'">'+e(o.id)+'</span>'+
    '<span class="ittl">'+title+(o.tags||'')+'</span>'+
    '<span class="iage" title="'+e(o.when||'')+'">'+e(o.age)+'</span>'+
    (o.why?'<span class="iwhy">'+e(o.why)+'</span>':'')+'</div>';
}
/* C's discipline inside B: show a few, SAY how many were withheld, and put
   the rest one click down rather than behind a page. */
function block(key,question,predicate,items,empty,note){
  var open=!!OPEN[key],rest=items.length-CAP;
  var head='<div class="qhead"><span class="qn">'+items.length+'</span>'+
    '<span class="qq">'+e(question)+'</span><span class="grow"></span>'+
    '<span class="qsrc">'+e(predicate)+'</span>'+
    (note?'<span class="qnote">'+e(note)+'</span>':'')+'</div>';
  var body=items.length?'<div class="qrows">'+items.slice(0,CAP).map(row).join('')+'</div>'
                       :'<div class="qempty">'+e(empty)+'</div>';
  if(rest>0)body+='<div class="qrest"'+(open?'':' hidden')+'>'+items.slice(CAP).map(row).join('')+'</div>'+
    '<button type="button" class="more" data-more="'+e(key)+'">'+
    (open?'hide the other '+rest:'show '+rest+' more')+'</button>';
  return '<section class="q'+(items.length?'':' calm')+'">'+head+body+'</section>';
}

/* ── what I can and cannot tell you ────────────────────────────────────────
   Governed by the WORST input, not by whether any count changed. */
function blindness(){
  var bad=[];
  var snap=genEpoch?(Date.now()/1000-genEpoch):null;
  if(snap!=null&&snap>120)
    bad.push('this page is '+human(snap)+' old — the regenerator is not keeping up');
  /* Every panel now records what it could see, so this line is driven
     by the warrants rather than by a hand-written list of the four inputs
     somebody remembered. A panel that could not be read is named here even
     when its counts below read as a confident zero — that zero is exactly the
     thing this line exists to contradict. */
  var P=D.panels||{};
  Object.keys(P).forEach(function(k){
    var e=P[k]||{},st=e.staleness||{};
    if(e.empty_is_evidence===false)
      bad.push((e.label||k)+' could not be read ('+(e.outcome||'?')+
        (e.why?' · '+e.why:'')+') — its numbers below are missing, not zero');
    else if(st.state==='stale')
      bad.push((e.label||k)+' was read '+(st.age_seconds==null?'—':human(st.age_seconds))+
        ' ago, past its budget — real numbers, earlier estate');
  });
  (D.loops||[]).forEach(function(l){
    if(!(l.autostart||l.is_hub)||l.freshness!=='stale')return;
    bad.push(l.name+' last ticked '+(l.heartbeat_age==null?'never':human(l.heartbeat_age)+' ago')+
      ' against a '+(l.interval||'?')+' cadence');
  });
  if(num(PR.unobserved))
    bad.push(PR.unobserved+' tracked PR'+(PR.unobserved===1?'':'s')+
      ' the spotter could not re-read — those rows are the last good observation, not live state');
  (PR.unreadable_sources||[]).forEach(function(s){
    bad.push('spotter source unreadable: '+((s&&s.source)||s)+
      (s&&s.outcome?' ('+s.outcome+')':''))});
  var b=D.brief||{},del=b.delivery||{},man=b.manifest||{};
  if(b.available&&del.status&&del.status!=='delivered')
    bad.push("the latest brief is '"+del.status+"' ("+(del.generation||'?')+
      (del.reason?' · '+del.reason:'')+') — it was not delivered');
  if(man.legacy)bad.push('the latest brief carries no source manifest — an empty section in it is not evidence');
  /* The same sentence the primary page's digest panel carries, injected from
     the one constant in bin/dashboard rather than written out again here. */
  if(b.board_moved&&window.OPS_BOARD_MOVED_NOTE)bad.push(window.OPS_BOARD_MOVED_NOTE);
  var src=man.sources||{},sick=[];
  Object.keys(src).forEach(function(k){
    var s=src[k];if(s.status!=='completed'||s.degraded)sick.push(k+' ('+(s.outcome||s.status)+')');
  });
  if(sick.length)bad.push('brief gatherer'+(sick.length===1?'':'s')+' that could not answer: '+sick.join(', '));
  if(EA.available===false)
    bad.push('the estate store could not be read'+(EA.error?': '+EA.error:'')+
      ' — every count below is missing, not zero');
  var el=document.getElementById('v2-blind');if(!el)return;
  if(!bad.length){
    el.className='v2-blind';
    el.innerHTML='Every input answered for itself. Snapshot '+e(snap==null?'—':human(snap)+' old')+
      ' · attention, PR board, brief and estate store all read clean.';
    return;
  }
  el.className='v2-blind deg';
  el.innerHTML='<b>partly blind</b>'+
    '<ul>'+bad.map(function(v){return '<li>'+e(v)+'</li>'}).join('')+'</ul>';
}

/* ── machine room ──────────────────────────────────────────────────────────
   Counted here, rendered on Today: no attention item is repeated below, and
   nothing below is waiting on anybody. Full detail is /estate.html's job. */
function mrow(label,value,cls){
  return '<div class="mrow"><span class="ml">'+e(label)+'</span>'+
    '<span class="mv '+(cls||'')+'">'+e(value)+'</span></div>';
}
function mchips(counts,order){
  var keys=order||Object.keys(counts||{}).sort();
  var out=keys.filter(function(k){return (counts||{})[k]!=null&&k!=='total'})
    .map(function(k){return '<span class="mchip">'+e(k)+' <b>'+e((counts||{})[k])+'</b></span>'});
  return out.length?'<div class="mchips">'+out.join('')+'</div>':'<div class="mnote">nothing on file</div>';
}
function mcard(title,body,cls){
  return '<div class="mcard '+(cls||'')+'"><span class="mk">'+e(title)+'</span>'+body+'</div>';
}
function machine(){
  var cards=[],night=D.night||{},mech=D.mechanic||{},b=D.brief||{},man=b.manifest||{},
      del=b.delivery||{},lh=EA.ledger_health||{},ex=EA.extractions||{},focus=D.focus||{};

  var live=0,configured=0,stale=0;
  (D.loops||[]).forEach(function(l){
    if(!(l.autostart||l.is_hub))return;configured++;
    if(l.session_present)live++;
    if(l.freshness==='stale')stale++;
  });
  cards.push(mcard('Loops',
    mrow('live / configured',live+' / '+configured,live===configured?'ok':'warn')+
    mrow('on-demand',String((D.loops||[]).filter(function(l){return !l.autostart&&!l.is_hub}).length),'mute')+
    mrow('stale heartbeat',String(stale),stale?'warn':'mute')));

  var eph=D.dispatch_sessions||[];
  cards.push(mcard('Dispatched workers',
    mrow('in flight',String(eph.length),eph.length?'ok':'mute')+
    eph.slice(0,4).map(function(w){
      return mrow(w.slug,(w.status||'?')+' · '+(w.age==null?'—':human(w.age)),'mute')}).join('')));

  var qc={};(night.queue||[]).forEach(function(q){qc[q.status]=(qc[q.status]||0)+1});
  cards.push(mcard('Night shift',
    mrow('queue',String((night.queue||[]).length))+
    mrow('last report',(night.report&&(night.report.date||night.report.name))||'—','mute')+
    mchips(qc)));

  var bk=PR.buckets||{},meta=PR.meta||{};
  cards.push(mcard('PR board',
    mrow('going astray',String(num(bk.going_astray)),num(bk.going_astray)?'bad':'mute')+
    mrow('waiting on you',String(num(bk.waiting_you)),num(bk.waiting_you)?'warn':'mute')+
    mrow('your review',String(num(bk.my_review)),'')+
    mrow('on reviewers / in progress',num(bk.on_reviewers)+' / '+num(bk.in_progress),'mute')+
    mrow('tracked · ignored',num(meta.active)+' · '+num(meta.ignored),'mute')+
    mrow('held (unobserved)',String(num(PR.unobserved)),num(PR.unobserved)?'warn':'mute')+
    '<div class="mnote">observed '+e(meta.updated||'—')+' · '+e(meta.scanning||'')+'</div>'));

  cards.push(mcard('Mechanic',
    mrow('night of',mech.date||'—','mute')+
    mrow('proposals filed',String((mech.proposals||[]).length))+
    mrow('observations',String((mech.observations||[]).length),'mute')+
    mrow('applied',mech.applied==null?'—':String(mech.applied),'mute')));

  var srcOk=0,srcAll=0,srcSrc=man.sources||{};
  Object.keys(srcSrc).forEach(function(k){srcAll++;
    if(srcSrc[k].status==='completed'&&!srcSrc[k].degraded)srcOk++});
  var cursor=man.cursor||{};
  cards.push(mcard('Briefer',
    mrow('latest',b.name||man.date||'—','mute')+
    mrow('generation · delivery',(del.generation||'—')+' · '+(del.status||'—'),
      del.status==='delivered'?'ok':'warn')+
    mrow('delivery attempts',String(num(del.attempts)),'mute')+
    mrow('gatherers answering',srcOk+' / '+srcAll,srcOk===srcAll&&srcAll?'ok':'warn')+
    mrow('ledger cutoff seq',String((man.ledger_cutoff||{}).seq||'—'),'mute')+
    mrow('published cursor seq',String(cursor.seq==null?'—':cursor.seq),'mute')));

  cards.push(mcard('Work store',
    mrow('tasks on file',String(Object.keys(EA.task_counts||{}).reduce(
      function(a,k){return a+num((EA.task_counts||{})[k])},0)))+
    mchips(EA.task_counts,['open','ready','claimed','blocked','needs-owner','done','dropped'])+
    '<div class="mnote">kinds</div>'+mchips(EA.kind_counts)));

  var ac=EA.attention_counts||{};
  cards.push(mcard('Attention set',
    mrow('waiting on a person',String(num(ac.total)),num(ac.total)?'warn':'ok')+
    mrow('notice interval',(EA.notice_days==null?'—':EA.notice_days+'d'),'mute')+
    mchips(ac,['overdue','due_today','due_soon','later','undated','unreadable'])+
    '<div class="mnote">every one of these is rendered on Today above; here it is only counted</div>'));

  cards.push(mcard('Proposals',
    mrow('pending review',String(num((EA.proposal_counts||{})['pending-review'])),'warn')+
    mchips(EA.proposal_counts,['pending-review','approved-backlog','rejected','stopped','resolved'])));

  cards.push(mcard('Structure',
    mrow('projects',String(Object.keys(EA.project_counts||{}).reduce(
      function(a,k){return a+num((EA.project_counts||{})[k])},0)),'mute')+
    mchips(EA.project_counts)+
    '<div class="mnote">dependency edges</div>'+mchips(EA.dep_counts)));

  cards.push(mcard('Extraction',
    mrow('available',ex.available?'yes':'no',ex.available?'ok':'mute')+
    mchips(ex.counts,['candidate','approved','extracting','blocked','synced'])));

  cards.push(mcard('Focus',
    mrow('projects',String(((focus.projects||[]).length)),'mute')+
    mrow('batch cap',String(focus.batch_cap==null?'—':focus.batch_cap),'mute')+
    mrow('generated',ago(focus.generated_at),'mute')));

  cards.push(mcard('Ledger',
    mrow('central entries',String(num(lh.count)),lh.available?'':'bad')+
    mchips(lh.by_class)+
    '<div class="mnote">latest</div>'+
    (D.ledger||[]).slice(0,6).map(function(v){    /* newest first, as built */
      var s=since(v.ts);
      return '<div class="mled"><span class="mt">'+e(s==null?'—':human(s))+'</span>'+
        '<span class="ms" title="'+e(v.summary||'')+'">'+e(v.summary||'')+'</span></div>';
    }).join(''),'wide'));

  put('v2-machine','<div class="mr">'+cards.join('')+'</div>');
}

/* ── page ──────────────────────────────────────────────────────────────── */
function render(d){
  D=d||{};EA=D.estate_activity||{};PR=D.pr_board||{};
  TASK={};(EA.tasks||[]).forEach(function(t){TASK[t.id]=t});
  SEEN={};
  genEpoch=D.generated_epoch||0;
  setText('v2-host',D.host||'—');
  var name=(D.estate||'estate')+' / '+(D.operator||'hub');
  setText('v2-name',name);
  put('usage',D.usage_html);
  put('vitals',D.vitals_html);
  put('sec-loops',(D.sections||{}).loops);

  var d1=decide(),d2=stopped(),d3=asked(),d4=quiet();
  var quietWeek=ids(EA.attention).filter(function(t){var a=dayAge(t.updated_at);return a!=null&&a>=7}).length;
  put('v2-today',
    block('decide','What needs a decision from you?',
      'estate: status = needs-owner · proposal stage = pending-review',d1,
      'Nothing is parked on your decision.')+
    block('stopped','What is stopped until you touch it?',
      'spotter: your court or astray · night shift: needs-you',d2,
      'Nothing is halted waiting for you.',stoppedNote())+
    block('asked','What did you ask for that has not come back?',
      'open inbox-message tasks · workers still in flight',d3,
      'Every message you sent has been closed out.')+
    block('quiet','What has gone quiet?',
      'the rest of the attention set, oldest movement first',d4,
      'Nothing else is waiting on a person.',
      quietWeek+' of the '+num((EA.attention_counts||{}).total)+
      ' items waiting on you have not moved in a week or more'));
  blindness();
  machine();
  tick();
}

function tick(){
  var age=genEpoch?Math.floor(Date.now()/1000)-genEpoch:null;
  setText('v2-gen',age==null?'…':human(age)+' ago');
  var pill=document.getElementById('v2-live');if(!pill)return;
  var sinceOk=everOk?(Date.now()-lastOk)/1000:Infinity,cls='live',label='live';
  if(!everOk||sinceOk>150){cls='offline';label='offline — retrying';}
  else if((age!=null&&age>120)||sinceOk>75){cls='stale';label='stale — regenerator lagging';}
  pill.className='stat '+cls;
  setText('v2-live-label',label);
}

document.addEventListener('click',function(ev){
  var more=ev.target.closest&&ev.target.closest('[data-more]');
  if(more){var k=more.getAttribute('data-more');OPEN[k]=!OPEN[k];if(D.generated_epoch)render(D);return;}
  var jump=ev.target.closest&&ev.target.closest('[data-jump]');
  if(jump){var t=document.getElementById(jump.getAttribute('data-jump'));
    if(t)t.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});}
});

function refresh(){
  fetch('dashboard.json?t='+Math.floor(Date.now()/1000),{cache:'no-store'})
    .then(function(r){if(!r.ok)throw 0;return r.json()})
    .then(function(d){lastOk=Date.now();everOk=true;render(d)})
    .catch(function(){tick()});
}
refresh();setInterval(refresh,30000);setInterval(tick,1000);
})();
"""


import dashboard_nav
def board_moved_config(dashboard) -> str:
    """The live-board-versus-digest sentence, handed to the client rather than
    restated. Same discipline as `dashboard_primary.source_config`:
    three surfaces say this, and one constant is what keeps them agreeing."""
    return ("window.OPS_BOARD_MOVED_NOTE="
            + json.dumps(dashboard.BOARD_MOVED_NOTE) + ";\n")


def render(dashboard, snapshot: dict) -> str:
    """The v2 shell: static HTML, every dynamic region filled from dashboard.json.

    Same contract as the primary shell and /estate.html — the page carries no
    data of its own, so it stays as fresh as the regenerator without being
    regenerated itself.
    """
    e = html.escape
    name = f'{snapshot["estate"]} / {snapshot["operator"]}'
    body = (
        '<div class="wrap v2">'
        + dashboard_nav.render("v2") +
        '<header class="masthead">'
        '<div class="brand">'
        '<div class="eyebrow">Autonomous operations estate · v2</div>'
        f'<div class="title"><span class="pulse" title="operator live"></span>'
        f'<span class="estate" id="v2-name">{e(name)}</span></div>'
        '<div class="subline">snapshot <b id="v2-gen">…</b> · host <b id="v2-host">…</b>'
        ' · one page: what waits on you, then the machinery</div>'
        '<div class="usage" id="usage"></div>'
        '</div>'
        '<span class="spacer"></span>'
        '<div class="vitals" id="vitals"></div>'
        '<div class="v2-nav">'
        '<button type="button" class="v2-link on" data-jump="today">Today</button>'
        '<button type="button" class="v2-link" data-jump="machine">Machine room</button>'
        '</div>'
        '</header>'

        '<section class="panel v2-strip">'
        '<div class="phead"><span class="k">Loops</span><span class="grow"></span>'
        '<span class="tick">persona · model · interval · heartbeat</span></div>'
        '<div class="pbody tight" id="sec-loops"></div>'
        '</section>'

        '<div class="v2-blind" id="v2-blind">reading…</div>'

        '<section id="today">'
        '<div class="v2-h"><span class="hk">Today</span>'
        '<span class="hs">what waits on you — one item, one question, no section '
        'named after the thing that produced it</span></div>'
        '<div id="v2-today"></div>'
        '</section>'

        '<section id="machine">'
        '<div class="v2-h"><span class="hk">Machine room</span>'
        '<span class="hs">Nothing here is waiting on you. Counts only — every '
        'attention item is rendered above, not repeated below.</span>'
        '<span class="grow"></span>'
        '<a href="/estate.html">Full detail → /estate.html</a></div>'
        '<div id="v2-machine"></div>'
        '</section>'

        '<footer class="foot">'
        '<span class="stat" id="v2-live"><span class="sdot"></span>'
        '<b id="v2-live-label">connecting…</b> · read-only · one fetch of '
        'dashboard.json</span>'
        '<span>auto-refresh 30s · regen <code>bin/dashboard --json</code> every 45s</span>'
        '</footer>'
        '</div>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<link rel="icon" href="{dashboard.FAVICON}">'
            f'<title>{e(name)} — dashboard v2</title>'
            f'<style>{dashboard.CSS}\n{CSS}</style></head>'
            f'<body>{body}<script>{board_moved_config(dashboard)}'
            f'{dashboard.JS}\n{JS}</script></body></html>\n')
