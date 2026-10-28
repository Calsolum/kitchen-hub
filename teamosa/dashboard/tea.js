
// ---- Tea: the Teamosa brewer (~/teamosa, kitchen-teamosa.service behind /teamosa/) ----
// /teamosa/status says whether THIS device may use it: the kiosk and Tariq's devices on
// Tailscale. Guests get {allowed:false}, never see the tile, and the service refuses them
// anyway. Edits save themselves after a short pause; Brew now asks first in its own panel.
// The service refuses a plain brew within 30 minutes of the last one (the brewer can't be
// stopped or asked whether it's busy), so meanwhile the button reads Brew anyway: its confirm
// says when the last brew started, and only it sends force. The schedule never forces.
//
// The panel is built once (teaBuild) and afterwards only updated in place (teaUpdate): text,
// classes and disabled flags, never replaced nodes. Replacing the element under a finger
// mid-tap means its touchend never reaches document, which leaves the drag guard above
// holding a stale start point and swallowing the next tap (traced on the kiosk 2026-10-27).
const TEA_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const TEA_PROBLEMS = { CUP: 'no cup on the sensor', EAU: 'the water tank is empty or low', COV: 'a lid problem' };
let teaState = null;        // last /teamosa/status reply
let teaDraft = null;        // settings as shown, saved after a pause
let teaSaveTimer = null;
let teaPollTimer = null;
let teaMsg = '';            // feedback from the last save / button press
let teaMsgBad = false;
let teaStatusTries = 0;
let teaForce = false;       // the open confirm is Brew anyway

async function checkTeaAllowed() {
  let allowed = false;
  try {
    const res = await fetch('/teamosa/status', { cache: 'no-store' });
    allowed = !!(await res.json()).allowed;
  } catch (e) {
    // Service not up yet (e.g. the kiosk browser starts before it does) -- try again shortly.
    if (++teaStatusTries < 10) setTimeout(checkTeaAllowed, 30000);
    return;
  }
  const tile = document.getElementById('tea-action-btn');
  if (tile) tile.style.display = allowed ? '' : 'none';
}
checkTeaAllowed();

function teaPanelOpen() {
  return document.getElementById('focus-overlay').classList.contains('active') && !!document.getElementById('tea-root');
}
function teaClock(h, m) { return new Date(2000, 0, 1, h, m).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
function teaWhen(iso) { return new Date(iso).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }); }
function teaMinutes(m) { return (m % 1 ? m.toFixed(1) : m.toFixed(0)) + ' min'; }
function teaTooSoon(st) { return !!(st && st.last_brew && st.last_brew.too_soon); }
function teaSet(id, text) {
  const el = document.getElementById(id);
  if (el && el.textContent !== text) el.textContent = text;
}

// The result line: what this service just did, else the last run the brewer tool recorded.
// Exits 0/3/4/5 write that record; 6 (too soon), 7 (locked) and -1 don't, so they show their own message.
function teaResult(st) {
  const a = st.action, r = st.last_run;
  if (a && a.running) return [a.kind === 'check' ? 'Checking the brewer…' : 'Starting the brew…', false];
  if (a && ![0, 3, 4, 5].includes(a.exit)) return [a.message, true];
  if (!r || !r.time) return ['', false];
  const when = teaWhen(r.time);
  const m = /^TEA(\d+),(\d+),/.exec(r.frame || '');
  const what = m ? ` (${m[1]} °C, ${teaMinutes(m[2] / 2)})` : '';
  const problems = (r.problems || []).map(p => TEA_PROBLEMS[p] || p).join(' and ');
  switch (r.outcome) {
    case 'brewed': return [`Last brew: ${when}${what}.`, false];
    case 'ready': return [`Checked ${when}: the brewer is reachable and reports no problems.`, false];
    case 'not_ready': return [`${when}: the brewer reported ${problems}, so nothing was sent.`, true];
    case 'connect_failed': return [`${when}: couldn’t reach the brewer. Nothing was sent.`, true];
    case 'write_unknown': return [`${when}: the brew command failed partway. Check the brewer.`, true];
  }
  return ['', false];
}

function teaBuild() {
  const days = TEA_DAYS.map((name, i) =>
    `<button class="tea-chip" data-day="${i}" onclick="teaToggleDay(${i})" disabled>${name}</button>`).join('');
  document.getElementById('focus-inner').innerHTML = `
    <div id="tea-root">
      <div class="focus-title">Tea</div>
      <div class="tea-next"><span id="tea-next-a">Loading…</span><b id="tea-next-b"></b><span id="tea-next-c"></span></div>
      <div class="tea-row"><div class="tea-label">Schedule</div>
        <button class="tea-chip tea-toggle" id="tea-toggle" onclick="teaToggleEnabled()" disabled>Off</button></div>
      <div class="tea-row"><div class="tea-label">Days</div><div class="tea-days">${days}</div></div>
      <div class="tea-row"><div class="tea-label">Time</div><div class="tea-stepper">
        <button class="tea-step" onclick="teaShiftTime(-60)" disabled>−1h</button>
        <button class="tea-step" onclick="teaShiftTime(-5)" disabled>−5</button>
        <div class="tea-value" id="tea-time">–</div>
        <button class="tea-step" onclick="teaShiftTime(5)" disabled>+5</button>
        <button class="tea-step" onclick="teaShiftTime(60)" disabled>+1h</button></div></div>
      <div class="tea-row"><div class="tea-label">Temperature</div><div class="tea-stepper">
        <button class="tea-step" id="tea-temp-down" onclick="teaStep('temp', -1)" disabled>−</button>
        <div class="tea-value" id="tea-temp">–</div>
        <button class="tea-step" id="tea-temp-up" onclick="teaStep('temp', 1)" disabled>+</button></div></div>
      <div class="tea-row"><div class="tea-label">Brew time</div><div class="tea-stepper">
        <button class="tea-step" id="tea-min-down" onclick="teaStep('minutes', -1)" disabled>−</button>
        <div class="tea-value" id="tea-min">–</div>
        <button class="tea-step" id="tea-min-up" onclick="teaStep('minutes', 1)" disabled>+</button></div></div>
      <div class="tea-actions">
        <button class="tea-btn" id="tea-check" onclick="teaCheck()" disabled>Check brewer</button>
        <button class="tea-btn go" id="tea-brew" onclick="teaAskBrew()" disabled>Brew now</button>
      </div>
      <div class="tea-msg" id="tea-msg"></div>
      <div class="tea-hint">The night before: water in the tank, dry leaves in the basket, a cup on the sensor.</div>
    </div>`;
}

function teaUpdate() {
  if (!teaPanelOpen()) return;
  const root = document.getElementById('tea-root');
  const buttons = root.querySelectorAll('button');
  if (!teaState || !teaState.allowed || !teaDraft) {
    buttons.forEach(b => { b.disabled = true; });
    teaSet('tea-next-a', teaState ? 'Not available on this device.' : 'Couldn’t reach the tea service.');
    teaSet('tea-next-b', ''); teaSet('tea-next-c', '');
    return;
  }
  const st = teaState, d = teaDraft, lim = st.limits, s = st.settings;
  if (st.next_run) {
    teaSet('tea-next-a', 'Next brew: ');
    teaSet('tea-next-b', teaWhen(st.next_run));
    teaSet('tea-next-c', ` · ${s.temp} °C · ${teaMinutes(s.minutes)}`);
  } else {
    teaSet('tea-next-a', s.enabled ? 'Pick at least one day.' : 'The schedule is off.');
    teaSet('tea-next-b', ''); teaSet('tea-next-c', '');
  }
  teaSet('tea-toggle', d.enabled ? 'On' : 'Off');
  document.getElementById('tea-toggle').classList.toggle('on', d.enabled);
  root.querySelectorAll('[data-day]').forEach(c => c.classList.toggle('on', d.days.includes(+c.dataset.day)));
  const [hh, mm] = d.time.split(':').map(Number);
  teaSet('tea-time', teaClock(hh, mm));
  teaSet('tea-temp', d.temp + ' °C');
  teaSet('tea-min', teaMinutes(d.minutes));
  const off = {
    'tea-temp-down': d.temp <= lim.temp_min, 'tea-temp-up': d.temp >= lim.temp_max,
    'tea-min-down': d.minutes <= lim.minutes_min, 'tea-min-up': d.minutes >= lim.minutes_max,
    'tea-check': st.busy, 'tea-brew': st.busy,
  };
  buttons.forEach(b => { b.disabled = !!off[b.id]; });
  const anyway = teaTooSoon(st);
  teaSet('tea-brew', anyway ? 'Brew anyway' : 'Brew now');
  document.getElementById('tea-brew').classList.toggle('anyway', anyway);
  const [msg, bad] = teaMsg && !(st.action && st.action.running) ? [teaMsg, teaMsgBad] : teaResult(st);
  teaSet('tea-msg', msg);
  document.getElementById('tea-msg').classList.toggle('bad', bad);
}

async function loadTeaFocus() {
  teaMsg = ''; teaMsgBad = false; teaDraft = null;
  teaBuild();
  await teaRefresh();
}

async function teaRefresh() {
  clearTimeout(teaPollTimer);
  try {
    const res = await fetch('/teamosa/status', { cache: 'no-store' });
    teaState = await res.json();
  } catch (e) {
    teaState = null;
  }
  if (!teaPanelOpen()) return;
  if (teaState && teaState.allowed && !teaSaveTimer) teaDraft = JSON.parse(JSON.stringify(teaState.settings));
  teaUpdate();
  // Fast while a check/brew runs, slowly otherwise (a scheduled brew can start while it's open).
  teaPollTimer = setTimeout(teaRefresh, teaState && teaState.busy ? 2000 : 15000);
}

function teaEdit(change) {
  if (!teaDraft) return;
  change(teaDraft);
  teaMsg = ''; teaMsgBad = false;
  teaUpdate();
  clearTimeout(teaSaveTimer);
  teaSaveTimer = setTimeout(teaSave, 800);
}
function teaToggleEnabled() { teaEdit(d => { d.enabled = !d.enabled; }); }
function teaToggleDay(i) {
  teaEdit(d => { d.days = d.days.includes(i) ? d.days.filter(x => x !== i) : d.days.concat(i).sort((a, b) => a - b); });
}
function teaShiftTime(delta) {
  teaEdit(d => {
    const [h, m] = d.time.split(':').map(Number);
    const t = (((h * 60 + m + delta) % 1440) + 1440) % 1440;
    d.time = String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0');
  });
}
function teaStep(field, dir) {
  const lim = teaState.limits;
  teaEdit(d => {
    if (field === 'temp') d.temp = Math.min(lim.temp_max, Math.max(lim.temp_min, d.temp + dir * lim.temp_step));
    else d.minutes = Math.min(lim.minutes_max, Math.max(lim.minutes_min, d.minutes + dir * lim.minutes_step));
  });
}

async function teaPost(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  return { res, data };
}

// Returns true when the settings on screen are the saved ones.
async function teaSave() {
  clearTimeout(teaSaveTimer);
  teaSaveTimer = null;
  let ok = false;
  try {
    const { res, data } = await teaPost('/teamosa/settings', teaDraft);
    ok = res.ok;
    if (ok) { teaState = data; teaMsg = 'Saved.'; teaMsgBad = false; }
    else { teaMsg = 'Not saved: ' + (data.error || ('error ' + res.status)); teaMsgBad = true; }
  } catch (e) {
    teaMsg = 'Not saved: the tea service didn’t answer.'; teaMsgBad = true;
  }
  teaUpdate();
  return ok;
}

async function teaCheck() {
  if (teaSaveTimer && !(await teaSave())) return;
  try {
    const { res, data } = await teaPost('/teamosa/check', { go: true });
    if (res.status === 202) { teaState = data; teaMsg = ''; teaMsgBad = false; }
    else { teaMsg = res.status === 409 ? 'The brewer is busy; try again in a minute.' : 'Couldn’t start the check: ' + (data.error || res.status); teaMsgBad = true; }
  } catch (e) {
    teaMsg = 'The tea service didn’t answer.'; teaMsgBad = true;
  }
  teaRefresh();
}

async function teaAskBrew() {
  if (teaSaveTimer && !(await teaSave())) return;   // don't brew with settings that didn't save
  const s = teaState.settings, last = teaState.last_brew;
  teaForce = teaTooSoon(teaState);
  let warn = '';
  if (teaForce) {
    const ago = last.minutes_ago >= 1 ? `${last.minutes_ago} min ago` : 'less than a minute ago';
    warn = `The last brew started ${ago}. Only brew again once it has finished: the brewer can’t be stopped. `;
  }
  document.querySelector('#tea-modal .power-title').textContent = teaForce ? 'Brew anyway?' : 'Brew now?';
  document.getElementById('tea-modal-text').textContent = warn +
    `${s.temp} °C for ${teaMinutes(s.minutes)}. Check there’s water in the tank, tea in the basket and a cup on the sensor.`;
  const go = document.getElementById('tea-modal-go');
  go.textContent = teaForce ? 'Brew anyway' : 'Brew';
  go.classList.toggle('anyway', teaForce);
  go.disabled = false;
  document.getElementById('tea-modal').classList.add('active');
}
function teaCloseModal() { document.getElementById('tea-modal').classList.remove('active'); }

async function teaConfirmBrew() {
  document.getElementById('tea-modal-go').disabled = true;
  try {
    const { res, data } = await teaPost('/teamosa/brew', teaForce ? { confirm: true, force: true } : { confirm: true });
    if (res.status === 202) { teaState = data; teaMsg = ''; teaMsgBad = false; }
    else { teaMsg = res.status === 409 ? 'The brewer is busy; try again in a minute.' : 'Not started: ' + (data.error || res.status); teaMsgBad = true; }
  } catch (e) {
    teaMsg = 'Not started: the tea service didn’t answer.'; teaMsgBad = true;
  }
  teaCloseModal();
  if (teaPanelOpen()) teaRefresh();
}
