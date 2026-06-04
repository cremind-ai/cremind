import { defineStore } from 'pinia';
import { useSettingsStore } from './settings';
import {
  fetchChannelCatalog,
  fetchChannels,
  createChannel as apiCreateChannel,
  updateChannel as apiUpdateChannel,
  deleteChannel as apiDeleteChannel,
  fetchChannelSenders,
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
      const externals = state.channels.filter(
        (ch) => ch.channel_type !== MAIN_CHANNEL_TYPE,
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
      return updated;
    },
    async deleteChannel(channelId: string) {
      const settings = useSettingsStore();
      await apiDeleteChannel(settings.agentUrl, settings.authToken, channelId);
      this.channels = this.channels.filter((c) => c.id !== channelId);
      // Reset filter if user just deleted the channel they were filtering on,
      // or if removing it leaves the ``all`` virtual option with no externals
      // to combine.
      const hasExternals = this.channels.some(
        (c) => c.channel_type !== MAIN_CHANNEL_TYPE,
      );
      const filterStillValid =
        this.activeFilter === MAIN_CHANNEL_TYPE
        || (this.activeFilter === ALL_CHANNELS_FILTER && hasExternals)
        || this.channels.some((c) => c.channel_type === this.activeFilter);
      if (!filterStillValid) {
        this.activeFilter = MAIN_CHANNEL_TYPE;
      }
    },
    async fetchSenders(channelId: string): Promise<ChannelSenderRow[]> {
      const settings = useSettingsStore();
      return fetchChannelSenders(settings.agentUrl, settings.authToken, channelId);
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
