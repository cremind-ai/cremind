#!/usr/bin/env node
/*
 * Cremind Zalo (personal account) sidecar.
 *
 * Spawned by the Python Zalo userbot adapter
 * (app/channels/adapters/zalo_userbot.py). Maintains one logged-in Zalo Web
 * session via the unofficial `zca-js` library and bridges it to the adapter
 * over a localhost WebSocket. Mirrors the WhatsApp/Baileys sidecar.
 *
 * Lifecycle:
 *   1. Parent passes --profile, --channel-id, --working-dir as argv.
 *   2. We open a WebSocket server on an ephemeral port and emit
 *      "WS_PORT=<port>" to stdout. Parent reads that line and connects.
 *   3. If saved credentials exist we `zalo.login(...)`; otherwise we
 *      `zalo.loginQR(...)` and emit {kind:"qr"} until the user scans.
 *   4. Events flow parent <-> sidecar as JSON frames:
 *        sidecar -> parent:  {kind:"qr"|"ready"|"incoming"|"disconnected"
 *                             |"send_error"|"error", ...}
 *        parent -> sidecar:  {kind:"send", sender_id, text}
 *                            {kind:"typing", sender_id}
 *                            {kind:"logout"}
 *
 * Credentials (cookie/imei/userAgent) are persisted per (profile, channel-id)
 * at <working-dir>/<profile>/zalo/<channel-id>/credentials.json so a paired
 * session survives restarts.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

import minimist from 'minimist';
import { WebSocketServer } from 'ws';
import { Zalo } from 'zca-js';

// zca-js LoginQRCallbackEventType values.
const QR_GENERATED = 0;
const QR_EXPIRED = 1;
const QR_SCANNED = 2;
const QR_DECLINED = 3;
const QR_GOT_LOGIN = 4;
// ThreadType: 0 = User (DM), 1 = Group.
const THREAD_USER = 0;
const THREAD_GROUP = 1;

const argv = minimist(process.argv.slice(2));
const profile = argv.profile || 'admin';
const channelId = argv['channel-id'] || 'default';
const workingDirArg = argv['working-dir'] || path.join(os.homedir(), '.cremind');
const workingDir = workingDirArg.startsWith('~')
  ? path.join(os.homedir(), workingDirArg.slice(1))
  : workingDirArg;
const sessionDir = path.join(workingDir, profile, 'zalo', channelId);
const credsFile = path.join(sessionDir, 'credentials.json');

fs.mkdirSync(sessionDir, { recursive: true });

let connectedClient = null;
let api = null;
let loginAttempt = 0;
let persistTimer = null;

function emit(payload) {
  if (connectedClient && connectedClient.readyState === 1) {
    try {
      connectedClient.send(JSON.stringify(payload));
    } catch (e) {
      // Client may have closed mid-send; nothing actionable.
    }
  }
}

function logInfo(line) {
  // stderr — the parent reads stdout only for the WS_PORT handshake.
  process.stderr.write(`[zalo-sidecar] ${line}\n`);
}

function logErr(stage, err) {
  process.stderr.write(`[zalo-sidecar] ${stage}: ${err && (err.stack || err.message || err)}\n`);
}

function loadCreds() {
  try {
    const raw = fs.readFileSync(credsFile, 'utf8');
    const parsed = JSON.parse(raw);
    if (parsed && parsed.imei && parsed.cookie && parsed.userAgent) return parsed;
  } catch (e) {
    // No/invalid saved session — fall through to QR login.
  }
  return null;
}

function saveCreds(creds) {
  if (!creds || !creds.imei || !creds.cookie || !creds.userAgent) return;
  try {
    const existing = loadCreds();
    const payload = {
      imei: creds.imei,
      cookie: creds.cookie,
      userAgent: creds.userAgent,
      language: creds.language || (existing && existing.language) || 'vi',
      createdAt: (existing && existing.createdAt) || new Date().toISOString(),
      lastUsedAt: new Date().toISOString(),
    };
    fs.writeFileSync(credsFile, JSON.stringify(payload), { mode: 0o600 });
  } catch (e) {
    logErr('saveCreds', e);
  }
}

function snapshotCreds(zaloApi, captured) {
  // Prefer a fresh snapshot from the API (Zalo rotates cookies during a
  // session); fall back to the credentials captured in the QR callback.
  try {
    const ctx = zaloApi.getContext ? zaloApi.getContext() : {};
    const cookieJson = zaloApi.getCookie ? zaloApi.getCookie().toJSON() : null;
    const cookie = (cookieJson && cookieJson.cookies) || (captured && captured.cookie);
    if (ctx && ctx.imei && cookie && ctx.userAgent) {
      return { imei: ctx.imei, cookie, userAgent: ctx.userAgent, language: ctx.language };
    }
  } catch (e) {
    logErr('snapshotCreds', e);
  }
  return captured || null;
}

function extractText(content) {
  if (typeof content === 'string') return content;
  if (content && typeof content === 'object') {
    return content.title || content.description || content.href || '';
  }
  return '';
}

async function startZalo() {
  loginAttempt += 1;
  const zalo = new Zalo({ selfListen: false, logging: false });
  const saved = loadCreds();

  let captured = null;
  if (saved) {
    logInfo('restoring saved Zalo session');
    api = await zalo.login({
      imei: saved.imei,
      cookie: saved.cookie,
      userAgent: saved.userAgent,
      language: saved.language,
    });
  } else {
    logInfo('no saved session — starting QR login');
    api = await zalo.loginQR(undefined, (event) => {
      if (!event) return;
      switch (event.type) {
        case QR_GENERATED: {
          const image = (event.data && event.data.image) || '';
          const dataUrl = image.startsWith('data:image')
            ? image
            : `data:image/png;base64,${image}`;
          emit({ kind: 'qr', qr: dataUrl, raw: (event.data && event.data.code) || '' });
          break;
        }
        case QR_SCANNED:
          logInfo('QR scanned — awaiting confirmation');
          break;
        case QR_EXPIRED:
          logInfo('QR expired — regenerating');
          try { event.actions && event.actions.retry && event.actions.retry(); } catch (e) { /* ignore */ }
          break;
        case QR_DECLINED:
          emit({ kind: 'error', error: 'QR login was declined on the phone' });
          break;
        case QR_GOT_LOGIN:
          captured = {
            imei: event.data && event.data.imei,
            cookie: event.data && event.data.cookie,
            userAgent: event.data && event.data.userAgent,
          };
          break;
        default:
          break;
      }
    });
  }

  // Session established — persist refreshed credentials and go live.
  saveCreds(snapshotCreds(api, captured));
  loginAttempt = 0;
  emit({ kind: 'ready' });
  logInfo('session ready — starting listener');
  startListener();

  // Re-persist rotated cookies periodically so a later restore keeps working.
  if (!persistTimer) {
    persistTimer = setInterval(() => {
      if (api) saveCreds(snapshotCreds(api, null));
    }, 10 * 60_000);
    if (persistTimer.unref) persistTimer.unref();
  }
}

function scheduleReconnect() {
  const delay = Math.min(30_000, 1_000 * Math.pow(2, Math.min(loginAttempt, 5)));
  logInfo(`reconnect scheduled in ${delay}ms`);
  setTimeout(() => { startZalo().catch((e) => logErr('reconnect', e)); }, delay);
}

function startListener() {
  const onMessage = (m) => {
    try {
      if (!m || m.isSelf) return;
      if (m.type === THREAD_GROUP) return; // DM-only in v1
      const data = m.data || {};
      const senderId = String(data.uidFrom || m.threadId || '');
      const text = extractText(data.content);
      if (!senderId || !text) return;
      const displayName = data.dName || senderId;
      emit({ kind: 'incoming', sender_id: senderId, display_name: displayName, text });
    } catch (e) {
      logErr('onMessage', e);
    }
  };
  const onError = (err) => {
    logErr('listener.error', err);
    emit({ kind: 'disconnected', logged_out: false });
    invalidateAndReconnect();
  };
  const onClosed = (code, reason) => {
    logInfo(`listener closed (${code}): ${reason || 'no reason'}`);
    emit({ kind: 'disconnected', logged_out: false });
    invalidateAndReconnect();
  };

  api.listener.on('message', onMessage);
  api.listener.on('error', onError);
  api.listener.on('closed', onClosed);
  api.listener.start({ retryOnClose: false });
}

let reconnecting = false;
function invalidateAndReconnect() {
  if (reconnecting) return;
  reconnecting = true;
  try { if (api && api.listener) api.listener.stop(); } catch (e) { /* ignore */ }
  api = null;
  scheduleReconnect();
  // Allow the next close/error to trigger another reconnect once this one lands.
  setTimeout(() => { reconnecting = false; }, 1000);
}

async function handleControl(msg) {
  if (!api) {
    emit({ kind: 'send_error', sender_id: msg.sender_id, error: 'sidecar not ready' });
    return;
  }
  if (msg.kind === 'send') {
    const threadId = String(msg.sender_id || '');
    const text = String(msg.text || '').slice(0, 2000);
    try {
      await api.sendMessage(text, threadId, THREAD_USER);
    } catch (e) {
      emit({ kind: 'send_error', sender_id: msg.sender_id, error: String((e && e.message) || e) });
    }
  } else if (msg.kind === 'typing') {
    const threadId = String(msg.sender_id || '');
    try {
      await api.sendTypingEvent(threadId, THREAD_USER);
    } catch (e) {
      // Non-fatal.
    }
  } else if (msg.kind === 'logout') {
    try { if (api && api.listener) api.listener.stop(); } catch (e) { /* ignore */ }
    try { fs.unlinkSync(credsFile); } catch (e) { /* ignore */ }
  }
}

const wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
wss.on('listening', () => {
  process.stdout.write(`WS_PORT=${wss.address().port}\n`);
});
wss.on('connection', (ws) => {
  if (connectedClient && connectedClient !== ws) {
    try { connectedClient.close(); } catch (e) { /* ignore */ }
  }
  connectedClient = ws;
  ws.on('message', async (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch (e) {
      emit({ kind: 'error', error: 'invalid JSON from parent' });
      return;
    }
    try {
      await handleControl(msg);
    } catch (e) {
      emit({ kind: 'error', error: String((e && e.message) || e) });
    }
  });
  ws.on('close', () => {
    if (connectedClient === ws) connectedClient = null;
  });
});

const shutdown = () => {
  try { if (api && api.listener) api.listener.stop(); } catch (e) { /* ignore */ }
  process.exit(0);
};
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

startZalo().catch((e) => {
  logErr('startup', e);
  emit({ kind: 'error', error: String((e && e.message) || e) });
  process.exit(1);
});
