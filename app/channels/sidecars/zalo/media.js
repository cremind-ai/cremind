/*
 * What a Zalo media message points at, and how to fetch it.
 *
 * Split out of index.js on purpose: index.js binds a socket and logs in the
 * moment it is imported, so nothing there can be exercised by a test. Every
 * decision about WHICH url to fetch, HOW often, and WHAT to tell the agent when
 * none of them answer lives here, with `fetch`, `sleep` and the clock injected
 * (tests/channels/test_zalo_sidecar_media.py drives it through node). index.js
 * keeps the side effects: the spool file, the log lines, the emitted frame.
 *
 * The download used to be a single bare `fetch(content.href)`. A photo sent
 * from the Zalo PC app answered 404 on Kubernetes while a photo sent from the
 * phone forty seconds later, on the same session, downloaded fine — and because
 * a caption-less photo whose download failed carries no text either, the message
 * was dropped and the agent never learned anything had been posted.
 *
 * One cause is MEASURED and is what the rung below answers first. A file's href
 * redirects to a router (`file-<pool>-<n>.flchat.vn`) that picks a CDN edge from
 * the CLIENT's network: from a residential ISP it answers 302 to an edge, and
 * from a hosting network (this deployment's Kubernetes egress) it answers a bare
 * 404 — same object, same IP, same seconds, headers irrelevant. The edges it
 * would have chosen serve that same path to anybody who asks them by name, so
 * when the router refuses us we ask them ourselves. Photos ride a different CDN
 * and never see this.
 *
 * Three further causes remain plausible but unproven, and the rest of the ladder
 * answers those:
 *
 *   - the object is stale (the PC app dedupes an upload by content hash, so a
 *     "new" send can point at an object minted — and since expired — days ago):
 *     nothing can fetch it, so try the other copies Zalo attached and, failing
 *     that, SAY SO instead of dropping the message;
 *   - the host wants a browser (a hotlink-gated CDN answers a header-less Node
 *     fetch with 404 as readily as 403): retry with the headers zca-js itself
 *     sends;
 *   - the object has not propagated to our edge yet: retry after a short wait.
 *
 * If it still fails, the summary line index.js logs from this module's result
 * names the host, the path, the sending platform and the message's age — the
 * facts that were missing the first time and that decide between the three.
 */

import path from 'path';

// zca-js msgType values that carry a downloadable payload in content.href.
// Unofficial library — the shapes drift between builds, so detection is
// defensive and every unrecognised media shape is logged rather than guessed
// at. Stickers are deliberately absent: they are reactions, not files.
export const MEDIA_MSG_TYPES = new Set([
  'chat.photo', 'share.file', 'chat.video.msg', 'chat.voice', 'chat.gif',
]);

// Keys Zalo puts a fuller copy of a photo under inside the JSON string
// `content.params`. zca-js never parses that string for media (only for
// chat.todo), so this list is read off its own OUTBOUND vocabulary —
// rawUrl/hdUrl/oriUrl/normalUrl in apis/sendMessage.js — and is therefore a
// hypothesis, not documentation. Unknown keys are ignored, and every key the
// message actually carried is logged when a download fails, so the list can be
// corrected from evidence rather than guessed at again.
const PARAM_URL_KEYS = ['hd', 'hdUrl', 'oriUrl', 'normalUrl', 'rawUrl', 'url', 'href'];

// A thumbnail is a low-resolution copy of a PICTURE. For a document, a video or
// a voice note it is a preview, not the payload, and spooling it would hand the
// agent a JPEG named `report.pdf`.
const THUMB_MSG_TYPES = new Set(['chat.photo', 'chat.gif']);

// The file CDN's router, whose 404 means "no edge is mapped to your network"
// rather than "no such object" (see the header). Deliberately narrow: it must
// match the router and NOTHING else, because a match sends six requests.
//   file-stal-22.flchat.vn            -> matches, pool label `file-stal-22`
//   file-stal-22-te-vnso-ne-2.flchat.vn -> no (an edge is not a router)
//   file-stal-22.dlfl.vn              -> no (the pre-redirect host; no edges
//                                        exist under that domain at all)
//   res-zalo-1.zdn.vn                 -> no (the photo CDN, a different shape)
const FILE_ROUTER_HOST = /^(file-[a-z0-9]+-\d+)\.flchat\.vn$/i;

// The edges that router hands residential clients, best-first as measured from
// the Kubernetes pod: ne-* are VNETWORK CDN nodes, pt-* are ISP-hosted PoPs.
// It is a WALK, not a single name: `ne-3` and `pt-63` answered 404 for an
// object the other four served, so one name would have fixed nothing. They stay
// on the list because they are two cheap 4xx at the end of a walk that has
// already failed four times. The pool label always comes from the router's own
// hostname — the same path under another pool's label answers 404.
export const FILE_EDGE_SUFFIXES = [
  'te-vnso-ne-2', 'te-vnso-ne-1', 'te-vnso-pt-64', 'te-vnso-pt-65',
  'te-vnso-ne-3', 'te-vnso-pt-63',
];

const KIND_BY_TYPE = {
  'chat.photo': 'photo',
  'chat.gif': 'GIF',
  'chat.video.msg': 'video',
  'chat.voice': 'voice message',
  'share.file': 'file',
};
const EXT_BY_TYPE = {
  'chat.photo': '.jpg',
  'chat.video.msg': '.mp4',
  'chat.voice': '.aac',
  'chat.gif': '.gif',
};

// Waits between attempts on the PRIMARY url. An alternate that answers 404 is a
// different object, not the same one arriving late, so it gets one attempt.
export const RETRY_DELAYS_MS = [1000, 3000];
// A ceiling against a socket that never closes, not a latency budget: a file
// near the 100 MB cap legitimately takes minutes on a slow link. Before this
// the fetch had no timeout at all and a hung host stalled that message forever.
export const ATTEMPT_TIMEOUT_MS = 120_000;
// How long we keep STARTING new attempts. A dead photo therefore costs about
// four seconds (1 s + 3 s of backoff) before the agent is told, not minutes.
export const TOTAL_BUDGET_MS = 30_000;

// Statuses that will answer the same way however often we ask.
const FINAL_STATUSES = new Set([400, 401, 402, 403, 405, 406, 410, 451]);

function isHttpUrl(value) {
  return typeof value === 'string' && /^https?:\/\//i.test(value.trim());
}

function isRetryable(status) {
  // Anything non-numeric is a network error, a timeout or a body read that
  // fell over — all worth one more go. `empty` is this url's final answer.
  if (typeof status !== 'number') return status !== 'empty';
  if (FINAL_STATUSES.has(status)) return false;
  return status === 404 || status === 408 || status === 425 || status === 429
    || status >= 500;
}

function hostnameOf(url) {
  try {
    return new URL(url).hostname;
  } catch (e) {
    return '';
  }
}

export function edgeCandidates(finalUrl, suffixes = FILE_EDGE_SUFFIXES) {
  // The CDN edges that `finalUrl`'s router would have redirected a residential
  // client to, or [] when that url is not a router. The path and query are
  // carried over untouched: a Zalo url can be signed, and the edge is asked for
  // exactly what the router was asked for, only by name.
  let parsed;
  try {
    parsed = new URL(finalUrl);
  } catch (e) {
    return [];
  }
  const match = FILE_ROUTER_HOST.exec(parsed.hostname);
  if (!match) return [];
  const pool = match[1];
  return (suffixes || []).map((suffix) => {
    const edge = new URL(parsed.toString());
    edge.hostname = `${pool}-${suffix}.flchat.vn`;
    return { source: `edge.${suffix}`, url: edge.toString() };
  });
}

export function attemptTrail(attempts) {
  // `href:404@file-stal-22.flchat.vn,edge.te-vnso-ne-2:200` — the host only
  // where a redirect moved the answer somewhere other than what we asked for,
  // which is the fact the first failing log line did not have.
  return (attempts || [])
    .map((a) => `${a.source}:${a.status}${a.host ? `@${a.host}` : ''}`)
    .join(',');
}

export function describeUrl(url) {
  // Host and path, plus the NAMES of any query parameters. A Zalo CDN url can
  // carry a signature, and this string is written to logs/app.log.
  try {
    const parsed = new URL(url);
    const keys = [...parsed.searchParams.keys()];
    return `${parsed.host}${parsed.pathname}${keys.length ? `?${keys.join('&')}` : ''}`;
  } catch (e) {
    return '<invalid url>';
  }
}

export function mediaCandidates(content, msgType) {
  // Every copy of the payload the message names, most faithful first, deduped.
  // `present` keeps the fields that carried a url even when the url repeats, so
  // a failure line can say what the message offered and what we tried.
  const candidates = [];
  const present = [];
  const seen = new Set();
  const push = (source, url) => {
    if (!isHttpUrl(url)) return;
    present.push(source);
    const trimmed = url.trim();
    if (seen.has(trimmed)) return;
    seen.add(trimmed);
    candidates.push({ source, url: trimmed });
  };
  if (!content || typeof content !== 'object') {
    return { candidates, present, paramKeys: [] };
  }
  push('href', content.href);
  let params = null;
  let paramKeys = [];
  try {
    params = typeof content.params === 'string' && content.params
      ? JSON.parse(content.params)
      : (content.params && typeof content.params === 'object' ? content.params : null);
  } catch (e) {
    paramKeys = ['<unparseable>'];
  }
  if (params && typeof params === 'object') {
    paramKeys = Object.keys(params);
    for (const key of PARAM_URL_KEYS) push(`params.${key}`, params[key]);
  }
  if (THUMB_MSG_TYPES.has(String(msgType || ''))) push('thumb', content.thumb);
  return { candidates, present, paramKeys };
}

export function mediaFromMessage(data, log = () => {}) {
  // A descriptor for a media message, or null. `title` is the FILENAME on a
  // share.file and (when present) the CAPTION on a photo/video — the caller
  // uses `isFile` to keep a filename from becoming message text.
  const content = data && data.content;
  const msgType = String((data && data.msgType) || '');
  if (!content || typeof content !== 'object' || !content.href) return null;
  if (!MEDIA_MSG_TYPES.has(msgType)) {
    if (msgType) log(`media-like content ignored (msgType=${msgType})`);
    return null;
  }
  const isFile = msgType === 'share.file';
  let name = isFile ? String(content.title || '').trim() : '';
  if (!name) {
    try {
      name = path.basename(new URL(content.href).pathname) || '';
    } catch (e) { /* fall through */ }
  }
  if (!name) name = `zalo_media${EXT_BY_TYPE[msgType] || ''}`;
  const { candidates, present, paramKeys } = mediaCandidates(content, msgType);
  return {
    url: content.href,
    name,
    isFile,
    msgType,
    kind: KIND_BY_TYPE[msgType] || 'file',
    candidates,
    present,
    paramKeys,
    // Which client sent it. Never logged before, and the cheapest way to tell a
    // PC-app message from a phone one when the next download fails.
    platformType: (data.paramsExt && data.paramsExt.platformType) ?? null,
    ts: Number(data && data.ts) || null,
  };
}

async function fetchOne(url, headers, opts) {
  // One attempt. Never throws: the caller decides what a failure means.
  const { fetchImpl, maxBytes, attemptTimeoutMs } = opts;
  const init = {};
  // Headers are the retry's whole point, so the FIRST attempt sends none —
  // byte-identical on the wire to the request that works today. The abort
  // signal rides every attempt: a server cannot see it, so it changes nothing
  // for the path that already succeeds.
  if (headers) init.headers = headers;
  if (globalThis.AbortSignal && AbortSignal.timeout) {
    init.signal = AbortSignal.timeout(attemptTimeoutMs);
  }
  let resp;
  try {
    resp = await fetchImpl(url, init);
  } catch (e) {
    let why = e && e.name === 'TimeoutError'
      ? `timeout after ${attemptTimeoutMs}ms`
      : String((e && (e.message || e.name)) || e);
    // undici reports every network failure as the same "fetch failed", and puts
    // the reason that actually names it (ENOTFOUND, ECONNREFUSED) in `cause`.
    // Without this a drifted CDN hostname and a refused connection read alike.
    const cause = e && e.cause && (e.cause.message || e.cause.code);
    if (cause) why += `: ${cause}`;
    return { ok: false, status: `error:${why}`, url };
  }
  // Where the answer actually came from: undici follows redirects and reports
  // the FINAL url here, which is how a router's 404 is told apart from the
  // href's own. A synthetic Response has no url; fall back to what we asked.
  const landed = (typeof resp.url === 'string' && resp.url) || url;
  if (!resp.ok) return { ok: false, status: resp.status, url: landed };
  const declared = Number(resp.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > maxBytes) {
    // The cap is our policy, not the CDN's answer: a bigger copy will not help
    // and a thumbnail is not the file that was sent, so this ends the ladder.
    return { ok: false, status: 'over-cap', capped: true, size: declared, url: landed };
  }
  let buffer;
  try {
    buffer = Buffer.from(await resp.arrayBuffer());
  } catch (e) {
    // A body that dies mid-read used to escape all the way to onMessage's
    // catch, which dropped the message exactly like a failed download.
    return {
      ok: false,
      status: `error:body:${String((e && e.message) || e)}`,
      url: landed,
    };
  }
  if (buffer.length > maxBytes) {
    return { ok: false, status: 'over-cap', capped: true, size: buffer.length, url: landed };
  }
  if (!buffer.length) return { ok: false, status: 'empty', url: landed };
  const mime = String(resp.headers.get('content-type') || '').split(';')[0] || null;
  return { ok: true, status: resp.status, buffer, mime, url: landed };
}

export async function downloadMedia(media, opts = {}) {
  // Walk the candidates until one answers. Returns
  //   {ok:true, buffer, mime, source, degraded, attempts, ...}
  // or {ok:false, status, host, pathname, attempts, present, paramKeys, capped}
  // and NEVER throws — a message that cannot be downloaded still has to flow.
  const {
    maxBytes = Infinity,
    headersFor = async () => null,
    fetchImpl = globalThis.fetch,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    now = Date.now,
    attemptTimeoutMs = ATTEMPT_TIMEOUT_MS,
    totalBudgetMs = TOTAL_BUDGET_MS,
    retryDelaysMs = RETRY_DELAYS_MS,
    edgeSuffixes = FILE_EDGE_SUFFIXES,
  } = opts;
  const candidates = (media && media.candidates) || [];
  const present = (media && media.present) || [];
  const paramKeys = (media && media.paramKeys) || [];
  const primary = candidates.length ? candidates[0].url : (media && media.url) || '';
  const started = now();
  const attempts = [];
  const urls = [];
  let lastStatus = candidates.length ? null : 'no-url';
  let budgetSpent = false;

  const failure = (extra) => ({
    ok: false,
    // An HTTP status the ladder actually saw beats the last marker: when a
    // router's 404 is followed by six edges that do not resolve, "HTTP 404" is
    // what the sender needs to read, not a hostname from a DNS error.
    status: attempts.reduce(
      (found, a) => (typeof a.status === 'number' ? a.status : found),
      null,
    ) ?? lastStatus,
    described: describeUrl(primary),
    urls,
    attempts,
    present,
    paramKeys,
    ...extra,
  });

  // A copy, because a router's 404 splices its edges in behind the candidate
  // that hit it — the ladder grows from what the CDN answers, not only from
  // what the message named.
  const queue = [...candidates];
  let edgesSpliced = false;

  for (let i = 0; i < queue.length; i += 1) {
    const cand = queue[i];
    const described = describeUrl(cand.url);
    if (!urls.includes(described)) urls.push(described);
    const maxTries = cand.source === 'href' ? retryDelaysMs.length + 1 : 1;
    for (let attempt = 0; attempt < maxTries; attempt += 1) {
      if (attempts.length && now() - started >= totalBudgetMs) {
        budgetSpent = true;
        break;
      }
      if (attempt > 0) await sleep(retryDelaysMs[attempt - 1]);
      // No headers on the very first request anywhere: that one still has to
      // look exactly like the request the working path makes today.
      const headers = attempts.length === 0 ? null : await headersFor(cand.url);
      const outcome = await fetchOne(cand.url, headers, {
        fetchImpl, maxBytes, attemptTimeoutMs,
      });
      const landed = hostnameOf(outcome.url);
      const record = { source: cand.source, status: outcome.status };
      if (landed && landed !== hostnameOf(cand.url)) record.host = landed;
      attempts.push(record);
      lastStatus = outcome.status;
      if (outcome.ok) {
        return {
          ok: true,
          buffer: outcome.buffer,
          mime: outcome.mime,
          status: outcome.status,
          source: cand.source,
          // A thumbnail IS the picture, just a poor copy of it — the caller
          // says so in the message rather than passing it off as the original.
          degraded: cand.source === 'thumb',
          attempts,
          urls,
        };
      }
      if (outcome.capped) return failure({ capped: true, size: outcome.size });
      if (outcome.status === 404 && !edgesSpliced) {
        const edges = edgeCandidates(outcome.url, edgeSuffixes);
        if (edges.length) {
          // The router refused this NETWORK, so asking it again is waiting for
          // nothing — that is the 1s+3s of backoff the first failing log spent.
          // Every copy one message names sits on one pool, so this happens once.
          edgesSpliced = true;
          queue.splice(i + 1, 0, ...edges);
          for (const edge of edges) {
            const edgeDescribed = describeUrl(edge.url);
            if (!urls.includes(edgeDescribed)) urls.push(edgeDescribed);
          }
          break;
        }
      }
      if (!isRetryable(outcome.status)) break;
    }
    if (budgetSpent) break;
  }
  return failure({ budgetSpent });
}

export function failureReason(failure) {
  // How the failure reads to a person: an HTTP status where there is one, and
  // the raw marker (`error:...`, `empty`, `no-url`) where there is not.
  const status = failure && failure.status;
  if (typeof status === 'number') return `HTTP ${status}`;
  if (!status) return 'unknown error';
  return String(status).startsWith('error:') ? String(status).slice(6) : String(status);
}

export function mediaFailureNotice(media, failure) {
  // The text that stands in for an attachment that never arrived. Without it a
  // caption-less photo whose download failed carried no text and no file, so
  // the parent dropped it and the agent answered a message it never saw.
  // Shaped like the server's own placeholder for a caption-less file
  // (app/channels/attachments.py placeholder_text) so the agent reads it the
  // same way. A photo's name is a CDN basename — an md5 and two ids — which is
  // noise to the agent, so only a real filename is named.
  const kind = (media && media.kind) || 'file';
  const detail = media && media.isFile && media.name ? `: ${media.name}` : '';
  return `[sent a ${kind}${detail}, but it could not be downloaded (${failureReason(failure)})]`;
}

export function thumbnailNotice(media) {
  const kind = (media && media.kind) || 'file';
  return `[the ${kind} above is a low-resolution thumbnail; the full copy could not be downloaded]`;
}
