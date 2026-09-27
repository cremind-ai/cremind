import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { useSettingsStore } from './settings';
import {
  assignTagBridge,
  cancelTagDelivery,
  claimTag,
  clearTag,
  createContentCredential,
  deleteTagCompanion,
  displayOnTag,
  fetchTagPreview,
  forgetHardwareDevice,
  getTagCommand,
  getTagDefaults,
  getTagDelivery,
  getTagHardware,
  getTagSettings,
  getTagsOverview,
  identifyTag,
  listContentCredentials,
  listTagCompanions,
  listTagDeliveries,
  queueTagCommand,
  refreshTag,
  registerTagCompanion,
  releaseTag,
  renameHardwareDevice,
  renameTagDevice,
  revokeContentCredential,
  rotateTagCompanion,
  saveTagDefaults,
  saveTagSettings,
  type DeliveryQuery,
  type DisplayNotePayload,
  type TagCommand,
  type TagCompanionSummary,
  type TagCredential,
  type TagDelivery,
  type TagHardware,
  type TagOptions,
  type TagOverview,
  type TagSettings,
} from '../services/tagsApi';

/**
 * Cremind Tag state for the signed-in profile: its tags (overview), its
 * settings and content credentials, and — for the admin — the hardware
 * inventory. Everything is per profile on the server; `reset()` runs when a
 * page mounts for a (possibly different) profile so nothing from the last
 * one is shown.
 */
export const useTagsStore = defineStore('tags', () => {
  const settingsStore = useSettingsStore();
  const url = () => settingsStore.agentUrl;
  const token = () => settingsStore.authToken;

  const overview = ref<TagOverview | null>(null);
  /** device id → deliveries still on their way to that tag. */
  const pendingByDevice = ref<Record<string, number>>({});
  const settings = ref<TagSettings | null>(null);
  const companions = ref<TagCompanionSummary[]>([]);
  const credentials = ref<TagCredential[]>([]);
  const hardware = ref<TagHardware | null>(null);
  const loadedFor = ref<string>('');

  const devices = computed(() => overview.value?.devices ?? []);

  function reset(profile: string) {
    if (loadedFor.value === profile) return;
    loadedFor.value = profile;
    overview.value = null;
    pendingByDevice.value = {};
    settings.value = null;
    companions.value = [];
    credentials.value = [];
    hardware.value = null;
  }

  // ── overview ──

  async function loadOverview(): Promise<TagOverview> {
    const [ov, active] = await Promise.all([
      getTagsOverview(url(), token()),
      listTagDeliveries(url(), token(), { state: 'active', limit: 200 })
        .catch(() => null),
    ]);
    overview.value = ov;
    if (active) {
      const counts: Record<string, number> = {};
      for (const d of active.deliveries) counts[d.device_id] = (counts[d.device_id] ?? 0) + 1;
      pendingByDevice.value = counts;
    }
    return ov;
  }

  function patchDevice(device: { id: string } & Record<string, any>) {
    const ov = overview.value;
    if (!ov) return;
    const idx = ov.devices.findIndex((d) => d.id === device.id);
    if (idx >= 0) {
      // Keep the overview-only fields (previews, companion) the PATCH lacks.
      ov.devices[idx] = { ...ov.devices[idx], ...device };
    }
  }

  async function renameDevice(deviceId: string, name: string) {
    const device = await renameTagDevice(url(), token(), deviceId, name);
    patchDevice(device);
    return device;
  }

  function display(deviceId: string, note: DisplayNotePayload): Promise<TagDelivery> {
    return displayOnTag(url(), token(), deviceId, note);
  }
  function clear(deviceId: string): Promise<TagDelivery> {
    return clearTag(url(), token(), deviceId);
  }
  function refresh(deviceId: string): Promise<TagCommand> {
    return refreshTag(url(), token(), deviceId);
  }
  function identify(deviceId: string): Promise<TagCommand> {
    return identifyTag(url(), token(), deviceId);
  }
  function preview(deviceId: string, kind: 'desired' | 'displayed') {
    return fetchTagPreview(url(), token(), deviceId, kind);
  }

  // ── deliveries ──

  function deliveries(query: DeliveryQuery) {
    return listTagDeliveries(url(), token(), query);
  }
  function delivery(id: number) {
    return getTagDelivery(url(), token(), id);
  }
  function cancelDelivery(id: number) {
    return cancelTagDelivery(url(), token(), id);
  }

  // ── settings + credentials ──

  async function loadSettings(): Promise<TagSettings> {
    settings.value = await getTagSettings(url(), token());
    return settings.value;
  }

  async function saveSettings(body: { enabled?: boolean; options?: TagOptions }): Promise<TagSettings> {
    const saved = await saveTagSettings(url(), token(), body);
    settings.value = saved;
    if (overview.value && body.enabled !== undefined) overview.value.enabled = saved.enabled;
    return saved;
  }

  async function loadCompanions() {
    companions.value = await listTagCompanions(url(), token());
    return companions.value;
  }

  async function loadCredentials() {
    credentials.value = await listContentCredentials(url(), token());
    return credentials.value;
  }

  async function createCredential(companionId: string, label: string) {
    const payload = await createContentCredential(url(), token(), {
      companion_id: companionId, ...(label.trim() ? { label: label.trim() } : {}),
    });
    credentials.value = [payload.credential, ...credentials.value];
    return payload;
  }

  async function revokeCredential(id: string) {
    const cred = await revokeContentCredential(url(), token(), id);
    credentials.value = credentials.value.map((c) => (c.id === id ? cred : c));
    return cred;
  }

  // ── admin: hardware ──

  async function loadHardware(): Promise<TagHardware> {
    hardware.value = await getTagHardware(url(), token());
    return hardware.value;
  }

  function registerCompanion(name: string) {
    return registerTagCompanion(url(), token(), name);
  }
  function rotateCompanion(id: string) {
    return rotateTagCompanion(url(), token(), id);
  }
  function deleteCompanion(id: string) {
    return deleteTagCompanion(url(), token(), id);
  }
  function queueCommand(companionId: string, kind: string, args?: Record<string, any>) {
    return queueTagCommand(url(), token(), { companion_id: companionId, kind, ...(args ? { args } : {}) });
  }
  function command(id: string) {
    return getTagCommand(url(), token(), id);
  }
  function claim(deviceId: string, body: { owner: string; bridge_id?: string; name?: string }) {
    return claimTag(url(), token(), deviceId, body);
  }
  function assign(deviceId: string, bridgeId: string) {
    return assignTagBridge(url(), token(), deviceId, bridgeId);
  }
  function release(deviceId: string) {
    return releaseTag(url(), token(), deviceId);
  }
  function renameHardware(deviceId: string, name: string) {
    return renameHardwareDevice(url(), token(), deviceId, name);
  }
  function forget(deviceId: string) {
    return forgetHardwareDevice(url(), token(), deviceId);
  }
  function defaults() {
    return getTagDefaults(url(), token());
  }
  function saveDefaults(value: TagOptions) {
    return saveTagDefaults(url(), token(), value);
  }

  return {
    overview,
    pendingByDevice,
    settings,
    companions,
    credentials,
    hardware,
    devices,
    reset,
    loadOverview,
    patchDevice,
    renameDevice,
    display,
    clear,
    refresh,
    identify,
    preview,
    deliveries,
    delivery,
    cancelDelivery,
    loadSettings,
    saveSettings,
    loadCompanions,
    loadCredentials,
    createCredential,
    revokeCredential,
    loadHardware,
    registerCompanion,
    rotateCompanion,
    deleteCompanion,
    queueCommand,
    command,
    claim,
    assign,
    release,
    renameHardware,
    forget,
    defaults,
    saveDefaults,
  };
});
