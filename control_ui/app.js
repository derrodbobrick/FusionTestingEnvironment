'use strict';
/* Helpers shared by every control-plane page. */

const $ = id => document.getElementById(id);

const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function toast(msg, kind){
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show ' + (kind || '');
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = ''; }, 5200);
}

/* Declared before api() uses it: `let` is hoisted but stays unreadable until
   this line runs, so a call made earlier would throw rather than see ''. */
let ADMIN = {ticket: '', expires_at: ''};
let ROSTER = [];

async function api(path, opts){
  opts = Object.assign({credentials:'same-origin'}, opts);
  // Carry the admin ticket on every call rather than at each call site: the
  // server decides what needs it, and a forgotten header would show up as a
  // baffling "unlock admin" on a route that was already unlocked.
  if(ADMIN.ticket){
    opts.headers = Object.assign({'X-Admin-Ticket': ADMIN.ticket}, opts.headers || {});
  }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch(e){}
  if(!r.ok && !data.error && !data.errors && !data.message){
    data.message = 'HTTP ' + r.status;
  }
  return {ok: r.ok, data};
}


/* Decision pill used by the inspection pages. */
const STATUS_LABEL = {pass:'Pass', fail:'Fail', pending:'Pending', na:'N/A'};
function chip(status){
  return '<span class="chip ' + status + '">' + (STATUS_LABEL[status] || status) + '</span>';
}


/* ---------- who is testing ----------
   Every pass/fail is attributed, so results from several people pool into one
   set and stay traceable.

   Choosing a name is a SELECTION, not a login. There is no password because
   there is nothing to protect: the roster exists so results carry a person and
   a time, not to keep anyone out. Treating it as authentication would promise
   a guarantee it cannot keep. */

function testerName(){
  return (localStorage.getItem('inspectionTester') || '').trim();
}

function testerId(){
  return (localStorage.getItem('inspectionUserId') || '').trim();
}

function setTester(user){
  // Accepts a roster entry, or a bare string for a name typed by hand when the
  // roster has not been set up yet.
  const name = (typeof user === 'string' ? user : (user && user.name) || '').trim().slice(0, 60);
  const id = (typeof user === 'string' ? '' : (user && user.id) || '');
  if(!name) return '';
  localStorage.setItem('inspectionTester', name);
  localStorage.setItem('inspectionUserId', id);
  document.dispatchEvent(new CustomEvent('testerchanged', {detail: {name: name, id: id}}));
  return name;
}

function clearTester(){
  localStorage.removeItem('inspectionTester');
  localStorage.removeItem('inspectionUserId');
}

/* Every write carries both the name and the roster id: the name is what a
   person reads months later, the id is what survives someone being renamed. */
function testerFields(){
  return {tester: testerName(), user_id: testerId()};
}

async function loadRoster(){
  const r = await api('/api/users');
  ROSTER = (r.ok && r.data && r.data.users) || [];
  return r.data || {users: [], admin_configured: false};
}

/* ---------- the picker ----------
   Shown when nobody is chosen yet, and whenever someone switches. Built here
   rather than per page so both the dashboard and the inspection page ask the
   same question the same way. */

let _pickResolve = null;

function pickerHtml(info, canCancel){
  const list = (info.users || []).map(u =>
    '<button class="userpick" data-id="' + esc(u.id) + '" data-name="' + esc(u.name) + '">' +
      '<span class="userpick-i">' + esc(initialsOf(u.name)) + '</span>' +
      '<span><b>' + esc(u.name) + '</b>' +
      (u.note ? '<span class="muted">' + esc(u.note) + '</span>' : '') + '</span>' +
    '</button>').join('');

  const empty = '<div class="note warn">The roster is empty. An admin adds ' +
    'testers from the pipeline VM &mdash; until then, type your name so your ' +
    'results are still attributed.</div>';

  return '<div class="pickcard">' +
    '<h1>Who is testing?</h1>' +
    '<p class="muted">Your name is stamped on every pass and fail you record, ' +
      'with the time you recorded it. No password &mdash; just pick yourself.</p>' +
    ((info.users || []).length ? '<div class="userpicks">' + list + '</div>' : empty) +
    '<div class="pickother">' +
      '<label><span>Not on the list?</span>' +
        '<input id="pickTyped" placeholder="Type your name" maxlength="60"></label>' +
      '<button class="small" id="pickTypedGo">Use this name</button>' +
    '</div>' +
    (canCancel ? '<div style="margin-top:10px"><button class="small" id="pickCancel">Cancel</button></div>' : '') +
  '</div>';
}

function initialsOf(name){
  return String(name || '?').trim().split(/\s+/).slice(0, 2)
    .map(w => w[0] || '').join('').toUpperCase() || '?';
}

async function choosePerson(canCancel){
  const info = await loadRoster();
  return new Promise(resolve => {
    _pickResolve = resolve;
    let host = $('userPicker');
    if(!host){
      host = document.createElement('div');
      host.id = 'userPicker';
      host.className = 'overlay';
      document.body.appendChild(host);
    }
    host.innerHTML = pickerHtml(info, canCancel);
    host.style.display = 'flex';

    host.querySelectorAll('.userpick').forEach(b => {
      b.onclick = () => finishPick({id: b.dataset.id, name: b.dataset.name});
    });
    const typed = $('pickTyped'), go = $('pickTypedGo');
    if(go) go.onclick = () => {
      const v = (typed.value || '').trim();
      if(!v){ typed.focus(); return; }
      finishPick(v);
    };
    if(typed) typed.onkeydown = e => { if(e.key === 'Enter') go.onclick(); };
    const cancel = $('pickCancel');
    if(cancel) cancel.onclick = () => { host.style.display = 'none'; resolve(testerName()); };
  });
}

function finishPick(user){
  const name = setTester(user);
  const host = $('userPicker');
  if(host) host.style.display = 'none';
  if(_pickResolve){ _pickResolve(name); _pickResolve = null; }
}

async function requireTester(){
  return testerName() || await choosePerson(false);
}

/* ---------- admin ----------
   A shared PIN that unlocks scenario setup and roster editing. It stops a
   tester wandering into the configuration, which is all it is meant to do:
   anyone who can reach the share can already edit the files by hand. */

function loadAdminSession(){
  try {
    const raw = sessionStorage.getItem('adminTicket');
    if(raw) ADMIN = JSON.parse(raw);
  } catch(e){ ADMIN = {ticket: '', expires_at: ''}; }
  return ADMIN;
}

function setAdminSession(session){
  ADMIN = session || {ticket: '', expires_at: ''};
  // sessionStorage, not localStorage: closing the browser should end admin,
  // and a shared PC must not leave the next person holding it.
  if(ADMIN.ticket) sessionStorage.setItem('adminTicket', JSON.stringify(ADMIN));
  else sessionStorage.removeItem('adminTicket');
  document.body.classList.toggle('admin-mode', !!ADMIN.ticket);
  document.dispatchEvent(new CustomEvent('adminchanged'));
}

function isAdmin(){ return !!ADMIN.ticket; }

async function adminUnlock(pin){
  const r = await api('/api/admin/unlock', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pin: pin, who: testerName()})
  });
  if(r.ok && r.data.ok){ setAdminSession(r.data); return {ok: true}; }
  return {ok: false, message: r.data.message || 'Unlock failed',
          unconfigured: !!r.data.unconfigured};
}

async function adminLock(){
  const t = ADMIN.ticket;
  setAdminSession(null);
  if(t) await api('/api/admin/lock', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ticket: t})
  });
}

/* Verify the ticket is still good; the service restarting invalidates it, and
   showing admin controls that then fail would be worse than showing none. */
async function refreshAdmin(){
  loadAdminSession();
  if(!ADMIN.ticket){ document.body.classList.remove('admin-mode'); return false; }
  const r = await api('/api/users');
  const ok = !!(r.ok && r.data && r.data.is_admin);
  if(!ok) setAdminSession(null);
  else document.body.classList.add('admin-mode');
  return ok;
}

/* Deployment shape: tester builds hide everything that acts on Fusion. */
let DEPLOY = {role: 'operator', is_tester: false};
async function loadDeployment(){
  const r = await api('/api/deployment');
  if(r.ok) DEPLOY = r.data || DEPLOY;
  document.body.classList.toggle('tester-mode', !!DEPLOY.is_tester);
  return DEPLOY;
}
