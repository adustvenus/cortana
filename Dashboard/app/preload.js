/* Dusk Dashboard preload: the only bridge between the dashboard page and the
 * OS. Exposes a minimal, validated API - no node/fs access reaches the page. */
const { contextBridge, ipcRenderer } = require('electron');

// Raw page source for the dashboard's template repair pass (DOM parsing
// lowercases camelCase attrs; the page re-adopts its raw template after boot).
// Fixed path only - the page cannot read arbitrary files through this.
// require('fs') is wrapped: under a sandboxed preload (e.g. if webPreferences
// ever revert to the default) it throws, and an uncaught throw here would kill
// the WHOLE preload - leaving even the IPC bridge unexposed (inert bubble).
let readRaw = () => '';
try {
  const fs = require('fs');
  const path = require('path');
  const PAGE = path.join(__dirname, '..', 'package', 'Dusk Dashboard.dc.html');
  readRaw = () => { try { return fs.readFileSync(PAGE, 'utf8'); } catch (e) { return ''; } };
} catch (e) { /* sandboxed: template repair falls back to fetch */ }
contextBridge.exposeInMainWorld('duskRaw', readRaw);

contextBridge.exposeInMainWorld('duskBridge', {
  /** Subscribe to agent snapshots. Immediately delivers current state, then
   *  pushes on change. Returns an unsubscribe function. */
  onAgents(cb) {
    if (typeof cb !== 'function') return () => {};
    const handler = (_e, list) => { try { cb(list); } catch (err) {} };
    ipcRenderer.on('agents:update', handler);
    ipcRenderer.invoke('agents:get').then(list => { try { cb(list); } catch (err) {} }).catch(() => {});
    return () => ipcRenderer.removeListener('agents:update', handler);
  },
  /** start | stop | restart an agent by registry id. Resolves {ok, error?, output?}. */
  control(id, action) {
    return ipcRenderer.invoke('agents:control', { id: String(id), action: String(action) });
  },
  open() { ipcRenderer.send('ui:open'); },
  toBubble() { ipcRenderer.send('ui:bubble'); },
  ctxMenu() { ipcRenderer.send('ui:ctx'); }
});

// Esc anywhere in the dashboard = minimize back to the bubble.
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') ipcRenderer.send('ui:bubble');
});
