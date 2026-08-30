/**
 * Long-lived SSE consumer for one group room.
 *
 * Uses fetch + ReadableStream rather than `EventSource` for the usual reason
 * (EventSource cannot send an Authorization header), and is wrapped in
 * `createSharedStream` so several tabs of the same room cost one connection
 * instead of one each.
 *
 * The cursor is read through a callback on every (re)connect attempt, not
 * captured once: a reconnect must resume from the newest ordering the store
 * has actually applied, or the replay re-delivers messages already on screen.
 */

import type { GroupChat, GroupMessage } from './groupChatApi';
import {
  createSharedStream,
  type SharedStreamHandle,
  type SharedStreamRawHandle,
} from './sharedStream';

/** Emitted by the endpoint right after the replay phase of a (re)connect. */
export interface GroupReadyFrame {
  type: 'ready';
  data: { agents: Record<string, string> };
}

export interface GroupMessageFrame {
  type: 'message';
  data: GroupMessage;
}

/**
 * Who the router woke, for a row the room already has.
 *
 * A post is published the moment it is recorded, which is before the
 * classification that decides who answers it even exists — so the stamp cannot
 * ride the `message` frame and arrives just behind it instead. Merged onto the
 * stored row rather than replacing it: everything else about the post is
 * already correct.
 *
 * Live-only (never in the replay ring): the ring holds the row itself, and the
 * backend stamps that object in place, so a replayed `message` frame already
 * carries the routing metadata.
 */
export interface GroupMessageRoutingFrame {
  type: 'message_routing';
  data: {
    message_id: string;
    routing: Record<string, any>;
    // See `GroupSeatEventFrame` — the store's last branch recognises `ready` by
    // elimination and reads `data.agents`.
    agents?: undefined;
  };
}

export interface GroupAgentStatusFrame {
  type: 'agent_status';
  data: { profile: string; agent_name: string; state: string };
}

export interface GroupUpdatedFrame {
  type: 'group_updated';
  data: GroupChat;
}

export interface GroupDeletedFrame {
  type: 'deleted';
  data: Record<string, never>;
}

/**
 * What of a member's running turn the room is allowed to show — mirrors
 * `SEAT_EVENT_TYPES` in app/groups/hooks.py.
 *
 * Deliberately no `text`: the room renders whole posts, written at turn end, so
 * streaming the tokens as well would race the post and show every answer twice.
 * Also absent are the frames that address one client's own session
 * (`user_message`, `flow_break`, the plan-mode frames), which a spectator can
 * neither read nor answer.
 */
export type SeatEventType =
  | 'thinking'
  | 'result'
  | 'terminal'
  | 'cwd'
  | 'token_usage'
  | 'compaction_auto_folded'
  | 'error'
  | 'complete';

/**
 * One step of a member's turn, tapped off its seat conversation and re-published
 * on the room. The inner `type`/`data` pair is the conversation frame verbatim,
 * so the same mappers read it (see utils/streamFrames.ts); the wrapper adds only
 * who it came from.
 *
 * These are filtered per viewer before they reach the wire — a member sees its
 * own agent's steps, the admin sees everyone's — so anything arriving here is
 * already allowed. They are also published ephemerally (never entering the
 * room's replay ring): a client joining mid-turn is caught up instead from a
 * snapshot of each busy seat, and those catch-up frames carry no group `seq`,
 * so the same step can arrive twice — once from the snapshot and once from the
 * live tail. `seat_seq` is what makes recognising that duplicate exact.
 */
export interface GroupSeatEventFrame {
  type: 'seat_event';
  data: {
    profile: string;
    conversation_id: string;
    type: SeatEventType;
    /**
     * The frame's sequence number on the SEAT's own bus, identical in the
     * catch-up copy and the live one. Deliberately not the group stream's
     * `seq` — that one is per room and would collide. Absent only from a
     * server older than this field.
     */
    seat_seq?: number | null;
    data: Record<string, any>;
    // Never on the wire. Declared because the room store recognises `ready` by
    // elimination — its last branch reads `frame.data.agents` after narrowing
    // away every named frame type — and this frame now falls into that branch
    // too. Drop it once that branch handles `seat_event` by name.
    agents?: undefined;
  };
}

export type GroupStreamFrame =
  | GroupReadyFrame
  | GroupMessageFrame
  | GroupMessageRoutingFrame
  | GroupAgentStatusFrame
  | GroupSeatEventFrame
  | GroupUpdatedFrame
  | GroupDeletedFrame;

/**
 * Connection state of the tab that actually holds the socket. Follower tabs
 * never see it — they infer liveness from the `ready` frame the leader
 * broadcasts on every (re)connect.
 */
export type GroupStreamStatus = 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface GroupChatStreamHandle {
  close: () => void;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) return agentUrl;
  return `${window.location.origin}${agentUrl}`;
}

function openGroupChatRaw(
  agentUrl: string,
  authToken: string,
  groupId: string,
  since: () => number,
  onFrame: (frame: GroupStreamFrame) => void,
  onStatus: (status: GroupStreamStatus) => void,
): SharedStreamRawHandle {
  const controller = new AbortController();
  let closed = false;
  let attempt = 0;
  const backoffs = [1000, 2000, 5000, 10000, 30000];

  const run = async () => {
    while (!closed) {
      try {
        onStatus(attempt === 0 ? 'connecting' : 'reconnecting');
        const base = resolveBaseUrl(agentUrl);
        const params = new URLSearchParams();
        params.set('since', String(since()));
        const url = `${base}/api/group-chats/${encodeURIComponent(groupId)}`
          + `/stream?${params.toString()}`;
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (authToken) headers['Authorization'] = `Bearer ${authToken}`;

        const res = await fetch(url, { headers, signal: controller.signal });
        if (!res.ok || !res.body) {
          throw new Error(`SSE failed: ${res.status} ${res.statusText}`);
        }
        attempt = 0;
        onStatus('open');

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (!closed) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let idx: number;
          while (
            (idx = (() => {
              const a = buffer.indexOf('\n\n');
              const b = buffer.indexOf('\r\n\r\n');
              if (a === -1) return b;
              if (b === -1) return a;
              return Math.min(a, b);
            })()) !== -1
          ) {
            const sep = buffer[idx] === '\r' ? 4 : 2;
            const frameStr = buffer.slice(0, idx);
            buffer = buffer.slice(idx + sep);

            // Data-only frames: the payload carries its own ``type``, so an
            // ``event:`` line is never emitted and never parsed here.
            const dataLines: string[] = [];
            for (const rawLine of frameStr.split(/\r?\n/)) {
              if (rawLine.startsWith('data:')) {
                dataLines.push(rawLine.slice(5).replace(/^ /, ''));
              }
            }
            if (dataLines.length === 0) continue;

            try {
              const payload = JSON.parse(dataLines.join('\n'));
              if (payload && typeof payload.type === 'string') {
                onFrame(payload as GroupStreamFrame);
              }
            } catch (err) {
              console.warn('[groupChatStream] bad frame:', dataLines, err);
            }
          }
        }
        // The server closed the stream cleanly (group deleted, shutdown).
        // Reconnecting would spin against a 404, so stop here.
        onStatus('closed');
        return;
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        onStatus('reconnecting');
        console.warn(`[groupChatStream] reconnecting in ${wait}ms after error:`, err);
        await new Promise(r => setTimeout(r, wait));
      }
    }
  };

  run();

  return {
    close() {
      if (closed) return;
      closed = true;
      controller.abort();
    },
  };
}

/**
 * Subscribe to a group's live timeline.
 *
 * `since` is called (not read) per connect so a reconnect resumes from the
 * newest applied ordering. `onStatus` reports only this tab's own connection
 * when it is the shared-stream leader.
 */
export function openGroupChatStream(
  agentUrl: string,
  authToken: string,
  groupId: string,
  since: () => number,
  onFrame: (frame: GroupStreamFrame) => void,
  onStatus?: (status: GroupStreamStatus) => void,
): GroupChatStreamHandle {
  const reportStatus = onStatus ?? (() => {});
  const shared: SharedStreamHandle = createSharedStream<GroupStreamFrame>({
    key: `cremind:group-chat:${authToken}:${groupId}`,
    bufferSize: 256,
    openRaw: (handleEvent) =>
      openGroupChatRaw(agentUrl, authToken, groupId, since, handleEvent, reportStatus),
    onEvent: onFrame,
  });
  return {
    close() {
      shared.close();
      reportStatus('closed');
    },
  };
}
