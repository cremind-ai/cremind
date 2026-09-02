import { defineStore } from 'pinia';
import { useSettingsStore } from './settings';
import {
  fetchChannelCatalog,
  fetchChannels,
  createChannel as apiCreateChannel,
  updateChannel as apiUpdateChannel,
  deleteChannel as apiDeleteChannel,
  repairChannel as apiRepairChannel,
  fetchChannelSenders,
  setSenderAuthenticated as apiSetSenderAuthenticated,
  setSenderConfirmation as apiSetSenderConfirmation,
  clearSenderHistory as apiClearSenderHistory,
  deleteSender as apiDeleteSender,
  fetchChannelGroups,
  updateChannelGroup as apiUpdateChannelGroup,
  deleteChannelGroup as apiDeleteChannelGroup,
  refreshChannelGroupRoster as apiRefreshChannelGroupRoster,
  fetchAvailableChannelGroups as apiFetchAvailableChannelGroups,
  addChannelGroup as apiAddChannelGroup,
  type AvailableChannelGroupList,
  type ChannelCatalogEntry,
  type ChannelGroup,
  type ChannelGroupSettings,
  type ChannelGroupStatus,
  type ChannelRow,
  type ChannelSenderRow,
  type CreateChannelPayload,
  type UpdateChannelPayload,
} from '../services/channelApi';

// The default conversation filter: built-in web/CLI conversations live on
// the implicit ``main`` channel. The filter dropdown shows ``main`` plus any
// external channels the user has registered, and an ``all`` virtual option
// (only when at least one external channel exists) that shows conversations
// across every channel sorted by recency.
export const MAIN_CHANNEL_TYPE = 'main';
export const ALL_CHANNELS_FILTER = 'all';

export const useChannelsStore = defineStore('channels', {
  state: () => ({
    catalog: {} as Record<string, ChannelCatalogEntry>,
    channels: [] as ChannelRow[],
    activeFilter: MAIN_CHANNEL_TYPE as string,
    loading: false,
    // Platform group chats, per channel. Loaded eagerly for every channel with
    // the feature on, because the pending badge has to show on a COLLAPSED card
    // — a group waiting for approval that only appears once you expand the card
    // is a group nobody approves.
    groupsByChannel: {} as Record<string, ChannelGroup[]>,
    groupsEnabledByChannel: {} as Record<string, boolean>,
    groupsLoading: {} as Record<string, boolean>,
  }),

  getters: {
    filterOptions(state): Array<{ value: string; label: string; icon?: string }> {
      // Notification-mode channels are push-only — they hold no conversations,
      // so they must never appear in the conversation-list channel filter.
      const externals = state.channels.filter(
        (ch) => ch.channel_type !== MAIN_CHANNEL_TYPE && ch.mode !== 'notification',
      );
      const opts: Array<{ value: string; label: string; icon?: string }> = [];
      // ``All`` only makes sense when there's more than one channel to combine.
      if (externals.length > 0) {
        opts.push({
          value: ALL_CHANNELS_FILTER,
          label: 'All',
          icon: 'mdi:format-list-bulleted',
        });
      }
      opts.push({ value: MAIN_CHANNEL_TYPE, label: 'Main', icon: 'mdi:home-outline' });
      for (const ch of externals) {
        const entry = state.catalog[ch.channel_type];
        opts.push({
          value: ch.channel_type,
          label: entry?.display_name || ch.channel_type,
          icon: entry?.icon,
        });
      }
      return opts;
    },
    channelById(state) {
      return (id: string | null | undefined) =>
        id ? state.channels.find((c) => c.id === id) : undefined;
    },
    mainChannel(state): ChannelRow | undefined {
      return state.channels.find((c) => c.channel_type === MAIN_CHANNEL_TYPE);
    },
    /** Whether this platform can take part in group chats at all. */
    supportsGroupChats(state) {
      return (channelType: string) =>
        state.catalog[channelType]?.supports_group_chats === true;
    },
    /** How many of a channel's groups are waiting for a decision. */
    pendingGroupCount(state) {
      return (channelId: string) =>
        (state.groupsByChannel[channelId] || []).filter(
          (g) => g.status === 'pending',
        ).length;
    },
  },

  actions: {
    async loadCatalog() {
      const settings = useSettingsStore();
      if (!settings.authToken) return;
      this.catalog = await fetchChannelCatalog(settings.agentUrl, settings.authToken);
    },
    async loadChannels() {
      const settings = useSettingsStore();
      if (!settings.authToken) return;
      this.loading = true;
      try {
        this.channels = await fetchChannels(settings.agentUrl, settings.authToken);
      } finally {
        this.loading = false;
      }
      this.ensureValidActiveFilter();
    },
    async createChannel(payload: CreateChannelPayload): Promise<ChannelRow> {
      const settings = useSettingsStore();
      const created = await apiCreateChannel(settings.agentUrl, settings.authToken, payload);
      await this.loadChannels();
      return created;
    },
    async updateChannel(channelId: string, payload: UpdateChannelPayload): Promise<ChannelRow> {
      const settings = useSettingsStore();
      const updated = await apiUpdateChannel(settings.agentUrl, settings.authToken, channelId, payload);
      const idx = this.channels.findIndex((c) => c.id === channelId);
      if (idx >= 0) this.channels[idx] = updated;
      // Editing a channel into notification mode (or otherwise) can make the
      // current filter selection disappear from the dropdown — re-validate it.
      this.ensureValidActiveFilter();
      return updated;
    },
    async deleteChannel(channelId: string) {
      const settings = useSettingsStore();
      await apiDeleteChannel(settings.agentUrl, settings.authToken, channelId);
      this.channels = this.channels.filter((c) => c.id !== channelId);
      this.ensureValidActiveFilter();
    },
    /** Wipe a channel's saved pairing session and restart it pairing again.
     *  Recovers a channel whose session the platform invalidated elsewhere,
     *  without losing the row (and with it, its senders and bound groups). */
    async repairChannel(channelId: string): Promise<ChannelRow> {
      const settings = useSettingsStore();
      const repaired = await apiRepairChannel(
        settings.agentUrl, settings.authToken, channelId,
      );
      const idx = this.channels.findIndex((c) => c.id === channelId);
      if (idx >= 0) this.channels[idx] = repaired;
      return repaired;
    },
    /** Reset ``activeFilter`` to ``main`` if it's no longer a selectable option
     *  (channel deleted, switched to notification mode, ``all`` left with no
     *  conversational externals, …). ``filterOptions`` is the source of truth. */
    ensureValidActiveFilter() {
      const valid = new Set(this.filterOptions.map((o) => o.value));
      if (!valid.has(this.activeFilter)) {
        this.activeFilter = MAIN_CHANNEL_TYPE;
      }
    },
    async fetchSenders(channelId: string): Promise<ChannelSenderRow[]> {
      const settings = useSettingsStore();
      return fetchChannelSenders(settings.agentUrl, settings.authToken, channelId);
    },
    async setSenderAuthenticated(
      channelId: string, senderId: string, authenticated: boolean,
    ): Promise<ChannelSenderRow> {
      const settings = useSettingsStore();
      return apiSetSenderAuthenticated(
        settings.agentUrl, settings.authToken, channelId, senderId, authenticated,
      );
    },
    async clearSenderHistory(
      channelId: string, senderId: string,
    ): Promise<{ conversation_id: string | null; cleared_messages: number }> {
      const settings = useSettingsStore();
      return apiClearSenderHistory(
        settings.agentUrl, settings.authToken, channelId, senderId,
      );
    },
    async setSenderConfirmation(
      channelId: string, senderId: string, mode: 'required' | 'skip' | null,
    ): Promise<ChannelSenderRow> {
      const settings = useSettingsStore();
      return apiSetSenderConfirmation(
        settings.agentUrl, settings.authToken, channelId, senderId, mode,
      );
    },
    async deleteSender(
      channelId: string, senderId: string,
    ): Promise<{ conversation_id: string | null; deleted_messages: number }> {
      const settings = useSettingsStore();
      return apiDeleteSender(
        settings.agentUrl, settings.authToken, channelId, senderId,
      );
    },
    async loadGroups(channelId: string): Promise<ChannelGroup[]> {
      const settings = useSettingsStore();
      if (!settings.authToken) return [];
      this.groupsLoading = { ...this.groupsLoading, [channelId]: true };
      try {
        const result = await fetchChannelGroups(
          settings.agentUrl, settings.authToken, channelId,
        );
        this.groupsByChannel = {
          ...this.groupsByChannel, [channelId]: result.groups,
        };
        this.groupsEnabledByChannel = {
          ...this.groupsEnabledByChannel, [channelId]: result.group_chats_enabled,
        };
        return result.groups;
      } finally {
        this.groupsLoading = { ...this.groupsLoading, [channelId]: false };
      }
    },
    /** Load groups for every channel that can have them.
     *
     *  ``allSettled`` because one channel failing (a stopped adapter, a
     *  transient 500) must not cost the others their pending badges. */
    async loadGroupsForAll() {
      const targets = this.channels.filter(
        (ch) => this.supportsGroupChats(ch.channel_type),
      );
      await Promise.allSettled(targets.map((ch) => this.loadGroups(ch.id)));
    },
    _replaceGroup(channelId: string, group: ChannelGroup) {
      const rows = this.groupsByChannel[channelId] || [];
      const idx = rows.findIndex((g) => g.id === group.id);
      const next = idx >= 0
        ? [...rows.slice(0, idx), group, ...rows.slice(idx + 1)]
        : [group, ...rows];
      this.groupsByChannel = { ...this.groupsByChannel, [channelId]: next };
    },
    async setGroupStatus(
      channelId: string, groupId: string, status: ChannelGroupStatus,
    ): Promise<ChannelGroup> {
      const settings = useSettingsStore();
      const group = await apiUpdateChannelGroup(
        settings.agentUrl, settings.authToken, channelId, groupId, { status },
      );
      this._replaceGroup(channelId, group);
      return group;
    },
    async setGroupSettings(
      channelId: string, groupId: string, patch: Partial<ChannelGroupSettings>,
    ): Promise<ChannelGroup> {
      const settings = useSettingsStore();
      const group = await apiUpdateChannelGroup(
        settings.agentUrl, settings.authToken, channelId, groupId,
        { settings: patch },
      );
      this._replaceGroup(channelId, group);
      return group;
    },
    async deleteGroup(channelId: string, groupId: string) {
      const settings = useSettingsStore();
      await apiDeleteChannelGroup(
        settings.agentUrl, settings.authToken, channelId, groupId,
      );
      this.groupsByChannel = {
        ...this.groupsByChannel,
        [channelId]: (this.groupsByChannel[channelId] || []).filter(
          (g) => g.id !== groupId,
        ),
      };
    },
    /** The groups the account is already in, for the picker.
     *
     *  Not cached: it is a live question for the platform, and a stale answer
     *  would offer a group the account has since left. */
    async loadAvailableGroups(
      channelId: string,
    ): Promise<AvailableChannelGroupList> {
      const settings = useSettingsStore();
      return apiFetchAvailableChannelGroups(
        settings.agentUrl, settings.authToken, channelId,
      );
    },
    /** Enable a set of picked groups, then re-read the channel's list.
     *
     *  Sequential rather than parallel: each one creates a conversation and
     *  asks the platform for a roster, and firing ten of those at a sidecar at
     *  once is how a paired session drops. */
    async addGroups(
      channelId: string,
      picks: { platform_chat_id: string; title?: string; chat_type?: string | null }[],
    ): Promise<number> {
      const settings = useSettingsStore();
      let added = 0;
      for (const pick of picks) {
        await apiAddChannelGroup(
          settings.agentUrl, settings.authToken, channelId, pick,
        );
        added += 1;
      }
      await this.loadGroups(channelId);
      return added;
    },
    async refreshGroupRoster(
      channelId: string, groupId: string,
    ): Promise<'roster' | 'unsupported'> {
      const settings = useSettingsStore();
      const result = await apiRefreshChannelGroupRoster(
        settings.agentUrl, settings.authToken, channelId, groupId,
      );
      if (result.group) this._replaceGroup(channelId, result.group);
      return result.source;
    },
    setFilter(filter: string) {
      this.activeFilter = filter || MAIN_CHANNEL_TYPE;
    },
    resetForProfileSwitch() {
      this.catalog = {};
      this.channels = [];
      this.activeFilter = MAIN_CHANNEL_TYPE;
      this.groupsByChannel = {};
      this.groupsEnabledByChannel = {};
      this.groupsLoading = {};
    },
  },
});
