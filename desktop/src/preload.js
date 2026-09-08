'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('teleops', {
  onState: (cb) => ipcRenderer.on('state', (_e, state) => cb(state)),
  action: (name) => ipcRenderer.send('splash-action', name),
});
