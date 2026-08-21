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
const { app, BrowserWindow, Tray, Menu, ipcMain, screen, nativeImage,
        powerMonitor, powerSaveBlocker, session } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { execFile } = require('child_process');

const APP_DIR = __dirname;
const PAGE = path.join(APP_DIR, '..', 'package', 'Dusk Dashboard.dc.html');
const CALENDAR_FILE = path.join(APP_DIR, '..', '..', 'calendar_state.json');
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
  let st = { state: 'offline', agent: '', detail: '', thoughts: [], ts: 0, mode: '' };
  try {
    const raw = JSON.parse(fs.readFileSync(a.stateFile, 'utf8'));
    if (raw && typeof raw === 'object') {
      st = {
        state: typeof raw.state === 'string' ? raw.state : 'idle',
        agent: typeof raw.agent === 'string' ? raw.agent : '',
        detail: typeof raw.detail === 'string' ? raw.detail : '',
        thoughts: Array.isArray(raw.thoughts) ? raw.thoughts.slice(-6).map(String) : [],
        ts: Number(raw.ts) || 0,
        mode: typeof raw.mode === 'string' ? raw.mode : ''   // talking mode: ptt|wake|open
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
    state: st.state, agent: st.agent, detail: st.detail, thoughts: st.thoughts, mode: st.mode,
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

// The YOUTUBE module's player builds its configuration from the Referer of the
// embedding page. This app is loaded with loadFile(), so the page origin is
// file:// and no Referer is sent at all - the player then aborts with "Video
// player configuration error (153)" and shows an empty box. Supplying one for
// YouTube's own hosts is enough to make it work.
//
// Deliberately scoped to those hosts: this is the only place the dashboard
// talks to them, and a blanket header rewrite would leak a false Referer to
// every request the app makes.
function youtubeReferer() {
  const urls = ['*://*.youtube.com/*', '*://*.youtube-nocookie.com/*',
                '*://*.ytimg.com/*', '*://*.googlevideo.com/*'];
  session.defaultSession.webRequest.onBeforeSendHeaders({ urls }, (details, cb) => {
    details.requestHeaders['Referer'] = 'https://www.youtube.com/';
    cb({ requestHeaders: details.requestHeaders });
  });
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
    // F12 / Ctrl+Shift+I. Without this there is no way to read a console
    // error out of the running dashboard: a module can fail silently and
    // the only recourse is guessing. Opening devtools changes nothing else.
    w.webContents.on('before-input-event', (e, input) => {
      if (input.type !== 'keyDown') return;
      const k = (input.key || '').toLowerCase();
      if (k === 'f12' || (input.control && input.shift && k === 'i')) {
        w.webContents.toggleDevTools();
        e.preventDefault();
      }
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
    tray.setToolTip('Cortana Dash');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: 'Open dashboard', click: () => showMain() },
      { label: 'Minimize to bubble', click: () => toBubble(true) },
      { type: 'separator' },
      { label: 'Quit Cortana Dash', click: () => { quitting = true; app.quit(); } }
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
    youtubeReferer();
    createWindows();
    createTray();
    placeForDisplays();
    pollServices();
    setInterval(pollServices, POLL_SERVICE_MS);
    setInterval(broadcast, POLL_STATE_MS);

    screen.on('display-added', placeForDisplays);
    screen.on('display-removed', placeForDisplays);

    // ── Power policy: the dash is the decider. ──
    // Always-awake: the machine hosts Cortana + the bridge, so the OS must
    // never suspend it out from under them. The SCREEN is what sleeps, and we
    // manage that ourselves (below + the orb's SLEEP SCREEN button).
    try { powerSaveBlocker.start('prevent-app-suspension'); }
    catch (e) { console.error('[dash] power-save blocker unavailable:', e.message); }

    // Auto-sleep: no input for AUTO_SLEEP_MIN minutes -> screen off via
    // sleep-screen.sh (keyboard wakes it; pointers stay dark-disabled).
    // The script's flock makes repeat triggers harmless. Set
    // CORTANA_AUTO_SLEEP_MIN=0 in the environment to disable.
    const AUTO_SLEEP_MIN = Number(process.env.CORTANA_AUTO_SLEEP_MIN ?? 30);
    if (AUTO_SLEEP_MIN > 0) {
      let sleeping = false;
      setInterval(() => {
        try {
          const idle = powerMonitor.getSystemIdleTime();   // seconds
          if (idle >= AUTO_SLEEP_MIN * 60 && !sleeping) {
            sleeping = true;
            const { spawn } = require('child_process');
            const child = spawn('bash', [path.join(APP_DIR, 'sleep-screen.sh')],
                                { detached: true, stdio: 'ignore' });
            child.unref();
          } else if (idle < 5) {
            sleeping = false;   // user is back; re-arm for the next idle span
          }
        } catch (e) { /* powerMonitor unavailable - auto-sleep just idles */ }
      }, 30000);
    }
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
      run('log',    ['log', '--oneline', '-15'],
          (e, s) => s ? s.split('\n').map(l => ({ hash: l.slice(0,7), msg: l.slice(8) })) : []);
      run('status', ['status', '--short'],
          (e, s) => ({ clean: !s, files: s ? s.split('\n').length : 0 }));
      run('branch', ['rev-parse', '--abbrev-ref', 'HEAD'],
          (e, s) => s || 'unknown');
    });
  });
  // Today's calendar events, written by Cortana's calendar loop. Read-only.
  ipcMain.handle('calendar:today', () => {
    try {
      const raw = JSON.parse(fs.readFileSync(CALENDAR_FILE, 'utf8'));
      // Same staleness rule the Python side enforces: never show another day's
      // agenda as if it were today's. Without this check the dashboard happily
      // rendered yesterday's events whenever Cortana was down.
      const today = new Date().toLocaleDateString('en-CA');   // YYYY-MM-DD, local
      if (raw.day && raw.day !== today) {
        return { events: [],
                 error: 'calendar data is from ' + raw.day + ' — is Cortana running?' };
      }
      return { events: Array.isArray(raw.events) ? raw.events : [],
               error: typeof raw.error === 'string' ? raw.error : '' };
    } catch (e) {
      return { events: [], error: 'no calendar data yet' };
    }
  });
  // Mic device inventory (written by Cortana's voice/mic.py) + selection
  // (read back by it). File handshake, same decoupling as hud/calendar state.
  const MIC_STATE = path.join(APP_DIR, '..', '..', 'mic_state.json');
  const MIC_SELECT = path.join(APP_DIR, '..', '..', 'mic_select.json');
  ipcMain.handle('mic:list', () => {
    try {
      const raw = JSON.parse(fs.readFileSync(MIC_STATE, 'utf8'));
      return {
        devices: Array.isArray(raw.devices)
          ? raw.devices.map(d => ({ index: Number(d.index), name: String(d.name || '') })) : [],
        current: typeof raw.current === 'string' ? raw.current : '',
        available: !!raw.available,
        ts: Number(raw.ts) || 0
      };
    } catch (e) { return { devices: [], current: '', available: false, ts: 0 }; }
  });
  ipcMain.handle('mic:set', (_e, name) => {
    if (typeof name !== 'string' || !name || name.length > 160)
      return { ok: false, error: 'bad device name' };
    try {
      const tmp = MIC_SELECT + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify({ name }));
      fs.renameSync(tmp, MIC_SELECT);   // atomic: Cortana never reads a half file
      return { ok: true };
    } catch (e) { return { ok: false, error: String(e.message) }; }
  });

  // FILES module: read-only listing of the home folder, and click-to-open in
  // the native file manager. Paths are jailed to $HOME - anything outside
  // resolves back to home - and listing skips hidden entries + heavy noise.
  const FILES_SKIP = new Set(['node_modules', '__pycache__', 'venv', '.venv', 'snap']);
  const jailToHome = (p) => {
    const home = os.homedir();
    const abs = path.resolve(home, String(p || ''));
    return abs.startsWith(home) ? abs : home;
  };
  // Native folder picker for the FILES module's starting directory.
  ipcMain.handle('files:pick', async () => {
    const { dialog } = require('electron');
    try {
      const r = await dialog.showOpenDialog(mainWin, {
        title: 'Choose the folder this module starts from',
        defaultPath: os.homedir(),
        properties: ['openDirectory', 'showHiddenFiles']
      });
      if (r.canceled || !r.filePaths.length) return { ok: false, canceled: true };
      return { ok: true, path: jailToHome(r.filePaths[0]) };
    } catch (e) { return { ok: false, error: String(e.message) }; }
  });

  ipcMain.handle('files:tree', (_e, depth, root) => {
    const maxDepth = Math.min(6, Math.max(1, Number(depth) || 2));
    const build = (dir, d) => {
      const node = { name: path.basename(dir) || dir, path: dir, dir: true, kids: [] };
      if (d >= maxDepth) return node;
      let entries = [];
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true })
          .filter(x => !x.name.startsWith('.') && !FILES_SKIP.has(x.name))
          .sort((a, b) => (b.isDirectory() - a.isDirectory()) || a.name.localeCompare(b.name))
          .slice(0, 14);
      } catch (e) { return node; }
      for (const x of entries) {
        const p = path.join(dir, x.name);
        node.kids.push(x.isDirectory() ? build(p, d + 1)
                                       : { name: x.name, path: p, dir: false, kids: [] });
      }
      return node;
    };
    try {
      const start = jailToHome(root || '');
      return { ok: true, root: start, tree: build(start, 0) };
    } catch (e) { return { ok: false, error: String(e.message) }; }
  });
  ipcMain.handle('files:open', (_e, p) => {
    // xdg-open on a directory (or file) opens the system file manager there.
    execFile('xdg-open', [jailToHome(p)], { timeout: 10000 }, () => {});
    return { ok: true };
  });

  // Manual calendar pull for the AGENDA module's refresh button: runs
  // Cortana's own one-shot fetch, which rewrites calendar_state.json. Uses her
  // venv so it shares the same token and code path as the scheduled loop.
  ipcMain.handle('calendar:refresh', () => {
    const REPO = path.join(os.homedir(), 'cortana');
    const py = ['venv/bin/python', 'cortana_venv/bin/python', '.venv/bin/python']
      .map(v => path.join(REPO, v)).find(v => { try { return fs.existsSync(v); } catch (e) { return false; } });
    if (!py) return { ok: false, error: 'no python venv found in ~/cortana' };
    return new Promise(resolve => {
      execFile(py, [path.join(REPO, 'main.py'), '--calendar-once'],
        { cwd: REPO, timeout: 60000 }, (err, stdout, stderr) => {
          const out = String(stdout || '') + String(stderr || '');
          if (err) return resolve({ ok: false, error: out.trim().slice(-160) || err.message });
          // --calendar-once prints CALENDAR ERROR on a failed fetch
          if (/CALENDAR ERROR/i.test(out))
            return resolve({ ok: false, error: out.split('CALENDAR ERROR:')[1].trim().slice(0, 160) });
          resolve({ ok: true });
        });
    });
  });

  // Screen off, machine on (burn-in guard): runs sleep-screen.sh detached.
  // The script disables pointer devices, forces DPMS off, and re-enables the
  // pointers when a keyboard press relights the panel.
  ipcMain.handle('screen:sleep', () => {
    try {
      const { spawn } = require('child_process');
      const child = spawn('bash', [path.join(APP_DIR, 'sleep-screen.sh')],
                          { detached: true, stdio: 'ignore' });
      child.unref();
      return { ok: true };
    } catch (e) { return { ok: false, error: String(e.message) }; }
  });

  require('./spotify').register(ipcMain);

  ipcMain.on('ui:open', showMain);
  ipcMain.on('ui:bubble', toBubble);
  ipcMain.on('ui:ctx', () => {
    Menu.buildFromTemplate([
      { label: 'Open dashboard', click: showMain },
      { type: 'separator' },
      { label: 'Quit Cortana Dash', click: () => { quitting = true; app.quit(); } }
    ]).popup({ window: bubbleWin });
  });
}
