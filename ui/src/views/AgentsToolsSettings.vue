<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import {
  ElCard, ElForm, ElFormItem, ElInput,
  ElButton, ElMessage, ElMessageBox, ElDialog, ElDivider,
  ElRadioGroup, ElRadioButton, ElTooltip,
  ElTour, ElTourStep,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { RouterLink } from 'vue-router';
import { useSettingsStore, type ProfileValue } from '../stores/settings';
import {
  listAgents, addAgent, removeAgent, updateAgentConfig,
  listTools, updateToolConfig, setToolEnabled, setToolFullReasoning,
  updateToolArguments, resetToolLLMKeys,
  listLLMProviders, getProviderModels,
  getSkillMode, setSkillMode,
  streamFeaturesInstall, FeatureNotInstalledError,
  type RemoteAgentInfo, type ToolStatus, type LLMProvider, type SkillMode,
  type FeatureNotInstalledDetail, type FeatureInstallEvent,
} from '../services/configApi';
import {
  registerSkillLongRunningApp, DuplicateAutostartError,
} from '../services/processApi';
import { openSettingsStateStream, type SettingsStateStreamHandle } from '../services/settingsStateStream';

type LLMDefaultField =
  | 'description'
  | 'system_prompt'
  | 'llm_provider'
  | 'llm_model'
  | 'reasoning_effort'
  | 'full_reasoning';
import type { JsonSchema } from '../services/agentApi';
import { getAuthUrl, unlinkAgent, reconnectAgent } from '../services/agentApi';
import { useLLMModels } from '../composables/useLLMModels';
import ItemCardHeader from '../components/shared/ItemCardHeader.vue';
import ToolVariablesForm from '../components/shared/ToolVariablesForm.vue';
import ToolArgumentsForm from '../components/shared/ToolArgumentsForm.vue';
import LLMParametersForm from '../components/shared/LLMParametersForm.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const route = useRoute();
const settingsStore = useSettingsStore();

// Element Plus Tour wiring — fired by ?skillId=...&tour=1 in the URL when the
// user clicks a `skill_register_required` notification.
const tourOpen = ref(false);
const tourTargetSelector = ref<string | undefined>(undefined);
const tourSkillName = ref<string>('');
const { rebuildModelList, getFilteredModels, getReasoningOptions } = useLLMModels();

// ── Unified item type ──
interface UnifiedItem {
  name: string;             // legacy: stable identifier (now == tool_id for builtin/skill)
  displayName: string;      // human-readable name shown on the card
  kind: 'builtin' | 'skill' | 'mcp-remote' | 'a2a-remote';

  toolName: string | null;  // tool_id used by /api/tools/{tool_id} endpoints
  toolConfigFields: Record<string, { description: string; type: string; secret: boolean; configured: boolean }>;
  toolConfigValues: Record<string, string>;
  toolConfigured: boolean;

  agentName: string;
  description: string;
  url: string;
  systemPrompt: string;
  llmProvider: string;
  llmModel: string;
  reasoningEffort: string;
  fullReasoning: boolean;
  enabled: boolean;

  showAuthenticate: boolean;
  showUnlink: boolean;
  badgeClass: string;
  statusText: string;
  connectionError: string | null;
  hasAgent: boolean;
  agentType: string | null;

  argumentsSchema: JsonSchema | null;
  argValues: Record<string, ProfileValue>;

  expanded: boolean;
  saving: boolean;
  /** Built-in tools only: false until a child LLM is bound (post-setup). */
  llmBound: boolean;
  /** Code-level defaults from TOOL_CONFIG.llm_parameters (placeholders only).
   *  Empty for skills / MCP / A2A — those don't ship code defaults. */
  llmDefaults: NonNullable<ToolStatus['llm_defaults']>;
  /** LLM fields whose user override is forbidden. The matching switches/inputs
   *  in the form are rendered disabled. */
  lockedLlmFields: string[];
  /** Skills only: declared ``long_running_app`` block; null when absent. */
  longRunningApp: { command: string; description?: string } | null;
  /** Transient: true while a Register Long-Running Process click is in flight. */
  registering: boolean;
  /** Transient: most recent successful registration result, used to render
   *  the inline confirmation card with a link to the process detail page. */
  lastRegisteredProcess: { process_id: string; command: string } | null;
}

// ── Data ──
const items = ref<UnifiedItem[]>([]);
const llmProviders = ref<LLMProvider[]>([]);
const loading = ref(false);
const skillMode = ref<SkillMode>('manual');
const skillModeSaving = ref(false);
const embeddingEnabled = ref(true);

// Add agent dialog
const showAddDialog = ref(false);
const addForm = ref({
  url: '',
  type: 'mcp' as 'mcp' | 'a2a',
  inputMode: 'url' as 'url' | 'json',
  jsonConfig: '',
  description: '',
  system_prompt: '',
  llm_provider: '',
  llm_model: '',
  reasoning_effort: '',
});
const adding = ref(false);
const jsonConfigError = ref('');

// Categorized items -- driven by tool_type, not by agent_type
const builtinItems = computed(() => items.value.filter(i => i.kind === 'builtin'));
const skillItems = computed(() => items.value.filter(i => i.kind === 'skill'));
const mcpRemoteItems = computed(() => items.value.filter(i => i.kind === 'mcp-remote'));
const a2aRemoteItems = computed(() => items.value.filter(i => i.kind === 'a2a-remote'));

// ── Feature install dialog ──
// Shown when toggling on a built-in tool whose optional pip extras are
// not yet installed. Mirrors EmbeddingSettings.vue's progress flow: an
// inline log streams pip output via SSE; the dialog closes on success
// and surfaces a "restart server" CTA when the feature requires it.
const featureInstallOpen = ref(false);
const featureInstallDetail = ref<FeatureNotInstalledDetail | null>(null);
const featureInstallPendingItem = ref<UnifiedItem | null>(null);
const featureInstallBusy = ref(false);
const featureInstallLog = ref<string[]>([]);
const featureInstallRestartRequired = ref(false);
const featureInstallError = ref<string | null>(null);

// Listen for auth completion from the OAuth callback tab
function handleAuthMessage(event: MessageEvent) {
  if (event.origin !== window.location.origin) return;
  if (event.data?.type === 'a2a-auth-complete') {
    reloadAll();
    ElMessage.success('Agent linked successfully');
  }
}

// Live updates: the settings-state SSE stream pings whenever any tool,
// agent, LLM provider, or skill mutates server-side. Refetching on each
// ping keeps the page reactive without a manual refresh — the stream is
// shared across tabs / drawers, so this doesn't open a duplicate
// connection.
let settingsStream: SettingsStateStreamHandle | null = null;

function openLiveSettingsStream() {
  if (settingsStream) return;
  if (!settingsStore.authToken || !settingsStore.profileId) return;
  // Skip the very first ping — onMounted's initial fetch is in flight.
  let firstPing = true;
  settingsStream = openSettingsStateStream(
    settingsStore.agentUrl, settingsStore.authToken, settingsStore.profileId,
    () => {
      if (firstPing) { firstPing = false; return; }
      reloadAll();
    },
  );
}

function closeLiveSettingsStream() {
  if (!settingsStream) return;
  try { settingsStream.close(); } catch { /* ignore */ }
  settingsStream = null;
}

onMounted(async () => {
  window.addEventListener('message', handleAuthMessage);
  openLiveSettingsStream();
  loading.value = true;
  try {
    const [agentRes, toolRes, provRes, skillModeRes] = await Promise.all([
      listAgents(settingsStore.agentUrl, settingsStore.authToken),
      listTools(settingsStore.agentUrl, settingsStore.authToken),
      listLLMProviders(settingsStore.agentUrl, settingsStore.authToken),
      getSkillMode(settingsStore.agentUrl, settingsStore.authToken, props.profile)
        .catch(() => ({ mode: 'manual' as SkillMode, embedding_enabled: false })),
    ]);

    llmProviders.value = provRes.providers;
    skillMode.value = skillModeRes.mode;
    embeddingEnabled.value = skillModeRes.embedding_enabled !== false;

    // Load models for all providers using composable
    const providersWithModels: { name: string; display_name: string; models: any[] }[] = [];
    for (const p of provRes.providers) {
      try {
        const modelRes = await getProviderModels(settingsStore.agentUrl, settingsStore.authToken, p.name);
        providersWithModels.push({ name: p.name, display_name: p.display_name, models: modelRes.models });
      } catch {
        providersWithModels.push({ name: p.name, display_name: p.display_name, models: [] });
      }
    }
    rebuildModelList(providersWithModels);

    buildUnifiedItems(agentRes.agents, toolRes.tools);
    await maybeStartSkillRegistrationTour();
  } catch (e) {
    ElMessage.error('Failed to load agents and tools');
  } finally {
    loading.value = false;
  }
});

async function maybeStartSkillRegistrationTour() {
  const skillId = typeof route.query.skillId === 'string' ? route.query.skillId : '';
  const wantsTour = route.query.tour === '1';
  if (!skillId || !wantsTour) return;

  const target = items.value.find(i => i.kind === 'skill' && i.name === skillId);
  if (!target || !target.longRunningApp) return;

  target.expanded = true;
  tourSkillName.value = target.displayName || target.name;

  await nextTick();
  // Scroll the .lra-section into view so the tour balloon lands on something
  // the user can actually see.
  const escapedId = (window.CSS && CSS.escape ? CSS.escape(skillId) : skillId);
  const sectionSelector = `.lra-section[data-skill-id="${escapedId}"]`;
  const sectionEl = document.querySelector<HTMLElement>(sectionSelector);
  if (sectionEl) {
    sectionEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  tourTargetSelector.value = `${sectionSelector} button`;

  await nextTick();
  tourOpen.value = true;

  // Strip the query so a refresh doesn't re-fire the tour, and so a second
  // click on a different skill notification re-triggers the watcher cleanly.
  router.replace({ query: {} });
}

onUnmounted(() => {
  window.removeEventListener('message', handleAuthMessage);
  closeLiveSettingsStream();
});

function getDefaultForType(type: string): ProfileValue {
  if (type === 'number') return 0;
  if (type === 'boolean') return false;
  return '';
}

function initVarValues(
  fields: Record<string, { default?: unknown }> | undefined,
  stored: Record<string, string> | undefined,
): Record<string, string> {
  const values: Record<string, string> = {};
  if (fields) {
    for (const [key, field] of Object.entries(fields)) {
      if (field.default != null) values[key] = String(field.default);
    }
  }
  if (stored) Object.assign(values, stored);
  return values;
}

function initArgValues(schema: JsonSchema | null, storedValues?: Record<string, unknown> | null): Record<string, ProfileValue> {
  if (!schema || !schema.properties) return {};
  const stored = storedValues || {};
  const values: Record<string, ProfileValue> = {};
  for (const [propName, propDef] of Object.entries(schema.properties)) {
    values[propName] = (stored[propName] as ProfileValue) ?? getDefaultForType(propDef.type);
  }
  return values;
}

function buildUnifiedItems(agents: RemoteAgentInfo[], tools: ToolStatus[]) {
  const result: UnifiedItem[] = [];
  // Remote a2a/mcp tools come from /api/agents below -- skip them in the
  // tools list to avoid duplicates. /api/tools now returns {builtin, skill,
  // a2a, mcp} (intrinsic is filtered server-side).
  const localTools = tools.filter(t => t.tool_type === 'builtin' || t.tool_type === 'skill');

  for (const tool of localTools) {
    const isSkill = tool.tool_type === 'skill';
    const llmCfg = (tool.config?.llm ?? {}) as Record<string, unknown>;
    const metaCfg = (tool.config?.meta ?? {}) as Record<string, string>;
    const schema = (tool.arguments_schema as JsonSchema | null) ?? null;

    const llmDefaults = tool.llm_defaults ?? {};
    const lockedLlmFields = tool.locked_llm_fields ?? [];
    // For locked `full_reasoning`, surface the code default in the toggle so
    // the user sees the effective value instead of an unwritten DB blank.
    const fullReasoningEffective = lockedLlmFields.includes('full_reasoning')
      ? !!llmDefaults.full_reasoning
      : !!tool.full_reasoning;
    result.push({
      name: tool.tool_id,
      displayName: tool.name,
      kind: isSkill ? 'skill' : 'builtin',
      toolName: tool.tool_id,
      toolConfigFields: { ...(tool.required_fields ?? {}) },
      toolConfigValues: initVarValues(tool.required_fields, tool.config?.variables),
      toolConfigured: tool.configured,
      agentName: tool.tool_id,
      // User override only; code-level defaults are rendered as placeholders
      // via `llmDefaults` on the form.
      description: metaCfg.description || '',
      url: tool.url || '',
      systemPrompt: (metaCfg.system_prompt as string) || '',
      llmProvider: (llmCfg.llm_provider as string) || '',
      llmModel: (llmCfg.llm_model as string) || '',
      reasoningEffort: (llmCfg.reasoning_effort as string) || '',
      fullReasoning: fullReasoningEffective,
      enabled: tool.enabled,
      connectionError: tool.connection_error ?? null,
      hasAgent: !isSkill,  // skills have no MCP/A2A backing agent
      agentType: isSkill ? 'skill' : 'mcp',  // built-in tools dispatch via the MCP-style adapter path
      showAuthenticate: false,
      showUnlink: false,
      badgeClass: '',
      statusText: '',
      argumentsSchema: schema,
      argValues: initArgValues(schema, tool.config?.arguments),
      expanded: false,
      saving: false,
      // Skills don't have a child LLM; treat them as bound.
      llmBound: isSkill ? true : (tool.llm_bound ?? true),
      llmDefaults,
      lockedLlmFields,
      longRunningApp: tool.long_running_app ?? null,
      registering: false,
      lastRegisteredProcess: null,
    });
  }

  // Remote agents -- only profile-owned/visible a2a + mcp from /api/agents
  const remoteAgents = agents;

  for (const agent of remoteAgents.filter(a => a.agent_type === 'mcp')) {
    const schema = (agent.arguments_schema as JsonSchema | null) ?? null;
    result.push({
      name: agent.tool_id,
      displayName: agent.name,
      kind: 'mcp-remote',
      toolName: agent.tool_id,
      toolConfigFields: {},
      toolConfigValues: {},
      toolConfigured: true,
      agentName: agent.tool_id,
      description: agent.description || '',
      url: agent.url,
      // Per-server LLM/system_prompt overrides are loaded on demand via /api/agents/{tool_id}/config
      systemPrompt: '',
      llmProvider: '',
      llmModel: '',
      reasoningEffort: '',
      fullReasoning: false,
      enabled: agent.enabled,
      connectionError: agent.connection_error,
      hasAgent: true,
      agentType: 'mcp',
      showAuthenticate: agent.show_authenticate,
      showUnlink: agent.show_unlink,
      badgeClass: agent.badge_class,
      statusText: agent.status_text,
      argumentsSchema: schema,
      argValues: initArgValues(schema),
      expanded: false,
      saving: false,
      llmBound: true,
      llmDefaults: {},
      lockedLlmFields: [],
      longRunningApp: null,
      registering: false,
      lastRegisteredProcess: null,
    });
  }

  for (const agent of remoteAgents.filter(a => a.agent_type === 'a2a')) {
    const schema = (agent.arguments_schema as JsonSchema | null) ?? null;
    result.push({
      name: agent.tool_id,
      displayName: agent.name,
      kind: 'a2a-remote',
      toolName: agent.tool_id,
      toolConfigFields: {},
      toolConfigValues: {},
      toolConfigured: true,
      agentName: agent.tool_id,
      description: agent.description || '',
      url: agent.url,
      systemPrompt: '',
      llmProvider: '',
      llmModel: '',
      reasoningEffort: '',
      fullReasoning: false,
      enabled: agent.enabled,
      connectionError: agent.connection_error,
      hasAgent: true,
      agentType: 'a2a',
      showAuthenticate: agent.show_authenticate,
      showUnlink: agent.show_unlink,
      badgeClass: agent.badge_class,
      statusText: agent.status_text,
      argumentsSchema: schema,
      argValues: initArgValues(schema),
      expanded: false,
      saving: false,
      llmBound: true,
      llmDefaults: {},
      lockedLlmFields: [],
      longRunningApp: null,
      registering: false,
      lastRegisteredProcess: null,
    });
  }

  items.value = result;
}

// ── Status tag helpers ──
function getBuiltinStatusTag(item: UnifiedItem) {
  if (!item.llmBound) return { label: 'Setup required', type: 'warning' as const };
  if (item.toolConfigured && item.enabled) return { label: 'Active', type: 'success' as const };
  if (!item.toolConfigured) return { label: 'Needs Config', type: 'warning' as const };
  return { label: 'Disabled', type: 'info' as const };
}

function getRemoteStatusTag(item: UnifiedItem) {
  if (item.connectionError) return { label: 'Connection Error', type: 'danger' as const };
  const type = item.badgeClass === 'badge-success' ? 'success' as const
    : item.badgeClass === 'badge-warning' ? 'warning' as const
    : 'danger' as const;
  return { label: item.statusText, type };
}

// ── Actions ──
async function saveItemConfig(item: UnifiedItem) {
  item.saving = true;
  try {
    if (item.toolName && Object.keys(item.toolConfigValues).length > 0) {
      await updateToolConfig(settingsStore.agentUrl, settingsStore.authToken, item.toolName, item.toolConfigValues);
    }

    if (item.toolName && item.agentType !== 'skill') {
      await setToolFullReasoning(settingsStore.agentUrl, settingsStore.authToken, item.toolName, item.fullReasoning);
    }

    if (item.kind !== 'a2a-remote' && item.hasAgent && item.agentType !== 'skill') {
      await updateAgentConfig(settingsStore.agentUrl, settingsStore.authToken, item.agentName, {
        description: item.description || null,
        system_prompt: item.systemPrompt || null,
        llm_provider: item.llmProvider || null,
        llm_model: item.llmModel || null,
        reasoning_effort: getReasoningOptions(item.llmModel).length > 0 ? (item.reasoningEffort || null) : null,
        full_reasoning: item.kind === 'mcp-remote' ? item.fullReasoning : undefined,
      });
    }

    if (item.argumentsSchema && Object.keys(item.argValues).length > 0) {
      const configName = item.toolName || item.agentName;
      await updateToolArguments(settingsStore.agentUrl, settingsStore.authToken, configName, { ...item.argValues });
    }

    ElMessage.success(`${item.displayName} configuration saved`);
    await reloadAll();
  } catch (e) {
    ElMessage.error(`Failed to save: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    item.saving = false;
  }
}

function resetLLMDefaults(item: UnifiedItem) {
  item.description = '';
  item.systemPrompt = '';
  item.llmProvider = '';
  item.llmModel = '';
  item.reasoningEffort = '';
  // Locked fields stay at their code default; clearing them would force a
  // forbidden value (e.g. documentation_search must keep full_reasoning=true).
  if (!item.lockedLlmFields.includes('full_reasoning')) {
    item.fullReasoning = false;
  }
}

/** Per-field "Reset to default" from LLMParametersForm.
 *
 *  Clears the DB override via DELETE /api/tools/{id}/llm and refreshes the
 *  list so the placeholder/default re-appears. Only wired for built-in tools
 *  today — MCP/A2A agents persist LLM overrides through a different endpoint. */
async function handleResetField(item: UnifiedItem, field: LLMDefaultField) {
  if (!item.toolName || item.kind !== 'builtin') return;
  try {
    await resetToolLLMKeys(
      settingsStore.agentUrl,
      settingsStore.authToken,
      item.toolName,
      [field],
    );
    await reloadAll();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to reset field');
  }
}

async function toggleItemEnabled(item: UnifiedItem, value: boolean) {
  // Single source of truth: /api/tools/{tool_id}/enabled writes the
  // profile_tools row for any tool type (builtin / skill / a2a / mcp).
  // The legacy /api/agents/{tool_id}/enabled endpoint resolves to the same
  // backend handler -- no need to call it twice.
  item.enabled = value;
  try {
    await setToolEnabled(
      settingsStore.agentUrl,
      settingsStore.authToken,
      item.toolName ?? item.name,
      item.enabled,
    );
  } catch (e) {
    if (e instanceof FeatureNotInstalledError && value) {
      // Pre-flight blocked enable: the tool's pip extras aren't on
      // disk. Roll back the optimistic flip and open the install
      // dialog. The user confirms; we drive the SSE install and retry
      // the toggle on success.
      item.enabled = !item.enabled;
      openFeatureInstallDialog(item, e.detail);
      return;
    }
    item.enabled = !item.enabled;
    ElMessage.error(e instanceof Error ? e.message : 'Failed to toggle');
  }
}

function openFeatureInstallDialog(item: UnifiedItem, detail: FeatureNotInstalledDetail) {
  featureInstallDetail.value = detail;
  featureInstallPendingItem.value = item;
  featureInstallLog.value = [];
  featureInstallRestartRequired.value = false;
  featureInstallError.value = null;
  featureInstallBusy.value = false;
  featureInstallOpen.value = true;
}

function closeFeatureInstallDialog() {
  featureInstallOpen.value = false;
  featureInstallDetail.value = null;
  featureInstallPendingItem.value = null;
  featureInstallLog.value = [];
  featureInstallRestartRequired.value = false;
  featureInstallError.value = null;
}

async function confirmFeatureInstall() {
  const detail = featureInstallDetail.value;
  const item = featureInstallPendingItem.value;
  if (!detail || !item || featureInstallBusy.value) return;
  featureInstallBusy.value = true;
  featureInstallError.value = null;
  featureInstallLog.value = [];

  const handleEvent = (evt: FeatureInstallEvent) => {
    const prefix = evt.event === 'error' ? '✖' : evt.event === 'done' ? '✓' : '•';
    if (evt.message) {
      featureInstallLog.value.push(`${prefix} ${evt.message}`);
    }
  };

  try {
    const result = await streamFeaturesInstall(
      settingsStore.agentUrl,
      settingsStore.authToken,
      [detail.feature_key],
      handleEvent,
    );
    if (!result.ok || result.failed.length) {
      featureInstallError.value =
        result.error || `Install failed for: ${result.failed.join(', ')}`;
      featureInstallBusy.value = false;
      return;
    }
    featureInstallRestartRequired.value = result.restart_required;
    // Retry the toggle now that the feature is on disk. The backend
    // pre-flight (is_installed -> find_spec) will accept it.
    try {
      item.enabled = true;
      await setToolEnabled(
        settingsStore.agentUrl,
        settingsStore.authToken,
        item.toolName ?? item.name,
        true,
      );
      ElMessage.success(
        result.restart_required
          ? `Installed '${detail.feature_key}' — restart the server to load it.`
          : `Installed '${detail.feature_key}' and enabled '${item.displayName}'.`,
      );
      if (!result.restart_required) {
        closeFeatureInstallDialog();
      } else {
        // Leave the dialog open so the user sees the restart CTA.
        featureInstallBusy.value = false;
      }
    } catch (toggleErr) {
      item.enabled = false;
      featureInstallError.value =
        toggleErr instanceof Error ? toggleErr.message : 'Failed to enable after install';
      featureInstallBusy.value = false;
    }
  } catch (e) {
    featureInstallError.value = e instanceof Error ? e.message : 'Install stream failed';
    featureInstallBusy.value = false;
  }
}

async function handleAuthenticate(item: UnifiedItem) {
  try {
    const url = await getAuthUrl(settingsStore.agentUrl, settingsStore.authToken, item.agentName);
    window.open(url, '_blank');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to get auth URL');
  }
}

async function handleUnlink(item: UnifiedItem) {
  try {
    await unlinkAgent(settingsStore.agentUrl, settingsStore.authToken, item.agentName);
    ElMessage.success(`Unlinked ${item.name}`);
    await reloadAll();
  } catch (e) {
    ElMessage.error('Failed to unlink');
  }
}

async function handleReconnect(item: UnifiedItem) {
  try {
    await reconnectAgent(settingsStore.agentUrl, settingsStore.authToken, item.agentName);
    ElMessage.success(`Reconnected ${item.name}`);
    await reloadAll();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to reconnect');
    await reloadAll();
  }
}

async function handleRemoveAgent(item: UnifiedItem) {
  try {
    await removeAgent(settingsStore.agentUrl, settingsStore.authToken, item.agentName);
    ElMessage.success(`Removed ${item.name}`);
    await reloadAll();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to remove agent');
  }
}

function openAddDialog(type: 'mcp' | 'a2a') {
  addForm.value = {
    url: '', type, inputMode: 'url', jsonConfig: '',
    description: '', system_prompt: '', llm_provider: '', llm_model: '', reasoning_effort: '',
  };
  jsonConfigError.value = '';
  showAddDialog.value = true;
}

async function handleAddAgent() {
  const isMcpJson = addForm.value.type === 'mcp' && addForm.value.inputMode === 'json';

  if (isMcpJson) {
    if (!addForm.value.jsonConfig.trim()) return;
    try {
      JSON.parse(addForm.value.jsonConfig);
      jsonConfigError.value = '';
    } catch {
      jsonConfigError.value = 'Invalid JSON format';
      return;
    }
  } else {
    if (!addForm.value.url.trim()) return;
  }

  adding.value = true;
  try {
    const config: { type: string; url?: string; json_config?: string; description?: string; system_prompt?: string; llm_provider?: string; llm_model?: string; reasoning_effort?: string } = {
      type: addForm.value.type,
    };

    if (isMcpJson) {
      config.json_config = addForm.value.jsonConfig.trim();
    } else {
      config.url = addForm.value.url.trim();
    }

    if (addForm.value.type === 'mcp') {
      if (addForm.value.description) config.description = addForm.value.description;
      if (addForm.value.system_prompt) config.system_prompt = addForm.value.system_prompt;
      if (addForm.value.llm_provider) config.llm_provider = addForm.value.llm_provider;
      if (addForm.value.llm_model) config.llm_model = addForm.value.llm_model;
      if (addForm.value.reasoning_effort) config.reasoning_effort = addForm.value.reasoning_effort;
    }

    await addAgent(settingsStore.agentUrl, settingsStore.authToken, config);
    ElMessage.success('Agent added successfully');
    showAddDialog.value = false;
    await reloadAll();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to add agent');
  } finally {
    adding.value = false;
  }
}

async function reloadAll() {
  try {
    const [agentRes, toolRes] = await Promise.all([
      listAgents(settingsStore.agentUrl, settingsStore.authToken),
      listTools(settingsStore.agentUrl, settingsStore.authToken),
    ]);
    buildUnifiedItems(agentRes.agents, toolRes.tools);
  } catch { /* ignore */ }
}

function goBack() { router.push(`/${props.profile}/settings`); }

async function registerLongRunningApp(item: UnifiedItem) {
  if (!item.toolName || !item.longRunningApp) return;
  item.registering = true;
  try {
    await doRegisterLongRunningApp(item, false);
  } catch (err) {
    if (err instanceof DuplicateAutostartError) {
      try {
        await ElMessageBox.confirm(
          `A long-running process with this command is already registered:\n\n${err.existing.command}\n\nRegister another copy anyway?`,
          'Duplicate registration',
          { confirmButtonText: 'Register again', cancelButtonText: 'Cancel', type: 'warning' },
        );
      } catch {
        item.registering = false;
        return;
      }
      try {
        await doRegisterLongRunningApp(item, true);
      } catch (err2) {
        ElMessage.error(err2 instanceof Error ? err2.message : String(err2));
      }
    } else {
      ElMessage.error(err instanceof Error ? err.message : String(err));
    }
  } finally {
    item.registering = false;
  }
}

async function doRegisterLongRunningApp(item: UnifiedItem, force: boolean) {
  const result = await registerSkillLongRunningApp(
    settingsStore.agentUrl, settingsStore.authToken, item.toolName!, force,
  );
  item.lastRegisteredProcess = {
    process_id: result.process_id,
    command: result.command,
  };
  ElMessage.success('Process registered and started');
}

async function onSkillModeChange(value: string | number | boolean | undefined) {
  const next = value as SkillMode;
  const previous: SkillMode = next === 'manual' ? 'automatic' : 'manual';
  skillMode.value = next;
  skillModeSaving.value = true;
  try {
    await setSkillMode(settingsStore.agentUrl, settingsStore.authToken, props.profile, next);
    ElMessage.success(`Skill mode set to ${next}`);
  } catch (e) {
    skillMode.value = previous;
    ElMessage.error(e instanceof Error ? e.message : 'Failed to update skill mode');
  } finally {
    skillModeSaving.value = false;
  }
}
</script>

<template>
  <div class="agents-tools-page">
    <div class="page-container">
      <div class="page-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="page-title">Tools & Skills</h1>
      </div>

      <div v-if="loading" class="loading-state">Loading...</div>

      <template v-else>
        <!-- Section 1: Built-in Tools -->
        <div class="section" v-if="builtinItems.length > 0">
          <h2 class="section-title">
            <Icon icon="mdi:toolbox" class="section-icon" /> Built-in Tools
          </h2>

          <div class="items-list">
            <ElCard v-for="item in builtinItems" :key="item.name" class="item-card" shadow="hover">
              <ItemCardHeader
                :name="item.displayName"
                :status-tag="getBuiltinStatusTag(item)"
                :expanded="item.expanded"
                :enabled="item.enabled"
                :show-authenticate="item.showAuthenticate"
                :show-unlink="item.showUnlink"
                @toggle-expand="item.expanded = !item.expanded"
                @update:enabled="toggleItemEnabled(item, $event)"
                @authenticate="handleAuthenticate(item)"
                @unlink="handleUnlink(item)"
              />

              <div v-if="item.expanded" class="item-config">
                <div v-if="Object.keys(item.toolConfigFields).length > 0" class="config-section">
                  <ToolVariablesForm
                    :fields="item.toolConfigFields"
                    :values="item.toolConfigValues"
                    @update:values="item.toolConfigValues = $event"
                  />
                </div>

                <div v-if="item.agentType !== 'skill'" class="config-section">
                  <h4 class="config-section-title">LLM Parameters</h4>
                  <LLMParametersForm
                    v-model:description="item.description"
                    v-model:system-prompt="item.systemPrompt"
                    v-model:llm-provider="item.llmProvider"
                    v-model:llm-model="item.llmModel"
                    v-model:reasoning-effort="item.reasoningEffort"
                    v-model:full-reasoning="item.fullReasoning"
                    :providers="llmProviders"
                    :get-filtered-models="getFilteredModels"
                    :get-reasoning-options="getReasoningOptions"
                    :default-description="item.llmDefaults.description || item.llmDefaults.server_instructions || ''"
                    :default-system-prompt="item.llmDefaults.system_prompt || ''"
                    :default-llm-provider="item.llmDefaults.llm_provider || ''"
                    :default-llm-model="item.llmDefaults.llm_model || ''"
                    :default-reasoning-effort="item.llmDefaults.reasoning_effort || ''"
                    :default-full-reasoning="item.llmDefaults.full_reasoning"
                    :locked-full-reasoning="item.lockedLlmFields.includes('full_reasoning')"
                    @reset-field="handleResetField(item, $event)"
                  />
                </div>

                <div v-if="item.argumentsSchema && Object.keys(item.argumentsSchema.properties).length > 0" class="config-section">
                  <ToolArgumentsForm
                    :schema="item.argumentsSchema"
                    :arg-values="item.argValues"
                    @update:arg-values="item.argValues = $event"
                  />
                </div>

                <div style="display: flex; align-items: center; gap: 8px;">
                  <ElButton type="primary" size="small" :loading="item.saving" @click="saveItemConfig(item)">Save</ElButton>
                  <ElButton size="small" @click="resetLLMDefaults(item)">Reset All to Default</ElButton>
                </div>
              </div>
            </ElCard>
          </div>
        </div>

        <ElDivider />

        <!-- Section 2: Skills -->
        <div class="section" v-if="skillItems.length > 0">
          <h2 class="section-title">
            <Icon icon="mdi:creation" class="section-icon" /> Skills
          </h2>

          <div class="skill-mode-row">
            <span class="skill-mode-label">Skill Mode</span>
            <ElRadioGroup
              v-model="skillMode"
              size="small"
              :disabled="skillModeSaving"
              @change="onSkillModeChange"
            >
              <ElRadioButton value="manual">Manual</ElRadioButton>
              <ElTooltip
                :disabled="embeddingEnabled"
                content="Enable Vector Embedding in setup to use Automatic mode."
                placement="top"
              >
                <span>
                  <ElRadioButton value="automatic" :disabled="!embeddingEnabled">Automatic</ElRadioButton>
                </span>
              </ElTooltip>
            </ElRadioGroup>
            <span class="skill-mode-hint">
              {{ !embeddingEnabled
                  ? 'Vector Embedding is disabled — Automatic mode is unavailable.'
                  : skillMode === 'manual'
                    ? 'All enabled skills are exposed to the agent.'
                    : 'Top 10 skills matching your message are selected via vector search.' }}
            </span>
          </div>

          <div class="items-list">
            <ElCard v-for="item in skillItems" :key="item.name" class="item-card" shadow="hover">
              <ItemCardHeader
                :name="item.displayName"
                :status-tag="getBuiltinStatusTag(item)"
                :expanded="item.expanded"
                :enabled="item.enabled"
                :show-authenticate="item.showAuthenticate"
                :show-unlink="item.showUnlink"
                @toggle-expand="item.expanded = !item.expanded"
                @update:enabled="toggleItemEnabled(item, $event)"
                @authenticate="handleAuthenticate(item)"
                @unlink="handleUnlink(item)"
              />

              <div v-if="item.expanded" class="item-config">
                <p v-if="item.description" class="item-desc skill-description">{{ item.description }}</p>

                <div v-if="Object.keys(item.toolConfigFields).length > 0" class="config-section">
                  <ToolVariablesForm
                    title="Skill Variables"
                    :fields="item.toolConfigFields"
                    :values="item.toolConfigValues"
                    @update:values="item.toolConfigValues = $event"
                  />
                  <ElButton type="primary" size="small" :loading="item.saving" @click="saveItemConfig(item)">Save</ElButton>
                </div>

                <div
                  v-if="item.longRunningApp"
                  class="config-section lra-section"
                  :data-skill-id="item.name"
                >
                  <h4 class="config-section-title">Register Long-Running Process</h4>
                  <p v-if="item.longRunningApp.description" class="lra-description">
                    {{ item.longRunningApp.description }}
                  </p>
                  <pre class="lra-command">{{ item.longRunningApp.command }}</pre>
                  <ElButton
                    type="primary"
                    size="small"
                    :loading="item.registering"
                    @click="registerLongRunningApp(item)"
                  >
                    <Icon icon="mdi:play-circle-outline" />&nbsp;Register Process
                  </ElButton>
                  <div v-if="item.lastRegisteredProcess" class="lra-result">
                    <Icon icon="mdi:check-circle" class="lra-result-icon" />
                    <span>
                      Process started:
                      <RouterLink
                        class="lra-link"
                        :to="{
                          name: 'process-terminal',
                          params: { profile: props.profile, pid: item.lastRegisteredProcess.process_id },
                        }"
                      >
                        {{ item.lastRegisteredProcess.process_id }}
                      </RouterLink>
                      — open the detail page to view logs and send input.
                    </span>
                  </div>
                </div>
              </div>
            </ElCard>
          </div>
        </div>

        <ElDivider />

        <!-- Section 3: MCP Server Remote -->
        <div class="section">
          <h2 class="section-title">
            <Icon icon="mdi:server-network" class="section-icon" /> MCP Server Remote
          </h2>

          <div v-if="mcpRemoteItems.length === 0" class="empty-state">No remote MCP servers configured.</div>

          <div v-else class="items-list">
            <ElCard v-for="item in mcpRemoteItems" :key="item.name" class="item-card" :class="{ 'item-card-error': !!item.connectionError }" shadow="hover">
              <ItemCardHeader
                :name="item.name"
                :status-tag="getRemoteStatusTag(item)"
                :expanded="item.expanded"
                :enabled="item.enabled"
                :show-authenticate="item.showAuthenticate && !item.connectionError"
                :show-unlink="item.showUnlink && !item.connectionError"
                :show-reconnect="!!item.connectionError"
                :show-remove="true"
                :remove-name="item.name"
                @toggle-expand="item.expanded = !item.expanded"
                @update:enabled="toggleItemEnabled(item, $event)"
                @authenticate="handleAuthenticate(item)"
                @unlink="handleUnlink(item)"
                @reconnect="handleReconnect(item)"
                @remove="handleRemoveAgent(item)"
              />
              <p class="item-url">{{ item.url }}</p>
              <p v-if="item.connectionError" class="item-error-msg"><Icon icon="mdi:alert-circle-outline" /> {{ item.connectionError }}</p>

              <div v-if="item.expanded" class="item-config">
                <div class="config-section">
                  <h4 class="config-section-title">LLM Parameters</h4>
                  <LLMParametersForm
                    v-model:description="item.description"
                    v-model:system-prompt="item.systemPrompt"
                    v-model:llm-provider="item.llmProvider"
                    v-model:llm-model="item.llmModel"
                    v-model:reasoning-effort="item.reasoningEffort"
                    v-model:full-reasoning="item.fullReasoning"
                    :providers="llmProviders"
                    :get-filtered-models="getFilteredModels"
                    :get-reasoning-options="getReasoningOptions"
                  />
                </div>

                <div v-if="item.argumentsSchema && Object.keys(item.argumentsSchema.properties).length > 0" class="config-section">
                  <ToolArgumentsForm
                    :schema="item.argumentsSchema"
                    :arg-values="item.argValues"
                    @update:arg-values="item.argValues = $event"
                  />
                </div>

                <div style="display: flex; align-items: center; gap: 8px;">
                  <ElButton type="primary" size="small" :loading="item.saving" @click="saveItemConfig(item)">Save</ElButton>
                  <ElButton size="small" @click="resetLLMDefaults(item)">Reset All to Default</ElButton>
                </div>
              </div>
            </ElCard>
          </div>

          <ElButton type="primary" class="add-btn" @click="openAddDialog('mcp')">
            <Icon icon="mdi:plus" /> Add MCP Server
          </ElButton>
        </div>

        <ElDivider />

        <!-- Section 4: A2A Agent Remote -->
        <div class="section">
          <h2 class="section-title">
            <Icon icon="mdi:robot" class="section-icon" /> A2A Agent Remote
          </h2>

          <div v-if="a2aRemoteItems.length === 0" class="empty-state">No A2A agents configured.</div>

          <div v-else class="items-list">
            <ElCard v-for="item in a2aRemoteItems" :key="item.name" class="item-card" :class="{ 'item-card-error': !!item.connectionError }" shadow="hover">
              <ItemCardHeader
                :name="item.name"
                :status-tag="getRemoteStatusTag(item)"
                :expanded="item.expanded"
                :enabled="item.enabled"
                :show-authenticate="item.showAuthenticate && !item.connectionError"
                :show-unlink="item.showUnlink && !item.connectionError"
                :show-reconnect="!!item.connectionError"
                :show-remove="true"
                :remove-name="item.name"
                @toggle-expand="item.expanded = !item.expanded"
                @update:enabled="toggleItemEnabled(item, $event)"
                @authenticate="handleAuthenticate(item)"
                @unlink="handleUnlink(item)"
                @reconnect="handleReconnect(item)"
                @remove="handleRemoveAgent(item)"
              />
              <p class="item-url">{{ item.url }}</p>
              <p v-if="item.connectionError" class="item-error-msg"><Icon icon="mdi:alert-circle-outline" /> {{ item.connectionError }}</p>
              <p v-else class="item-desc">{{ item.description }}</p>

              <div v-if="item.expanded" class="item-config">
                <div v-if="item.argumentsSchema && Object.keys(item.argumentsSchema.properties).length > 0" class="config-section">
                  <ToolArgumentsForm
                    :schema="item.argumentsSchema"
                    :arg-values="item.argValues"
                    @update:arg-values="item.argValues = $event"
                  />
                </div>

                <div v-else class="empty-args">No arguments required for this agent.</div>

                <ElButton type="primary" size="small" :loading="item.saving" @click="saveItemConfig(item)">Save</ElButton>
              </div>
            </ElCard>
          </div>

          <ElButton type="primary" class="add-btn" @click="openAddDialog('a2a')">
            <Icon icon="mdi:plus" /> Add A2A Agent
          </ElButton>
        </div>
      </template>
    </div>

    <!-- Add Agent Dialog -->
    <ElDialog v-model="showAddDialog" :title="addForm.type === 'mcp' ? 'Add MCP Server' : 'Add A2A Agent'" width="520px">
      <ElForm label-position="top">
        <!-- Input mode toggle (MCP only) -->
        <template v-if="addForm.type === 'mcp'">
          <ElFormItem>
            <ElRadioGroup v-model="addForm.inputMode" size="small">
              <ElRadioButton value="url">URL</ElRadioButton>
              <ElRadioButton value="json">JSON Config</ElRadioButton>
            </ElRadioGroup>
          </ElFormItem>
        </template>

        <!-- URL mode -->
        <template v-if="addForm.inputMode === 'url' || addForm.type === 'a2a'">
          <ElFormItem label="URL" required>
            <ElInput v-model="addForm.url" placeholder="http://localhost:9000/mcp" />
          </ElFormItem>
        </template>

        <!-- JSON config mode (MCP only) -->
        <template v-if="addForm.type === 'mcp' && addForm.inputMode === 'json'">
          <ElFormItem label="JSON Configuration" required :error="jsonConfigError">
            <ElInput
              v-model="addForm.jsonConfig"
              type="textarea"
              :rows="8"
              placeholder='{ "myServer": { "type": "stdio", "command": "npx", "args": ["-y", "@example/mcp-server"] } }'
              @input="jsonConfigError = ''"
              class="json-config-input"
            />
            <div class="json-config-hint">
              Supports <code>stdio</code> and <code>http</code> transport types.
              Use the VS Code MCP server config format.
            </div>
          </ElFormItem>
        </template>

        <template v-if="addForm.type === 'mcp'">
          <LLMParametersForm
            v-model:description="addForm.description"
            v-model:system-prompt="addForm.system_prompt"
            v-model:llm-provider="addForm.llm_provider"
            v-model:llm-model="addForm.llm_model"
            v-model:reasoning-effort="addForm.reasoning_effort"
            :full-reasoning="false"
            :providers="llmProviders"
            :get-filtered-models="getFilteredModels"
            :get-reasoning-options="getReasoningOptions"
            :show-full-reasoning="false"
          />
        </template>
      </ElForm>
      <template #footer>
        <ElButton @click="showAddDialog = false">Cancel</ElButton>
        <ElButton
          type="primary"
          :loading="adding"
          @click="handleAddAgent"
          :disabled="addForm.type === 'mcp' && addForm.inputMode === 'json'
            ? !addForm.jsonConfig.trim()
            : !addForm.url.trim()"
        >Add</ElButton>
      </template>
    </ElDialog>

    <ElTour v-model="tourOpen">
      <ElTourStep
        :target="tourTargetSelector"
        :title="`Register the ${tourSkillName || 'background'} process`"
        description="This skill ships with a long-running listener. Click here to start it — once registered, the process will be relaunched automatically across server restarts."
        placement="top"
      />
    </ElTour>

    <!-- Feature install dialog (toggled by toggleItemEnabled when the
         backend returns 409 FeatureNotInstalled). Streams pip output
         from /api/features/install over SSE and retries the enable
         toggle once the install completes. -->
    <ElDialog
      v-model="featureInstallOpen"
      :title="featureInstallDetail ? `Install '${featureInstallDetail.feature_key}' feature?` : 'Install feature'"
      width="560px"
      :close-on-click-modal="!featureInstallBusy"
      :close-on-press-escape="!featureInstallBusy"
      :show-close="!featureInstallBusy"
    >
      <div v-if="featureInstallDetail" class="feature-install-body">
        <p>
          Enabling
          <strong>{{ featureInstallPendingItem?.displayName ?? featureInstallDetail.tool_id }}</strong>
          requires installing the
          <code>{{ featureInstallDetail.feature_key }}</code>
          optional dependency group
          (<code>cremind[{{ featureInstallDetail.extras.join(',') }}]</code>).
        </p>
        <p v-if="featureInstallDetail.requires_restart_after_install" class="feature-install-warn">
          A server restart will be required after the install completes
          before the new feature can load.
        </p>

        <div v-if="featureInstallLog.length" class="feature-install-log">
          <div v-for="(line, i) in featureInstallLog" :key="i">{{ line }}</div>
        </div>

        <p v-if="featureInstallError" class="feature-install-error">
          {{ featureInstallError }}
        </p>

        <p
          v-if="featureInstallRestartRequired"
          class="feature-install-restart"
        >
          Install complete. Restart the Cremind server to load the new
          feature.
        </p>
      </div>
      <template #footer>
        <ElButton
          v-if="!featureInstallRestartRequired"
          @click="closeFeatureInstallDialog"
          :disabled="featureInstallBusy"
        >
          Cancel
        </ElButton>
        <ElButton
          v-if="!featureInstallRestartRequired"
          type="primary"
          :loading="featureInstallBusy"
          @click="confirmFeatureInstall"
        >
          {{ featureInstallError ? 'Retry install' : 'Install' }}
        </ElButton>
        <ElButton
          v-if="featureInstallRestartRequired"
          type="primary"
          @click="closeFeatureInstallDialog"
        >
          Close
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.agents-tools-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.page-container { max-width: 800px; margin: 0 auto; }

/* Feature install dialog: pip output is monospaced and scrollable so a
   30-second sentence-transformers install doesn't push the action
   buttons off-screen. */
.feature-install-body p { margin: 8px 0; }
.feature-install-body code {
  background: var(--el-fill-color-light);
  padding: 1px 4px; border-radius: 3px;
}
.feature-install-warn { color: var(--el-color-warning); }
.feature-install-error { color: var(--el-color-danger); }
.feature-install-restart {
  color: var(--el-color-success);
  font-weight: 600;
}
.feature-install-log {
  max-height: 240px; overflow-y: auto;
  background: var(--el-fill-color-darker);
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  padding: 8px 12px;
  margin: 12px 0;
  font-family: var(--el-font-family-monospace, ui-monospace, Menlo, monospace);
  font-size: 12px;
  white-space: pre-wrap;
}
.page-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 12px;
}
.back-btn:hover { color: var(--primary-color); }
.page-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0; }
.loading-state { text-align: center; padding: 40px; color: var(--text-secondary); }

.section { margin-bottom: 8px; }
.section-title {
  font-size: 1.1rem; font-weight: 600; color: var(--text-primary);
  margin: 0 0 12px 0; display: flex; align-items: center; gap: 8px;
}
.section-icon { font-size: 20px; color: var(--primary-color); }

.items-list { display: flex; flex-direction: column; gap: 10px; }

.item-card { background: var(--surface-color); }
.item-card-error { border-color: var(--el-color-danger-light-5, #fab6b6); }
.item-url { color: var(--text-tertiary); font-size: 0.75rem; font-family: monospace; margin: 4px 0 0 0; }
.item-desc { color: var(--text-secondary); font-size: 0.8rem; margin: 2px 0 0 0; }
.skill-description { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border-color, #e4e7ed); }
.item-error-msg { color: var(--el-color-danger, #f56c6c); font-size: 0.75rem; margin: 4px 0 0 0; display: flex; align-items: center; gap: 4px; }

.item-config {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-color, #e4e7ed);
}

.config-section {
  margin-bottom: 12px;
}
.config-section + .config-section {
  padding-top: 12px;
  border-top: 1px dashed var(--border-color, #e4e7ed);
}
.config-section-title {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
  margin: 0 0 8px 0;
}

.empty-args { color: var(--text-tertiary); font-size: 0.8rem; padding: 4px 0 12px 0; }
.empty-state { color: var(--text-tertiary); font-size: 0.85rem; padding: 16px 0; }
.add-btn { margin-top: 8px; }

.lra-description {
  margin: 0 0 8px 0;
  font-size: 0.85rem;
  color: var(--text-secondary);
}
.lra-command {
  margin: 0 0 10px 0;
  padding: 8px 10px;
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 0.8rem;
  background: var(--surface-color);
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-all;
}
.lra-result {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 10px;
  padding: 8px 10px;
  font-size: 0.85rem;
  background: #f0f9eb;
  border: 1px solid #c2e7b0;
  border-radius: 4px;
  color: #1f3a1f;
}
[data-theme="dark"] .lra-result {
  background: rgba(52, 211, 153, 0.14);
  border-color: rgba(52, 211, 153, 0.45);
  color: var(--text-primary);
}
.lra-result-icon { color: var(--success-color, #67c23a); flex-shrink: 0; margin-top: 2px; }
.lra-link { color: var(--primary-color, #409eff); text-decoration: underline; font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace); }
.lra-link:hover { color: var(--primary-color-hover, #66b1ff); }

.skill-mode-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
  padding: 8px 12px;
  background: var(--surface-color);
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 6px;
}
.skill-mode-label {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-primary);
}
.skill-mode-hint {
  font-size: 0.75rem;
  color: var(--text-tertiary);
  flex: 1 1 auto;
}

.json-config-input :deep(textarea) { font-family: monospace; font-size: 0.8rem; }
.json-config-hint {
  font-size: 0.75rem; color: var(--text-tertiary); margin-top: 4px; line-height: 1.4;
}
.json-config-hint code {
  background: var(--el-fill-color-light, #f5f7fa); padding: 1px 4px; border-radius: 3px;
  font-size: 0.7rem;
}
</style>
