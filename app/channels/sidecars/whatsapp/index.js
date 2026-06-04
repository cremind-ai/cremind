#!/usr/bin/env node
/*
 * Cremind WhatsApp sidecar.
 *
 * Spawned by the Python WhatsApp channel adapter
 * (app/channels/adapters/whatsapp.py). Maintains one WhatsApp Web session
 * via Baileys and bridges it to the adapter over a localhost WebSocket.
 *
 * Lifecycle:
 *   1. Parent passes --profile, --channel-id, --working-dir as argv.
 *   2. We open a WebSocket server on an ephemeral port and emit
 *      "WS_PORT=<port>" to stdout. Parent reads that line and connects.
 *   3. We initialise Baileys with `useMultiFileAuthState` rooted at
 *      <working-dir>/<profile>/whatsapp/<channel-id>/session/ so paired
 *      sessions survive restarts.
 *   4. Events flow parent <-> sidecar as JSON frames:
 *        sidecar -> parent:  {kind: "qr"|"ready"|"incoming"|"disconnected"
 *                             |"send_error"|"error", ...}
 *        parent -> sidecar:  {kind: "send", sender_id, text}
 *                            {kind: "logout"}
 *
 * Auth-state on disk is unique per (profile, channel-id) so multiple
 * profiles can each have their own WhatsApp without collision.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');

const minimist = require('minimist');
const { WebSocketServer } = require('ws');
const QRCode = require('qrcode');

const argv = minimist(process.argv.slice(2));
const profile = argv.profile || 'admin';
const channelId = argv['channel-id'] || 'default';
const workingDirArg = argv['working-dir'] || path.join(os.homedir(), '.cremind');
const workingDir = workingDirArg.startsWith('~')
  ? path.join(os.homedir(), workingDirArg.slice(1))
  : workingDirArg;
const sessionDir = path.join(workingDir, profile, 'whatsapp', channelId, 'session');

fs.mkdirSync(sessionDir, { recursive: true });

let connectedClient = null;
let sock = null;
let socketStartCount = 0;

function emit(payload) {
  if (connectedClient && connectedClient.readyState === 1) {
    try {
      connectedClient.send(JSON.stringify(payload));
    } catch (e) {
      // Client may have closed mid-send; nothing actionable.
    }
  }
}

function logErr(stage, err) {
  process.stderr.write(`[whatsapp-sidecar] ${stage}: ${err && (err.stack || err.message || err)}\n`);
}

function logInfo(line) {
  // stderr (not stdout) — the parent reads stdout for the WS_PORT handshake
  // and would mis-parse anything else there. The Python adapter tails
  // stderr and forwards each line to the Cremind logger, so anything we
  // write here is visible in the server logs prefixed with
  // ``whatsapp[<channel_id>] sidecar:``.
  process.stderr.write(`[whatsapp-sidecar] ${line}\n`);
}

async function startSocket() {
  // Late import — Baileys is heavy and we want any earlier failure (missing
  // node_modules) to surface as a plain require error before we open the WS.
  const baileys = require('@whiskeysockets/baileys');
  const makeWASocket = baileys.default;
  const { useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason } = baileys;
  const { Boom } = require('@hapi/boom');

  socketStartCount += 1;
  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  let version;
  try {
    ({ version } = await fetchLatestBaileysVersion());
  } catch (e) {
    // Latest-version fetch failed (offline?). Baileys will fall back to its
    // bundled default if we omit the version field.
    version = undefined;
  }

  sock = makeWASocket({
    auth: state,
    printQRInTerminal: false,
    ...(version ? { version } : {}),
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      logInfo('QR code emitted (awaiting scan)');
      try {
        const dataUrl = await QRCode.toDataURL(qr, { width: 320, margin: 1 });
        emit({ kind: 'qr', qr: dataUrl, raw: qr });
      } catch (e) {
        emit({ kind: 'qr', qr: null, raw: qr });
      }
    }
    if (connection === 'close') {
      const status = lastDisconnect && lastDisconnect.error
        && lastDisconnect.error.output && lastDisconnect.error.output.statusCode;
      const loggedOut = status === DisconnectReason.loggedOut;
      logInfo(`connection close (status=${status}, logged_out=${loggedOut})`);
      emit({ kind: 'disconnected', logged_out: !!loggedOut, status });
      if (!loggedOut) {
        // Reconnect with a small backoff to avoid hammering on repeated failures.
        const delay = Math.min(30_000, 1_000 * Math.pow(2, Math.min(socketStartCount, 5)));
        logInfo(`reconnect scheduled in ${delay}ms`);
        setTimeout(() => { startSocket().catch((e) => logErr('reconnect', e)); }, delay);
      }
    } else if (connection === 'open') {
      socketStartCount = 0;
      logInfo('connection open — paired and receiving');
      emit({ kind: 'ready' });
    } else if (connection === 'connecting') {
      logInfo('connecting…');
    }
  });

  sock.ev.on('messages.upsert', ({ messages, type }) => {
    logInfo(`messages.upsert type=${type} count=${(messages || []).length}`);
    if (type !== 'notify') return;
    for (const m of messages) {
      if (!m.key) continue;
      const remoteJid = m.key.remoteJid || '';
      const fromMe = !!m.key.fromMe;
      logInfo(`  msg jid=${remoteJid} fromMe=${fromMe} hasMessage=${!!m.message} pushName=${m.pushName || ''}`);
      if (fromMe) continue;
      // Skip non-DM JIDs:
      //   @g.us         → WhatsApp groups (not routed in v1)
      //   @broadcast    → broadcast lists / status updates
      //   @newsletter   → channels (the WhatsApp-product "Channels", not ours)
      if (
        remoteJid.endsWith('@g.us')
        || remoteJid.endsWith('@broadcast')
        || remoteJid.endsWith('@newsletter')
      ) {
        logInfo(`  skipped (non-DM JID class)`);
        continue;
      }

      // Unwrap protocol envelopes that hide the actual text payload.
      // ``ephemeralMessage`` wraps disappearing-mode messages;
      // ``viewOnceMessage`` / ``viewOnceMessageV2`` wrap one-shot media.
      let root = m.message || {};
      while (root && (root.ephemeralMessage || root.viewOnceMessage || root.viewOnceMessageV2)) {
        root = (root.ephemeralMessage && root.ephemeralMessage.message)
          || (root.viewOnceMessage && root.viewOnceMessage.message)
          || (root.viewOnceMessageV2 && root.viewOnceMessageV2.message)
          || {};
      }

      const text = root.conversation
        || (root.extendedTextMessage && root.extendedTextMessage.text)
        || (root.imageMessage && root.imageMessage.caption)
        || (root.videoMessage && root.videoMessage.caption)
        || '';
      if (!text) {
        const kinds = Object.keys(root || {});
        logInfo(`  skipped (no text payload; root kinds=[${kinds.join(',')}])`);
        continue;
      }
      logInfo(`  -> incoming sender=${remoteJid} text_len=${text.length}`);

      // Preserve the **full JID** as the sender id. Multi-device WhatsApp
      // exposes opaque ``<id>@lid`` identifiers for some contacts; stripping
      // the suffix and treating the digits as a phone number caused replies
      // to go to whatever real phone matched those digits. The full JID is
      // what ``sock.sendMessage`` expects, so a round-trip via the same
      // sender_id always reaches the same conversation.
      const senderId = remoteJid;
      const displayName = m.pushName || remoteJid.split('@')[0] || senderId;
      emit({
        kind: 'incoming',
        sender_id: senderId,
        display_name: displayName,
        text,
      });
    }
  });
}

async function handleControl(msg) {
  if (!sock) {
    emit({ kind: 'send_error', sender_id: msg.sender_id, error: 'sidecar not ready' });
    return;
  }
  if (msg.kind === 'send') {
    const senderId = String(msg.sender_id || '');
    const jid = senderId.includes('@') ? senderId : `${senderId}@s.whatsapp.net`;
    try {
      await sock.sendMessage(jid, { text: String(msg.text || '') });
    } catch (e) {
      emit({ kind: 'send_error', sender_id: msg.sender_id, error: String(e && e.message || e) });
    }
  } else if (msg.kind === 'typing') {
    const senderId = String(msg.sender_id || '');
    const jid = senderId.includes('@') ? senderId : `${senderId}@s.whatsapp.net`;
    try {
      await sock.sendPresenceUpdate('composing', jid);
    } catch (e) {
      // Non-fatal.
    }
  } else if (msg.kind === 'logout') {
    try {
      if (sock) await sock.logout();
    } catch (e) {
      // Ignore — caller is going to terminate us anyway.
    }
  }
}

(async () => {
  const wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  wss.on('listening', () => {
    const port = wss.address().port;
    process.stdout.write(`WS_PORT=${port}\n`);
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
        emit({ kind: 'error', error: String(e && e.message || e) });
      }
    });
    ws.on('close', () => {
      if (connectedClient === ws) connectedClient = null;
    });
  });

  // Graceful shutdown when the parent goes away.
  const shutdown = () => {
    try { if (sock) sock.end && sock.end(); } catch (e) { /* ignore */ }
    process.exit(0);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);

  try {
    await startSocket();
  } catch (e) {
    logErr('startup', e);
    emit({ kind: 'error', error: String(e && e.message || e) });
    process.exit(1);
  }
})();
