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
 *        sidecar -> parent:  {kind:"qr"|"ready"|"incoming"|"incoming_group"
 *                             |"disconnected"|"send_error"|"send_file_result"
 *                             |"error", ...}
 *        parent -> sidecar:  {kind:"send", sender_id, text, thread_type?}
 *                            {kind:"send_file", sender_id, path, name?,
 *                             caption?, thread_type?, request_id}
 *                            {kind:"typing", sender_id, thread_type?}
 *                            {kind:"logout"}
 *
 * `sender_id` carries a thread id, which is a person's on a DM and a room's in
 * a group; `thread_type` (default THREAD_USER) is the only thing that says
 * which, so a room send without it would be delivered to whoever owns that id.
 *
 * Media never rides the WebSocket (4 MiB frame cap). Inbound media is
 * downloaded AT RECEIPT into --media-dir and the frame carries
 * {files: [{path, name, mime, size}]}; the parent moves or deletes the
 * spooled file. Which url is fetched, and how often, is media.js's business —
 * including asking Zalo's CDN edges directly when the file router refuses this
 * host's network, which is what a server on a hosting provider gets.
 * A media message whose download FAILS still flows: its frame carries a
 * bracketed notice in `text` instead of the file, because a caption-less photo
 * that could not be fetched has neither and the parent would drop it — which
 * is how a photo posted to a bound room reached nobody at all. Outbound
 * "send_file" carries an absolute path — parent and sidecar share the
 * filesystem by design — and is answered with exactly one correlated
 * {kind:"send_file_result", request_id, ok, error?}.
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

import {
  attemptTrail,
  downloadMedia,
  mediaFailureNotice,
  mediaFromMessage,
  thumbnailNotice,
} from './media.js';

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
// Inbound-media spool (see the frame notes above). Parent wipes it per spawn.
const mediaDir = argv['media-dir'] || path.join(sessionDir, 'media_spool');
const mediaMaxBytes = Number(argv['media-max-bytes']) > 0
  ? Number(argv['media-max-bytes'])
  : 100 * 1024 * 1024;

fs.mkdirSync(sessionDir, { recursive: true });
fs.mkdirSync(mediaDir, { recursive: true });

let connectedClient = null;
let api = null;
let loginAttempt = 0;
let persistTimer = null;
// threadId -> room name. Only names we actually resolved are kept, so a lookup
// that failed (or a room renamed since) is retried on the next message rather
// than remembered as nameless forever.
const groupTitles = new Map();

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

function ownId() {
  // Which Zalo account this session logged in as. The parent stores it so a
  // bound room can tell our own posts from a member's. Both accessors are tried
  // because zca-js has moved this between the API surface and the context.
  try {
    if (api && api.getOwnId) {
      const uid = api.getOwnId();
      if (uid) return String(uid);
    }
  } catch (e) {
    // Fall through to the context below.
  }
  try {
    const ctx = api && api.getContext ? api.getContext() : null;
    return String((ctx && ctx.uid) || '');
  } catch (e) {
    return '';
  }
}

function groupTitle(threadId) {
  // The room's name, cached per thread because getGroupInfo is a network round
  // trip. Deliberately never awaited: the listener is an EventEmitter, so
  // suspending here lets the next message's handler emit first and the room's
  // timeline records the two in whichever order the lookups happened to
  // return. The title is decoration the parent already has from the binding,
  // so the first message from a room reports none and the next one does.
  if (groupTitles.has(threadId)) return groupTitles.get(threadId);
  groupTitles.set(threadId, null);
  Promise.resolve()
    .then(() => api.getGroupInfo(threadId))
    .then((info) => {
      const entry = (info && info.gridInfoMap && info.gridInfoMap[threadId])
        || (info && info.groupInfo)
        || null;
      groupTitles.set(threadId, (entry && (entry.name || entry.groupName)) || null);
    })
    .catch((e) => {
      // Drop the claim so a later message retries instead of caching the miss.
      groupTitles.delete(threadId);
      logInfo(`group title lookup failed for ${threadId}: ${e && (e.message || e)}`);
    });
  return null;
}

function extractText(content) {
  if (typeof content === 'string') return content;
  if (content && typeof content === 'object') {
    return content.title || content.description || content.href || '';
  }
  return '';
}

async function mediaHeadersFor(url) {
  // What zca-js sends on every request it makes itself (utils.js
  // getDefaultHeaders), minus its JSON Accept. A bare Node fetch presents no
  // Referer and a User-Agent of "node", which is exactly what a hotlink filter
  // is built to turn away — and such a host answers 404 as readily as 403.
  // No Cookie: the jar only holds zalo.me cookies, so a CDN host would get
  // none anyway (and undici drops Cookie across a cross-origin redirect, so a
  // hand-set one would not survive the hop to the CDN in any case).
  const headers = {
    Accept: 'image/*,video/*,audio/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    Origin: 'https://chat.zalo.me',
    Referer: 'https://chat.zalo.me/',
  };
  try {
    const ctx = api && api.getContext ? api.getContext() : null;
    if (ctx && ctx.userAgent) headers['User-Agent'] = ctx.userAgent;
  } catch (e) {
    // Mid-reconnect there is no context; the other headers still stand.
  }
  return headers;
}

async function spoolIncomingMedia(media) {
  // Download a media message's payload into the spool NOW (the CDN URLs are
  // session-scoped) and hand the parent the path. Returns {files, failure,
  // degraded}: `files` is empty for a download that never landed, and
  // `failure` then says why so the caller can tell the agent — losing the
  // message instead was the bug this replaced.
  const none = { files: [], failure: null, degraded: false };
  if (!media) return none;
  try {
    const result = await downloadMedia(media, {
      maxBytes: mediaMaxBytes,
      headersFor: mediaHeadersFor,
    });
    const trail = attemptTrail(result.attempts);
    if (!result.ok) {
      if (result.capped) {
        // A cap we chose, not a failure to report: the sender can see the file
        // arrived at Zalo, and re-sending it smaller is the only fix.
        logInfo(`media skipped (${result.size} bytes > cap ${mediaMaxBytes}) for ${media.name}`);
        return none;
      }
      // Everything needed to reproduce this with curl from the same host, and
      // to tell the three causes apart: which client sent it, how old the
      // message was when we fetched, what each attempt answered, which fields
      // the message offered, and what keys its params really had.
      logInfo(
        `media download failed for ${media.name}: msgType=${media.msgType}`
        + ` platform=${media.platformType} ageMs=${media.ts ? Date.now() - media.ts : null}`
        + ` status=${result.status}${result.budgetSpent ? ' budget=spent' : ''}`
        + ` attempts=${trail} present=${result.present.join(',')}`
        + ` params_keys=${result.paramKeys.join(',')} urls=${result.urls.join(' ')}`,
      );
      return { files: [], failure: result, degraded: false };
    }
    const safeName = path.basename(media.name).replace(/[\\/:*?"<>|]/g, '_') || 'file';
    const spoolName = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}_${safeName}`;
    const spoolPath = path.join(mediaDir, spoolName);
    fs.writeFileSync(spoolPath, result.buffer);
    // `platform` on the success line too: the first PC-app photo that works is
    // the comparison the failing one never had.
    const via = result.attempts.length > 1 ? ` via ${result.source} after ${trail}` : '';
    logInfo(
      `media spooled ${spoolName} (${result.buffer.length} bytes)`
      + ` platform=${media.platformType}${via}`,
    );
    return {
      files: [{ path: spoolPath, name: safeName, mime: result.mime, size: result.buffer.length }],
      failure: null,
      degraded: Boolean(result.degraded),
    };
  } catch (e) {
    // downloadMedia does not throw and the spool write is the only thing left
    // that can; catching here keeps the notice path unconditional.
    logErr('spoolIncomingMedia', e);
    return { files: [], failure: { status: `error:${e && (e.message || e)}` }, degraded: false };
  }
}

function withTimeout(promise, ms, message) {
  // zca-js's cookie login can sit on a dead session without ever settling, and
  // a login that never resolves emits nothing at all — the pairing dialog then
  // waits on a flow that was never started. Bound it so a stuck restore fails
  // like a rejected one and reaches the QR fallback below.
  let timer = null;
  return Promise.race([
    promise.finally(() => { if (timer) clearTimeout(timer); }),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), ms);
    }),
  ]);
}

const LOGIN_TIMEOUT_MS = 60_000;

async function startZalo() {
  loginAttempt += 1;
  // `selfListen: true` is load-bearing for group discovery, not a debugging
  // knob. zca-js computes `isSelf` for a group event from whether the changed
  // members include us, then drops the event when `isSelf && !selfListen` — so
  // "you were added to a group", the one event this feature is built on, is
  // exactly the one it filters out. Our own MESSAGES are still ignored, by the
  // `m.isSelf` guard in `onMessage` below (and by the echo check in Python).
  const zalo = new Zalo({ selfListen: true, logging: false });
  const saved = loadCreds();

  let captured = null;
  if (saved) {
    // A saved session that Zalo has since invalidated — the account paired in
    // another environment, most often — is indistinguishable on disk from a
    // good one. Restoring it fails, and because the QR callback lives only on
    // the other branch, failing here used to mean no QR was ever produced and
    // the pairing dialog waited forever. Treat a failed restore as proof the
    // session is dead: drop it and pair from scratch.
    try {
      logInfo('restoring saved Zalo session');
      api = await withTimeout(
        zalo.login({
          imei: saved.imei,
          cookie: saved.cookie,
          userAgent: saved.userAgent,
          language: saved.language,
        }),
        LOGIN_TIMEOUT_MS,
        `saved session did not restore within ${LOGIN_TIMEOUT_MS}ms`,
      );
    } catch (e) {
      logErr('login (saved session)', e);
      api = null;
      try {
        fs.unlinkSync(credsFile);
        logInfo('saved session rejected — credentials cleared, pairing again');
      } catch (unlinkErr) {
        logErr('clearing rejected credentials', unlinkErr);
      }
    }
  }
  if (!api) {
    logInfo('no usable saved session — starting QR login');
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
  emit({ kind: 'ready', self_id: ownId() });
  logInfo('session ready — starting listener');
  startListener();
  announceSelfName();

  // Re-persist rotated cookies periodically so a later restore keeps working.
  if (!persistTimer) {
    persistTimer = setInterval(() => {
      if (api) saveCreds(snapshotCreds(api, null));
    }, 10 * 60_000);
    if (persistTimer.unref) persistTimer.unref();
  }
}

function announceSelfName() {
  // The name this account shows above its own messages in a group. Zalo has no
  // usernames and no typeable mention token, so this is the ONLY handle another
  // member can address the agent by — without it the agent reads "Lý Nguyen,
  // what time is it?" as a question for somebody else.
  //
  // Deliberately after `ready` and never awaited: it is one more network call
  // on a session that is already live, and a slow or failed profile lookup must
  // not delay (or fail) pairing.
  Promise.resolve()
    .then(() => api && api.fetchAccountInfo())
    .then((info) => {
      const profile = (info && info.profile) || {};
      const name = String(profile.displayName || profile.zaloName || '').trim();
      if (name) emit({ kind: 'self_info', self_name: name });
    })
    .catch((e) => logInfo(`fetchAccountInfo failed: ${e && (e.message || e)}`));
}

function scheduleReconnect() {
  const delay = Math.min(30_000, 1_000 * Math.pow(2, Math.min(loginAttempt, 5)));
  logInfo(`reconnect scheduled in ${delay}ms`);
  setTimeout(() => { startZalo().catch((e) => logErr('reconnect', e)); }, delay);
}

function startListener() {
  const onMessage = async (m) => {
    try {
      if (!m || m.isSelf) return;
      const data = m.data || {};
      const media = mediaFromMessage(data, logInfo);
      const spooled = media ? await spoolIncomingMedia(media) : null;
      const files = spooled ? spooled.files : [];
      // For a media message the old extractText would surface content.title —
      // the FILENAME on a share.file — or the raw CDN href as "text". With the
      // file itself flowing, the text is only what the sender actually typed:
      // a photo/video caption (title), or nothing.
      let text;
      if (media) {
        text = media.isFile ? '' : String((data.content && data.content.title) || '');
        // A download that failed must never be silent. The notice is what
        // carries this message past the empty-message guard below (and the
        // parent's own), so the agent can tell the sender their photo is not
        // here rather than answering a message it never saw.
        const notice = spooled.failure
          ? mediaFailureNotice(media, spooled.failure)
          : (spooled.degraded ? thumbnailNotice(media) : '');
        if (notice) text = text ? `${text}\n${notice}` : notice;
      } else {
        text = extractText(data.content);
      }
      if (!text && files.length === 0) return;
      if (m.type === THREAD_GROUP) {
        // A room message is addressed to the room. It used to be dropped here,
        // which is why a personal account could never carry a bound group.
        const chatId = String(m.threadId || '');
        const groupSenderId = String(data.uidFrom || '');
        if (!chatId || !groupSenderId) return;
        emit({
          kind: 'incoming_group',
          chat_id: chatId,
          chat_title: groupTitle(chatId),
          sender_id: groupSenderId,
          display_name: data.dName || groupSenderId,
          message_id: String(data.msgId || data.cliMsgId || '') || null,
          // Zalo stamps milliseconds; the parent's dedupe key wants seconds.
          timestamp: Number(data.ts) / 1000,
          // A Zalo mention is a structured annotation, never text, so the
          // parent cannot find it by reading the message. Same for a quote.
          mentioned_ids: (data.mentions || [])
            .map((mention) => String((mention && mention.uid) || ''))
            .filter(Boolean),
          quoted_sender_id: (data.quote && String(data.quote.ownerId || '')) || null,
          text,
          files,
        });
        return;
      }
      const senderId = String(data.uidFrom || m.threadId || '');
      if (!senderId) return;
      const displayName = data.dName || senderId;
      emit({ kind: 'incoming', sender_id: senderId, display_name: displayName, text, files });
    } catch (e) {
      logErr('onMessage', e);
    }
  };
  const onGroupEvent = async (event) => {
    // zca-js reports group membership changes here as a GroupEventType (see
    // its models/GroupEvent.d.ts). Only three concern us, and they are matched
    // EXACTLY: an earlier `includes('join')` also caught `join_request`, which
    // is somebody else asking to join a group we are already in.
    try {
      const type = String((event && event.type) || '').toLowerCase();
      const isJoin = type === 'join';
      const isLeave = type === 'leave' || type === 'remove_member';
      if (!isJoin && !isLeave) {
        if (type && type !== 'unknown') logInfo(`group_event ignored: ${type}`);
        return;
      }
      const data = (event && event.data) || {};
      const threadId = String((event && event.threadId) || data.groupId || '');
      if (!threadId) return;
      const own = String((api && api.getOwnId && api.getOwnId()) || '');
      const members = []
        .concat(data.updateMembers || [], data.memberIds || [], data.uids || [])
        .map((m) => String((m && (m.id || m.uid)) || m || ''));
      // `isSelf` is zca-js's own answer to the same question; trust it when the
      // member list came back in a shape we did not recognise.
      const namesUs = members.length
        ? members.includes(own)
        : Boolean(event && event.isSelf);
      if (own && !namesUs) return;
      if (data.groupName) groupTitles.set(threadId, data.groupName);
      const kind = isJoin ? 'group_joined' : 'group_left';
      logInfo(`  -> ${kind} chat=${threadId}`);
      emit({
        kind,
        chat_id: threadId,
        chat_title: groupTitles.get(threadId) || null,
      });
    } catch (e) {
      logErr('onGroupEvent', e);
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
  try {
    api.listener.on('group_event', onGroupEvent);
  } catch (e) {
    // Older zca-js builds do not emit this; a group is then discovered
    // by its first message instead, which is the same outcome later.
    logInfo('group_event listener unavailable');
  }
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

function threadTypeOf(msg) {
  // Defaults to the user thread so a parent frame that predates rooms (or one
  // for a DM, which never carries the field) delivers exactly as it always did.
  const raw = Number(msg.thread_type);
  return Number.isInteger(raw) ? raw : THREAD_USER;
}

// Last typing OUTCOME seen per thread — the accepted status as readily as the
// error. Logging only failures leaves two very different states looking
// identical: Zalo accepted the event and showed nothing, or we never asked at
// all. That ambiguity is what made a room with no indicator take a full
// investigation to explain. One line per thread per change of outcome, so a
// steady state (working or broken) stays quiet and a change is always said.
const typingOutcomes = new Map();

function describeThrown(e) {
  // Never throws and never renders "[object Object]": a control-frame handler
  // that raises while describing a failure would escape into the socket's
  // error path and surface as a channel error, which a missed typing pulse has
  // no business doing.
  try {
    if (e instanceof Error) return e.message || e.name || 'Error';
    if (e && typeof e.message === 'string' && e.message) return e.message;
    return JSON.stringify(e) || Object.prototype.toString.call(e);
  } catch (inner) {
    // A circular object, a throwing getter, a null-prototype value.
    return Object.prototype.toString.call(e);
  }
}

function noteTypingResult(threadId, threadType, outcome) {
  const key = `${threadId}:${threadType}`;
  if (typingOutcomes.get(key) === outcome) return;
  typingOutcomes.set(key, outcome);
  // THREAD_GROUP is called out by name because it is the interesting half:
  // Zalo answers a group typing event on a different service from a DM's, and
  // accepts one for a two-person room that it then never broadcasts.
  const kind = threadType === THREAD_GROUP ? 'group' : 'user';
  logInfo(`sendTypingEvent ${outcome} thread=${threadId} type=${threadType} (${kind})`);
}

async function handleControl(msg) {
  if (!api) {
    // Answer on the correlation id the caller waits on where there is one, so a
    // roster request fails fast instead of hanging until its timeout.
    if (msg.kind === 'group_info' || msg.kind === 'list_groups' || msg.kind === 'send_file') {
      emit({
        kind: `${msg.kind}_result`,
        request_id: msg.request_id,
        ok: false,
        error: 'sidecar not ready',
      });
      return;
    }
    if (msg.kind === 'typing') {
      // The parent re-ticks every 4s while a run composes, so a session that is
      // reconnecting would report one lost "send" per tick for a message nobody
      // sent. Dropping the tick is the whole correct answer.
      return;
    }
    emit({ kind: 'send_error', sender_id: msg.sender_id, error: 'sidecar not ready' });
    return;
  }
  if (msg.kind === 'send') {
    const threadId = String(msg.sender_id || '');
    // The parent splits at this same cap, so the slice is a floor under a frame
    // that somehow arrives longer — it truncates, it does not chunk.
    const text = String(msg.text || '').slice(0, 2000);
    try {
      await api.sendMessage(text, threadId, threadTypeOf(msg));
    } catch (e) {
      emit({ kind: 'send_error', sender_id: msg.sender_id, error: String((e && e.message) || e) });
    }
  } else if (msg.kind === 'send_file') {
    // zca-js MessageContent takes an `attachments` array of local file paths;
    // the caption rides in `msg`. Answered with a correlated result frame so
    // the parent's strict senders can record what really happened.
    const threadId = String(msg.sender_id || '');
    try {
      const filePath = String(msg.path || '');
      if (!filePath || !fs.existsSync(filePath)) {
        throw new Error(`file not found: ${filePath}`);
      }
      await api.sendMessage(
        { msg: String(msg.caption || ''), attachments: [filePath] },
        threadId,
        threadTypeOf(msg),
      );
      emit({ kind: 'send_file_result', request_id: msg.request_id, ok: true });
    } catch (e) {
      emit({
        kind: 'send_file_result',
        request_id: msg.request_id,
        ok: false,
        error: String((e && e.message) || e),
      });
    }
  } else if (msg.kind === 'list_groups') {
    // Every group this account is in. `getAllGroups` returns ids only, so the
    // names come from a second call — batched, because an account in fifty
    // groups would otherwise be fifty round trips.
    try {
      const all = await api.getAllGroups();
      const ids = Object.keys((all && all.gridVerMap) || {});
      const groups = [];
      for (let i = 0; i < ids.length; i += 50) {
        const batch = ids.slice(i, i + 50);
        let info = null;
        try {
          info = await api.getGroupInfo(batch);
        } catch (e) {
          logInfo(`getGroupInfo batch failed: ${e && (e.message || e)}`);
        }
        const map = (info && info.gridInfoMap) || {};
        for (const id of batch) {
          const entry = map[id] || {};
          const name = entry.name || entry.groupName || null;
          if (name) groupTitles.set(id, name);
          groups.push({
            id,
            name,
            member_count: Number(entry.totalMember) || null,
          });
        }
      }
      emit({ kind: 'list_groups_result', request_id: msg.request_id, ok: true, groups });
    } catch (e) {
      emit({
        kind: 'list_groups_result',
        request_id: msg.request_id,
        ok: false,
        error: String((e && e.message) || e),
      });
    }
  } else if (msg.kind === 'group_info') {
    // The room's member list. Correlated by ``request_id`` because the parent
    // awaits this one, unlike everything else the sidecar sends.
    const threadId = String(msg.chat_id || '');
    try {
      const info = await api.getGroupInfo(threadId);
      const entry = (info && info.gridInfoMap && info.gridInfoMap[threadId])
        || (info && info.groupInfo)
        || {};
      if (entry.name || entry.groupName) {
        groupTitles.set(threadId, entry.name || entry.groupName);
      }
      const admins = (entry.adminIds || []).map(String);
      // ``currentMems`` carries names; ``memVerList`` is ids suffixed with a
      // version (``<id>_<ver>``) and is the fallback when it is absent.
      const members = (entry.currentMems || []).length
        ? entry.currentMems.map((mem) => ({
          id: String((mem && (mem.id || mem.uid)) || ''),
          display_name: (mem && (mem.dName || mem.zaloName)) || null,
        }))
        : (entry.memVerList || []).map((raw) => ({
          id: String(raw || '').split('_')[0],
          display_name: null,
        }));
      emit({
        kind: 'group_info_result',
        request_id: msg.request_id,
        ok: true,
        chat_id: threadId,
        name: entry.name || entry.groupName || null,
        members: members
          .filter((mem) => mem.id)
          .map((mem) => ({ ...mem, is_admin: admins.includes(mem.id) })),
      });
    } catch (e) {
      emit({
        kind: 'group_info_result',
        request_id: msg.request_id,
        ok: false,
        chat_id: threadId,
        error: String((e && e.message) || e),
      });
    }
  } else if (msg.kind === 'typing') {
    // A room's typing is not a person's: zca-js sends ThreadType.User to
    // `<chat service>/api/message/typing` with {toid, destType} and
    // ThreadType.Group to `<group service>/api/group/typing` with {grid} —
    // two different services, which fail independently. This branch used to
    // swallow the failure whole, so a room that showed no "typing…" left
    // nothing anywhere to say why while DMs kept working.
    const threadId = String(msg.sender_id || '');
    const threadType = threadTypeOf(msg);
    try {
      const res = await api.sendTypingEvent(threadId, threadType);
      // zca-js throws on a non-OK status, a non-zero error_code, or a body it
      // cannot parse (utils.js resolveResponse), so reaching here means Zalo
      // ACCEPTED the event. Said out loud because an accepted event that no
      // client ever renders is a platform answer, not a bug on this side.
      noteTypingResult(threadId, threadType, `accepted status=${(res && res.status)}`);
    } catch (e) {
      // Still non-fatal — the parent re-ticks every 4s and a missing
      // indicator must never cost the reply — but no longer invisible.
      noteTypingResult(threadId, threadType, `failed: ${describeThrown(e)}`);
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
