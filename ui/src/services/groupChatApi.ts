/**
 * REST client for the multi-profile group chat (`/api/group-chats`).
 *
 * A group is system-wide with per-profile membership, so — unlike every other
 * service here — the profile is never part of a path. It is implied by the
 * Bearer token, and the backend answers with what that profile may see: admin
 * gets every group, a member gets its own.
 */

import type { MessageRecord } from './conversationApi';

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(authToken: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`;
  }
  return headers;
}

/**
 * Surface the server's own message on a failed call. The group endpoints
 * answer 400 on a malformed settings blob and 409 on an already-bound chat,
 * and both messages are the only useful thing to show the operator.
 */
async function readError(res: Response): Promise<string> {
  let detail = res.statusText;
  try {
    const data = await res.json();
    detail = data?.message || data?.error || detail;
  } catch {
    /* ignore body parse errors */
  }
  return detail;
}

export interface GroupSettings {
  max_agent_hops: number;
  max_agent_posts_per_minute: number;
  web_sender_name: string;
  /**
   * Ask a cheap model which members a post is for, and start a turn only for
   * those. The wire name is the backend's ``ROUTING_SETTING_KEY``.
   *
   * Every write of this blob must carry it: the server rebuilds the settings
   * from its defaults and only overrides the keys it is sent, so omitting the
   * field silently turns routing back on.
   */
  smart_routing: boolean;
}

export interface GroupMember {
  group_id: string;
  profile: string;
  shadow_conversation_id: string | null;
  joined_at: number;
  /**
   * Where this member's agent is working right now. Only the GET of a single
   * group fills it in, and only for the seats the caller may look behind (its
   * own, or every one for the admin) — the list response and the
   * ``group_updated`` stream frame both carry the row without it, so treat an
   * absent value as "unchanged", never as "reset to the default".
   */
  working_directory?: string;
}

export interface GroupMessage {
  id: string;
  group_id: string;
  /** Monotonic per group. Doubles as the stream's replay cursor. */
  ordering: number;
  sender_kind: 'user' | 'agent' | 'system';
  sender_profile: string | null;
  sender_name: string;
  sender_identity: Record<string, any> | null;
  content: string;
  /** 0 for a human post; agent posts count up until `max_agent_hops`. */
  hop: number;
  source_conversation_id: string | null;
  source_message_id: string | null;
  segment: number;
  delivered_to: string[] | null;
  metadata: Record<string, any> | null;
  created_at: number;
  /**
   * The reasoning behind this post, and the artefacts its turn produced —
   * decorated onto the row at request time, only for an agent post the caller
   * may look behind, and only on the last segment of a turn. The two-party chat
   * inlines the same thing on every message, which is what lets a reload render
   * the thinking process with no further requests.
   *
   * Transient: the store files them under `traceBySource` and strips them, so
   * they are never on a row the timeline keeps.
   */
  thinking_steps?: MessageRecord['thinking_steps'];
  source_parts?: any[];
  /** What the seat turn behind this post spent. Same gate as the steps. */
  source_token_usage?: MessageRecord['token_usage'];
}

/**
 * Who a post was routed to, as `app/groups/fanout.py` stamps it onto the row
 * under `metadata.routing`.
 *
 * Only present when the router actually ran — a room with `smart_routing` off
 * writes nothing here. Routing can only take a turn away: every member is
 * delivered every message either way, so this says who was asked to answer,
 * not who was allowed to read.
 */
export interface GroupRouting {
  /** Profile ids, sorted. Empty when `everyone` or `nobody`. */
  targets: string[];
  /** The fail-open answer: wake the whole room. */
  everyone: boolean;
  /**
   * Nobody was woken. Only ever set on an agent's own reply — a post that
   * answered the person and asked nothing of the other agents. Absent on rows
   * written before the outcome existed, which read as `false`.
   */
  nobody: boolean;
  reason: string;
  /** The classification could not run at all (no model, timeout, exception). */
  errored: boolean;
  model: string | null;
}

/**
 * Read the routing stamp off a post, or `null` when it carries none.
 *
 * Hand-narrowed rather than cast: `metadata` is an untyped JSON blob, so a row
 * written by an older server (or by a future one that adds a field) has to read
 * as "no routing" instead of rendering `undefined` into the chip.
 *
 * A `quiet` row is refused whatever it carries. `quiet` is the backend's cap —
 * the hop limit, the flood brake, a system notice — and it silences the whole
 * room, which the routing stamp beside it cannot say: the chip would read "only
 * Mia started a turn; everyone else still received the message" on a post where
 * nobody started one, and that is precisely the post a reader has opened the
 * room to explain. The fan-out no longer classifies a capped post at all, so
 * this is the belt to that braces: it also covers the rows an older server
 * stamped before the two were untangled.
 */
export function readRouting(message: GroupMessage): GroupRouting | null {
  const raw = message.metadata?.routing;
  if (!raw || typeof raw !== 'object') return null;
  if (message.metadata?.quiet) return null;
  const targets: string[] = Array.isArray(raw.targets)
    ? raw.targets.filter((t: unknown): t is string => typeof t === 'string')
    : [];
  return {
    targets,
    everyone: !!raw.everyone,
    nobody: !!raw.nobody,
    reason: typeof raw.reason === 'string' ? raw.reason : '',
    errored: !!raw.errored,
    model: typeof raw.model === 'string' ? raw.model : null,
  };
}

export interface GroupChat {
  id: string;
  name: string;
  settings: GroupSettings;
  created_by: string | null;
  members: string[];
  member_rows: GroupMember[];
  created_at: number;
  updated_at: number;
  /** Only present in the list response. */
  last_message?: GroupMessage | null;
}

/** GET of a single group also reports who is mid-turn right now. */
export interface GroupChatDetail {
  group: GroupChat;
  thinking: string[];
}

/**
 * The reasoning trace behind one agent post. Deliberately free of
 * `llm_messages` — the raw provider trace is never handed to another profile.
 */
/** The reasoning behind one agent post.
 *
 *  Deliberately narrower than a MessageRecord: the server withholds the raw
 *  provider trace, because everyone in the room can read this and it would carry
 *  another profile's tool arguments and results.
 */
export interface GroupMessageTrace {
  conversation_id: string | null;
  message: {
    id: string;
    content: string;
    thinking_steps: MessageRecord['thinking_steps'];
    /** The turn's artefacts — terminals, files — as message parts. */
    parts?: any[];
    /** What the turn spent, so this fallback shows the same chip as the page. */
    token_usage?: MessageRecord['token_usage'];
    provider: string | null;
    model: string | null;
    created_at: number;
  };
}

export interface CreateGroupPayload {
  name: string;
  members?: string[];
  settings?: Partial<GroupSettings>;
}

export interface UpdateGroupPayload {
  name?: string;
  members?: string[];
  /** Replaced whole — send the full blob, not a patch. */
  settings?: GroupSettings;
}

export async function listGroups(
  agentUrl: string, authToken: string,
): Promise<GroupChat[]> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/group-chats`, {
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(await readError(res));
  const data = await res.json();
  return data.groups ?? [];
}

export async function createGroup(
  agentUrl: string, authToken: string, payload: CreateGroupPayload,
): Promise<GroupChat> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/group-chats`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readError(res));
  const data = await res.json();
  return data.group;
}

export async function getGroup(
  agentUrl: string, authToken: string, groupId: string,
): Promise<GroupChatDetail> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/group-chats/${encodeURIComponent(groupId)}`, {
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(await readError(res));
  const data = await res.json();
  return { group: data.group, thinking: data.thinking ?? [] };
}

export async function updateGroup(
  agentUrl: string, authToken: string, groupId: string, payload: UpdateGroupPayload,
): Promise<GroupChat> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/group-chats/${encodeURIComponent(groupId)}`, {
    method: 'PATCH',
    headers: authHeaders(authToken),
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readError(res));
  const data = await res.json();
  return data.group;
}

export async function deleteGroup(
  agentUrl: string, authToken: string, groupId: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/group-chats/${encodeURIComponent(groupId)}`, {
    method: 'DELETE',
    headers: authHeaders(authToken),
  });
  if (!res.ok) throw new Error(await readError(res));
}

/**
 * Timeline page. `after` is an ordering cursor (exclusive); pass -1 — the
 * server default — for the beginning of the room.
 */
export async function fetchGroupMessages(
  agentUrl: string, authToken: string, groupId: string,
  opts: { after?: number; limit?: number } = {},
): Promise<GroupMessage[]> {
  const base = resolveBaseUrl(agentUrl);
  const params = new URLSearchParams();
  if (opts.after !== undefined) params.set('after', String(opts.after));
  if (opts.limit !== undefined) params.set('limit', String(opts.limit));
  const query = params.toString();
  const res = await fetch(
    `${base}/api/group-chats/${encodeURIComponent(groupId)}/messages${query ? `?${query}` : ''}`,
    { headers: authHeaders(authToken) },
  );
  if (!res.ok) throw new Error(await readError(res));
  const data = await res.json();
  return data.messages ?? [];
}

/**
 * Post into the room. Answers 202 with the persisted row: fan-out to the
 * member agents happens in the background, so the reply bubbles arrive later
 * over the stream, not in this response.
 */
export async function postGroupMessage(
  agentUrl: string, authToken: string, groupId: string,
  text: string, asProfile?: string,
): Promise<GroupMessage> {
  const base = resolveBaseUrl(agentUrl);
  const body: Record<string, unknown> = { text };
  if (asProfile) body.as_profile = asProfile;
  const res = await fetch(
    `${base}/api/group-chats/${encodeURIComponent(groupId)}/messages`,
    {
      method: 'POST',
      headers: authHeaders(authToken),
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) throw new Error(await readError(res));
  const data = await res.json();
  return data.message;
}

export async function fetchMessageTrace(
  agentUrl: string, authToken: string, groupId: string, messageId: string,
): Promise<GroupMessageTrace> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(
    `${base}/api/group-chats/${encodeURIComponent(groupId)}`
    + `/messages/${encodeURIComponent(messageId)}/trace`,
    { headers: authHeaders(authToken) },
  );
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}
