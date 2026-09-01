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
 *        sidecar -> parent:  {kind: "qr"|"ready"|"incoming"|"incoming_group"
 *                             |"disconnected"|"send_ack"|"send_error"
 *                             |"resolve_result"|"error", ...}
 *        parent -> sidecar:  {kind: "send", sender_id, text, request_id?}
 *                            {kind: "send_file", sender_id, path, name?, mime?,
 *                             caption?, request_id?}
 *                            {kind: "resolve", phone, request_id}
 *                            {kind: "logout"}
 *
 *      "incoming" is a 1:1 message and "incoming_group" one written in a
 *      @g.us room; they are separate kinds because a room message belongs to
 *      the room, not to whoever sent it, and the parent routes it into a group
 *      timeline instead of a per-sender conversation.
 *
 *      Media never rides the WebSocket (its frames are capped at 4 MiB).
 *      Inbound media is downloaded AT RECEIPT into --media-dir (Baileys'
 *      media keys are only reliably usable near the event) and the frame
 *      carries {files: [{path, name, mime, size}]}; the parent moves or
 *      deletes the spooled file. Outbound "send_file" carries the file's
 *      absolute path — parent and sidecar share the filesystem by design.
 *
 *      A "send"/"send_file" carrying a request_id is answered with exactly one
 *      {kind: "send_ack", request_id} or {kind: "send_error", request_id, error}
 *      so the parent can await real delivery to WhatsApp's servers instead of
 *      assuming a write to this socket succeeded. Sends without a request_id
 *      keep the old fire-and-forget behaviour (errors are still reported).
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
// Where inbound media is spooled for the parent to claim (see the frame notes
// above). The parent wipes and recreates it on every spawn.
const mediaDir = argv['media-dir']
  || path.join(workingDir, profile, 'whatsapp', channelId, 'media_spool');
const mediaMaxBytes = Number(argv['media-max-bytes']) > 0
  ? Number(argv['media-max-bytes'])
  : 100 * 1024 * 1024;

fs.mkdirSync(sessionDir, { recursive: true });
fs.mkdirSync(mediaDir, { recursive: true });

let connectedClient = null;
let sock = null;
let socketStartCount = 0;
// group JID -> subject, populated in the background by groupSubject().
const groupSubjects = new Map();

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

function bareJid(value) {
  // Drop the ``:<device>`` suffix WhatsApp appends to a JID's local part. It
  // changes every time the account links a device, so two ids for one person
  // would never compare equal and the parent would read our own mirror as a
  // stranger's message.
  const jid = String(value || '').trim();
  if (!jid.includes('@')) return '';
  const [local, domain] = jid.split('@');
  return `${local.split(':')[0]}@${domain}`;
}

function selfIdentity() {
  // Which account this linked device is. WhatsApp flags nothing as
  // bot-authored, so a bound room can only recognise the mirrors we post
  // ourselves by our own ids — without these the agent answers its own answer.
  const user = (sock && sock.user) || {};
  const out = {};
  const digits = String(user.id || '').split('@')[0].split(':')[0].replace(/[^0-9]/g, '');
  if (digits) out.self_id = digits;
  const lid = bareJid(user.lid);
  if (lid) out.self_lid = lid;
  // The pushName: the only thing a group shows above our messages, and so the
  // only name another member can address this account by.
  const name = String(user.name || user.verifiedName || '').trim();
  if (name) out.self_name = name;
  return out;
}

function unwrapEnvelopes(message) {
  // Protocol envelopes that hide the actual text payload:
  // ``ephemeralMessage`` wraps disappearing-mode messages,
  // ``viewOnceMessage`` / ``viewOnceMessageV2`` wrap one-shot media, and
  // ``documentWithCaptionMessage`` wraps a captioned document.
  let root = message || {};
  while (root && (root.ephemeralMessage || root.viewOnceMessage
    || root.viewOnceMessageV2 || root.documentWithCaptionMessage)) {
    root = (root.ephemeralMessage && root.ephemeralMessage.message)
      || (root.viewOnceMessage && root.viewOnceMessage.message)
      || (root.viewOnceMessageV2 && root.viewOnceMessageV2.message)
      || (root.documentWithCaptionMessage && root.documentWithCaptionMessage.message)
      || {};
  }
  return root || {};
}

function messageText(root) {
  return root.conversation
    || (root.extendedTextMessage && root.extendedTextMessage.text)
    || (root.imageMessage && root.imageMessage.caption)
    || (root.videoMessage && root.videoMessage.caption)
    || (root.documentMessage && root.documentMessage.caption)
    || '';
}

// mime -> filename extension for synthesized media names. Best-effort: an
// unknown mime just gets no extension and the parent's tools sniff it.
const EXT_BY_MIME = {
  'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
  'image/gif': '.gif', 'video/mp4': '.mp4', 'video/3gpp': '.3gp',
  'audio/ogg': '.ogg', 'audio/mpeg': '.mp3', 'audio/mp4': '.m4a',
  'audio/aac': '.aac', 'audio/wav': '.wav', 'application/pdf': '.pdf',
};

function extForMime(mime) {
  const bare = String(mime || '').split(';')[0].trim().toLowerCase();
  return EXT_BY_MIME[bare] || '';
}

function mediaNode(root) {
  // The one media payload a message carries, if any. Stickers are skipped on
  // purpose — people use them as reactions, not as files.
  if (root.documentMessage) {
    const node = root.documentMessage;
    return {
      node,
      name: String(node.fileName || '').trim()
        || `document${extForMime(node.mimetype)}`,
      mime: node.mimetype || null,
    };
  }
  if (root.imageMessage) {
    const node = root.imageMessage;
    return { node, name: `image${extForMime(node.mimetype) || '.jpg'}`, mime: node.mimetype || 'image/jpeg' };
  }
  if (root.videoMessage) {
    const node = root.videoMessage;
    return { node, name: `video${extForMime(node.mimetype) || '.mp4'}`, mime: node.mimetype || 'video/mp4' };
  }
  if (root.audioMessage) {
    const node = root.audioMessage;
    return { node, name: `audio${extForMime(node.mimetype) || '.ogg'}`, mime: node.mimetype || 'audio/ogg' };
  }
  return null;
}

// Minimal pino-shaped logger for downloadMediaMessage's context arg — it only
// ever logs; a missing method there must not cost us the download.
const noopLogger = {
  level: 'silent',
  trace() {}, debug() {}, info() {}, warn() {}, error() {}, fatal() {},
  child() { return noopLogger; },
};

async function spoolIncomingMedia(m, root) {
  // Download a message's media into the spool NOW — Baileys' media keys are
  // only reliably usable near receipt — and hand the parent the path. Returns
  // [] for no media, over-cap media, or a failed download: the message itself
  // still flows, just without its file.
  const media = mediaNode(root);
  if (!media) return [];
  const declared = Number(media.node.fileLength);
  if (Number.isFinite(declared) && declared > mediaMaxBytes) {
    logInfo(`  media skipped (declared ${declared} bytes > cap ${mediaMaxBytes})`);
    return [];
  }
  try {
    const { downloadMediaMessage } = require('@whiskeysockets/baileys');
    const buffer = await downloadMediaMessage(
      m, 'buffer', {},
      { logger: noopLogger, reuploadRequest: sock.updateMediaMessage },
    );
    if (!buffer || !buffer.length) {
      logInfo('  media skipped (empty download)');
      return [];
    }
    if (buffer.length > mediaMaxBytes) {
      logInfo(`  media skipped (downloaded ${buffer.length} bytes > cap ${mediaMaxBytes})`);
      return [];
    }
    const safeName = path.basename(media.name).replace(/[\\/:*?"<>|]/g, '_') || 'file';
    const spoolName = `${Date.now()}_${((m.key && m.key.id) || 'msg').replace(/[^A-Za-z0-9_-]/g, '')}_${safeName}`;
    const spoolPath = path.join(mediaDir, spoolName);
    fs.writeFileSync(spoolPath, buffer);
    logInfo(`  media spooled ${spoolName} (${buffer.length} bytes)`);
    return [{ path: spoolPath, name: safeName, mime: media.mime, size: buffer.length }];
  } catch (e) {
    logInfo(`  media download failed: ${e && (e.message || e)}`);
    return [];
  }
}

function groupSubject(jid) {
  // The room's name, cached per JID because groupMetadata is a network round
  // trip and a busy room would otherwise make one per message. Deliberately
  // never awaited: the title is decoration the parent already has from the
  // binding, and a slow or failing lookup must not cost us the message. The
  // first message from a room therefore reports no title; the next one does.
  if (groupSubjects.has(jid)) return groupSubjects.get(jid);
  groupSubjects.set(jid, null);
  Promise.resolve()
    .then(() => sock.groupMetadata(jid))
    .then((meta) => { groupSubjects.set(jid, (meta && meta.subject) || null); })
    .catch((e) => {
      // Drop the claim so a later message retries instead of caching the miss.
      groupSubjects.delete(jid);
      logInfo(`group subject lookup failed for ${jid}: ${e && (e.message || e)}`);
    });
  return null;
}

function mentionContext(root) {
  // Who this message pings, and whom it quotes. WhatsApp carries both as
  // structured annotations rather than in the text: ``@1555…`` renders as a
  // mention but the text the parent receives is just the digits, and a quote is
  // nowhere in the text at all. Without these the agent could be addressed
  // directly and never know.
  const ctx = (root.extendedTextMessage && root.extendedTextMessage.contextInfo)
    || (root.imageMessage && root.imageMessage.contextInfo)
    || (root.videoMessage && root.videoMessage.contextInfo)
    || null;
  if (!ctx) return { mentionedIds: [], quotedSenderId: null };
  const mentionedIds = [];
  for (const jid of ctx.mentionedJid || []) {
    const bare = bareJid(jid);
    if (bare && !mentionedIds.includes(bare)) mentionedIds.push(bare);
  }
  return { mentionedIds, quotedSenderId: bareJid(ctx.participant) || null };
}

function groupSenderIds(m) {
  // In a group ``remoteJid`` is the ROOM, so the sender has to come from the
  // participant fields — reading it off remoteJid the way the DM path does
  // would collapse every human in the room into one identity.
  //
  // Which form WhatsApp reports depends on the account's privacy settings, so
  // every one seen is handed to the parent. The phone JID leads when there is
  // one: it is the only form carrying a number a person can recognise, and the
  // parent resolves an @lid-only participant through the alternates.
  const seen = [];
  for (const candidate of [
    m.key.participantPn, m.key.participant, m.participant, m.key.participantLid,
  ]) {
    const jid = bareJid(candidate);
    if (jid && !seen.includes(jid)) seen.push(jid);
  }
  const primary = seen.find((jid) => jid.endsWith('@s.whatsapp.net')) || seen[0] || '';
  return { primary, alts: seen.filter((jid) => jid !== primary) };
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
      emit({ kind: 'ready', ...selfIdentity() });
    } else if (connection === 'connecting') {
      logInfo('connecting…');
    }
  });

  sock.ev.on('groups.upsert', (groups) => {
    // Fired when this account joins (or is added to) a group. WhatsApp gives no
    // "you were added" event of its own, so this is the closest thing, and it
    // is what lets a group reach the operator for approval before anybody has
    // spoken in it.
    for (const g of groups || []) {
      if (!g || !g.id) continue;
      if (g.subject) groupSubjects.set(g.id, g.subject);
      logInfo(`  -> group_joined chat=${g.id}`);
      emit({
        kind: 'group_joined',
        chat_id: g.id,
        chat_title: g.subject || null,
      });
    }
  });

  sock.ev.on('group-participants.update', (update) => {
    // The other way in: somebody adds this number to an existing group. Only an
    // 'add' naming US counts — every other participant change is somebody
    // else's business.
    try {
      if (!update || update.action !== 'add') return;
      const ownJid = bareJid(sock.user && sock.user.id);
      const ownLid = bareJid(sock.user && sock.user.lid);
      const added = (update.participants || []).map(bareJid);
      if (!added.some((jid) => jid && (jid === ownJid || jid === ownLid))) return;
      logInfo(`  -> group_joined (participant add) chat=${update.id}`);
      emit({
        kind: 'group_joined',
        chat_id: update.id,
        chat_title: groupSubjects.get(update.id) || null,
      });
    } catch (e) {
      logInfo(`group-participants.update failed: ${e && (e.message || e)}`);
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    logInfo(`messages.upsert type=${type} count=${(messages || []).length}`);
    if (type !== 'notify') return;
    for (const m of messages) {
      if (!m.key) continue;
      const remoteJid = m.key.remoteJid || '';
      const fromMe = !!m.key.fromMe;
      logInfo(`  msg jid=${remoteJid} fromMe=${fromMe} hasMessage=${!!m.message} pushName=${m.pushName || ''}`);
      if (fromMe) continue;
      // Nobody is conversing in these, so they are dropped outright:
      //   @broadcast    → broadcast lists / status updates
      //   @newsletter   → channels (the WhatsApp-product "Channels", not ours)
      if (remoteJid.endsWith('@broadcast') || remoteJid.endsWith('@newsletter')) {
        logInfo(`  skipped (non-conversational JID class)`);
        continue;
      }

      const root = unwrapEnvelopes(m.message);
      const text = messageText(root);
      const files = await spoolIncomingMedia(m, root);
      if (!text && files.length === 0) {
        const kinds = Object.keys(root || {});
        logInfo(`  skipped (no text or media payload; root kinds=[${kinds.join(',')}])`);
        continue;
      }

      if (remoteJid.endsWith('@g.us')) {
        const { primary, alts } = groupSenderIds(m);
        if (!primary) {
          logInfo(`  skipped (group message with no resolvable participant)`);
          continue;
        }
        const timestamp = Number(m.messageTimestamp);
        const { mentionedIds, quotedSenderId } = mentionContext(root);
        logInfo(`  -> incoming_group chat=${remoteJid} sender=${primary} text_len=${text.length} files=${files.length}`);
        emit({
          kind: 'incoming_group',
          chat_id: remoteJid,
          chat_title: groupSubject(remoteJid),
          sender_id: primary,
          sender_alt_ids: alts,
          display_name: m.pushName || null,
          message_id: (m.key && m.key.id) || null,
          timestamp: Number.isFinite(timestamp) ? timestamp : null,
          mentioned_ids: mentionedIds,
          quoted_sender_id: quotedSenderId,
          text,
          files,
        });
        continue;
      }
      logInfo(`  -> incoming sender=${remoteJid} text_len=${text.length} files=${files.length}`);

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
        files,
      });
    }
  });
}

async function handleControl(msg) {
  if (!sock) {
    // Answer on the same correlation id the caller is waiting on, so a
    // request never hangs until its timeout just because we aren't paired yet.
    const kind = msg.kind === 'resolve'
      ? 'resolve_result'
      : (msg.kind === 'group_metadata' ? 'group_metadata_result' : 'send_error');
    emit({
      kind,
      request_id: msg.request_id,
      sender_id: msg.sender_id,
      ok: false,
      error: 'sidecar not ready',
    });
    return;
  }
  if (msg.kind === 'send') {
    const senderId = String(msg.sender_id || '');
    const jid = senderId.includes('@') ? senderId : `${senderId}@s.whatsapp.net`;
    try {
      await sock.sendMessage(jid, { text: String(msg.text || '') });
      if (msg.request_id) {
        emit({ kind: 'send_ack', request_id: msg.request_id, sender_id: msg.sender_id, ok: true });
      }
    } catch (e) {
      emit({
        kind: 'send_error',
        request_id: msg.request_id,
        sender_id: msg.sender_id,
        error: String(e && e.message || e),
      });
    }
  } else if (msg.kind === 'send_file') {
    // The frame carries a PATH (never bytes — the WS caps frames at 4 MiB);
    // parent and sidecar share the filesystem by design. The content shape is
    // picked by mime so a photo lands as a photo and everything else as a
    // document with its filename intact. ``{url: <path>}`` streams from disk,
    // so a big file never has to fit in memory here.
    const senderId = String(msg.sender_id || '');
    const jid = senderId.includes('@') ? senderId : `${senderId}@s.whatsapp.net`;
    try {
      const filePath = String(msg.path || '');
      if (!filePath || !fs.existsSync(filePath)) {
        throw new Error(`file not found: ${filePath}`);
      }
      const name = String(msg.name || path.basename(filePath) || 'file');
      const mime = String(msg.mime || '') || 'application/octet-stream';
      const caption = msg.caption ? String(msg.caption) : undefined;
      let content;
      if (mime.startsWith('image/') && mime !== 'image/svg+xml') {
        content = { image: { url: filePath }, caption };
      } else if (mime.startsWith('video/')) {
        content = { video: { url: filePath }, caption, mimetype: mime };
      } else if (mime.startsWith('audio/')) {
        content = { audio: { url: filePath }, mimetype: mime };
      } else {
        content = { document: { url: filePath }, fileName: name, mimetype: mime, caption };
      }
      await sock.sendMessage(jid, content);
      if (msg.request_id) {
        emit({ kind: 'send_ack', request_id: msg.request_id, sender_id: msg.sender_id, ok: true });
      }
    } catch (e) {
      emit({
        kind: 'send_error',
        request_id: msg.request_id,
        sender_id: msg.sender_id,
        error: String(e && e.message || e),
      });
    }
  } else if (msg.kind === 'resolve') {
    // Does this phone number have a WhatsApp account, and what is its
    // canonical JID? ``onWhatsApp`` runs a USync query that also returns the
    // contact's ``@lid`` alias — the identity multi-device WhatsApp may use
    // when they reply — so the parent can record both and avoid forking a
    // second sender row later.
    const phone = String(msg.phone || '').replace(/[^0-9]/g, '');
    try {
      const rows = await sock.onWhatsApp(`${phone}@s.whatsapp.net`);
      const hit = (rows || [])[0];
      emit({
        kind: 'resolve_result',
        request_id: msg.request_id,
        ok: true,
        phone,
        exists: !!(hit && hit.exists),
        jid: (hit && hit.jid) || null,
        lid: (hit && hit.lid) || null,
      });
    } catch (e) {
      emit({
        kind: 'resolve_result',
        request_id: msg.request_id,
        ok: false,
        phone,
        error: String(e && e.message || e),
      });
    }
  } else if (msg.kind === 'list_groups') {
    // Every group this number is in. One call — Baileys keeps the list on the
    // socket — so unlike the other platforms there is no batching to do.
    try {
      const all = await sock.groupFetchAllParticipating();
      const groups = Object.values(all || {}).map((g) => {
        if (g && g.id && g.subject) groupSubjects.set(g.id, g.subject);
        return {
          id: (g && g.id) || '',
          name: (g && g.subject) || null,
          member_count: ((g && g.participants) || []).length || null,
        };
      }).filter((g) => g.id);
      emit({ kind: 'list_groups_result', request_id: msg.request_id, ok: true, groups });
    } catch (e) {
      emit({
        kind: 'list_groups_result',
        request_id: msg.request_id,
        ok: false,
        error: String(e && e.message || e),
      });
    }
  } else if (msg.kind === 'group_metadata') {
    // The group's participant list. Correlated like ``resolve`` because the
    // parent awaits it: a roster refresh is a request/response, not a stream.
    const chatId = String(msg.chat_id || '');
    try {
      const meta = await sock.groupMetadata(chatId);
      if (meta && meta.subject) groupSubjects.set(chatId, meta.subject);
      emit({
        kind: 'group_metadata_result',
        request_id: msg.request_id,
        ok: true,
        chat_id: chatId,
        subject: (meta && meta.subject) || null,
        participants: ((meta && meta.participants) || []).map((p) => ({
          id: bareJid(p.id),
          lid: bareJid(p.lid),
          // Baileys reports 'admin' | 'superadmin' | undefined.
          admin: p.admin || null,
        })),
      });
    } catch (e) {
      emit({
        kind: 'group_metadata_result',
        request_id: msg.request_id,
        ok: false,
        chat_id: chatId,
        error: String(e && e.message || e),
      });
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
