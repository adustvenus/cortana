/* Dusk Dashboard — Electron shell.
 *
 * Lifecycle, fully decoupled from Cortana: this app only READS agent state
 * files and drives systemd user units. Cortana restarting/crashing never
 * touches this process, and vice versa.
 *
 * Screen policy (re-evaluated live on display hotplug):
 *   >= 2 displays -> frameless fullscreen on a non-primary display
 *   1 display     -> hidden; floating always-on-top "bubble" orb at top-left.
 *                    Click bubble = open fullscreen. Esc / minimize = back to bubble.
 * Closing the main window hides to bubble instead of quitting (accidental-close
 * guard). Real quit: tray menu, or right-click the bubble.
 */
const { app, BrowserWindow, Tray, Menu, ipcMain, screen, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { execFile } = require('child_process');

const APP_DIR = __dirname;
const PAGE = path.join(APP_DIR, '..', 'package', 'Dusk Dashboard.dc.html');
const ICON = path.join(APP_DIR, 'icons', 'dusk.png');
const POLL_STATE_MS = 300;
const POLL_SERVICE_MS = 2500;
const VALID_ACTIONS = new Set(['start', 'stop', 'restart']);

// ── agent registry ──────────────────────────────────────────────────────────
function resolvePath(p) {
  if (!p) return null;
  if (p.startsWith('~')) return path.join(os.homedir(), p.slice(1));
  return path.isAbsolute(p) ? p : path.resolve(APP_DIR, p);
}

let AGENTS = [];
try {
  const reg = JSON.parse(fs.readFileSync(path.join(APP_DIR, 'agents.json'), 'utf8'));
  AGENTS = (reg.agents || []).filter(a => a && a.id).map(a => ({
    id: String(a.id),
    name: String(a.name || a.id).toUpperCase(),
    stateFile: resolvePath(a.stateFile),
    staleAfterSec: Number(a.staleAfterSec) > 0 ? Number(a.staleAfterSec) : 5,
    systemdUnit: a.systemdUnit ? String(a.systemdUnit) : null
  }));
} catch (e) {
  console.error('[dusk] agents.json unreadable:', e.message);
}

const serviceState = {};   // agentId -> 'active' | 'inactive' | 'failed' | 'unknown'

function readAgent(a) {
  let st = { state: 'offline', agent: '', detail: '', thoughts: [], ts: 0 };
  try {
    const raw = JSON.parse(fs.readFileSync(a.stateFile, 'utf8'));
    if (raw && typeof raw === 'object') {
      st = {
        state: typeof raw.state === 'string' ? raw.state : 'idle',
        agent: typeof raw.agent === 'string' ? raw.agent : '',
        detail: typeof raw.detail === 'string' ? raw.detail : '',
        thoughts: Array.isArray(raw.thoughts) ? raw.thoughts.slice(-6).map(String) : [],
        ts: Number(raw.ts) || 0
      };
    }
  } catch (e) { /* missing/corrupt file -> offline defaults */ }
  // NOTE: hud_state.py only rewrites the file when the payload changes, so ts
  // freezes while the agent idles. For systemd-managed agents the unit state is
  // the liveness authority and staleness gets a generous 600s floor; for
  // status-only agents (no unit) the configured staleAfterSec is honored as-is,
  // since the state file is all we have.
  const ageSec = st.ts ? (Date.now() / 1000 - st.ts) : Infinity;
  const staleCutoff = a.systemdUnit ? Math.max(a.staleAfterSec, 600) : a.staleAfterSec;
  return {
    id: a.id, name: a.name,
    state: st.state, agent: st.agent, detail: st.detail, thoughts: st.thoughts,
    ts: st.ts, stale: ageSec > staleCutoff,
    fresh: ageSec < 10,   // actively writing right now (e.g. run manually outside systemd)
    service: a.systemdUnit ? (serviceState[a.id] || 'unknown') : 'unknown'
  };
}

function snapshot() { return AGENTS.map(readAgent); }

function pollServices() {
  for (const a of AGENTS) {
    if (!a.systemdUnit) continue;
    execFile('systemctl', ['--user', 'is-active', a.systemdUnit], { timeout: 4000 },
      (err, stdout) => {
        const out = String(stdout || '').trim();
        // is-active exits nonzero for inactive/failed but still prints the state
        serviceState[a.id] = out || (err ? 'unknown' : 'unknown');
      });
  }
}

// ── windows ─────────────────────────────────────────────────────────────────
let mainWin = null, bubbleWin = null, tray = null;
let quitting = false;
let lastSent = '';

function broadcast() {
  const snap = snapshot();
  const json = JSON.stringify(snap);
  if (json === lastSent) return;
  lastSent = json;
  for (const w of [mainWin, bubbleWin]) {
    if (w && !w.isDestroyed()) w.webContents.send('agents:update', snap);
  }
}

function externalDisplay() {
  const primary = screen.getPrimaryDisplay();
  return screen.getAllDisplays().find(d => d.id !== primary.id) || null;
}

let userBubbled = false;   // explicit Esc/minimize/close: display hotplug must not override it

function showMain() {
  if (!mainWin || mainWin.isDestroyed()) return;
  userBubbled = false;
  const ext = externalDisplay();
  const target = ext || screen.getPrimaryDisplay();
  // Leaving fullscreen, moving displays, and re-entering fullscreen in one
  // synchronous burst races the X11 WM on a visible window (hotplug case).
  // Sequence it: drop fullscreen only if needed, let the WM settle, then
  // set bounds and re-enter fullscreen on the next tick.
  if (mainWin.isMinimized()) mainWin.restore();
  const enter = () => {
    if (!mainWin || mainWin.isDestroyed()) return;
    mainWin.setBounds(target.bounds);
    mainWin.setFullScreen(true);
    mainWin.show();
    mainWin.focus();
    if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.hide();
  };
  if (mainWin.isFullScreen()) {
    // setFullScreen is async on X11 (a _NET_WM_STATE round-trip): wait for the
    // WM to confirm leaving before moving displays, or its geometry restore
    // clobbers our setBounds. Timeout fallback in case the event never fires.
    let entered = false;
    const once = () => { if (!entered) { entered = true; enter(); } };
    mainWin.once('leave-full-screen', once);
    setTimeout(once, 400);
    mainWin.setFullScreen(false);
  } else enter();
}

function toBubble(explicit = false) {
  if (explicit) userBubbled = true;
  if (mainWin && !mainWin.isDestroyed()) mainWin.hide();
  if (bubbleWin && !bubbleWin.isDestroyed()) {
    // Pin the bubble to the PRIMARY display's top-left (global (14,14) lands on
    // the leftmost display, which may be a TV across the room).
    const wa = screen.getPrimaryDisplay().workArea;
    bubbleWin.setPosition(wa.x + 14, wa.y + 14);
    bubbleWin.showInactive();
    lastSent = '';            // force a fresh push so the bubble paints current state
    broadcast();
  }
}

function placeForDisplays() {
  // Hotplug policy: auto-open on an external display, but never override an
  // explicit user minimize-to-bubble (TV power-save re-handshakes fire
  // display-removed/added and would otherwise keep stealing focus).
  if (externalDisplay() && !userBubbled) showMain();
  else toBubble();
}

function createWindows() {
  mainWin = new BrowserWindow({
    show: false,
    frame: false,
    backgroundColor: '#221d33',
    icon: ICON,
    webPreferences: {
      preload: path.join(APP_DIR, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false          // preload needs fs for duskRaw (template repair)
    }
  });
  mainWin.loadFile(PAGE);
  mainWin.on('close', (e) => {
    if (!quitting) { e.preventDefault(); toBubble(true); }   // accidental-close guard
  });
  mainWin.on('minimize', () => { mainWin.restore(); toBubble(true); });
  mainWin.webContents.on('render-process-gone', crashGuard(() => mainWin));

  bubbleWin = new BrowserWindow({
    width: 72, height: 72, x: 14, y: 14,
    show: false,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    fullscreenable: false,
    hasShadow: false,
    icon: ICON,
    webPreferences: {
      preload: path.join(APP_DIR, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false          // same preload as main: sandboxed require('fs') would throw
    }
  });
  bubbleWin.loadFile(path.join(APP_DIR, 'bubble.html'));
  bubbleWin.setAlwaysOnTop(true, 'screen-saver');   // stay above fullscreen apps on X11
  bubbleWin.on('close', (e) => { if (!quitting) { e.preventDefault(); bubbleWin.hide(); } });
  bubbleWin.webContents.on('render-process-gone', crashGuard(() => bubbleWin));

  // Lock both windows down: never navigate away from our local files, never
  // open child windows (which would inherit the unsandboxed preload + bridge).
  for (const w of [mainWin, bubbleWin]) {
    w.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    w.webContents.on('will-navigate', (e, url) => {
      if (!url.startsWith('file://')) e.preventDefault();
    });
  }
}

// Reload a crashed renderer, but never in a tight loop: cap at 3 reloads per
// minute, and don't fight deliberate kills.
const crashLog = { times: [] };
function crashGuard(getWin) {
  return (_e, details) => {
    if (quitting) return;
    if (details && (details.reason === 'killed' || details.reason === 'clean-exit')) return;
    const now = Date.now();
    crashLog.times = crashLog.times.filter(t => now - t < 60000);
    if (crashLog.times.length >= 3) {
      console.error('[dusk] renderer crash-looping; not reloading (restart the service)');
      return;
    }
    crashLog.times.push(now);
    const w = getWin();
    if (w && !w.isDestroyed()) setTimeout(() => w.reload(), 1000);
  };
}

function createTray() {
  try {
    const img = nativeImage.createFromPath(ICON).resize({ width: 22, height: 22 });
    tray = new Tray(img);
    tray.setToolTip('Dusk Dashboard');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: 'Open dashboard', click: () => showMain() },
      { label: 'Minimize to bubble', click: () => toBubble(true) },
      { type: 'separator' },
      { label: 'Quit Dusk Dashboard', click: () => { quitting = true; app.quit(); } }
    ]));
    tray.on('click', () => showMain());
  } catch (e) { console.error('[dusk] tray unavailable:', e.message); }
}

// ── single instance ─────────────────────────────────────────────────────────
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => showMain());

  app.whenReady().then(() => {
    createWindows();
    createTray();
    placeForDisplays();
    pollServices();
    setInterval(pollServices, POLL_SERVICE_MS);
    setInterval(broadcast, POLL_STATE_MS);

    screen.on('display-added', placeForDisplays);
    screen.on('display-removed', placeForDisplays);
  });

  app.on('before-quit', () => { quitting = true; });
  app.on('window-all-closed', () => { /* keep alive for the bubble/tray */ });

  // ── IPC ───────────────────────────────────────────────────────────────────
  ipcMain.handle('agents:get', () => snapshot());

  ipcMain.handle('agents:control', (_e, payload) => {
    const { id, action } = payload || {};
    const agent = AGENTS.find(a => a.id === id);
    if (!agent) return { ok: false, error: 'unknown agent' };
    if (!VALID_ACTIONS.has(action)) return { ok: false, error: 'invalid action' };
    if (!agent.systemdUnit) return { ok: false, error: 'agent is status-only' };
    return new Promise(resolve => {
      // stop/restart can legitimately take up to systemd's 90s unit stop
      // timeout; a short exec timeout would misreport slow success as FAILED.
      execFile('systemctl', ['--user', action, agent.systemdUnit], { timeout: 120000 },
        (err, stdout, stderr) => {
          if (err) resolve({ ok: false, error: String(stderr || err.message).trim().slice(0, 200) });
          else resolve({ ok: true, output: String(stdout).trim() });
          setTimeout(pollServices, 500);   // reflect the new unit state quickly
        });
    });
  });


  ipcMain.handle('git:status', () => {
    const REPO = path.join(os.homedir(), 'cortana');
    return new Promise(resolve => {
      // Run three git commands in parallel
      let done = 0; const out = {};
      const run = (key, args, cb) => {
        execFile('git', ['-C', REPO, ...args], { timeout: 6000 }, (err, stdout) => {
          out[key] = cb(err, String(stdout || '').trim());
          if (++done === 3) resolve(out);
        });
      };
      run('log',    ['log', '--oneline', '-5'],
          (e, s) => s ? s.split('\n').map(l => ({ hash: l.slice(0,7), msg: l.slice(8) })) : []);
      run('status', ['status', '--short'],
          (e, s) => ({ clean: !s, files: s ? s.split('\n').length : 0 }));
      run('branch', ['rev-parse', '--abbrev-ref', 'HEAD'],
          (e, s) => s || 'unknown');
    });
  });
  ipcMain.on('ui:open', showMain);
  ipcMain.on('ui:bubble', toBubble);
  ipcMain.on('ui:ctx', () => {
    Menu.buildFromTemplate([
      { label: 'Open dashboard', click: showMain },
      { type: 'separator' },
      { label: 'Quit Dusk Dashboard', click: () => { quitting = true; app.quit(); } }
    ]).popup({ window: bubbleWin });
  });
}
