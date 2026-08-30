import { defineStore } from 'pinia';
import { useSettingsStore } from './settings';
import { useTerminalPanelStore } from './terminalPanel';
import type { TerminalAttachment, ThinkingStep, TokenUsage } from './chat';
import {
  attachResultToSteps,
  terminalAttachmentFromFrame,
  terminalAttachmentsFromParts,
  thinkingStepFromFrame,
  thinkingStepsFromRecord,
  tokenUsageFromFrame,
  tokenUsageFromRecord,
} from '../utils/streamFrames';
import { fetchAgentNames } from '../services/configApi';
import {
  listGroups,
  getGroup,
  createGroup as apiCreateGroup,
  updateGroup as apiUpdateGroup,
  deleteGroup as apiDeleteGroup,
  fetchGroupMessages,
  postGroupMessage,
  fetchMessageTrace,
  type CreateGroupPayload,
  type GroupChat,
  type GroupMessage,
  type GroupMessageTrace,
  type UpdateGroupPayload,
} from '../services/groupChatApi';
import {
  openGroupChatStream,
  type GroupChatStreamHandle,
  type GroupSeatEventFrame,
  type GroupStreamFrame,
  type GroupStreamStatus,
} from '../services/groupChatStream';

/**
 * What one member's agent has done so far in the turn it is running right now.
 *
 * Assembled from `seat_event` frames, which the server only sends for seats the
 * viewer may look behind, so the mere presence of an entry here means the steps
 * are the viewer's to see. Ephemeral by design: it exists between the member's
 * first step and the post that ends its turn, at which point the steps are
 * handed to `traceBySource` and this entry is dropped.
 */
export interface GroupLiveTurn {
  /** The member's seat conversation — the terminal panel's bucket key. */
  conversationId: string;
  thinkingSteps: ThinkingStep[];
  terminalAttachments: TerminalAttachment[];
  tokenUsage?: TokenUsage;
  /**
   * Seat-bus sequence numbers already applied. A client that joins mid-turn is
   * caught up from the seat's ring and then hears the live tail, so a frame
   * published between the two arrives twice — and identical parallel tool calls
   * are indistinguishable by contents alone.
   */
  seenSeatSeqs: Record<number, true>;
  startedAt: number;
}

/**
 * The reasoning behind one agent post, keyed by the seat message it came from.
 *
 * Filled either by watching the turn happen (no request at all) or, for a post
 * made before this tab was looking, by the trace endpoint. `loaded` is what
 * separates "fetched and there was nothing" from "never asked", so an empty
 * trace is reported as empty instead of re-fetched on every expand.
 */
export interface GroupTrace {
  thinkingSteps: ThinkingStep[];
  terminalAttachments: TerminalAttachment[];
  /** What the turn behind the post spent. Absent when it made no LLM call. */
  tokenUsage?: TokenUsage;
  loaded: boolean;
  loading?: boolean;
  error?: string | null;
}

/**
 * Identity of one tool call, for suppressing the duplicate a mid-turn join
 * produces.
 *
 * Superseded by `seat_seq`, which is exact; this is the fallback for a server
 * that does not stamp one. Note its limit: two identical parallel calls in one
 * model step share the tuple and the second is dropped, which is why the
 * sequence number is preferred whenever it is there. `callId` is the real
 * identity when the provider emits one.
 */
function stepKey(step: ThinkingStep): string {
  if (step.callId) return `c:${step.callId}`;
  return `s:${step.step ?? ''}|${step.tool}|${step.toolInput}`;
}

// Live SSE handles, one per open room. Kept out of the Pinia state for the
// same reason the chat store keeps its own: the handle wraps an
// AbortController, which has no business inside Vue's reactivity.
const streamHandles = new Map<string, GroupChatStreamHandle>();

// How much of the timeline the room loads on entry, and how much a
// post-reconnect reconcile pulls per request.
const PAGE_SIZE = 200;

// Ceiling on the reconcile's paging loop. 25 full pages is far more backlog
// than any real disconnect leaves behind, and it stops a server that never
// returns a short page from spinning the tab forever.
const RECONCILE_MAX_PAGES = 25;

function closeStream(groupId: string) {
  const handle = streamHandles.get(groupId);
  if (!handle) return;
  handle.close();
  streamHandles.delete(groupId);
}

export const useGroupChatStore = defineStore('groupChat', {
  state: () => ({
    groups: [] as GroupChat[],
    /** False until the first successful list — separates "empty" from "not yet asked". */
    groupsLoaded: false,
    loading: false,
    activeGroupId: null as string | null,
    messagesByGroup: {} as Record<string, GroupMessage[]>,
    /** Newest ordering applied per group; the stream's resume cursor. */
    lastOrderingByGroup: {} as Record<string, number>,
    /**
     * Where the next reconnect's catch-up starts reading, per group.
     *
     * Not the same as the resume cursor, and deliberately behind it: a
     * reconnect replays the room's ring first, and applying those frames moves
     * the resume cursor past the very rows the catch-up exists to audit. Rows
     * that arrive only by replay would then never be read from the API, so a
     * post whose steps this tab collected across a dropped connection would
     * keep the partial copy forever. This floor is moved only once a catch-up
     * has actually read the window.
     */
    reconcileFloorByGroup: {} as Record<string, number>,
    /** groupId → profile → state ('thinking' | 'idle'). */
    agentStatusByGroup: {} as Record<string, Record<string, string>>,
    /** groupId → profile → the turn that member is running right now. */
    liveTurns: {} as Record<string, Record<string, GroupLiveTurn>>,
    /**
     * groupId → seat message id → the reasoning behind the post(s) it produced.
     * Keyed by the SOURCE message rather than the room row because one turn can
     * post several segments and they all share the one trace.
     */
    traceBySource: {} as Record<string, Record<string, GroupTrace>>,
    streamStatusByGroup: {} as Record<string, GroupStreamStatus>,
    /** profile → agent name, for member chips and "X is thinking…". */
    agentNames: {} as Record<string, string>,
    sending: false,
    error: null as string | null,
  }),

  getters: {
    /** Group management is admin-owned; members can only view and post. */
    isAdmin(): boolean {
      return useSettingsStore().profileId === 'admin';
    },
    activeGroup(state): GroupChat | null {
      if (!state.activeGroupId) return null;
      return state.groups.find((g) => g.id === state.activeGroupId) ?? null;
    },
    activeMessages(state): GroupMessage[] {
      if (!state.activeGroupId) return [];
      return state.messagesByGroup[state.activeGroupId] ?? [];
    },
    /** Admin posts as the operator; a member profile posts as its own agent. */
    canPost(): (group: GroupChat | null) => boolean {
      const profile = useSettingsStore().profileId;
      const admin = profile === 'admin';
      return (group: GroupChat | null) => {
        if (!group) return false;
        return admin || group.members.includes(profile);
      };
    },
    thinkingProfiles(state): (groupId: string | null) => string[] {
      return (groupId: string | null) => {
        if (!groupId) return [];
        const states = state.agentStatusByGroup[groupId] ?? {};
        return Object.keys(states).filter((p) => states[p] === 'thinking');
      };
    },
    /** Display name for a member profile, falling back to the profile itself. */
    nameFor(state): (profile: string) => string {
      return (profile: string) => state.agentNames[profile] || profile;
    },
    /**
     * Whose reasoning this viewer is allowed to watch. Mirrors `_may_see_seat`
     * on the server, which is the authority — this only decides whether to
     * render the panel, never whether the frames arrive.
     */
    visibleSeatProfiles(): (group: GroupChat | null) => string[] {
      const viewer = useSettingsStore().profileId;
      const admin = viewer === 'admin';
      return (group: GroupChat | null) => {
        if (!group) return [];
        const members = group.members ?? [];
        return admin ? [...members] : members.filter((p) => p === viewer);
      };
    },
    /** A member's seat conversation id — the terminal/cwd bucket for its work. */
    seatIdFor(state): (group: GroupChat | null, profile: string) => string | null {
      return (group: GroupChat | null, profile: string) => {
        if (!group || !profile) return null;
        const row = (group.member_rows ?? []).find((m) => m.profile === profile);
        if (row?.shadow_conversation_id) return row.shadow_conversation_id;
        // A seat created during this session is on the frames before it is on
        // the group row we loaded.
        return state.liveTurns[group.id]?.[profile]?.conversationId ?? null;
      };
    },
    liveTurnFor(state): (groupId: string | null, profile: string) => GroupLiveTurn | null {
      return (groupId: string | null, profile: string) => {
        if (!groupId || !profile) return null;
        return state.liveTurns[groupId]?.[profile] ?? null;
      };
    },
    traceFor(state): (groupId: string | null, message: GroupMessage) => GroupTrace | null {
      return (groupId: string | null, message: GroupMessage) => {
        const key = message?.source_message_id;
        if (!groupId || !key) return null;
        return state.traceBySource[groupId]?.[key] ?? null;
      };
    },
    /**
     * Whether this post is the tail of its turn. A turn that spoke mid-flight
     * posts one row per segment, and the backend attaches the steps to the last
     * of them — so the reasoning panel belongs there and nowhere else, or the
     * same trace would be offered under every segment.
     */
    isLastSegment(state): (groupId: string | null, message: GroupMessage) => boolean {
      return (groupId: string | null, message: GroupMessage) => {
        const key = message?.source_message_id;
        if (!groupId || !key) return true;
        const list = state.messagesByGroup[groupId] ?? [];
        return !list.some((m) => (
          m.id !== message.id
          && m.source_message_id === key
          && m.source_conversation_id === message.source_conversation_id
          && m.segment > message.segment
        ));
      };
    },
  },

  actions: {
    async loadGroups() {
      const settings = useSettingsStore();
      if (!settings.authToken) return;
      this.loading = true;
      try {
        this.groups = await listGroups(settings.agentUrl, settings.authToken);
        this.groupsLoaded = true;
        this.error = null;
      } catch (e) {
        this.error = e instanceof Error ? e.message : 'Failed to load groups';
        throw e;
      } finally {
        this.loading = false;
      }
    },

    /** Agent names for every visible profile — never fatal, chips fall back
     *  to the raw profile name. */
    async loadAgentNames() {
      const settings = useSettingsStore();
      if (!settings.authToken) return;
      try {
        const { agents } = await fetchAgentNames(settings.agentUrl, settings.authToken);
        const map: Record<string, string> = {};
        for (const entry of agents) map[entry.profile] = entry.name;
        this.agentNames = map;
      } catch {
        /* keep whatever we had; the getter falls back to the profile name */
      }
    },

    /**
     * Enter a room: close whatever was open, load the tail of the timeline and
     * the live "who is thinking" snapshot, then start the stream from the
     * newest ordering we actually applied.
     */
    async openGroup(groupId: string) {
      const settings = useSettingsStore();
      if (!settings.authToken || !groupId) return;
      if (this.activeGroupId && this.activeGroupId !== groupId) {
        closeStream(this.activeGroupId);
        // Its stream is gone, so nothing will ever finish those turns. Left
        // behind, the half-collected steps would reappear as this room's when
        // the user walks back in and the reconnect replays them again.
        delete this.liveTurns[this.activeGroupId];
      }
      this.activeGroupId = groupId;

      try {
        const [detail, messages] = await Promise.all([
          getGroup(settings.agentUrl, settings.authToken, groupId),
          fetchGroupMessages(settings.agentUrl, settings.authToken, groupId, {
            limit: PAGE_SIZE,
          }),
        ]);
        this.mergeGroup(detail.group);
        const statuses: Record<string, string> = {};
        for (const profile of detail.thinking) statuses[profile] = 'thinking';
        this.agentStatusByGroup[groupId] = statuses;
        const ordered = [...messages].sort((a, b) => a.ordering - b.ordering);
        // Before the rows land: each carries its own reasoning inline, and the
        // bubbles render from `traceBySource`, so seeding first means a reload
        // shows the thinking process on the first paint rather than after it.
        for (const row of ordered) this.seedTraceFromRow(groupId, row);
        this.messagesByGroup[groupId] = ordered;
        this.lastOrderingByGroup[groupId] = ordered.length
          ? ordered[ordered.length - 1].ordering
          : -1;
        // Everything up to here came from the API with its reasoning attached,
        // so the first catch-up has nothing to re-read before this point.
        this.reconcileFloorByGroup[groupId] = this.lastOrderingByGroup[groupId];
        this.error = null;
      } catch (e) {
        this.error = e instanceof Error ? e.message : 'Failed to open the group';
        throw e;
      }

      // A second openGroup for the same room (route re-entry) must not stack
      // a second connection on top of the live one.
      if (streamHandles.has(groupId)) return;
      this.streamStatusByGroup[groupId] = 'connecting';
      streamHandles.set(
        groupId,
        openGroupChatStream(
          settings.agentUrl,
          settings.authToken,
          groupId,
          () => this.lastOrderingByGroup[groupId] ?? -1,
          (frame) => { void this.handleFrame(groupId, frame); },
          (status) => { this.streamStatusByGroup[groupId] = status; },
        ),
      );
    },

    /** Leave the active room. The timeline stays cached for a quick return. */
    closeGroup() {
      const groupId = this.activeGroupId;
      if (!groupId) return;
      closeStream(groupId);
      this.streamStatusByGroup[groupId] = 'closed';
      // Live steps and fetched traces both die with the stream: the next visit
      // reconnects, and the server re-sends a snapshot of every busy seat.
      delete this.liveTurns[groupId];
      delete this.traceBySource[groupId];
      this.activeGroupId = null;
    },

    async handleFrame(groupId: string, frame: GroupStreamFrame) {
      if (frame.type === 'message') {
        // Before the row lands, because the bubble it creates renders its own
        // reasoning panel and would otherwise offer a fetch for steps this tab
        // already watched happen.
        this.adoptLiveTurn(groupId, frame.data);
        this.upsertMessage(groupId, frame.data);
        this.refreshSeatUsage(frame.data);
        return;
      }
      if (frame.type === 'message_routing') {
        this.applyRouting(groupId, frame.data.message_id, frame.data.routing);
        return;
      }
      if (frame.type === 'seat_event') {
        this.applySeatEvent(groupId, frame.data);
        return;
      }
      if (frame.type === 'agent_status') {
        const states = this.agentStatusByGroup[groupId] ?? {};
        this.agentStatusByGroup[groupId] = {
          ...states,
          [frame.data.profile]: frame.data.state,
        };
        if (frame.data.agent_name && frame.data.profile) {
          this.agentNames = {
            ...this.agentNames,
            [frame.data.profile]: frame.data.agent_name,
          };
        }
        // The turn is over — its steps have already been handed to the post it
        // produced. Keeping the entry would prepend this turn's work to the
        // next one, because a seat reuses the same conversation forever.
        if (frame.data.state !== 'thinking' && this.liveTurns[groupId]) {
          delete this.liveTurns[groupId][frame.data.profile];
        }
        return;
      }
      if (frame.type === 'group_updated') {
        this.mergeGroup(frame.data);
        return;
      }
      if (frame.type === 'deleted') {
        closeStream(groupId);
        this.groups = this.groups.filter((g) => g.id !== groupId);
        delete this.messagesByGroup[groupId];
        delete this.lastOrderingByGroup[groupId];
        delete this.reconcileFloorByGroup[groupId];
        delete this.agentStatusByGroup[groupId];
        delete this.liveTurns[groupId];
        delete this.traceBySource[groupId];
        this.streamStatusByGroup[groupId] = 'closed';
        if (this.activeGroupId === groupId) this.activeGroupId = null;
        return;
      }
      // `ready` ends the replay phase of a (re)connect. A follower tab never
      // runs the socket, so this frame is also its only proof the stream is
      // alive — flip the status here as well as from the raw loop.
      this.streamStatusByGroup[groupId] = 'open';
      const statuses: Record<string, string> = {};
      for (const [profile, state] of Object.entries(frame.data.agents ?? {})) {
        statuses[profile] = state;
      }
      this.agentStatusByGroup[groupId] = statuses;
      // The replay ring is bounded, so a long disconnect can drop messages the
      // reconnect never replays. Pull anything past our cursor from the DB.
      const settings = useSettingsStore();
      if (!settings.authToken) return;
      // Page, rather than fetch once: a backlog bigger than PAGE_SIZE would
      // otherwise leave a hole in the middle of the timeline that nothing ever
      // comes back for. A short page means we have caught up.
      //
      // Read from the floor, not from the resume cursor: the replay this
      // connect just applied has already pushed that cursor past its own rows,
      // and those are exactly the ones worth re-reading — they arrived without
      // their reasoning, and a post the tab watched across the drop is holding
      // whatever steps it managed to collect. Re-reading a row it already has
      // is free (upsert replaces by id, seeding overwrites the same key).
      const floor = this.reconcileFloorByGroup[groupId]
        ?? this.lastOrderingByGroup[groupId] ?? -1;
      let cursor = floor;
      try {
        for (let page = 0; page < RECONCILE_MAX_PAGES; page += 1) {
          const missed = await fetchGroupMessages(
            settings.agentUrl, settings.authToken, groupId,
            { after: cursor, limit: PAGE_SIZE },
          );
          for (const message of missed) {
            this.seedTraceFromRow(groupId, message);
            this.upsertMessage(groupId, message);
            if (message.ordering > cursor) cursor = message.ordering;
          }
          if (missed.length < PAGE_SIZE) break;
        }
        // Only now: the window has been read from the authority, so the next
        // reconnect starts from here rather than reading it all again.
        this.reconcileFloorByGroup[groupId] = this.lastOrderingByGroup[groupId] ?? floor;
      } catch {
        /* the next frame will carry the timeline forward anyway */
      }
    },

    /**
     * One step of one member's running turn.
     *
     * The inner `type`/`data` pair is a conversation frame verbatim, so it is
     * read with the very mappers the two-party chat uses (utils/streamFrames) —
     * the room's job is only to file it under the member it came from.
     *
     * Everything reaching here is already the viewer's to see: the endpoint
     * drops seat frames for profiles the viewer may not look behind, so there is
     * no second permission check to make and no way to accumulate steps that
     * later turn out to be private.
     */
    applySeatEvent(groupId: string, payload: GroupSeatEventFrame['data']) {
      const profile = payload?.profile;
      const seatId = payload?.conversation_id;
      const data = payload?.data ?? {};
      if (!groupId || !profile || !seatId) return;

      if (payload.type === 'cwd') {
        // Straight to the workspace, keyed by the SEAT: a room has one file
        // tree per member, and the panel keeps them in separate buckets.
        if (typeof data.working_directory === 'string' && data.working_directory) {
          useTerminalPanelStore().setConversationCwd(seatId, data.working_directory);
        }
        return;
      }

      // Only the frames that carry a turn's WORK may open an entry. `error`,
      // `complete` and `compaction_auto_folded` end or annotate a turn — the
      // room hears about those from the post and from `agent_status` — and
      // creating an entry for one of them puts back the live card that the
      // preceding `agent_status: idle` had just removed, leaving a member
      // apparently thinking with nothing to show until its next turn.
      if (
        payload.type !== 'thinking' && payload.type !== 'result'
        && payload.type !== 'terminal' && payload.type !== 'token_usage'
      ) return;

      // Assign, then re-read. `a[k] ?? (a[k] = {})` evaluates to the RAW object
      // being assigned rather than the reactive proxy Vue installs, so every
      // mutation made through it afterwards is invisible to the renderer.
      if (!this.liveTurns[groupId]) this.liveTurns[groupId] = {};
      const turns = this.liveTurns[groupId];
      if (!turns[profile] || turns[profile].conversationId !== seatId) {
        turns[profile] = {
          conversationId: seatId,
          thinkingSteps: [],
          terminalAttachments: [],
          seenSeatSeqs: {},
          startedAt: Date.now(),
        };
      }
      const turn = turns[profile];

      // The catch-up snapshot and the live tail stamp one frame with the same
      // seat sequence number, so this recognises the overlap exactly — where
      // matching on contents cannot tell two identical parallel calls apart.
      const seatSeq = payload.seat_seq;
      if (typeof seatSeq === 'number') {
        if (turn.seenSeatSeqs[seatSeq]) return;
        turn.seenSeatSeqs[seatSeq] = true;
      }

      switch (payload.type) {
        case 'thinking': {
          const step = thinkingStepFromFrame(data);
          // Fallback duplicate check, for a server that stamps no `seat_seq`.
          const key = stepKey(step);
          if (turn.thinkingSteps.some((s) => stepKey(s) === key)) return;
          turn.thinkingSteps.push(step);
          return;
        }
        case 'result': {
          // Same duplicate: re-attaching a result whose call already has one
          // would push it onto the next unanswered step instead, quietly
          // pairing an output with the wrong tool.
          if (
            data.call_id
            && turn.thinkingSteps.some((s) => s.callId === data.call_id && s.result)
          ) return;
          attachResultToSteps(turn.thinkingSteps, data);
          return;
        }
        case 'terminal': {
          const attachment = terminalAttachmentFromFrame(data);
          if (turn.terminalAttachments.some((t) => t.processId === attachment.processId)) {
            return;
          }
          // Named after its owner: a room's tab strip mixes several members'
          // shells, and the command alone does not say whose it is.
          const owned = { ...attachment, ownerLabel: this.nameFor(profile) };
          turn.terminalAttachments.push(owned);
          useTerminalPanelStore().openTerminalFor(seatId, owned);
          return;
        }
        default: {
          turn.tokenUsage = tokenUsageFromFrame(data);
        }
      }
    },

    /**
     * File the reasoning the timeline sent inline under `traceBySource`, and
     * strip it off the row.
     *
     * The server decorates an agent post with the steps of the turn behind it —
     * for the posts this viewer may look behind, on the last segment of each
     * turn — which is what lets a reload render the thinking process with no
     * request per bubble. Stripping keeps the timeline rows lean and leaves one
     * home for a trace, whether it arrived here, was watched live, or was
     * fetched.
     */
    /**
     * Re-read what a just-finished turn cost, for the post it produced.
     *
     * A turn's per-source usage records are written with it, so the rollup this
     * tab already holds for that seat predates them — and the store is
     * cache-first. Without this the new bubble's chip asks, gets the pre-turn
     * answer, finds no record of its own turn and shows tokens with no cost
     * until a reload. FORCED rather than merely invalidated: the bubble mounts
     * and asks in the same tick this runs, so dropping the cache alone is a
     * race it can lose.
     *
     * Only for a seat this viewer may read, which is the same rule that decides
     * whether the chip is rendered at all — otherwise a member watching a busy
     * room would fire a 403 per peer post.
     */
    refreshSeatUsage(row: GroupMessage) {
      const seat = row?.source_conversation_id;
      const profile = row?.sender_profile;
      if (row?.sender_kind !== 'agent' || !seat || !profile) return;
      if (!this.isAdmin && profile !== useSettingsStore().profileId) return;
      // Lazy, like the two-party chat's copy: the usage store must not become a
      // load-time dependency of this one.
      import('./usage')
        .then((m) => {
          const usage = m.useUsageStore();
          usage.invalidateConversation(seat);
          return usage.loadConversationUsage(seat, true);
        })
        .catch(() => {});
    },

    seedTraceFromRow(groupId: string, row: GroupMessage) {
      const key = row?.source_message_id;
      if (!key || row.sender_kind !== 'agent') return;
      if (
        row.thinking_steps === undefined
        && row.source_parts === undefined
        && row.source_token_usage === undefined
      ) return;
      if (!this.traceBySource[groupId]) this.traceBySource[groupId] = {};
      this.traceBySource[groupId][key] = {
        thinkingSteps: thinkingStepsFromRecord(row.thinking_steps) ?? [],
        terminalAttachments: terminalAttachmentsFromParts(row.source_parts).map(
          (t) => ({ ...t, ownerLabel: this.nameFor(row.sender_profile || '') }),
        ),
        tokenUsage: tokenUsageFromRecord(row.source_token_usage),
        loaded: true,
        loading: false,
        error: null,
      };
      delete row.thinking_steps;
      delete row.source_parts;
      delete row.source_token_usage;
    },

    /**
     * Hand a finished turn's live steps to the post it produced.
     *
     * The alternative is for the bubble to fetch a trace this tab just watched
     * being made, so the reasoning it was showing a second ago blinks out and
     * comes back over the network. Keyed by the source message, which every
     * segment of the turn shares.
     */
    adoptLiveTurn(groupId: string, message: GroupMessage) {
      const profile = message.sender_profile;
      const sourceId = message.source_message_id;
      if (message.sender_kind !== 'agent' || !profile || !sourceId) return;
      const turn = this.liveTurns[groupId]?.[profile];
      if (!turn) return;
      if (
        message.source_conversation_id
        && turn.conversationId !== message.source_conversation_id
      ) return;
      // Assign then re-read, never `a[k] ?? (a[k] = {})` — that evaluates to
      // the raw object, not the proxy, and writes through it are invisible to
      // the renderer (see applySeatEvent).
      if (!this.traceBySource[groupId]) this.traceBySource[groupId] = {};
      const traces = this.traceBySource[groupId];
      traces[sourceId] = {
        // Copied, not aliased: the live entry is about to be dropped and its
        // arrays would otherwise still be the ones the bubble renders.
        thinkingSteps: [...turn.thinkingSteps],
        terminalAttachments: [...turn.terminalAttachments],
        // Carried over for the same reason as the steps: the tab watched this
        // turn spend the tokens, and a bubble that showed a count while it ran
        // must not lose it the moment the post lands. The estimated cost still
        // arrives with the usage fetch the chip makes.
        tokenUsage: turn.tokenUsage,
        loaded: true,
        loading: false,
        error: null,
      };
    },

    /**
     * The persisted reasoning behind one agent post, fetched once.
     *
     * The fallback path: a reload and a reconnect both bring the steps inline
     * with the rows (`seedTraceFromRow`), and a turn this tab watched hands its
     * own over (`adoptLiveTurn`). What is left are the rows that arrive by
     * neither route — a `message` frame replayed out of the room's ring moves
     * the cursor past what the reconcile would have re-fetched — so a bubble
     * that mounts with no trace asks for one. `loaded` is set even for an empty
     * answer, so a turn that called no tool is not re-requested every time it
     * scrolls back into view.
     */
    async loadTrace(groupId: string, message: GroupMessage) {
      const key = message?.source_message_id;
      if (!groupId || !key) return;
      // Assign then re-read (see adoptLiveTurn). It matters most here: the
      // result lands AFTER an await, past the flush the outer assignment
      // scheduled, so a write through the raw object would leave the bubble
      // blank forever — and `loaded` would still be set, so nothing retries.
      if (!this.traceBySource[groupId]) this.traceBySource[groupId] = {};
      const traces = this.traceBySource[groupId];
      const existing = traces[key];
      if (existing?.loaded || existing?.loading) return;
      traces[key] = {
        thinkingSteps: [], terminalAttachments: [], loaded: false, loading: true, error: null,
      };
      try {
        // Note the shape: the endpoint answers {conversation_id, message:{…}},
        // so the steps are one level down, not on the root.
        const trace = await this.fetchTrace(groupId, message.id);
        traces[key] = {
          thinkingSteps: thinkingStepsFromRecord(trace?.message?.thinking_steps) ?? [],
          terminalAttachments: terminalAttachmentsFromParts(trace?.message?.parts).map(
            (t) => ({ ...t, ownerLabel: this.nameFor(message.sender_profile || '') }),
          ),
          tokenUsage: tokenUsageFromRecord(trace?.message?.token_usage),
          loaded: true,
          loading: false,
          error: null,
        };
      } catch (e) {
        traces[key] = {
          thinkingSteps: [],
          terminalAttachments: [],
          loaded: false,
          loading: false,
          error: e instanceof Error ? e.message : 'Failed to load the reasoning steps',
        };
      }
    },

    /**
     * Add a message to a room's timeline, in ordering position. Ids are the
     * dedupe key because the same row reaches the tab twice routinely — once
     * from the POST response, once from the stream.
     */
    /**
     * Attach a routing decision to a post already on screen.
     *
     * Arrives as its own frame because the post is published before the router
     * has run (see `GroupMessageRoutingFrame`), so this is what puts the chip on
     * a message the viewer watched arrive rather than only on one they reloaded
     * into.
     *
     * A message we do not hold is ignored rather than stubbed: the only way to
     * get here without it is a reconnect whose replay skipped the row as already
     * seen, and that copy came from the API with the stamp already on it.
     */
    applyRouting(groupId: string, messageId: string, routing: Record<string, any>) {
      const list = this.messagesByGroup[groupId] ?? [];
      const at = list.findIndex((m) => m.id === messageId);
      if (at < 0) return;
      const message = list[at];
      // Replaced, not mutated in place: `readRouting` is called from a computed
      // over the list, and only a new object is certain to invalidate it.
      list[at] = { ...message, metadata: { ...(message.metadata ?? {}), routing } };
      this.messagesByGroup[groupId] = list;
    },

    upsertMessage(groupId: string, message: GroupMessage) {
      const list = this.messagesByGroup[groupId] ?? [];
      const existing = list.findIndex((m) => m.id === message.id);
      if (existing >= 0) {
        list[existing] = message;
      } else {
        let at = list.length;
        while (at > 0 && list[at - 1].ordering > message.ordering) at -= 1;
        list.splice(at, 0, message);
      }
      this.messagesByGroup[groupId] = list;
      const cursor = this.lastOrderingByGroup[groupId] ?? -1;
      if (message.ordering > cursor) this.lastOrderingByGroup[groupId] = message.ordering;
    },

    async sendMessage(text: string) {
      const settings = useSettingsStore();
      const groupId = this.activeGroupId;
      if (!groupId || !text.trim()) return;
      this.sending = true;
      try {
        // The POST answers 202 with the persisted row before any agent has
        // run, so showing it right away is not an optimistic bubble — and the
        // id dedupe absorbs the stream's copy of the same row.
        const message = await postGroupMessage(
          settings.agentUrl, settings.authToken, groupId, text,
        );
        if (message) this.upsertMessage(groupId, message);
        this.error = null;
      } catch (e) {
        this.error = e instanceof Error ? e.message : 'Failed to post the message';
        throw e;
      } finally {
        this.sending = false;
      }
    },

    async createGroup(payload: CreateGroupPayload): Promise<GroupChat> {
      const settings = useSettingsStore();
      const group = await apiCreateGroup(settings.agentUrl, settings.authToken, payload);
      this.mergeGroup(group);
      return group;
    },

    async updateGroup(groupId: string, payload: UpdateGroupPayload): Promise<GroupChat> {
      const settings = useSettingsStore();
      const group = await apiUpdateGroup(
        settings.agentUrl, settings.authToken, groupId, payload,
      );
      this.mergeGroup(group);
      return group;
    },

    async deleteGroup(groupId: string) {
      const settings = useSettingsStore();
      await apiDeleteGroup(settings.agentUrl, settings.authToken, groupId);
      closeStream(groupId);
      this.groups = this.groups.filter((g) => g.id !== groupId);
      delete this.messagesByGroup[groupId];
      delete this.lastOrderingByGroup[groupId];
      delete this.reconcileFloorByGroup[groupId];
      delete this.agentStatusByGroup[groupId];
      delete this.liveTurns[groupId];
      delete this.traceBySource[groupId];
      delete this.streamStatusByGroup[groupId];
      if (this.activeGroupId === groupId) this.activeGroupId = null;
    },

    async fetchTrace(groupId: string, messageId: string): Promise<GroupMessageTrace> {
      const settings = useSettingsStore();
      return fetchMessageTrace(settings.agentUrl, settings.authToken, groupId, messageId);
    },

    /** Insert or replace a group row without disturbing the list's order. */
    mergeGroup(group: GroupChat) {
      const idx = this.groups.findIndex((g) => g.id === group.id);
      if (idx >= 0) {
        // Preserve the list-only ``last_message`` when a detail/PATCH payload
        // omits it, so the sidebar preview does not blink away on every edit.
        const previous = this.groups[idx];
        this.groups[idx] = {
          ...group,
          last_message: group.last_message ?? previous.last_message,
        };
      } else {
        this.groups = [...this.groups, group];
      }
    },

    resetForProfileSwitch() {
      for (const groupId of Array.from(streamHandles.keys())) closeStream(groupId);
      this.groups = [];
      this.groupsLoaded = false;
      this.loading = false;
      this.activeGroupId = null;
      this.messagesByGroup = {};
      this.lastOrderingByGroup = {};
      this.reconcileFloorByGroup = {};
      this.agentStatusByGroup = {};
      // Both are one profile's view of what it was allowed to see — the profile
      // being switched to may be allowed less.
      this.liveTurns = {};
      this.traceBySource = {};
      this.streamStatusByGroup = {};
      this.agentNames = {};
      this.sending = false;
      this.error = null;
    },
  },
});
