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
  // freezes while the agent idles. Staleness is therefore advisory; the
  // renderer treats the systemd unit state as the liveness authority.
  const stale = !st.ts || (Date.now() / 1000 - st.ts) > Math.max(a.staleAfterSec, 600);
  return {
    id: a.id, name: a.name,
    state: st.state, agent: st.agent, detail: st.detail, thoughts: st.thoughts,
    ts: st.ts, stale,
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

function showMain() {
  if (!mainWin) return;
  const ext = externalDisplay();
  const target = ext || screen.getPrimaryDisplay();
  mainWin.setFullScreen(false);            // leave fullscreen before moving displays
  mainWin.setBounds(target.bounds);
  mainWin.setFullScreen(true);
  mainWin.show();
  mainWin.focus();
  if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.hide();
}

function toBubble() {
  if (mainWin && !mainWin.isDestroyed()) mainWin.hide();
  if (bubbleWin && !bubbleWin.isDestroyed()) {
    bubbleWin.showInactive();
    lastSent = '';            // force a fresh push so the bubble paints current state
    broadcast();
  }
}

function placeForDisplays() {
  if (externalDisplay()) showMain();
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
    if (!quitting) { e.preventDefault(); toBubble(); }   // accidental-close guard
  });
  mainWin.on('minimize', (e) => { e.preventDefault(); toBubble(); });
  mainWin.webContents.on('render-process-gone', () => { if (!quitting) mainWin.reload(); });

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
      nodeIntegration: false
    }
  });
  bubbleWin.loadFile(path.join(APP_DIR, 'bubble.html'));
  bubbleWin.setAlwaysOnTop(true, 'screen-saver');   // stay above fullscreen apps on X11
  bubbleWin.on('close', (e) => { if (!quitting) { e.preventDefault(); bubbleWin.hide(); } });
  bubbleWin.webContents.on('render-process-gone', () => { if (!quitting) bubbleWin.reload(); });
}

function createTray() {
  try {
    const img = nativeImage.createFromPath(ICON).resize({ width: 22, height: 22 });
    tray = new Tray(img);
    tray.setToolTip('Dusk Dashboard');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: 'Open dashboard', click: showMain },
      { label: 'Minimize to bubble', click: toBubble },
      { type: 'separator' },
      { label: 'Quit Dusk Dashboard', click: () => { quitting = true; app.quit(); } }
    ]));
    tray.on('click', showMain);
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
      execFile('systemctl', ['--user', action, agent.systemdUnit], { timeout: 15000 },
        (err, stdout, stderr) => {
          if (err) resolve({ ok: false, error: String(stderr || err.message).trim().slice(0, 200) });
          else resolve({ ok: true, output: String(stdout).trim() });
          setTimeout(pollServices, 500);   // reflect the new unit state quickly
        });
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
