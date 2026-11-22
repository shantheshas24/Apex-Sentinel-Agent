'use strict';
// ─────────────────────── State ────────────────────────────────────────────────
let allEmails = [];
let activeFilter = 'all';
let focusedIdx = -1;
let wsConnected = false;
let gmailStatus = 'loading';
let canPoll = false;
let pollInProgress = false;
let connectingGoogle = false;
let renderedEmailMarkup = '';

// ─────────────────────── Utilities ───────────────────────────────────────────
function esc(s){ return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function timeAgo(ts){
  if(!ts) return '';
  const d=Math.floor((Date.now()-new Date(ts))/1000);
  if(d<60) return d+'s ago';
  if(d<3600) return Math.floor(d/60)+'m ago';
  if(d<86400) return Math.floor(d/3600)+'h ago';
  return Math.floor(d/86400)+'d ago';
}
function showToast(msg,type=''){
  const c=document.getElementById('toast-container');
  const t=document.createElement('div');
  t.className='toast'+(type?' toast-'+type:'');
  t.textContent=msg;
  c.appendChild(t);
  requestAnimationFrame(()=>requestAnimationFrame(()=>t.classList.add('show')));
  setTimeout(()=>{t.classList.remove('show');setTimeout(()=>t.remove(),260);},3500);
}

// ─────────────────────── Tab navigation ──────────────────────────────────────
const tabs=[...document.querySelectorAll('.nav-item')];
const tabHash={deadlines:'deadlines',analytics:'analytics',inject:'test-deadline',prefs:'preferences',connections:'connections'};

function updateNavigation(){
  const current=tabs.find(button=>location.hash==='#'+tabHash[button.dataset.tab])||tabs[0];
  for(const button of tabs){
    const active=button===current;
    button.classList.toggle('active',active);
    if(active) button.setAttribute('aria-current','page');
    else button.removeAttribute('aria-current');
    document.getElementById('tab-'+button.dataset.tab).classList.toggle('active',active);
  }
  const {tab,title}=current.dataset;
  document.getElementById('page-location').textContent=title;
  document.title=title+' · PersonalOS';
  if(tab==='analytics') loadAnalytics();
  if(tab==='prefs') loadPreferences();
  if(tab==='connections') loadHealth();
}

for(const button of tabs) button.addEventListener('click',()=>{location.hash=tabHash[button.dataset.tab];});
document.getElementById('connection-shortcut').addEventListener('click',()=>{location.hash=tabHash.connections;});
window.addEventListener('hashchange',updateNavigation);

const themeButton=document.getElementById('theme-toggle');
function setTheme(dark){
  document.body.classList.toggle('dark',dark);
  themeButton.setAttribute('aria-pressed',String(dark));
  themeButton.firstElementChild.textContent=dark?'☀':'☾';
}
setTheme(localStorage.getItem('theme')==='dark');
themeButton.addEventListener('click',()=>{
  const dark=!document.body.classList.contains('dark');
  setTheme(dark);
  localStorage.setItem('theme',dark?'dark':'light');
});

// ─────────────────────── WebSocket ───────────────────────────────────────────
function connectWS(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(proto+'//'+location.host+'/ws');
  ws.onopen=()=>{
    wsConnected=true;
    document.getElementById('ws-label').textContent='Live updates';
    document.querySelector('.live-dot').classList.add('live');
  };
  ws.onmessage=(e)=>{
    const msg=JSON.parse(e.data||'{}');
    if(msg.type==='refresh') refresh();
    if(msg.type==='reminder'){
      loadReminders();
      announceReminders(msg.items||[]);
    }
    if(msg.type==='new_plan') showToast(msg.message||'New plan arrived','');
  };
  ws.onclose=()=>{
    wsConnected=false;
    document.getElementById('ws-label').textContent='Reconnecting…';
    document.querySelector('.live-dot').classList.remove('live');
    setTimeout(connectWS,3000);
  };
  ws.onerror=()=>ws.close();
}
connectWS();
setInterval(()=>{if(!wsConnected) refresh();}, 8000);
setInterval(loadEmails, 30000);
setInterval(loadGoogleConnection, 30000);

// ─────────────────────── Email rendering ─────────────────────────────────────
const PRIORITY_BADGE={critical:'badge-critical',high:'badge-high',medium:'badge-medium',low:'badge-low'};
const PRIORITY_LABEL={critical:'Critical',high:'High',medium:'Medium',low:'Low'};
const TASK_STATUS={
  needs_review:['Needs review','badge-response-pending'],
  pending:['Pending','badge-low'],
  upcoming:['Upcoming','badge-conf'],
  overdue:['Overdue','badge-response-rejected'],
  completed:['Completed','badge-response-approved'],
};
const PHONE_STATUS={
  submitting:'Submitting call',
  queued:'Call queued',
  ringing:'Call ringing',
  'in-progress':'Call in progress',
  completed:'Call completed',
  busy:'Line busy',
  'no-answer':'Call unanswered',
  failed:'Call failed',
  canceled:'Call canceled',
  skipped:'Call skipped',
  unconfirmed:'Call unconfirmed',
};
function formatDate(value){
  return value?new Date(value).toLocaleString([], {dateStyle:'medium', timeStyle:'short'}):'';
}

function renderEmails(emails){
  const el=document.getElementById('email-list');
  if(!emails.length){
    const filtered=activeFilter!=='all'||document.getElementById('search-box').value.trim();
    const title=filtered?'No matching deadlines':'No deadlines yet';
    const hint=filtered?'Try another filter or search term.':
      gmailStatus==='ok'?'Check Gmail to look for new deadlines.':'Connect Gmail or try a sample email in Test Deadline.';
    renderedEmailMarkup='';
    el.innerHTML=`<div class="queue-empty"><span class="empty-icon"><span class="icon i-calendar" aria-hidden="true"></span></span><strong>${title}</strong><p>${hint}</p></div>`;
    return;
  }
  const markup=emails.map(item=>{
    const id=encodeURIComponent(item.id);
    const priority=PRIORITY_LABEL[item.priority] ? item.priority : 'medium';
    const status=TASK_STATUS[item.task_status]||TASK_STATUS.needs_review;
    const complete=item.task_status==='completed';
    const snoozed=item.snoozed_until&&new Date(item.snoozed_until)>new Date();
    const remaining = Date.parse(item.due_at) - Date.now();
    const snoozeMinutes = remaining > 61*60000 ? 60 : remaining > 11*60000 ? 10 : 0;
    const due=item.due_at?formatDate(item.due_at):'Confirm the date and time';
    return `<article class="email-card priority-${priority}" id="card-${id}" tabindex="0">
      <div class="email-header">
        <span class="email-subject">${esc(item.deadline)}</span>
        <span class="badge ${PRIORITY_BADGE[priority]}">${PRIORITY_LABEL[priority]}</span>
        <span class="badge ${status[1]}">${status[0]}</span>
        ${item.synthetic?'<span class="badge badge-low">Sample</span>':''}
      </div>
      <div class="task-due">${esc(due)}</div>
      ${Object.hasOwn(PHONE_STATUS,item.phone_status)?`<div class="email-from">Twilio · ${PHONE_STATUS[item.phone_status]}</div>`:''}
      ${snoozed?`<div class="email-from">Snoozed until ${esc(formatDate(item.snoozed_until))}</div>`:''}
      <blockquote class="task-evidence">${esc(item.evidence)}</blockquote>
      <div class="email-from">${esc(item.subject||'(no subject)')} &middot; ${esc(item.from_addr||'—')}${item.email_date?' &middot; '+esc(item.email_date):''}</div>
      <div class="actions-row">
        ${complete?`<button class="btn" data-task-action="reopen" data-task-id="${id}">Reopen</button>`:
          `<button class="btn btn-purple" data-task-action="edit" data-task-id="${id}">${item.due_at?'Edit deadline':'Confirm deadline'}</button>
          ${snoozed||snoozeMinutes?`<button class="btn" data-task-action="${snoozed?'unsnooze':'snooze'}" data-snooze="${snoozeMinutes}" data-task-id="${id}">${snoozed?'End snooze':snoozeMinutes===60?'Snooze 1 hour':'Snooze 10 min'}</button>`:''}
          <button class="btn btn-green" data-task-action="complete" data-task-id="${id}">Mark complete</button>`}
      </div>
    </article>`;
  }).join('');
  if(markup===renderedEmailMarkup) return;
  const focused=document.activeElement?.closest('.email-card');
  const focusedAction=document.activeElement?.dataset.taskAction;
  renderedEmailMarkup=markup;
  el.innerHTML=markup;
  if(focused){
    const restored=document.getElementById(focused.id);
    const action=restored?.querySelector(`[data-task-action="${focusedAction}"]`);
    (action||restored)?.focus({preventScroll:true});
  }
}

function updateStats(){
  document.getElementById('stat-total').textContent=allEmails.length;
  document.getElementById('export-button').disabled=!allEmails.length;
  const stats={review:'needs_review',pending:'pending',upcoming:'upcoming',overdue:'overdue',completed:'completed'};
  for(const [id,status] of Object.entries(stats)){
    document.getElementById('stat-'+id).textContent=allEmails.filter(task=>task.task_status===status).length;
  }
}

async function loadEmails(){
  try{
    const response=await fetch('/api/deadlines');
    if(!response.ok) throw new Error();
    allEmails=await response.json();
    updateStats();
    applyFilter();
  }catch(error){
    if(document.getElementById('stat-total').textContent==='–'){
      document.getElementById('email-list').innerHTML='<p class="empty">Deadlines are temporarily unavailable.</p>';
    }
  }
}

function applyFilter(){
  const query=(document.getElementById('search-box').value||'').toLowerCase().trim();
  const tasks=allEmails.filter(task=>{
    const matchesFilter=activeFilter==='all'||task.task_status===activeFilter||
      (activeFilter==='critical'&&task.priority==='critical'&&task.task_status!=='completed');
    const matchesSearch=!query||[task.deadline,task.due,formatDate(task.due_at),task.subject,task.from_addr]
      .some(value=>String(value||'').toLowerCase().includes(query));
    return matchesFilter&&matchesSearch;
  });
  tasks.sort((a,b)=>{
    const order={overdue:0,needs_review:1,upcoming:2,pending:3,completed:4};
    return order[a.task_status]-order[b.task_status]||
      Date.parse(a.due_at)-Date.parse(b.due_at)||
      Date.parse(b.created_at)-Date.parse(a.created_at);
  });
  document.getElementById('email-count').textContent=tasks.length;
  renderEmails(tasks);
}

function filterEmails(filter){
  activeFilter=filter;
  document.querySelectorAll('.filter-chip').forEach(chip=>{
    const selected=chip.dataset.filter===filter;
    chip.classList.toggle('active',selected);
    chip.setAttribute('aria-pressed',String(selected));
  });
  document.querySelectorAll('[data-stat-filter]').forEach(card=>{
    card.setAttribute('aria-pressed',String(card.dataset.statFilter===filter));
  });
  applyFilter();
}

for(const button of document.querySelectorAll('[data-filter], [data-stat-filter]')){
  button.addEventListener('click',()=>filterEmails(button.dataset.filter||button.dataset.statFilter));
}

function dateInputValue(date){
  const local=new Date(date.getTime()-date.getTimezoneOffset()*60000);
  return local.toISOString().slice(0,19);
}

const taskDialog=document.getElementById('task-dialog');
const taskForm=document.getElementById('task-form');
const editError=document.getElementById('edit-error');

function editTask(task){
  taskForm.dataset.taskId=task.id;
  document.getElementById('edit-evidence').textContent=task.evidence;
  document.getElementById('edit-title').value=task.deadline;
  document.getElementById('edit-due').value=task.due_at?dateInputValue(new Date(task.due_at)):'';
  document.getElementById('edit-zone').textContent='Local time in '+Intl.DateTimeFormat().resolvedOptions().timeZone;
  document.getElementById('edit-importance').value=task.importance;
  editError.hidden=true;
  taskDialog.showModal();
}

async function saveTask(id,changes){
  const response=await fetch('/api/deadlines/'+encodeURIComponent(id),{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(changes),
  });
  if(!response.ok){
    const {detail}=await response.json();
    throw new Error(typeof detail==='string'?detail:'Check the date and time and try again.');
  }
  await loadEmails();
  return response.json();
}

taskForm.addEventListener('submit',async event=>{
  event.preventDefault();
  const due=document.getElementById('edit-due').value;
  const submit=document.getElementById('edit-save');
  try{
    const timestamp=due?new Date(due):null;
    if(timestamp&&(!Number.isFinite(timestamp.getTime())||!dateInputValue(timestamp).startsWith(due))){
      throw new Error('Choose a valid date and time in your time zone.');
    }
    submit.disabled=true;
    const changes={
      title:document.getElementById('edit-title').value,
      importance:document.getElementById('edit-importance').value,
      due_at:timestamp?timestamp.toISOString():null,
    };
    await saveTask(taskForm.dataset.taskId,changes);
    taskDialog.close();
    showToast('Deadline saved','ok');
  }catch(error){
    editError.textContent=error.message;
    editError.hidden=false;
  }finally{
    submit.disabled=false;
  }
});

document.getElementById('edit-cancel').addEventListener('click',()=>taskDialog.close());

document.getElementById('email-list').addEventListener('click',async event=>{
  const button=event.target.closest('[data-task-action]');
  if(!button) return;
  const task=allEmails.find(item=>item.id===decodeURIComponent(button.dataset.taskId));
  if(!task) return;
  const action=button.dataset.taskAction;
  if(action==='edit'){editTask(task);return;}
  const changes={
    complete:{completed:true},
    reopen:{completed:false},
    unsnooze:{snoozed_until:null},
  };
  if(action==='snooze') changes.snooze={snoozed_until:new Date(Date.now()+Number(button.dataset.snooze)*60000).toISOString()};
  button.disabled=true;
  try{
    await saveTask(task.id,changes[action]);
    const messages={complete:'Task completed',reopen:'Task reopened',snooze:'Reminder snoozed',unsnooze:'Snooze ended'};
    showToast(messages[action],'ok');
  }catch(error){
    showToast(error.message,'err');
  }finally{
    button.disabled=false;
  }
});

async function loadReminders(){
  try{
    const response=await fetch('/api/reminders');
    if(!response.ok) return;
    const data=await response.json();
    document.getElementById('phone-reminder-status').textContent=data.phone_available
      ? 'Twilio calls are enabled for critical Gmail deadlines during active reminder hours.'
      : 'Twilio calls are disabled. Dashboard reminders are still active.';
    document.getElementById('reminder-count').textContent=data.items.length;
    document.getElementById('reminder-policy').textContent=data.quiet_start===data.quiet_end?'':`Quiet hours ${data.quiet_start}–${data.quiet_end} (${data.timezone})`;
    document.getElementById('reminder-list').innerHTML=data.items.length?data.items.map(item=>`
      <div class="feed-item"><span class="feed-time">${esc(formatDate(item.sent_at))}</span>
        <span class="feed-action">${esc(item.title)} · due ${esc(formatDate(item.due_at))}</span>
      </div>`).join(''):'<p class="empty">No reminders yet</p>';
  }catch(error){}
}

const notificationButton=document.getElementById('enable-notifications');
function updateBrowserNotifications(){
  const permission=window.Notification?.permission;
  const messages={
    granted:'Browser notifications are on while this dashboard is open.',
    denied:'Browser notifications are blocked in your browser settings.',
    default:'Get a browser notification for each reminder while this dashboard is open.',
  };
  document.getElementById('browser-notification-status').textContent=messages[permission]
    ||'This browser cannot show notifications. Reminders are available in the dashboard.';
  notificationButton.hidden=permission!=='default';
}

notificationButton.addEventListener('click',async()=>{
  try{await Notification.requestPermission();}
  catch(error){showToast('This browser could not enable notifications.','err');}
  updateBrowserNotifications();
});
window.addEventListener('focus',updateBrowserNotifications);

function announceReminders(items){
  if(!items.length) return;
  const single=items.length===1;
  const title=single?'Deadline reminder':`${items.length} deadline reminders`;
  const body=single?`${items[0].title} · Due ${formatDate(items[0].due_at)}`:
    'Open the dashboard to see the latest reminders.';
  showToast(title+' · '+body);
  if(window.Notification?.permission==='granted'){
    const tag='personalos-'+items[0].task_id+'-'+items[0].sent_at;
    try{new Notification(title,{body,tag});}
    catch(error){}
  }
}

function exportCSV(){
  const cols=['deadline','due_at','due','task_status','importance','subject','from_addr','email_date','priority','confidence','reason','created_at'];
  const rows=[cols.join(',')];
  const csvCell=value=>{
    let text=String(value??'');
    if(/^[\s]*[=+@-]|^[\t\r\n]/.test(text)) text="'"+text;
    return '"'+text.replace(/"/g,'""')+'"';
  };
  allEmails.forEach(e=>rows.push(cols.map(c=>csvCell(e[c])).join(',')));
  const blob=new Blob([rows.join('\n')],{type:'text/csv'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='personalos_deadlines.csv'; a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}

// ─────────────────────── Feed ─────────────────────────────────────────────────
async function loadFeed(){
  try{
    const res=await fetch('/api/feed');
    const entries=await res.json();
    const el=document.getElementById('activity-feed');
    if(!entries.length){el.innerHTML='<p class="empty">No activity yet</p>';return;}
    el.innerHTML=entries.map(e=>`
      <div class="feed-item">
        <span class="feed-dot"></span>
        <span class="feed-time">${esc(formatDate(e.timestamp))}</span>
        <span class="feed-agent">${esc(e.agent)}</span>
        <span class="feed-arrow">→</span>
        <span class="feed-action">${esc(e.action)}</span>
      </div>`).join('');
  }catch(e){}
}

// ─────────────────────── Health ───────────────────────────────────────────────
const GMAIL_STATES={
  ok:['Connected','Gmail is connected',''],
  disconnected:['Connection required','Connect your Gmail','Grant read-only access to find deadlines in your inbox.'],
  reconnect:['Reconnect required','Reconnect your Gmail','Grant read-only access so PersonalOS can check your inbox for deadlines.'],
  error:['Connection unavailable','Gmail connection unavailable','Gmail could not be reached. Try again or reconnect your account.'],
  disabled:['Unavailable','Gmail is disabled','The dashboard is running in dashboard-only mode.'],
};

function updatePollButton(){
  const button=document.getElementById('poll-button');
  button.disabled=!canPoll||pollInProgress;
  button.title=canPoll?'Check Gmail for new inbox messages':gmailStatus==='ok'?'Automatic Gmail polling is disabled':'Connect Gmail to check for new email';
}

function updateConnection(status){
  const changed=gmailStatus!==status.status;
  gmailStatus=status.status;
  canPoll=gmailStatus==='ok'&&status.polling;
  const [label,title,message]=GMAIL_STATES[gmailStatus]||GMAIL_STATES.error;
  let description=message;
  if(gmailStatus==='ok'){
    const account=status.account||'Your Google account';
    description=canPoll?`${account} · Read-only access. Inbox changes are checked automatically.`:`${account} · Inbox polling is paused.`;
  }else if(status.can_connect===false&&gmailStatus!=='disabled'){
    description='Open this dashboard on localhost and configure a Google Desktop OAuth client to connect Gmail.';
  }
  document.querySelectorAll('[data-gmail-dot]').forEach(dot=>{dot.dataset.status=gmailStatus;});
  document.querySelectorAll('[data-gmail-status]').forEach(element=>{element.textContent=label;});
  document.querySelectorAll('[data-gmail-title]').forEach(element=>{element.textContent=title;});
  document.querySelectorAll('[data-gmail-description]').forEach(element=>{element.textContent=description;});
  document.querySelectorAll('[data-connect-google]').forEach(button=>{
    button.hidden=gmailStatus==='ok'||gmailStatus==='disabled';
    button.disabled=connectingGoogle||status.can_connect===false;
    button.querySelector('[data-connect-label]').textContent=connectingGoogle?'Opening Google…':gmailStatus==='disconnected'?'Connect Gmail':'Reconnect Gmail';
  });
  document.getElementById('connection-shortcut').setAttribute('aria-label','Gmail settings. '+label);
  updatePollButton();
  if(changed&&document.getElementById('stat-total').textContent!=='–') applyFilter();
}

async function loadGoogleConnection(){
  try{
    const response=await fetch('/api/google/status');
    if(!response.ok) throw new Error();
    updateConnection(await response.json());
  }catch(error){
    updateConnection({status:'error'});
  }
}

async function connectGoogle(){
  if(connectingGoogle) return;
  connectingGoogle=true;
  const buttons=document.querySelectorAll('[data-connect-google]');
  buttons.forEach(button=>{
    button.disabled=true;
    button.querySelector('[data-connect-label]').textContent='Opening Google…';
  });
  try{
    const response=await fetch('/api/google/connect',{method:'POST'});
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail||'Google sign-in is unavailable.');
    location.assign(data.authorization_url);
  }catch(error){
    connectingGoogle=false;
    showToast(error.message||'Could not open Google sign-in.','err');
    loadGoogleConnection();
  }
}

document.querySelectorAll('[data-connect-google]').forEach(button=>button.addEventListener('click',connectGoogle));

function showConnectionResult(){
  const url=new URL(location.href);
  const result=url.searchParams.get('gmail');
  if(!result) return;
  const messages={
    connected:['Gmail connected.','ok'],
    cancelled:['Google sign-in was cancelled.',''],
    expired:['The sign-in session expired. Start again from Connect Gmail.','err'],
    failed:['Gmail could not connect. Try again and grant read-only Gmail access.','err'],
  };
  if(Object.hasOwn(messages,result)) showToast(...messages[result]);
  url.searchParams.delete('gmail');
  history.replaceState(null,'',url);
}

async function loadHealth(){
  const el=document.getElementById('health-grid');
  try{
    const res=await fetch('/api/health');
    if(!res.ok) throw new Error();
    const data=await res.json();
    const svcMap={redis:'Redis',openrouter:'Language model',google:'Gmail',twilio:'Twilio',chromadb:'ChromaDB',mcp_server:'MCP server'};
    const q=data.services?.redis?.queues||{};
    const chips=Object.entries(data.services).map(([key,svc])=>{
      const color=svc.status==='ok'?'dot-ok':svc.status==='error'?'dot-error':'dot-warn';
      const detail=key==='google'?(GMAIL_STATES[svc.status]||GMAIL_STATES.error)[0]:
        svc.status==='ok'?(svc.latency_ms!=null?`Operational · ${svc.latency_ms} ms`:'Operational'):
        svc.status==='disabled'?'Disabled':'Unavailable';
      return `<div class="health-chip"><span class="health-dot ${color}"></span><span class="health-name">${esc(svcMap[key]||key)}</span><span class="health-detail">${esc(detail)}</span></div>`;
    });
    if(q.events!=null) chips.push(`<div class="health-chip"><span class="health-name">Events queued</span><span class="health-detail">${esc(q.events)}</span></div>`);
    el.innerHTML=chips.join('');
  }catch(e){el.innerHTML='<p class="empty">Service status is temporarily unavailable.</p>';}
}

// ─────────────────────── Analytics ───────────────────────────────────────────
async function loadAnalytics(){
  try{
    const [mRes,fRes]=await Promise.all([fetch('/api/metrics'),fetch('/api/feed?limit=10')]);
    const m=await mRes.json();
    const feed=await fRes.json();
    renderAnalytics(m,feed);
  }catch(e){document.getElementById('analytics-grid').innerHTML='<p class="empty">Error loading metrics</p>';}
}
function barRow(label,val,max,fillClass){
  const pct=max>0?Math.round(val/max*100):0;
  return `<div class="bar-row"><span class="bar-label">${esc(label)}</span><div class="bar-track"><div class="bar-fill ${fillClass}" style="width:${pct}%"></div></div><span class="bar-val">${val}</span></div>`;
}
function renderAnalytics(m,feed){
  const completionRate=m.total>0?Math.round((m.by_status?.completed||0)/m.total*100):0;
  const avgConf=m.avg_confidence?m.avg_confidence.toFixed(1):'—';
  const maxPri=Math.max(...Object.values(m.by_priority||{}),1);
  const maxStatus=Math.max(1,...Object.values(m.by_status||{}));
  const maxConf=Math.max(...Object.values(m.confidence_distribution||{}),1);
  document.getElementById('analytics-grid').innerHTML=`
    <div class="analytics-card">
      <h3>Task completion</h3>
      <div class="big-num">${completionRate}%</div>
      <div class="big-sub">${m.by_status?.completed||0} of ${m.total} deadlines completed</div>
      <div style="margin-top:14px;font-size:.74rem;color:var(--text-3)">Avg Confidence: <span style="color:var(--purple-hi);font-weight:700">${avgConf}%</span></div>
    </div>
    <div class="analytics-card">
      <h3>Priority Breakdown</h3>
      <div class="bar-chart">
        ${barRow('Critical',m.by_priority.critical||0,maxPri,'bar-fill-high')}
        ${barRow('High',m.by_priority.high||0,maxPri,'bar-fill-high')}
        ${barRow('Medium',m.by_priority.medium||0,maxPri,'bar-fill-med')}
        ${barRow('Low',m.by_priority.low||0,maxPri,'bar-fill-low')}
      </div>
    </div>
    <div class="analytics-card">
      <h3>Deadline status</h3>
      <div class="bar-chart">
        ${barRow('Review',m.by_status.needs_review||0,maxStatus,'bar-fill-med')}
        ${barRow('Pending',m.by_status.pending||0,maxStatus,'bar-fill-low')}
        ${barRow('Upcoming',m.by_status.upcoming||0,maxStatus,'bar-fill-pend')}
        ${barRow('Overdue',m.by_status.overdue||0,maxStatus,'bar-fill-high')}
        ${barRow('Completed',m.by_status.completed||0,maxStatus,'bar-fill-auto')}
      </div>
    </div>
    <div class="analytics-card">
      <h3>Confidence Distribution</h3>
      <div class="bar-chart">
        ${barRow('0–30%',(m.confidence_distribution||{})['0-30']||0,maxConf,'bar-fill-conf0')}
        ${barRow('31–60%',(m.confidence_distribution||{})['31-60']||0,maxConf,'bar-fill-conf1')}
        ${barRow('61–80%',(m.confidence_distribution||{})['61-80']||0,maxConf,'bar-fill-conf2')}
        ${barRow('81–100%',(m.confidence_distribution||{})['81-100']||0,maxConf,'bar-fill-conf3')}
      </div>
    </div>
    <div class="analytics-card">
      <h3>Scan queue</h3>
      <div class="big-num">${m.queue_depths.events}</div>
      <div class="big-sub">${m.queue_depths.processing||0} processing · ${m.queue_depths.retrying||0} scheduled to retry</div>
    </div>
  `;
  const fEl=document.getElementById('analytics-feed');
  if(!feed.length){fEl.innerHTML='<p class="empty">No activity yet</p>';return;}
  fEl.innerHTML=feed.map(e=>`
    <div class="feed-item">
      <span class="feed-dot"></span>
      <span class="feed-time">${esc(formatDate(e.timestamp))}</span>
      <span class="feed-agent">${esc(e.agent)}</span>
      <span class="feed-arrow">→</span>
      <span class="feed-action">${esc(e.action)}</span>
    </div>`).join('');
}

// ─────────────────────── Preferences ─────────────────────────────────────────
async function loadPreferences(){
  try{
    const res=await fetch('/api/preferences');
    const prefs=await res.json();
    document.getElementById('pref-count').textContent=prefs.length;
    const el=document.getElementById('pref-list');
    if(!prefs.length){el.innerHTML='<p class="empty">No preferences stored yet</p>';return;}
    el.innerHTML=prefs.map(p=>`
      <div class="pref-item">
        <div>
          <div class="pref-text">${esc(p.document)}</div>
          <div class="pref-meta"><span class="cat-badge">${esc(p.metadata?.category||'general')}</span>source: ${esc(p.metadata?.source||'—')} &bull; added ${timeAgo(p.metadata?.created_at)}</div>
        </div>
      </div>`).join('');
  }catch(e){document.getElementById('pref-list').innerHTML='<p class="empty">Error loading preferences</p>';}
}
async function addPreference(){
  const text=document.getElementById('pref-text').value.trim();
  const res=document.getElementById('pref-result');
  if(!text){showToast('Enter a preference statement','err');return;}
  try{
    const r=await fetch('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    const d=await r.json();
    if(r.ok){
      res.className='inject-result inject-ok'; res.textContent='Preference saved!'; res.style.display='block';
      document.getElementById('pref-text').value='';
      loadPreferences();
    } else {
      res.className='inject-result inject-err'; res.textContent=d.error||'Error'; res.style.display='block';
    }
    setTimeout(()=>res.style.display='none',3000);
  }catch(e){showToast('Error saving preference','err');}
}

// ─────────────────────── Event injection ─────────────────────────────────────
async function injectEvent(){
  const btn=document.getElementById('inj-btn');
  const res=document.getElementById('inj-result');
  btn.disabled=true; btn.textContent='Queuing…';
  const body={
    from:document.getElementById('inj-from').value||'demo@example.com',
    subject:document.getElementById('inj-subject').value||'Assignment deadline',
    snippet:document.getElementById('inj-snippet').value,
    urgent:document.getElementById('inj-urgent').checked,
  };
  try{
    const r=await fetch('/api/events/inject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(r.ok){
      res.className='inject-result inject-ok';
      res.textContent='Test email queued. Check the Deadlines tab for the result.';
      res.style.display='block';
      showToast('Test email queued','ok');
    } else {
      res.className='inject-result inject-err';
      res.textContent=(typeof d.detail==='string'&&d.detail)||d.error||'Error';
      res.style.display='block';
    }
  }catch(e){
    res.className='inject-result inject-err'; res.textContent=String(e); res.style.display='block';
  }
  btn.disabled=false; btn.textContent='Check Email';
  setTimeout(()=>res.style.display='none',6000);
}

// ─────────────────────── Keyboard shortcuts ──────────────────────────────────
document.addEventListener('keydown',e=>{
  if(taskDialog.open||e.altKey||e.ctrlKey||e.metaKey||e.target.closest('input, textarea, select, [contenteditable]')||
    !document.getElementById('tab-deadlines').classList.contains('active')) return;
  const cards=[...document.querySelectorAll('.email-card')];
  if(!cards.length) return;
  if(e.key==='ArrowDown'){focusedIdx=Math.min(focusedIdx+1,cards.length-1);cards[focusedIdx].focus();e.preventDefault();}
  else if(e.key==='ArrowUp'){focusedIdx=Math.max(focusedIdx-1,0);cards[focusedIdx].focus();e.preventDefault();}
});

// ─────────────────────── Poll Now ────────────────────────────────────────────
async function pollNow(){
  if(!canPoll||pollInProgress) return;
  pollInProgress=true;
  updatePollButton();
  try{
    const r=await fetch('/api/poll/now',{method:'POST'});
    const d=await r.json();
    if(!r.ok||d.status!=='triggered') throw new Error(d.message||'Gmail check could not start.');
    showToast('Checking for new emails…');
    setTimeout(refresh,4000);
    setTimeout(refresh,10000);
  }catch(error){
    showToast(error.message,'err');
  }finally{
    pollInProgress=false;
    updatePollButton();
  }
}

// ─────────────────────── Init ─────────────────────────────────────────────────
function refresh(){loadEmails();loadFeed();loadReminders();}
updateNavigation();
updateBrowserNotifications();
refresh();
loadGoogleConnection();
showConnectionResult();
