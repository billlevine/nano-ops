"""Estate-operations dashboard page.

One deliberately narrow write surface: a token-authenticated proposal decision
invokes the same audited transition operation as ``bin/estate proposal stage``.
Nothing else on this page mutates estate state; task, project and dependency
controls remain display-only. The banner states that boundary explicitly.

The sections answer four different questions and are ordered that way:

  Attention     what is waiting on a PERSON — open follow-ups and `needs-owner`
                tasks, with the follow-up envelope decoded and the due state
                derived. Resolved follow-ups sit behind a History toggle; a
                section that mixes them teaches its reader to skip it.
  Inbox         which of the owner's own messages the hub has not finished.
                A cut across the same task rows, not a second store: every one
                is an `inbox-message` task, and the escalated ones also appear
                in Attention above. It exists because the Slack-side answer to
                "did that get handled?" depends on the owner finding a thread, and
                this one does not depend on Slack at all.
  Tasks         what work exists and where it is. `Active` means every
                NON-TERMINAL status, which is what the old "Open" button
                claimed and never was — it matched the literal string `open`
                and so hid every `ready`, `claimed`, `blocked` and
                `needs-owner` row in the estate.
  Projects      the operational groupings, each with counts rolled up from the
                task rows, plus the synthetic "Unprojected work" bucket that
                `project_id IS NULL` would otherwise render as nothing at all.
  Dependencies  what is actually holding work up (unresolved `blocks` edges,
                both endpoints named) and, separately, the whole edge list
                across all four kinds.

Then the shared recent-event feed, unchanged. Shared feed answers "what changed
recently?"; per-item history answers "how did this item get here?". Both, for
different purposes — the drill-downs keep reading each item's complete
authoritative history (P-16).

Every derived field on this page arrives already computed by
lib/estate_work.py. The browser re-derives nothing: it indexes, filters and
renders. That is deliberate — a second implementation of the attention rule or
the blocking rule in JavaScript is a second rule.
"""
from __future__ import annotations
import html

CSS = r"""
.estate-nav,.filter{color:var(--info);text-decoration:none;border:1px solid var(--edge);background:var(--surface-2);border-radius:7px;padding:7px 10px;font:inherit}.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.filter.active{color:var(--accent);border-color:var(--accent)}.filter{cursor:pointer}.search{flex:1;min-width:180px;background:var(--surface-2);color:var(--ink);border:1px solid var(--edge);border-radius:7px;padding:8px 10px}.estate-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.estate-stat,.subpanel{background:var(--surface-2);border:1px solid var(--hair);border-radius:9px;padding:13px}.estate-stat b,.subpanel .n{display:block;font-size:24px}.estate-stat span,.label{color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.1em}.task,.row{border-bottom:1px solid var(--hair)}.task>summary,.row{display:grid;grid-template-columns:95px 110px 1fr auto;gap:12px;padding:11px 2px;font-size:12px}.task>summary{cursor:pointer;list-style:none}.task.open .status{color:var(--accent)}.task.done{opacity:.68}.details{padding:0 12px 14px 105px;color:var(--dim);font-size:11px;line-height:1.55}.id,.time{color:var(--faint);font-family:var(--mono)}.empty,.readiness{padding:25px;color:var(--faint);text-align:center}.readiness{text-align:left;line-height:1.6}.chips{display:flex;flex-wrap:wrap;gap:7px}.chipx{background:var(--surface-2);border:1px solid var(--hair);border-radius:99px;padding:5px 8px;font-size:11px}.split{display:grid;grid-template-columns:1fr 1fr;gap:12px}.source-badge{color:var(--good);font-size:10px;text-transform:uppercase}@media(max-width:800px){.estate-grid,.split{grid-template-columns:1fr 1fr}.task>summary,.row{grid-template-columns:80px 1fr}.time{display:none}.details{padding-left:12px}}
.memory-tabs{display:flex;gap:7px;flex-wrap:wrap;padding:12px 0}.memory-tab{font:inherit;color:var(--dim);background:var(--surface-2);border:1px solid var(--edge);border-radius:7px;padding:7px 10px;cursor:pointer}.memory-tab.active{color:var(--accent);border-color:var(--accent)}.memory-body{white-space:pre-wrap;font:12px/1.6 var(--mono);color:var(--dim);background:var(--surface-2);border:1px solid var(--hair);border-radius:9px;padding:14px;max-height:420px;overflow:auto}.event{border-bottom:1px solid var(--hair)}.event>summary{display:grid;grid-template-columns:95px 120px 1fr auto;gap:12px;padding:10px 2px;cursor:pointer;list-style:none;font-size:12px}.event-detail{padding:0 12px 14px 227px;color:var(--dim);font-size:11px;line-height:1.55}.event-detail pre{white-space:pre-wrap;overflow-wrap:anywhere}
.blocked{color:var(--warn,#d08a1e);font-family:var(--mono);font-size:10.5px;white-space:nowrap;border:1px solid currentColor;border-radius:99px;padding:1px 6px;margin-left:4px}.task.is-blocked .status{color:var(--warn,#d08a1e)}
.stage{font-family:var(--mono);font-size:10.5px;white-space:nowrap;border:1px solid currentColor;border-radius:99px;padding:1px 6px;margin-left:4px;color:var(--dim)}.stage.approved-backlog{color:var(--good,#4fca6a)}.stage.pending-review{color:var(--warn,#d08a1e)}.stage.rejected,.stage.stopped{color:var(--crit,#f2594c)}.recur{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:4px}
.readonly{display:flex;gap:9px;align-items:center;flex-wrap:wrap;background:var(--surface-2);border:1px solid var(--edge);border-left:3px solid var(--info);border-radius:9px;padding:10px 13px;margin:0 0 14px;font-size:11.5px;color:var(--dim)}.readonly b{color:var(--ink);font-size:11px;text-transform:uppercase;letter-spacing:.1em}.readonly code{font-family:var(--mono);color:var(--info)}
.due{font-family:var(--mono);font-size:10.5px;white-space:nowrap;border:1px solid currentColor;border-radius:99px;padding:1px 6px;margin-left:4px;color:var(--dim)}.due.overdue,.due.unreadable{color:var(--crit,#f2594c)}.due.due_today,.due.due_soon{color:var(--warn,#d08a1e)}.due.later{color:var(--info)}.due.undated{color:var(--faint)}
.legacy{font-family:var(--mono);font-size:10.5px;color:var(--info);margin-left:4px}.pill{font-family:var(--mono);font-size:10.5px;color:var(--faint);border:1px solid var(--hair);border-radius:99px;padding:1px 6px;margin-left:4px}.pill.grouping{color:var(--info);border-color:currentColor}.pill.missing{color:var(--crit,#f2594c);border-color:currentColor}
.sel{background:var(--surface-2);color:var(--ink);border:1px solid var(--edge);border-radius:7px;padding:7px 9px;font:inherit;max-width:210px}
.edge{display:grid;grid-template-columns:1fr 120px 1fr auto;gap:12px;padding:11px 2px;font-size:12px;border-bottom:1px solid var(--hair);align-items:baseline}.edge .arrow{font-family:var(--mono);font-size:10.5px;text-align:center;color:var(--faint);border:1px solid var(--hair);border-radius:99px;padding:1px 6px}.edge.blocks .arrow{color:var(--warn,#d08a1e);border-color:currentColor}.edge.inactive{opacity:.6}.edge .end{overflow-wrap:anywhere}@media(max-width:800px){.edge{grid-template-columns:1fr}}
.hist{margin-top:9px;border-top:1px solid var(--hair);padding-top:7px}.hist>div{padding:5px 0}.history-entry{border-bottom:1px solid var(--hair)}.history-entry:last-child{border-bottom:0}.history-meta{line-height:1.5}.kv{display:grid;grid-template-columns:120px 1fr;gap:4px 12px;margin:6px 0}.kv .label{align-self:baseline}
.md-note{font:12.5px/1.65 var(--sans);color:var(--dim);margin:8px 0;overflow-wrap:anywhere}.md-note a{color:var(--info)}.md-note h1,.md-note h2,.md-note h3,.md-note h4{color:var(--ink);line-height:1.3;margin:18px 0 8px}.md-note h1{font-size:20px}.md-note h2{font-size:17px;border-bottom:1px solid var(--hair);padding-bottom:5px}.md-note h3{font-size:14px}.md-note h4{font-size:12.5px}.md-note p{margin:8px 0}.md-note ul,.md-note ol{margin:8px 0;padding-left:24px}.md-note li{margin:4px 0}.md-note code{font-family:var(--mono);font-size:.92em;color:var(--info);background:var(--bg);border:1px solid var(--hair);border-radius:4px;padding:1px 4px}.md-note pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--hair);border-radius:7px;padding:10px;overflow:auto}.md-note pre code{border:0;padding:0;color:var(--dim)}.md-note blockquote{border-left:3px solid var(--edge);margin:10px 0;padding:2px 12px;color:var(--faint)}.md-table-wrap{overflow:auto;margin:11px 0}.md-note table{border-collapse:collapse;width:100%;font-size:11.5px}.md-note th,.md-note td{border:1px solid var(--edge);padding:7px 9px;text-align:left;vertical-align:top}.md-note th{color:var(--ink);background:var(--surface-2)}.md-note tr:nth-child(even) td{background:rgba(255,255,255,.015)}.note-preview{max-height:120px;overflow:hidden;position:relative}.note-preview::after{content:'';position:absolute;left:0;right:0;bottom:0;height:36px;background:linear-gradient(transparent,var(--surface))}.full-note-button{font:inherit;font-size:11px;color:var(--info);background:var(--surface-2);border:1px solid var(--edge);border-radius:7px;padding:6px 9px;cursor:pointer;margin:3px 0 7px}.full-note-button:hover{border-color:var(--info)}.note-dialog{width:min(1180px,calc(100vw - 80px));max-height:calc(100vh - 70px);padding:0;border:1px solid var(--edge);border-radius:12px;background:var(--surface);color:var(--ink);box-shadow:0 24px 80px rgba(0,0,0,.55)}.note-dialog::backdrop{background:rgba(0,0,0,.72)}.note-dialog-head{position:sticky;top:0;z-index:1;display:flex;gap:12px;align-items:center;padding:14px 18px;background:var(--surface-2);border-bottom:1px solid var(--edge)}.note-dialog-title{font-weight:600}.note-dialog-body{padding:12px 24px 28px}.note-close{margin-left:auto;font:inherit;color:var(--dim);background:var(--bg);border:1px solid var(--edge);border-radius:7px;padding:6px 9px;cursor:pointer}
/* proposal-review:start */
.proposal{border-bottom:1px solid var(--hair)}.proposal>summary{display:grid;grid-template-columns:82px 132px minmax(260px,1fr) minmax(280px,1.2fr) auto;gap:14px;padding:14px 2px;cursor:pointer;list-style:none;font-size:12px;align-items:start}.proposal-title{color:var(--ink);font-weight:600;line-height:1.45}.proposal-recommendation{color:var(--dim);line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.proposal-body{padding:0 14px 18px 228px;color:var(--dim);font-size:12px}.review-grid{display:grid;grid-template-columns:1.15fr 1.15fr .8fr;gap:12px;margin:12px 0}.review-field{background:var(--surface-2);border:1px solid var(--hair);border-radius:8px;padding:12px;white-space:pre-wrap;line-height:1.55}.review-field.recommendation{border-color:rgba(242,171,53,.35);color:var(--ink)}.review-field.missing{color:var(--faint);font-style:italic}.review-badge{font-family:var(--mono);font-size:10px;border:1px solid currentColor;border-radius:99px;padding:2px 6px;margin-left:5px;color:var(--faint);white-space:nowrap}.review-badge.incomplete{color:var(--warn)}.review-badge.recurring{color:var(--info)}.decision-box{margin-top:14px;padding:13px;border:1px solid var(--edge);border-radius:9px;background:var(--surface-2)}.decision-note{width:100%;box-sizing:border-box;min-height:62px;resize:vertical;background:var(--bg);color:var(--ink);border:1px solid var(--edge);border-radius:7px;padding:9px 10px;font:12px/1.5 var(--sans);margin:7px 0 9px}.decision-actions{display:flex;gap:8px;flex-wrap:wrap}.decision{font:inherit;font-size:11px;border:1px solid var(--edge);border-radius:7px;padding:7px 10px;background:var(--bg);color:var(--ink);cursor:pointer}.decision:hover{border-color:var(--accent)}.decision.approved-backlog{color:var(--good)}.decision.rejected,.decision.stopped{color:var(--crit)}.decision.resolved{color:var(--info)}.decision[disabled]{opacity:.5;cursor:wait}.decision-status{font-size:11px;color:var(--faint);margin-left:auto;align-self:center}.decision-status.error{color:var(--crit)}.legacy-note{color:var(--warn);font-size:11px}@media(max-width:1000px){.proposal>summary{grid-template-columns:80px 125px 1fr auto}.proposal-recommendation{grid-column:3}.proposal-body{padding-left:12px}}
/* proposal-review:end */
"""

# The V1 dashboard embeds this exact review surface. Keeping the bounded CSS
# here makes the estate page the owner of proposal-review presentation.
PROPOSAL_CSS = CSS.split("/* proposal-review:start */", 1)[1].split(
    "/* proposal-review:end */", 1)[0]

JS_HEAD = r"""(function(){
var DATA={},FILTER='active',KIND_FILTER='',PROJECT_FILTER='',ATTN_FILTER='all',ATTN_HISTORY=false,DEP_FILTER='all',PROPOSAL_STAGE='pending-review',MEMORY_SCOPE='shared',MEMORY_SCROLL=0,OPEN_TASKS={},OPEN_EVENTS={},OPEN_PROJECTS={},OPEN_PROPOSALS={},DECISION_NOTES={},INDEX={},EVENT_INDEX={};
// The task lifecycle's two terminal statuses. Mirrors bin/estate's TERMINAL
// and lib/estate_work's; "Active" on this page means NOT one of these, which
// is the whole correction — the old button matched the literal string 'open'
// and hid every ready/claimed/blocked/needs-owner row in the estate.
var TERMINAL=['done','dropped'];
var DUE_STATES=['unreadable','overdue','due_today','due_soon','later','undated'];
var DEP_KINDS=['blocks','parent','related','discovered-from'];
function e(v){var d=document.createElement('div');d.textContent=v==null?'':String(v);return d.innerHTML}
function age(v){if(!v)return '—';var n=typeof v==='number'?v*1000:Date.parse(v),s=Math.max(0,(Date.now()-n)/1000);if(s<90)return Math.round(s)+'s ago';if(s<5400)return Math.round(s/60)+'m ago';if(s<172800)return Math.round(s/3600)+'h ago';return Math.round(s/86400)+'d ago'}
// Minute precision, and the trailing Z only when the stored value really was
// UTC — every estate timestamp is, but inventing one on a value that is not
// would be a claim the snapshot never made.
function stamp(v){if(!v)return '—';var s=String(v),utc=/(\+00:00|Z)$/.test(s);return s.replace('T',' ').slice(0,16)+(utc?'Z':'')}
function stats(c,names){return '<div class="estate-grid">'+names.map(function(k){return '<div class="estate-stat"><b>'+e((c||{})[k]||0)+'</b><span>'+e(k)+'</span></div>'}).join('')+'</div>'}
function rows(a,fn,msg){return a&&a.length?'<div class="estate-list">'+a.map(fn).join('')+'</div>':'<div class="empty">'+msg+'</div>'}
function terminal(t){return TERMINAL.indexOf(t&&t.status)>=0}
// P-16: a task's drill-down is its COMPLETE history, served pre-grouped from
// the events table by `estate events`' own query surface. The filter over
// DATA.events is only a fallback for a snapshot written before task_events
// existed — that path shows a tail, which is the bug this replaced.
function taskEvents(id){var m=DATA.task_events;if(m&&m[id])return m[id];if(m)return [];return (DATA.events||[]).filter(function(v){return v.task_id===id})}
function task(id){return INDEX[id]}
function taskTitle(id){var t=INDEX[id];return t?t.title:''}
function pretty(v){if(!v)return '';try{return JSON.stringify(typeof v==='string'?JSON.parse(v):v,null,2)}catch(_){return typeof v==='string'?v:JSON.stringify(v,null,2)}}
// Actors arrive already folded into the taxonomy (docs/actor-taxonomy.md); an
// ephemeral row is anonymous, so show its dispatch slug next to the actor.
function who(v){var a=v.actor||'unknown';return e(v.session_id?a+' · '+v.session_id:a)}
function chips(c){var k=Object.keys(c||{});k.sort(function(a,b){return (c[b]-c[a])||a.localeCompare(b)});return k.map(function(n){return '<span class="chipx">'+e(n)+' <b>'+e(c[n])+'</b></span>'}).join('')}
function drawMemories(m){var tabs=[{scope:'shared',available:true,body:(m.items||[]).map(function(v){return '['+v.status+'] '+v.id+' · '+v.kind+'\n'+v.body}).join('\n\n')}].concat(m.private||[]);var buttons=tabs.map(function(v){return '<button class="memory-tab" data-memory="'+e(v.scope)+'">'+e(v.scope)+(v.available?'':' · empty')+'</button>'}).join('');document.getElementById('memories').innerHTML='<div class="memory-tabs">'+buttons+'</div><div class="memory-body" id="memory-body"></div>';function select(scope,restore){var v=tabs.find(function(x){return x.scope===scope})||tabs[0],body=v.body||'',box=document.getElementById('memory-body');MEMORY_SCOPE=v.scope;box.innerHTML=body?e(body):'<span class="empty">No '+e(v.scope)+' memories yet.</span>';document.querySelectorAll('[data-memory]').forEach(function(b){b.classList.toggle('active',b.dataset.memory===v.scope)});box.scrollTop=restore?MEMORY_SCROLL:0}document.querySelectorAll('[data-memory]').forEach(function(b){b.onclick=function(){MEMORY_SCROLL=0;select(b.dataset.memory,false)}});select(MEMORY_SCOPE,true)}
// P-18: blocked_by arrives computed from unresolved `blocks` edges and is NOT
// a status — a blocked task still reads `ready`, because that is what the
// schema says and being blocked is a fact about its neighbours. The badge is
// the visible half of what `estate ready` already acted on silently. It is
// recomputed server-side on every snapshot, so it clears itself the moment the
// blocker finishes; nothing here caches it.
function blockedBy(t){return (t.blocked_by||[])}
function blockedBadge(t){var b=blockedBy(t);return b.length?'<span class="blocked" title="blocked by '+e(b.join(', '))+'">⛔ '+e(b.join(' '))+'</span>':''}
// P-02: `stage` is the RETAINED REVIEW STATE — whether the owner has seen this,
// agreed to it, or turned it down. It is not a status and does not replace
// one: a proposal awaiting review and one already approved are both `ready`
// rows, and without the badge the board cannot tell them apart. Only
// approved-backlog is claimable, so the badge is also the visible half of the
// gate `estate ready` enforces.
function stageBadge(t){return t.stage?'<span class="stage '+e(t.stage)+'" title="review stage">'+e(t.stage)+'</span>':''}
// The recurring-condition disposition. `estate proposal add` never mints a
// second row for a fingerprint already on file — it records one of these on
// the existing task instead, so the count IS how many times the condition
// came back after it was first proposed.
function recurBadge(t){if(!t.stage)return '';var n=taskEvents(t.id).filter(function(v){return v.summary==='condition observed again'}).length;return n?'<span class="recur" title="condition observed again since it was filed">↻ '+e(n)+'</span>':''}
// P-07: `due_state` is DERIVED server-side against one instant under one
// explicit notice interval, never stored — a deadline arriving changes no row.
// A terminal task carries no state at all: its deadline is history, and
// calling a finished task overdue is how a section teaches its reader to skip
// it. `undated` is an answer, not the absence of one.
function dueBadge(t){if(!t.due_state)return '';var left=t.days_left==null?'':' '+t.days_left+'d';return '<span class="due '+e(t.due_state)+'" title="'+e(t.due_at?'due '+t.due_at:'no deadline recorded')+'">'+e(t.due_state+left)+'</span>'}
function legacyBadge(t){var f=t.followup;return f&&f.legacy_id?'<span class="legacy" title="legacy follow-up identity">'+e(f.legacy_id)+'</span>':''}
function projectBadge(t){return t.project_id?'<span class="pill" title="project '+e(t.project_id)+'">'+e(t.project_title||t.project_id)+'</span>':''}
function kv(label,value){return value==null||value===''?'':'<span class="label">'+e(label)+'</span><span>'+e(value)+'</span>'}
// Both directions of one edge set, resolved to titles server-side so nothing
// here linear-scans the task array to name a blocker.
function relatedLine(label,ids,titles){if(!ids||!ids.length)return '';return '<div><span class="label">'+e(label)+'</span> '+ids.map(function(i){return e(i)+((titles||{})[i]?' · '+e(titles[i]):'')}).join(' · ')+'</div>'}
function mdInline(s){var code=[],x=e(s||'');x=x.replace(/`([^`\n]+)`/g,function(_,v){code.push(v);return '§§CODE'+(code.length-1)+'§§'});x=x.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');x=x.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');x=x.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');return x.replace(/§§CODE(\d+)§§/g,function(_,n){return '<code>'+code[Number(n)]+'</code>'})}
function mdCells(line){var s=line.trim();if(s[0]==='|')s=s.slice(1);if(s[s.length-1]==='|')s=s.slice(0,-1);return s.split('|').map(function(v){return v.trim()})}
function markdown(raw){var lines=String(raw||'').replace(/\r\n?/g,'\n').split('\n'),out=[],i=0,para=[];function flush(){if(para.length){out.push('<p>'+mdInline(para.join(' '))+'</p>');para=[]}}function separator(line){return mdCells(line).every(function(v){return /^:?-{3,}:?$/.test(v)})}while(i<lines.length){var line=lines[i],trim=line.trim();if(!trim){flush();i++;continue}var fence=trim.match(/^```\s*([\w-]*)/);if(fence){flush();var code=[];i++;while(i<lines.length&&!/^```\s*$/.test(lines[i].trim()))code.push(lines[i++]);if(i<lines.length)i++;out.push('<pre><code>'+e(code.join('\n'))+'</code></pre>');continue}var head=trim.match(/^(#{1,4})\s+(.+)$/);if(head){flush();var n=head[1].length;out.push('<h'+n+'>'+mdInline(head[2])+'</h'+n+'>');i++;continue}if(/^([-*_])(?:\s*\1){2,}$/.test(trim)){flush();out.push('<hr>');i++;continue}if(line.indexOf('|')>=0&&i+1<lines.length&&separator(lines[i+1])){flush();var heads=mdCells(line);i+=2;var body=[];while(i<lines.length&&lines[i].indexOf('|')>=0&&lines[i].trim())body.push(mdCells(lines[i++]));out.push('<div class="md-table-wrap"><table><thead><tr>'+heads.map(function(v){return '<th>'+mdInline(v)+'</th>'}).join('')+'</tr></thead><tbody>'+body.map(function(row){return '<tr>'+heads.map(function(_,j){return '<td>'+mdInline(row[j]||'')+'</td>'}).join('')+'</tr>'}).join('')+'</tbody></table></div>');continue}var bullet=trim.match(/^[-*+]\s+(.+)$/),ordered=trim.match(/^\d+[.)]\s+(.+)$/);if(bullet||ordered){flush();var tag=ordered?'ol':'ul',items=[];while(i<lines.length){var m=lines[i].trim().match(ordered?/^\d+[.)]\s+(.+)$/:/^[-*+]\s+(.+)$/);if(!m)break;var item=[m[1]];i++;while(i<lines.length&&lines[i].trim()&&!/^(?:[-*+]\s+|\d+[.)]\s+|#{1,4}\s+)/.test(lines[i].trim()))item.push(lines[i++].trim());items.push(item.join(' '))}out.push('<'+tag+'>'+items.map(function(v){return '<li>'+mdInline(v)+'</li>'}).join('')+'</'+tag+'>');continue}if(/^>\s?/.test(trim)){flush();var quote=[];while(i<lines.length&&/^>\s?/.test(lines[i].trim()))quote.push(lines[i++].trim().replace(/^>\s?/,''));out.push('<blockquote>'+mdInline(quote.join(' '))+'</blockquote>');continue}para.push(trim);i++}flush();return '<div class="md-note">'+out.join('')+'</div>'}
var LONG_NOTE=4000;
function noteDetail(v){if(!v.detail)return '';if(v.kind!=='note')return '<br>'+e(v.detail);if(v.detail.length<LONG_NOTE)return markdown(v.detail);var first=v.detail.split(/\n\s*\n/)[0];return '<div class="note-preview">'+markdown(first)+'</div><button type="button" class="full-note-button" data-full-note="'+e(String(v.seq))+'">Read full note · '+e(v.detail.length.toLocaleString())+' characters</button>'}
function historyLine(v){return '<div class="history-entry"><div class="history-meta"><span class="time">'+e(stamp(v.ts))+'</span> <b>'+e(v.summary)+'</b> · '+who(v)+(v.kind?' · '+e(v.kind):'')+(v.phase?' · '+e(v.phase):'')+'</div>'+noteDetail(v)+'</div>'}
function history(list,msg){return '<div class="hist"><span class="label">history ('+e(list.length)+')</span>'+(list.length?list.map(historyLine).join(''):'<div>'+e(msg)+'</div>')+'</div>'}
function taskDetails(t){var refs=pretty(t.refs),f=t.followup;
return '<div class="details"><div class="kv">'+kv('kind',t.kind)+kv('status',t.status)+kv('project',t.project_id?(t.project_title||'')+' ('+t.project_id+')':'unprojected')+kv('lane',t.lane)+kv('review stage',t.stage)+kv('created by',t.created_by)+kv('created',stamp(t.created_at)+' · '+age(t.created_at))+kv('updated',stamp(t.updated_at)+' · '+age(t.updated_at))+kv('closed',t.closed_at?stamp(t.closed_at):'')+kv('claimed by',t.claimed_by)+kv('lease expires',t.claim_expires_at)+kv('due',t.due_at?stamp(t.due_at)+' ('+(t.due_state||'terminal')+')':'')+kv('intent',t.intent)+'</div>'
+(f?'<div class="kv">'+kv('follow-up',f.legacy_id)+kv('state',f.state)+kv('source',f.source)+kv('ref',f.ref)+kv('context',f.context)+kv('resolution',f.resolution)+kv('attention key',f.attention_key)+kv('resolved at',f.resolved_at?stamp(f.resolved_at):'')+'</div>':'')
+relatedLine('blocked by',blockedBy(t),t.blocked_by_titles)+relatedLine('blocks',t.blocks,t.blocks_titles)
+(refs&&refs!=='{}'?'<div><span class="label">refs</span><pre>'+e(refs)+'</pre></div>':'')
+history(taskEvents(t.id),'No events recorded for this task.')+'</div>'}
function taskRow(t){var b=blockedBy(t);return '<details data-task="'+e(t.id)+'" class="task '+e(t.status)+(b.length?' is-blocked':'')+'"'+(OPEN_TASKS[t.id]?' open':'')+'><summary><span class="id">'+e(t.id)+'</span><span class="status">'+e(t.status)+'</span><span>'+e(t.title)+' '+legacyBadge(t)+blockedBadge(t)+stageBadge(t)+recurBadge(t)+dueBadge(t)+projectBadge(t)+'</span><span>'+e(age(t.updated_at))+'</span></summary>'+taskDetails(t)+'</details>'}
function haystack(t){var f=t.followup||{};return (t.id+' '+t.title+' '+t.kind+' '+(t.stage||'')+' '+(t.lane||'')+' '+(t.intent||'')+' '+(t.claimed_by||'')+' '+(t.project_id||'')+' '+(t.project_title||'')+' '+(f.legacy_id||'')+' '+(f.source||'')+' '+(f.ref||'')+' '+(f.context||'')).toLowerCase()}
// "Active" is every NON-TERMINAL status. `blocked` and `attention` are cuts
// across the same set rather than statuses of their own — being blocked is a
// fact about a task's neighbours and being attention is a fact about who is
// waiting, and neither is stored on the row.
function matchesFilter(t){if(FILTER==='all')return true;if(FILTER==='active')return !terminal(t);if(FILTER==='blocked')return blockedBy(t).length>0;if(FILTER==='attention')return !!t.is_attention;if(FILTER==='terminal')return terminal(t);return t.status===FILTER}
function drawTasks(){var q=(document.getElementById('search').value||'').toLowerCase();var a=(DATA.tasks||[]).filter(function(t){return matchesFilter(t)&&(!KIND_FILTER||t.kind===KIND_FILTER)&&(!PROJECT_FILTER||(PROJECT_FILTER==='unprojected'?!t.project_id:t.project_id===PROJECT_FILTER))&&(!q||haystack(t).indexOf(q)>=0)});document.getElementById('task-count').textContent=a.length+' shown';document.getElementById('tasks').innerHTML=rows(a,taskRow,'No matching tasks.')}
"""

JS_ATTENTION = r"""
// §5.3 A — what is waiting on a PERSON. The set is a query the server already
// answered (docs/attention-contract.md): an open follow-up, or a task parked
// at `needs-owner`. DATA.attention is an ORDERED LIST OF IDS, most urgent
// first, so nothing here re-sorts and nothing re-derives who is in the set.
function attentionRows(){var ids=DATA.attention||[];return ids.map(task).filter(Boolean)}
// Resolved follow-ups are history, not attention, and they live behind this
// toggle rather than mixed into the list above.
function resolvedFollowups(){return (DATA.followups||[]).map(task).filter(function(t){return t&&t.followup&&t.followup.state==='resolved'})}
function sourceBadge(t){var f=t.followup;return f&&f.source?'<span class="pill" title="source'+(f.ref?' · '+e(f.ref):'')+'">'+e(f.source)+'</span>':''}
function attentionRow(t){var b=blockedBy(t);return '<details data-task="'+e(t.id)+'" class="task '+e(t.status)+(b.length?' is-blocked':'')+'"'+(OPEN_TASKS[t.id]?' open':'')+'><summary><span class="id">'+e(t.id)+legacyBadge(t)+'</span><span class="status">'+e(t.status)+'</span><span>'+e(t.title)+' '+dueBadge(t)+blockedBadge(t)+projectBadge(t)+sourceBadge(t)+'</span><span>'+e(age(t.updated_at))+'</span></summary>'+taskDetails(t)+'</details>'}
function drawAttention(){var list=ATTN_HISTORY?resolvedFollowups():attentionRows();if(!ATTN_HISTORY&&ATTN_FILTER!=='all')list=list.filter(function(t){return t.due_state===ATTN_FILTER});document.getElementById('attention-stats').innerHTML=stats(DATA.attention_counts,DUE_STATES);document.getElementById('attention-count').textContent=(ATTN_HISTORY?'history · ':'')+list.length+' shown'+(DATA.notice_days!=null?' · notice '+DATA.notice_days+'d':'');document.getElementById('attention').innerHTML=rows(list,attentionRow,ATTN_HISTORY?'No resolved follow-ups on file.':'Nothing is waiting on a person.')}
"""

JS_INBOX = r"""
// t-296 — open inbox messages. The escape hatch that does not go through
// Slack: every self-DM message the hub mirrored and has not closed, oldest
// first, so "did that ever get answered?" is a glance rather than a memory of
// which thread it was in. DATA.inbox_messages is an ORDERED LIST OF IDS on the
// same terms as attention — the order is the payload, nothing here re-sorts.
// `ready` means registered and not resolved, `claimed` means a dispatched
// worker has it, `needs-owner` also appears in Attention above (deliberately:
// one attention set, and this panel is a cut across the conversations, not a
// second one).
function inboxRows(){return (DATA.inbox_messages||[]).map(task).filter(Boolean)}
function threadBadge(t){var m=t.message||{};return m.threaded?'<span class="pill" title="arrived as a thread reply under '+e(m.thread_ts)+'">thread</span>':''}
function inboxBadge(t){var m=t.message||{};return m.inbox?'<span class="pill" title="inbox">'+e(m.inbox)+'</span>':''}
function inboxRow(t){return '<details data-task="'+e(t.id)+'" class="task '+e(t.status)+'"'+(OPEN_TASKS[t.id]?' open':'')+'><summary><span class="id">'+e(t.id)+'</span><span class="status">'+e(t.status)+'</span><span>'+e(t.title)+' '+inboxBadge(t)+threadBadge(t)+blockedBadge(t)+'</span><span>'+e(age(t.created_at))+'</span></summary>'+taskDetails(t)+'</details>'}
function drawInbox(){var list=inboxRows(),c=DATA.inbox_message_counts||{};document.getElementById('inbox-count').textContent=(c.total||0)+' open';document.getElementById('inbox-stats').innerHTML=stats(c,['ready','claimed','needs-owner','blocked']);document.getElementById('inbox').innerHTML=rows(list,inboxRow,'No inbox message is still open.')}
"""

JS_PROJECTS = r"""
// §5.3 C — projects as operational groupings. Every count is rolled up from
// the task rows on the server's read, never stored: a counter column would
// have to be re-written every time a task moved, and the once it was not is
// the time this page lies. "Unprojected work" is a SYNTHETIC bucket for
// project_id IS NULL — most of the store — and carries the same rollup shape
// as a real project so nothing here special-cases it.
function projectEvents(id){var m=DATA.project_events;return (m&&m[id])||[]}
// A project's full activity is its own events PLUS its tasks' — composed here
// rather than duplicated into the snapshot, which is already append-only and
// growing (t-124 §4.10). The snapshot ships each row exactly once (a row that
// names both a task and a project rides in task_events), so this is a join,
// not a merge; the seq dedup is a guard, not the mechanism.
// The synthetic grouping is deliberately excluded: "everything that belongs to
// no project" has no history of its own, and composing one would just re-print
// most of the estate's ledger under a heading that claims otherwise. Each of
// its tasks still carries its own.
function projectHistory(p){if(p.unprojected)return null;var seen={},out=projectEvents(p.id).slice();out.forEach(function(v){seen[v.seq]=true});(p.rollup.task_ids||[]).forEach(function(id){taskEvents(id).forEach(function(v){if(!seen[v.seq]){seen[v.seq]=true;out.push(v)}})});out.sort(function(a,b){return (a.seq||0)-(b.seq||0)});return out}
function projectRow(p){var r=p.rollup||{},cls=p.unprojected?'grouping':(p.missing?'missing':'');
return '<details data-project="'+e(p.id)+'" class="task"'+(OPEN_PROJECTS[p.id]?' open':'')+'><summary><span class="id">'+e(p.id)+'</span><span class="status">'+e(p.status)+'</span><span>'+e(p.title)+(cls?'<span class="pill '+e(cls)+'">'+e(p.unprojected?'grouping':'unknown project')+'</span>':'')+'<span class="pill">'+e(r.open||0)+' open</span><span class="pill">'+e(r.terminal||0)+' terminal</span>'+((r.blocked||0)?'<span class="blocked">⛔ '+e(r.blocked)+' blocked</span>':'')+((r.attention||0)?'<span class="due due_soon">'+e(r.attention)+' attention</span>':'')+((r.overdue||0)?'<span class="due overdue">'+e(r.overdue)+' overdue</span>':'')+'</span><span>'+e(age(r.last_activity))+'</span></summary><div class="details"><div class="kv">'+kv('kind',p.kind)+kv('external key',p.external_key)+kv('created by',p.created_by)+kv('created',p.created_at?stamp(p.created_at)+' · '+age(p.created_at):'')+kv('closed',p.closed_at?stamp(p.closed_at):'')+kv('tasks',r.tasks||0)+kv('last task activity',r.last_activity?stamp(r.last_activity)+' · '+age(r.last_activity):'')+'</div><div class="chips">'+chips(r.by_status)+'</div><div class="chips" style="padding-top:6px">'+chips(r.by_kind)+'</div><div style="padding-top:9px"><span class="label">tasks</span> '+((r.task_ids||[]).length?(r.task_ids||[]).map(function(i){var t=task(i);return e(i)+(t?' ('+e(t.status)+') '+e(t.title):'')}).join('<br>'):'none')+'</div>'+(projectHistory(p)?history(projectHistory(p),'No events recorded for this project.'):'<div class="hist"><span class="label">history</span><div>Work that belongs to no project has no history of its own — each task above carries its own.</div></div>')+'</div></details>'}
function drawProjects(){var list=DATA.projects||[];document.getElementById('project-stats').innerHTML='<div class="chips">'+(chips(DATA.project_counts)||'<span class="chipx">no projects on file</span>')+'</div>';document.getElementById('projects').innerHTML=rows(list,projectRow,'No projects and no unprojected work.')}
"""

JS_DEPS = r"""
// §5.3 D — two complementary reads of ONE stored fact. The edge is what the
// store holds; `blocked_by` and `blocks` are that edge read from either end,
// which is why neither needs its own array on a task row.
//
// Operational blockers: unresolved `blocks` edges only — `active` is the same
// rule `estate ready` schedules on, the SOURCE task not yet terminal. Both
// endpoints arrive with title and status resolved, so a blocker is never a
// bare id here.
function edges(){return DATA.dependencies||[]}
function edgeRow(v){return '<div class="edge '+e(v.kind)+(v.active===false?' inactive':'')+'"><span class="end"><span class="id">'+e(v.from_task)+'</span> '+e(v.from_title||'')+(v.from_status?'<span class="pill">'+e(v.from_status)+'</span>':'')+'</span><span class="arrow" title="'+e(v.kind)+'">'+e(v.kind)+'</span><span class="end"><span class="id">'+e(v.to_task)+'</span> '+e(v.to_title||'')+(v.to_status?'<span class="pill">'+e(v.to_status)+'</span>':'')+'</span><span class="time" title="'+e(v.created_at||'')+'">'+e(v.active===false?'resolved':age(v.created_at))+'</span></div>'}
function drawDeps(){var all=edges(),live=all.filter(function(v){return v.kind==='blocks'&&v.active});
document.getElementById('blockers').innerHTML=rows(live,edgeRow,'Nothing is holding any task up.');
var list=DEP_FILTER==='all'?all:all.filter(function(v){return v.kind===DEP_FILTER});
document.getElementById('dep-count').textContent=list.length+' of '+all.length+' edges';
document.getElementById('dep-stats').innerHTML='<div class="chips">'+(chips(DATA.dep_counts)||'<span class="chipx">no dependencies on file</span>')+'</div>';
document.getElementById('deps').innerHTML=rows(list,edgeRow,'No matching relationships.')}
"""

JS_PROPOSALS = r"""
var PROPOSAL_TRANSITIONS={
 'pending-review':['approved-backlog','rejected','resolved','stopped'],
 'approved-backlog':['resolved','stopped','rejected','pending-review'],
 'rejected':['pending-review'],'resolved':['pending-review'],
 'stopped':['pending-review','approved-backlog']};
function reviewField(label,value,cls){var missing=!String(value||'').trim();return '<div class="review-field '+(cls||'')+(missing?' missing':'')+'"><span class="label">'+e(label)+'</span><br>'+e(missing?'not recorded (legacy proposal)':value)+'</div>'}
function reviewBadges(p){var out=[];if(p.legacy)out.push('<span class="review-badge incomplete">incomplete record</span>');if(p.recurrences)out.push('<span class="review-badge recurring">recurring ×'+e(p.recurrences)+'</span>');return out.join('')}
function decisionBox(t){var stages=PROPOSAL_TRANSITIONS[t.stage]||[];if(!stages.length)return '';var labels={'approved-backlog':'Approve to backlog','rejected':'Reject','resolved':'Mark resolved','stopped':'Stop','pending-review':'Return to review'};return '<div class="decision-box"><span class="label">Record stage decision</span><textarea class="decision-note" data-decision-note="'+e(t.id)+'" placeholder="Decision note (required)">'+e(DECISION_NOTES[t.id]||'')+'</textarea><div class="decision-actions">'+stages.map(function(s){return '<button type="button" class="decision '+e(s)+'" data-decision-id="'+e(t.id)+'" data-decision-stage="'+e(s)+'">'+e(labels[s]||s)+'</button>'}).join('')+'<span class="decision-status" data-decision-status="'+e(t.id)+'"></span></div></div>'}
function proposalRow(t){var p=t.proposal||{},recommendation=p.desired_outcome||'Recommendation not recorded';return '<details data-proposal="'+e(t.id)+'" class="proposal"'+(OPEN_PROPOSALS[t.id]?' open':'')+'><summary><span class="id">'+e(t.id)+'</span><span class="stage '+e(t.stage)+'">'+e(t.stage)+'</span><span class="proposal-title">'+e(t.title)+reviewBadges(p)+'</span><span class="proposal-recommendation"><span class="label">Recommendation</span><br>'+e(recommendation)+'</span><span>'+e(age(t.updated_at))+'</span></summary><div class="proposal-body">'+(p.legacy?'<div class="legacy-note">Review record incomplete: one or more evidence fields were never recorded.</div>':'')+'<div class="review-grid">'+reviewField('What was observed',p.condition)+reviewField('Recommendation',p.desired_outcome,'recommendation')+reviewField('Done when',p.completion_check)+'</div><div class="kv">'+kv('fingerprint',p.fingerprint||'not recorded')+kv('work status',t.status)+kv('created by',t.created_by)+kv('updated',stamp(t.updated_at)+' · '+age(t.updated_at))+kv('recurrences',p.recurrences||0)+(p.last_recurrence_at?kv('last recurrence',stamp(p.last_recurrence_at)):'')+'</div>'+(t.intent?'<div class="review-field"><span class="label">intent</span><br>'+e(t.intent)+'</div>':'')+decisionBox(t)+history(taskEvents(t.id),'No events recorded for this proposal.')+'</div></details>'}
function drawProposals(){var q=(document.getElementById('proposal-search').value||'').toLowerCase(),list=(DATA.proposals||[]).map(task).filter(Boolean);if(PROPOSAL_STAGE!=='all')list=list.filter(function(t){return t.stage===PROPOSAL_STAGE});if(q)list=list.filter(function(t){var p=t.proposal||{};return [t.id,t.title,t.intent,p.condition,p.desired_outcome,p.completion_check,p.fingerprint].join(' ').toLowerCase().indexOf(q)>=0});document.getElementById('proposal-stats').innerHTML=stats(DATA.proposal_counts,['pending-review','approved-backlog','rejected','stopped','resolved']);document.getElementById('proposal-count').textContent=list.length+' shown';document.getElementById('proposals').innerHTML=rows(list,proposalRow,'No proposals in this stage.')}
var TOKEN_KEY='ops-dashboard-write-token';
function decisionToken(){var t=null;try{t=localStorage.getItem(TOKEN_KEY)}catch(_){t=null}if(!t){try{t=window.prompt('Dashboard write token (required for proposal decisions):')||''}catch(_){t=''}if(t)try{localStorage.setItem(TOKEN_KEY,t)}catch(_){}}return t}
function decisionStatus(id,text,error){var s=document.querySelector('[data-decision-status="'+id+'"]');if(s){s.textContent=text||'';s.classList.toggle('error',!!error)}}
function decideProposal(button){var id=button.dataset.decisionId,target=button.dataset.decisionStage,t=task(id),note=document.querySelector('[data-decision-note="'+id+'"]');if(!t||!note)return;var text=note.value.trim();if(!text){decisionStatus(id,'A decision note is required.',true);note.focus();return}if(['rejected','resolved','stopped'].indexOf(target)>=0&&!window.confirm('Move '+id+' to '+target+'?'))return;var tok=decisionToken();if(!tok){decisionStatus(id,'Write token required.',true);return}var buttons=document.querySelectorAll('[data-decision-id="'+id+'"]');buttons.forEach(function(b){b.disabled=true});decisionStatus(id,'Recording…',false);fetch('proposal-stage',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Dashboard-Token':tok},body:JSON.stringify({id:id,expected_stage:t.stage,stage:target,note:text})}).then(function(r){return r.json().catch(function(){return {error:'Request failed'}}).then(function(body){if(!r.ok){var x=Error(body.error||('HTTP '+r.status));x.status=r.status;throw x}return body})}).then(function(result){var old=t.stage;t.stage=result.stage;t.status=result.task_status||t.status;delete DECISION_NOTES[id];if(DATA.proposal_counts){DATA.proposal_counts[old]=Math.max(0,(DATA.proposal_counts[old]||0)-1);DATA.proposal_counts[t.stage]=(DATA.proposal_counts[t.stage]||0)+1}OPEN_PROPOSALS[id]=false;drawProposals()}).catch(function(err){if(err.status===401)try{localStorage.removeItem(TOKEN_KEY)}catch(_){}decisionStatus(id,err.message+(err.status===401?' Token cleared; retry to enter it again.':''),true);buttons.forEach(function(b){b.disabled=false})})}
"""

JS_BODY = r"""
function options(sel,items,current,label){var el=document.getElementById(sel);if(!el)return;el.innerHTML='<option value="">'+label+'</option>'+items.map(function(v){return '<option value="'+e(v.value)+'"'+(v.value===current?' selected':'')+'>'+e(v.text)+'</option>'}).join('')}
function refreshSelectors(){var kinds={},projects=[];(DATA.tasks||[]).forEach(function(t){kinds[t.kind]=(kinds[t.kind]||0)+1});
options('kind-filter',Object.keys(kinds).sort().map(function(k){return {value:k,text:k+' ('+kinds[k]+')'}}),KIND_FILTER,'any kind');
(DATA.projects||[]).forEach(function(p){projects.push({value:p.unprojected?'unprojected':p.id,text:p.title+' ('+((p.rollup||{}).tasks||0)+')'})});
options('project-filter',projects,PROJECT_FILTER,'any project')}
function render(d){var box=document.getElementById('memory-body');if(box)MEMORY_SCROLL=box.scrollTop;document.querySelectorAll('[data-decision-note]').forEach(function(v){DECISION_NOTES[v.dataset.decisionNote]=v.value});OPEN_TASKS={};OPEN_EVENTS={};OPEN_PROJECTS={};OPEN_PROPOSALS={};document.querySelectorAll('[data-task][open]').forEach(function(v){OPEN_TASKS[v.dataset.task]=true});document.querySelectorAll('[data-event][open]').forEach(function(v){OPEN_EVENTS[v.dataset.event]=true});document.querySelectorAll('[data-project][open]').forEach(function(v){OPEN_PROJECTS[v.dataset.project]=true});document.querySelectorAll('[data-proposal][open]').forEach(function(v){OPEN_PROPOSALS[v.dataset.proposal]=true});var x=d.estate_activity||{},ex=x.extractions||{},lh=x.ledger_health||{},m=x.memories||{};DATA=x;
INDEX={};(x.tasks||[]).forEach(function(t){INDEX[t.id]=t});
EVENT_INDEX={};Object.keys(x.task_events||{}).forEach(function(id){(x.task_events[id]||[]).forEach(function(v){EVENT_INDEX[v.seq]=v})});(x.events||[]).forEach(function(v){EVENT_INDEX[v.seq]=v});
document.getElementById('estate-title').textContent=(d.estate||'estate')+' / '+(d.operator||'hub');
var gen=document.getElementById('generated-at');if(gen)gen.textContent=d.generated_at?'snapshot '+age(d.generated_at):'snapshot age unknown';
refreshSelectors();
drawAttention();drawInbox();
document.getElementById('task-stats').innerHTML=stats(x.task_counts,['open','ready','claimed','blocked','needs-owner','done','dropped']);drawTasks();
drawProjects();drawDeps();
drawProposals();
document.getElementById('events').innerHTML=rows((x.events||[]).slice(0,30),function(v){var target=v.task_id||v.memory_id||v.project_id||'estate',title=taskTitle(v.task_id),refs=pretty(v.refs);return '<details data-event="'+e(v.seq)+'" class="event"'+(OPEN_EVENTS[v.seq]?' open':'')+'><summary><span class="time">'+e(age(v.ts))+'</span><span class="id">'+e(target)+'</span><span><b>'+e(v.summary)+'</b>'+(title?' · '+e(title):'')+'</span><span>'+who(v)+'</span></summary><div class="event-detail"><span class="label">kind</span> '+e(v.kind)+' · <span class="label">at</span> '+e(v.ts)+noteDetail(v)+(refs?'<pre>'+e(refs)+'</pre>':(!v.detail?'<p>No additional detail recorded.</p>':''))+'</div></details>'},'No estate events yet.');
document.getElementById('extraction-stats').innerHTML=stats(ex.counts,['candidate','extracting','blocked','synced']);
document.getElementById('extractions').innerHTML=rows(ex.items,function(i){return '<div class="row"><span class="id">'+e(i.id)+'</span><span class="status">'+e(i.status)+'</span><span>'+e(i.title)+'</span><span>'+e(age(i.updated_at))+'</span></div>'},'No extraction candidates on file yet.');
document.getElementById('ledger-summary').innerHTML='<div class="split"><div class="subpanel"><span class="label">central ledger</span><div class="n">'+e(lh.count||0)+'</div><span class="source-badge">'+(lh.available?'live JSONL':'not available')+'</span></div><div class="subpanel"><span class="label">activity by actor</span><div class="chips">'+chips(lh.by_actor)+'</div></div></div>';
document.getElementById('ledger').innerHTML=rows((lh.recent||[]).slice(0,30),function(v){return '<div class="row"><span class="time">'+e(age(v.ts))+'</span><span>'+e(v.kind||'activity')+'</span><span>'+e(v.summary)+'</span><span>'+who(v)+'</span></div>'},'No central-ledger entries yet.');
drawMemories(m);
}
"""
JS_TAIL = r"""
// Every handler below is display-only except the explicitly named proposal
// decision delegation above. No task/project/dependency mutation is exposed.
document.querySelectorAll('[data-filter]').forEach(function(b){b.onclick=function(){FILTER=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(function(x){x.classList.toggle('active',x===b)});drawTasks()}});
document.querySelectorAll('[data-attn]').forEach(function(b){b.onclick=function(){var v=b.dataset.attn;if(v==='history'){ATTN_HISTORY=!ATTN_HISTORY}else{ATTN_HISTORY=false;ATTN_FILTER=v}document.querySelectorAll('[data-attn]').forEach(function(x){x.classList.toggle('active',x.dataset.attn==='history'?ATTN_HISTORY:(!ATTN_HISTORY&&x.dataset.attn===ATTN_FILTER))});drawAttention()}});
document.querySelectorAll('[data-dep]').forEach(function(b){b.onclick=function(){DEP_FILTER=b.dataset.dep;document.querySelectorAll('[data-dep]').forEach(function(x){x.classList.toggle('active',x===b)});drawDeps()}});
document.querySelectorAll('[data-proposal-stage]').forEach(function(b){b.onclick=function(){PROPOSAL_STAGE=b.dataset.proposalStage;document.querySelectorAll('[data-proposal-stage]').forEach(function(x){x.classList.toggle('active',x===b)});drawProposals()}});
document.getElementById('kind-filter').onchange=function(){KIND_FILTER=this.value;drawTasks()};
document.getElementById('project-filter').onchange=function(){PROJECT_FILTER=this.value;drawTasks()};
document.getElementById('search').oninput=drawTasks;
document.getElementById('proposal-search').oninput=drawProposals;
document.getElementById('proposals').onclick=function(ev){var b=ev.target.closest('[data-decision-id]');if(b)decideProposal(b)};
document.addEventListener('click',function(ev){var b=ev.target.closest('[data-full-note]');if(!b)return;var v=EVENT_INDEX[b.dataset.fullNote],dialog=document.getElementById('note-dialog');if(!v||!dialog)return;document.getElementById('note-dialog-title').textContent=(v.task_id?v.task_id+' · ':'')+v.summary;document.getElementById('note-dialog-body').innerHTML=markdown(v.detail||'');dialog.showModal()});
document.getElementById('note-close').onclick=function(){document.getElementById('note-dialog').close()};
function refresh(){fetch('dashboard.json?t='+Date.now(),{cache:'no-store'}).then(function(r){if(!r.ok)throw Error('fetch failed');return r.json()}).then(render).catch(function(){document.getElementById('tasks').innerHTML='<div class="empty">Dashboard data could not be loaded.</div>'})}
refresh();setInterval(refresh,30000);
})();
"""
JS = (JS_HEAD + JS_ATTENTION + JS_INBOX + JS_PROJECTS + JS_DEPS + JS_PROPOSALS
      + JS_BODY + JS_TAIL)

# The page states its own deliberately narrow write contract. Proposal review
# decisions are the sole estate mutation; every other control only changes the
# display, and the server refuses proposal writes without its configured token.
BANNER = ('<div class="readonly"><b>Narrow write surface</b>'
          '<span>Proposal decisions write through the same audited lifecycle as '
          '<code>bin/estate proposal stage</code> and require the dashboard '
          'write token. Every other control only filters or expands.</span>'
          '<span class="spacer"></span>'
          '<span class="time" id="generated-at"></span></div>')

ATTENTION_BUTTONS = ''.join(
    f'<button class="filter{" active" if state == "all" else ""}" '
    f'data-attn="{state}">{label}</button>'
    for state, label in (("all", "All"), ("overdue", "Overdue"),
                         ("due_today", "Due today"), ("due_soon", "Due soon"),
                         ("later", "Later"), ("undated", "Undated"),
                         ("unreadable", "Unreadable"), ("history", "History")))

TASK_BUTTONS = ''.join(
    f'<button class="filter{" active" if key == "active" else ""}" '
    f'data-filter="{key}">{label}</button>'
    for key, label in (("active", "Active"), ("blocked", "Blocked"),
                       ("attention", "Attention"), ("needs-owner", "Needs owner"),
                       ("terminal", "Terminal"), ("all", "All")))

DEP_BUTTONS = ''.join(
    f'<button class="filter{" active" if key == "all" else ""}" '
    f'data-dep="{key}">{label}</button>'
    for key, label in (("all", "All"), ("blocks", "blocks"),
                       ("parent", "parent"), ("related", "related"),
                       ("discovered-from", "discovered-from")))

PROPOSAL_BUTTONS = ''.join(
    f'<button class="filter{" active" if key == "pending-review" else ""}" '
    f'data-proposal-stage="{key}">{label}</button>'
    for key, label in (("pending-review", "Pending review"),
                       ("approved-backlog", "Approved backlog"),
                       ("rejected", "Rejected"), ("stopped", "Stopped"),
                       ("resolved", "Resolved"), ("all", "All")))


def render(dashboard, snapshot: dict) -> str:
    e=html.escape; name=f'{snapshot["estate"]} / {snapshot["operator"]}'
    def panel(title, ident):
        return f'<section class="panel col-12"><div class="phead"><span class="k">{title}</span></div><div class="pbody tight" id="{ident}"></div></section>'
    attention=('<section class="panel col-12"><div class="phead"><span class="k">Attention</span>'
               '<span class="grow"></span><span class="source-badge" id="attention-count"></span></div>'
               '<div class="pbody" id="attention-stats"></div>'
               f'<div class="pbody"><div class="toolbar">{ATTENTION_BUTTONS}</div></div>'
               '<div class="pbody tight" id="attention"></div></section>')
    inbox=('<section class="panel col-12"><div class="phead"><span class="k">Open inbox messages</span>'
           '<span class="grow"></span><span class="source-badge" id="inbox-count"></span>'
           '<span class="source-badge">bin/estate task list --kind inbox-message</span></div>'
           '<div class="pbody" id="inbox-stats"></div>'
           '<div class="pbody tight" id="inbox"></div></section>')
    tasks=('<section class="panel col-12"><div class="phead"><span class="k">Tasks</span>'
           '<span class="grow"></span><span class="source-badge" id="task-count"></span>'
           '<span class="source-badge">SQLite source of truth</span></div>'
           '<div class="pbody" id="task-stats"></div>'
           f'<div class="pbody"><div class="toolbar">{TASK_BUTTONS}'
           '<select class="sel" id="kind-filter"></select>'
           '<select class="sel" id="project-filter"></select>'
           '<input class="search" id="search" placeholder="Filter tasks…"></div></div>'
           '<div class="pbody tight" id="tasks"></div></section>')
    projects=('<section class="panel col-12"><div class="phead"><span class="k">Projects</span>'
              '<span class="grow"></span><span class="source-badge">rollups derived per read</span></div>'
              '<div class="pbody" id="project-stats"></div>'
              '<div class="pbody tight" id="projects"></div></section>')
    deps=('<section class="panel col-12"><div class="phead"><span class="k">Dependencies</span>'
          '<span class="grow"></span><span class="source-badge" id="dep-count"></span></div>'
          '<div class="pbody"><span class="label">unresolved blockers</span></div>'
          '<div class="pbody tight" id="blockers"></div>'
          '<div class="pbody"><span class="label">all relationships</span></div>'
          '<div class="pbody" id="dep-stats"></div>'
          f'<div class="pbody"><div class="toolbar">{DEP_BUTTONS}</div></div>'
          '<div class="pbody tight" id="deps"></div></section>')
    proposals=('<section class="panel col-12" id="proposals-panel"><div class="phead"><span class="k">Proposal review</span>'
               '<span class="grow"></span><span class="source-badge" id="proposal-count"></span>'
               '<span class="source-badge">review here · audited by estate</span></div>'
               '<div class="pbody" id="proposal-stats"></div>'
               f'<div class="pbody"><div class="toolbar">{PROPOSAL_BUTTONS}'
               '<input class="search" id="proposal-search" placeholder="Filter proposals…"></div></div>'
               '<div class="pbody tight" id="proposals"></div></section>')
    body=(f'<div class="wrap"><header class="masthead"><div class="brand">'
          f'<div class="eyebrow">Estate operations</div>'
          f'<div class="title" id="estate-title">{e(name)}</div>'
          f'<div class="subline">attention · tasks · projects · dependencies · ledgers · memory</div></div>'
          f'<span class="spacer"></span><a class="estate-nav" href="/">Daily dashboard</a>'
          f'<button class="themebtn" id="themebtn">☾ Night shift</button></header>'
          f'{BANNER}<div class="grid">{attention}{inbox}{proposals}{tasks}{projects}{deps}'
          f'{panel("Recent task events","events")}'
          f'{panel("Central ledger","ledger-summary")}'
          f'{panel("Recent central-ledger activity","ledger")}'
          f'{panel("Extraction lifecycle","extraction-stats")}'
          f'{panel("Extraction candidates","extractions")}'
          f'{panel("Shared memories","memories")}</div></div>'
          '<dialog class="note-dialog" id="note-dialog">'
          '<div class="note-dialog-head"><span class="note-dialog-title" '
          'id="note-dialog-title"></span><button type="button" class="note-close" '
          'id="note-close">Close</button></div><article class="note-dialog-body" '
          'id="note-dialog-body"></article></dialog>')
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(name)} — estate operations</title><style>{dashboard.CSS}\n{CSS}</style></head><body>{body}<script>{dashboard.JS}\n{JS}</script></body></html>\n'
