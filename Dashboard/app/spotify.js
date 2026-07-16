/* Spotify integration for the Dusk dashboard (Electron main process).
 *
 * OAuth 2.0 Authorization Code + PKCE (no client secret needed - safe for a
 * desktop app). The user creates a Spotify Developer app, adds the redirect
 * URI below, and pastes the Client ID into spotify.json. Playback control
 * (play/pause/next/prev) requires Spotify Premium; the now-playing readout
 * works on free too.
 *
 * Tokens live in spotify_token.json (gitignored - the refresh token is a
 * secret). Client ID is public under PKCE, so spotify.json is committed with a
 * placeholder.
 */
const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { BrowserWindow } = require('electron');

const CONFIG_FILE = path.join(__dirname, 'spotify.json');
const TOKEN_FILE = path.join(__dirname, 'spotify_token.json');
const REDIRECT_PORT = 8888;
const REDIRECT_URI = `http://127.0.0.1:${REDIRECT_PORT}/callback`;
const SCOPES = 'user-read-playback-state user-modify-playback-state user-read-currently-playing';

function clientId() {
  try { return String(JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')).clientId || ''); }
  catch (e) { return ''; }
}
function configured() { const c = clientId(); return !!c && !c.startsWith('YOUR_'); }
function loadToken() { try { return JSON.parse(fs.readFileSync(TOKEN_FILE, 'utf8')); } catch (e) { return null; } }
function saveToken(t) { try { fs.writeFileSync(TOKEN_FILE, JSON.stringify(t)); } catch (e) {} }
function b64url(buf) { return buf.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); }

let pkceVerifier = null;
let authServer = null;

async function exchangeCode(code) {
  const body = new URLSearchParams({
    grant_type: 'authorization_code', code, redirect_uri: REDIRECT_URI,
    client_id: clientId(), code_verifier: pkceVerifier
  });
  const r = await fetch('https://accounts.spotify.com/api/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
  if (!r.ok) throw new Error('token exchange failed: ' + r.status);
  const j = await r.json();
  // Persist the granted scope so we can tell a scope-shortfall from an
  // allowlist/Premium 403 at a glance.
  saveToken({ access_token: j.access_token, refresh_token: j.refresh_token,
              scope: j.scope || '', expires_at: Date.now() + j.expires_in * 1000 });
}

async function refreshToken() {
  const t = loadToken();
  if (!t || !t.refresh_token) return null;
  const body = new URLSearchParams({
    grant_type: 'refresh_token', refresh_token: t.refresh_token, client_id: clientId() });
  const r = await fetch('https://accounts.spotify.com/api/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
  if (!r.ok) return null;
  const j = await r.json();
  const nt = { access_token: j.access_token,
               refresh_token: j.refresh_token || t.refresh_token,
               expires_at: Date.now() + j.expires_in * 1000 };
  saveToken(nt);
  return nt;
}

async function accessToken() {
  let t = loadToken();
  if (!t) return null;
  if (Date.now() > (t.expires_at || 0) - 60000) t = await refreshToken();   // refresh a minute early
  return t ? t.access_token : null;
}

function login() {
  return new Promise((resolve) => {
    if (!configured()) {
      resolve({ ok: false, error: 'Set your Client ID in Dashboard/app/spotify.json' });
      return;
    }
    pkceVerifier = b64url(crypto.randomBytes(64));
    const challenge = b64url(crypto.createHash('sha256').update(pkceVerifier).digest());
    const authUrl = 'https://accounts.spotify.com/authorize?' + new URLSearchParams({
      client_id: clientId(), response_type: 'code', redirect_uri: REDIRECT_URI,
      code_challenge_method: 'S256', code_challenge: challenge, scope: SCOPES,
      // Force the consent screen so scopes are always freshly granted - without
      // this Spotify silently reuses a prior grant, which can lack our scopes.
      show_dialog: 'true' });

    let settled = false;
    const finish = (r) => { if (!settled) { settled = true; resolve(r); } };
    if (authServer) { try { authServer.close(); } catch (e) {} authServer = null; }

    authServer = http.createServer(async (req, res) => {
      if (!req.url.startsWith('/callback')) { res.writeHead(404); res.end(); return; }
      const code = new URL(req.url, REDIRECT_URI).searchParams.get('code');
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end('<html><body style="background:#221d33;color:#fdf3ec;font-family:sans-serif;text-align:center;padding-top:20vh"><h2>Spotify connected.</h2><p>You can close this window.</p></body></html>');
      try { authServer.close(); } catch (e) {} authServer = null;
      if (win && !win.isDestroyed()) win.close();
      if (!code) return finish({ ok: false, error: 'no code returned' });
      try { await exchangeCode(code); finish({ ok: true }); }
      catch (e) { finish({ ok: false, error: String(e.message) }); }
    });

    let win = null;
    authServer.on('error', (e) => finish({ ok: false, error: 'loopback failed: ' + e.message }));
    authServer.listen(REDIRECT_PORT, '127.0.0.1', () => {
      // alwaysOnTop + center + focus so it appears OVER the fullscreen kiosk
      // dashboard (a plain child window can otherwise open behind it on X11).
      win = new BrowserWindow({
        width: 520, height: 720, title: 'Connect Spotify', autoHideMenuBar: true,
        alwaysOnTop: true, center: true, minimizable: false, maximizable: false,
        webPreferences: { nodeIntegration: false, contextIsolation: true }
      });
      win.setAlwaysOnTop(true, 'screen-saver');
      win.loadURL(authUrl);
      win.once('ready-to-show', () => { win.show(); win.focus(); });
      win.webContents.on('did-fail-load', (_e, code, desc) =>
        finish({ ok: false, error: 'auth page failed to load: ' + desc + ' (' + code + ')' }));
      win.on('closed', () => {
        if (authServer) { try { authServer.close(); } catch (e) {} authServer = null; }
        finish({ ok: false, error: 'window closed' });
      });
    });
  });
}

function parsePlayback(j) {
  const it = j.item || {};
  const img = (it.album && it.album.images && it.album.images[0]) || {};
  return {
    configured: true, connected: true, active: true, playing: !!j.is_playing,
    track: it.name || '', artist: (it.artists || []).map(a => a.name).join(', '),
    art: img.url || '', progress: j.progress_ms || 0, duration: it.duration_ms || 0
  };
}

async function errBody(r) {
  // Spotify error bodies carry the real reason: {"error":{"status,message,reason"}}.
  // Fall back to raw text so a non-JSON / message-less 403 still tells us something.
  try {
    const txt = await r.text();
    try { const j = JSON.parse(txt); if (j && j.error && (j.error.message || j.error.reason)) return j.error.message || j.error.reason; } catch (e) {}
    return (txt || '').trim().slice(0, 160);
  } catch (e) { return ''; }
}

async function state() {
  if (!configured()) return { configured: false, connected: false };
  const t = loadToken();
  const at = await accessToken();
  if (!at) return { configured: true, connected: false };
  const H = { Authorization: 'Bearer ' + at };
  const grantedScope = (t && t.scope) || '';
  try {
    // /me/player is authoritative but returns 204 when no device is "active"
    // in the API's view - which happens even while playing on some devices.
    // Fall back to /me/player/currently-playing, which reports across devices.
    let r = await fetch('https://api.spotify.com/v1/me/player', { headers: H });
    if (r.ok && r.status !== 204) return parsePlayback(await r.json());
    if (r.status !== 204 && !r.ok)
      return { configured: true, connected: true, error: r.status, errorMsg: await errBody(r), grantedScope };
    const r2 = await fetch('https://api.spotify.com/v1/me/player/currently-playing', { headers: H });
    if (r2.status === 204) return { configured: true, connected: true, active: false, playing: false, grantedScope };
    if (!r2.ok)
      return { configured: true, connected: true, error: r2.status, errorMsg: await errBody(r2), grantedScope };
    const j2 = await r2.json();
    if (!j2 || !j2.item) return { configured: true, connected: true, active: false, playing: false, grantedScope };
    return parsePlayback(j2);
  } catch (e) { return { configured: true, connected: true, error: String(e.message), grantedScope }; }
}

async function control(action) {
  const at = await accessToken();
  if (!at) return { ok: false, error: 'not connected' };
  const map = { play: ['PUT', '/me/player/play'], pause: ['PUT', '/me/player/pause'],
                next: ['POST', '/me/player/next'], previous: ['POST', '/me/player/previous'] };
  const m = map[action];
  if (!m) return { ok: false, error: 'bad action' };
  try {
    const r = await fetch('https://api.spotify.com/v1' + m[1],
                          { method: m[0], headers: { Authorization: 'Bearer ' + at } });
    // 404 = no active device; surface a friendly hint for the UI
    if (r.status === 404) return { ok: false, error: 'no active Spotify device - start playback once on any device' };
    return { ok: r.ok || r.status === 204, status: r.status };
  } catch (e) { return { ok: false, error: String(e.message) }; }
}

function register(ipcMain) {
  ipcMain.handle('spotify:login', () => login());
  ipcMain.handle('spotify:state', () => state());
  ipcMain.handle('spotify:control', (_e, action) => control(String(action)));
}

module.exports = { register };
