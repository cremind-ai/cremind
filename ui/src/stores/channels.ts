import { defineStore } from 'pinia';
import { useSettingsStore } from './settings';
import {
  fetchChannelCatalog,
  fetchChannels,
  createChannel as apiCreateChannel,
  updateChannel as apiUpdateChannel,
  deleteChannel as apiDeleteChannel,
  fetchChannelSenders,
  setSenderAuthenticated as apiSetSenderAuthenticated,
  type ChannelCatalogEntry,
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
    setFilter(filter: string) {
      this.activeFilter = filter || MAIN_CHANNEL_TYPE;
    },
    resetForProfileSwitch() {
      this.catalog = {};
      this.channels = [];
      this.activeFilter = MAIN_CHANNEL_TYPE;
    },
  },
});
