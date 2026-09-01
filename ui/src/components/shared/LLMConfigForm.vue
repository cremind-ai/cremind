<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { ElForm, ElFormItem, ElSelect, ElOption, ElButton, ElSwitch, ElMessage } from 'element-plus';
import {
  listLLMProviders,
  getProviderModels,
  updateProvider,
  deleteProviderConfig,
  createCustomProvider,
  getModelGroups,
  updateModelGroups,
  type LLMProvider,
  type LLMModel,
  type CustomProviderModel,
} from '../../services/configApi';
import { useLLMModels } from '../../composables/useLLMModels';
import ProviderConfigFields, { type ProviderWithState } from './ProviderConfigFields.vue';
import CustomProviderForm from './CustomProviderForm.vue';
import ModelGroupFields from './ModelGroupFields.vue';

/**
 * The single LLM-configuration surface: provider credentials + every model
 * role. Both hosts render *this* component so a role added here (or a copy
 * tweak, or a new auth flow) can never drift between them again:
 *
 *  - Settings → LLM Providers (`LLMSettings.vue`) mounts it `self-saving`, so
 *    it talks to the REST API itself (per-provider Save, model-groups Save,
 *    custom providers, browser OAuth).
 *  - The Setup Wizard's LLM step (`StepLLMConfig.vue`) mounts it *controlled*:
 *    nothing is saved here, the whole configuration is emitted as one flat
 *    record that the wizard bundles into `POST /api/config/setup`.
 *
 * The capability props below are what separates those two hosts. They're
 * booleans rather than a single "mode" flag because the differences are
 * genuinely independent — setup has no profile to store OAuth tokens against,
 * but it *can* run GitHub Copilot's device-code flow; it must not offer the
 * admin's custom providers, but it does want the same model roles.
 */
const props = withDefaults(defineProps<{
  agentUrl: string;
  /** Bearer token for the LLM endpoints. Empty during first-run setup, where
   *  the endpoints are open; the admin's JWT during per-profile setup. */
  token: string;
  /** Controlled (setup) seed: the flat `llm_config` record the wizard holds.
   *  Its presence is what selects the seeded provider builder over the
   *  server's `current_values`. Read once, at mount — the wizard replaces its
   *  copy from our own emit, so re-reading it would feed us our own output. */
  initialConfig?: Record<string, string> | null;
  /** Save to the REST API directly (Settings) instead of emitting a config
   *  record for a host to persist later (setup wizard). */
  selfSaving?: boolean;
  /** Offer browser-OAuth sign-in ("Sign in with ChatGPT"). Those flows capture
   *  tokens server-side against an existing profile, so setup leaves this off
   *  and `ProviderConfigFields` shows a pointer to Settings instead. */
  allowBrowserOauth?: boolean;
  /** Offer user-defined custom providers (list them + the "add" affordance).
   *  Off during setup: per-profile setup runs on the *admin's* JWT, so the
   *  listing would show custom providers the new profile will never have. */
  allowCustomProviders?: boolean;
  /** Show the per-field "Set" badges. Only meaningful where fields were loaded
   *  from stored config (Settings); setup has nothing stored yet. */
  showConfiguredBadge?: boolean;
  /** Heading level + spacing only. `step` renders subtitles inside the
   *  wizard's 720px card; `page` renders section titles on a settings page. */
  variant?: 'step' | 'page';
}>(), {
  initialConfig: null,
  selfSaving: false,
  allowBrowserOauth: false,
  allowCustomProviders: false,
  showConfiguredBadge: false,
  variant: 'page',
});

/**
 * What a host needs to know to gate on this configuration, derived here
 * because only this component holds the provider catalog. The flat record
 * alone can't answer either question: `model_group.high` may be absent
 * because nothing was picked, and "no credentials" is fine for a provider
 * whose auth method needs none (ollama/vllm are `kind: "none"`).
 */
export interface LLMConfigValidity {
  /** `model_group.high`, empty when no main model is picked. */
  mainModel: string;
  /** Display name of the provider serving the main model (for messages). */
  mainProviderLabel: string;
  /** The main model's provider wants credentials this record doesn't carry.
   *  Advisory, not fatal — a key can legitimately come from the environment
   *  (the provider factory falls back to it), so hosts should warn, not block. */
  mainProviderNeedsCredentials: boolean;
}

const emit = defineEmits<{
  /** Controlled mode only: the COMPLETE flat configuration record. Never a
   *  partial patch — the wizard replaces its `llmConfig` wholesale. */
  'update:config': [config: Record<string, string>];
  /** Controlled mode only: emitted with every `update:config`, describing the
   *  same record. Kept separate so the host gates on a decision made here
   *  rather than re-deriving one it lacks the catalog to make. */
  'update:validity': [validity: LLMConfigValidity];
}>();

// Sentinel dropdown value for the "add a new custom provider" affordance.
const ADD_CUSTOM = '__add_custom__';

type ModelRoleKey = 'high' | 'plan' | 'low' | 'vision' | 'audio';

interface ModelRole {
  /** Config key suffix: `model_group.<key>` and the model-groups API field. */
  key: ModelRoleKey;
  title: string;
  /** Settings copy. May carry inline markup — these are static module
   *  constants (never user input), so the template renders them with v-html. */
  description: string;
  /** Wizard copy. The setup card is 720px wide with a 300px floor, so the
   *  full-page paragraphs are trimmed rather than reflowed. */
  stepDescription: string;
  /** Render the Reasoning Effort row (and persist the role's effort). */
  showReasoning: boolean;
  /** Optional roles let the user clear the provider/model back to "inherit". */
  clearable: boolean;
  /** Opt-in roles carry an enable switch and hide their picker while off; the
   *  switch persists as `model_group.<key>.enabled`. */
  optIn: boolean;
  useVision: boolean;
  useAudio: boolean;
  modelPlaceholder: string;
}

/**
 * Every model role, in the order Settings has always shown them. This array is
 * the whole point of the shared component: sections are rendered by `v-for`
 * over it, so neither host can end up with a role the other lacks (the setup
 * wizard was missing `plan` for exactly that reason).
 */
const MODEL_ROLES: ModelRole[] = [
  {
    key: 'high',
    title: 'Model',
    description: 'The single model the assistant uses for reasoning, tool calls, and replies.',
    stepDescription: 'The main model the assistant uses for reasoning, tool calls, and replies.',
    showReasoning: true,
    clearable: false,
    optIn: false,
    useVision: false,
    useAudio: false,
    modelPlaceholder: 'Select model',
  },
  {
    key: 'plan',
    title: 'Plan Model',
    description:
      'The model used while in plan mode — researching, asking clarifying questions, and '
      + 'writing the plan for your approval. When you accept a plan, execution automatically '
      + 'switches to the <strong>Model</strong> above; if you cancel, planning keeps using this '
      + 'model. Point this at a stronger, more capable model for high-quality planning while '
      + 'keeping your main Model cheaper for the longer execution phase — so requests are still '
      + 'fulfilled intelligently while saving a significant amount of tokens. Leave empty to fall '
      + 'back to the main model.',
    stepDescription:
      'Optional. The model used while in plan mode — researching and writing the plan for your '
      + 'approval; execution then switches to the <strong>Model</strong> above. Point it at a '
      + 'stronger model for better plans while keeping the main Model cheaper. Leave empty to '
      + 'fall back to the main model.',
    showReasoning: true,
    clearable: true,
    optIn: false,
    useVision: false,
    useAudio: false,
    modelPlaceholder: 'Select model (defaults to main)',
  },
  {
    key: 'low',
    title: 'Low-Performance Model',
    description:
      'An optional cheaper/faster model used for lightweight background tasks — currently the '
      + 'skill-event matching gate that checks whether an incoming event matches your automation '
      + 'rule before running the assistant. Can be any model from the list above. Leave empty to '
      + 'fall back to the main model.',
    stepDescription:
      'Optional. A cheaper/faster model for lightweight background tasks (e.g. the skill-event '
      + 'matching gate). Leave empty to fall back to the main model.',
    showReasoning: true,
    clearable: true,
    optIn: false,
    useVision: false,
    useAudio: false,
    modelPlaceholder: 'Select model (defaults to main)',
  },
  {
    key: 'vision',
    title: 'Specialized Vision Model (Image Understanding)',
    description:
      "When off (default), images are understood by your main model if it supports vision. Turn "
      + "this on to use a separate, dedicated vision model — useful when your main model can't see "
      + "images. Leave the model empty to fall back to the main model.",
    stepDescription:
      "Optional. Turn on to use a dedicated vision model for image understanding when your main "
      + "model can't see images. Leave empty to fall back to the main model.",
    showReasoning: false,
    clearable: true,
    optIn: true,
    useVision: true,
    useAudio: false,
    modelPlaceholder: 'Select vision model (defaults to main)',
  },
  {
    key: 'audio',
    title: 'Specialized Audio Model (Audio Understanding)',
    description:
      "When off (default), audio is understood by your main model if it supports audio input. Turn "
      + "this on to use a separate, dedicated audio model — useful when your main model can't "
      + "process audio. Only audio-capable models are listed (e.g. OpenAI GPT Audio, Google Gemini, "
      + "Mistral Voxtral, Qwen Omni); Anthropic models have no audio input. Leave the model empty "
      + "to fall back to the main model.",
    stepDescription:
      "Optional. Turn on to use a dedicated audio model for audio understanding when your main "
      + "model can't process audio. Leave empty to fall back to the main model.",
    showReasoning: false,
    clearable: true,
    optIn: true,
    useVision: false,
    useAudio: true,
    modelPlaceholder: 'Select audio model (defaults to main)',
  },
];

const { rebuildModelList, allModels } = useLLMModels();
// A second flattener over the UNION of every model we've fetched, rather than
// only the auth method each provider is currently on. Both lists are needed:
// picking a method must re-scope what the model dropdowns OFFER (that's the
// feature), but a role already bound under the other method still has to show
// its name and its reasoning options. See `rebuildModelLists`.
const { rebuildModelList: rebuildKnownModels, allModels: knownModels } = useLLMModels();

/** Extract provider name from a model group value like "groq/mixtral-8x7b". */
function extractProvider(groupValue: string | undefined): string {
  if (!groupValue) return '';
  const idx = groupValue.indexOf('/');
  return idx > 0 ? groupValue.substring(0, idx) : groupValue;
}

// Snapshot of the controlled seed. Taken once so a re-emit (which the wizard
// writes straight back into the prop) can never re-seed our own state.
const seedConfig: Record<string, string> = { ...(props.initialConfig ?? {}) };
const hasSeed = props.initialConfig != null;

// Per-role state, keyed by ModelRole.key. Records rather than one ref per role
// so a new entry in MODEL_ROLES needs no new state declaration.
function seedModelGroups(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const role of MODEL_ROLES) out[role.key] = seedConfig[`model_group.${role.key}`] || '';
  return out;
}

function seedRoleProviders(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const role of MODEL_ROLES) out[role.key] = extractProvider(seedConfig[`model_group.${role.key}`]);
  // The main role falls back to the seeded default provider, so a wizard preset
  // that set only `default_provider` still opens on a provider.
  if (hasSeed && !out.high) out.high = seedConfig.default_provider || 'groq';
  return out;
}

function seedReasoningEfforts(): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const role of MODEL_ROLES) {
    if (role.showReasoning) out[role.key] = seedConfig[`model_group.${role.key}.reasoning_effort`] || null;
  }
  return out;
}

/** Specialized Vision / Audio Model feature toggles (opt-in; off by default). */
function seedOptionalEnabled(): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const role of MODEL_ROLES) {
    if (role.optIn) out[role.key] = seedConfig[`model_group.${role.key}.enabled`] === 'true';
  }
  return out;
}

const providers = ref<ProviderWithState[]>([]);
const modelGroups = ref<Record<string, string>>(seedModelGroups());
const roleProviders = ref<Record<string, string>>(seedRoleProviders());
const reasoningEfforts = ref<Record<string, string | null>>(seedReasoningEfforts());
const optionalEnabled = ref<Record<string, boolean>>(seedOptionalEnabled());
const apiKeyProvider = ref('');
const loading = ref(false);
const saving = ref(false);

const selectedApiKeyProvider = computed(() =>
  providers.value.find(p => p.name === apiKeyProvider.value) || null,
);

const addingCustomProvider = computed(() =>
  props.allowCustomProviders && apiKeyProvider.value === ADD_CUSTOM,
);
const editingCustomProvider = computed(() =>
  props.allowCustomProviders && !!selectedApiKeyProvider.value?.is_custom,
);

const apiKeysDescription = computed(() => {
  if (props.variant === 'step') {
    return 'Pick a provider to configure its credentials. You can switch providers and configure '
      + 'as many as you like — all entries are saved together when you finish setup.';
  }
  return props.allowCustomProviders
    ? 'Configure API keys and credentials for your LLM providers, or add a custom '
      + 'OpenAI API-compatible provider not in the list.'
    : 'Configure API keys and credentials for your LLM providers.';
});

function descriptionFor(role: ModelRole): string {
  return props.variant === 'step' ? role.stepDescription : role.description;
}

// ── Provider loading ────────────────────────────────────────────────────────

/** Whether a provider from the catalog is offered by this host. */
function isVisibleProvider(p: LLMProvider): boolean {
  return props.allowCustomProviders || !p.is_custom;
}

/**
 * Build a ProviderWithState from an API LLMProvider response.
 *
 * Two seeding strategies, chosen by whether a controlled `initialConfig` was
 * supplied: the wizard hydrates every field from the flat record it is holding
 * (nothing is stored server-side yet), Settings hydrates from the server's
 * `current_values` and leaves secrets blank.
 */
function buildProviderState(p: LLMProvider): ProviderWithState {
  const authMethods = p.auth_methods || [];
  let selectedMethod = p.active_auth_method || '';
  if (!selectedMethod && authMethods.length > 0) {
    const defaultMethod = authMethods.find(m => m.is_default);
    selectedMethod = defaultMethod ? defaultMethod.id : authMethods[0].id;
  }
  if (hasSeed) {
    const savedMethod = seedConfig[`${p.name}.auth_method`];
    if (savedMethod) selectedMethod = savedMethod;
  }
  // A browser-OAuth method can't be completed where we don't offer the flow,
  // and per-profile setup inherits the *admin's* `active_auth_method` — so
  // fall back to the default non-oauth method rather than fetching (and later
  // persisting) a method this host has no tokens for.
  if (!props.allowBrowserOauth) {
    const chosen = authMethods.find(m => m.id === selectedMethod);
    if (chosen?.kind === 'oauth') {
      const alt = authMethods.find(m => m.kind !== 'oauth' && m.is_default)
        || authMethods.find(m => m.kind !== 'oauth');
      selectedMethod = alt ? alt.id : '';
    }
  }

  if (hasSeed) {
    return {
      ...p,
      apiKey: seedConfig[`${p.name}.api_key`] || '',
      configValues: Object.fromEntries(
        Object.keys(p.config_fields || {}).map(k => [k, seedConfig[`${p.name}.${k}`] || '']),
      ),
      models: [],
      selectedAuthMethod: selectedMethod,
      authFieldValues: Object.fromEntries(
        authMethods.flatMap(am =>
          Object.keys(am.fields).map(k => [k, seedConfig[`${p.name}.${k}`] || '']),
        ),
      ),
    };
  }

  return {
    ...p,
    apiKey: '',
    configValues: { ...(p.current_values || {}) },
    models: [],
    selectedAuthMethod: selectedMethod,
    authFieldValues: {},
  };
}

/** Provider name → model id → model, accumulated across auth-method fetches. */
const seenModels = new Map<string, Map<string, LLMModel>>();

function rememberModels(p: ProviderWithState) {
  let byId = seenModels.get(p.name);
  if (!byId) {
    byId = new Map();
    seenModels.set(p.name, byId);
  }
  for (const m of p.models) byId.set(m.id, m);
}

/**
 * Reflatten both model lists: the offered one from each provider's current
 * (auth-method-scoped) catalog, the labelling one from every model seen since
 * mount. A provider that's no longer listed drops out of both.
 */
function rebuildModelLists() {
  rebuildModelList(providers.value);
  rebuildKnownModels(providers.value.map(p => ({
    ...p,
    models: [...(seenModels.get(p.name)?.values() ?? [])],
  })));
}

/**
 * Load one provider's models for its *currently selected* auth method.
 *
 * The method matters: some providers (OpenAI under Codex OAuth) serve a
 * different model set per method, and letting the server fall back to the
 * stored/default one is what made the wizard and Settings disagree.
 */
async function loadModelsFor(p: ProviderWithState): Promise<void> {
  try {
    const modelRes = await getProviderModels(
      props.agentUrl, props.token, p.name, p.selectedAuthMethod || undefined,
    );
    p.models = modelRes.models;
    rememberModels(p);
  } catch {
    // Provider catalog may not exist (or the refetch failed) — keep what we have.
  }
}

/** Fetch every visible provider and eagerly load its model list. */
async function fetchProvidersWithModels(): Promise<ProviderWithState[]> {
  const provRes = await listLLMProviders(props.agentUrl, props.token);
  const list = provRes.providers.filter(isVisibleProvider).map(buildProviderState);
  for (const p of list) {
    await loadModelsFor(p);
  }
  return list;
}

/** Reload the provider list + flattened model options; optionally reselect one. */
async function reloadProviders(selectName?: string) {
  providers.value = await fetchProvidersWithModels();
  rebuildModelLists();
  if (selectName !== undefined) apiKeyProvider.value = selectName;
}

/**
 * Drop role selections pointing at a provider this host doesn't list.
 *
 * Per-profile setup runs on the admin's JWT, so a seeded config can name a
 * `custom:<slug>` provider that the new profile will never have. Rendering it
 * would show a broken option and then persist a dead model reference.
 */
function pruneRolesForMissingProviders() {
  const known = new Set(providers.value.map(p => p.name));
  for (const role of MODEL_ROLES) {
    const owner = extractProvider(modelGroups.value[role.key]);
    if (owner && !known.has(owner)) {
      modelGroups.value[role.key] = '';
      if (role.showReasoning) reasoningEfforts.value[role.key] = null;
    }
    const selected = roleProviders.value[role.key];
    if (selected && !known.has(selected)) roleProviders.value[role.key] = '';
  }
}

onMounted(async () => {
  loading.value = true;
  try {
    if (props.selfSaving) {
      const [list, groupRes] = await Promise.all([
        fetchProvidersWithModels(),
        getModelGroups(props.agentUrl, props.token),
      ]);

      providers.value = list;

      for (const role of MODEL_ROLES) {
        const value = groupRes.model_groups[role.key] || '';
        modelGroups.value[role.key] = value;
        roleProviders.value[role.key] = extractProvider(value);
      }
      // The main role falls back to the stored default provider so a fresh
      // profile that never picked a model still opens on its provider.
      roleProviders.value.high =
        extractProvider(groupRes.model_groups.high) || groupRes.default_provider || '';
      optionalEnabled.value.vision = groupRes.vision_enabled ?? false;
      optionalEnabled.value.audio = groupRes.audio_enabled ?? false;

      rebuildModelLists();
      // Seed every reasoning-capable role, then let the stored efforts win —
      // a role the server has never heard of still gets a key to bind to.
      const efforts: Record<string, string | null> = {};
      for (const role of MODEL_ROLES) {
        if (role.showReasoning) efforts[role.key] = null;
      }
      reasoningEfforts.value = { ...efforts, ...(groupRes.reasoning_efforts || {}) };
    } else {
      providers.value = await fetchProvidersWithModels();
      rebuildModelLists();
      pruneRolesForMissingProviders();
    }
  } catch {
    if (props.selfSaving) ElMessage.error('Failed to load LLM settings');
    // Controlled mode: fall back to showing empty provider cards.
  } finally {
    loading.value = false;
  }
});

// ── Auth-method / OAuth reactions ───────────────────────────────────────────

/**
 * Refetch one provider's models after its auth method effectively changed.
 *
 * Called from child events only — never from the deep watcher below, which
 * would loop (fetch → mutate providers → watcher → fetch).
 *
 * Clicking the radio is a *preview*: nothing is saved until the card's own
 * Save (or a completed sign-in). So only the offered list narrows — the model
 * a role is bound to keeps its label and reasoning options via `knownModels`,
 * and the role's own refs are never touched here.
 */
async function refreshProviderModels(provider: ProviderWithState) {
  await loadModelsFor(provider);
  rebuildModelLists();
}

/** The "Sign in with ChatGPT" flow completed: tokens are stored server-side,
 *  so the provider now serves its OAuth-only model set. Pull it in place
 *  instead of reloading everything (a reload would discard credentials typed
 *  into the other provider cards).
 *
 *  The provider is named by the event rather than read off the dropdown: the
 *  sign-in completes minutes later in another window, and by then the user may
 *  well be looking at a different provider's card. */
async function handleOauthComplete(providerName: string) {
  const provider = providers.value.find(p => p.name === providerName);
  if (!provider) return;
  provider.configured = true;
  await refreshProviderModels(provider);
}

// ── Controlled mode: emit the full flat config ──────────────────────────────

/**
 * Emit the COMPLETE configuration record.
 *
 * The wizard replaces its `llmConfig` wholesale with whatever arrives here, so
 * every emit has to carry every key — a partial patch would silently drop
 * previously-entered values from `POST /api/config/setup`.
 */
function emitConfig() {
  if (props.selfSaving) return;

  const config: Record<string, string> = {
    // Derive default_provider from the main model's provider.
    default_provider: roleProviders.value.high || 'groq',
  };

  for (const role of MODEL_ROLES) {
    const value = modelGroups.value[role.key] || '';
    if (value) config[`model_group.${role.key}`] = value;
    if (role.showReasoning) {
      const effort = reasoningEfforts.value[role.key];
      if (effort) config[`model_group.${role.key}.reasoning_effort`] = effort;
    }
    // Always emit the opt-in toggle so turning a feature off also persists.
    if (role.optIn) {
      config[`model_group.${role.key}.enabled`] = String(!!optionalEnabled.value[role.key]);
    }
  }

  for (const p of providers.value) {
    const authMethods = p.auth_methods || [];
    if (authMethods.length > 0 && p.selectedAuthMethod) {
      const activeMethod = authMethods.find(m => m.id === p.selectedAuthMethod);
      // Browser-OAuth methods capture their tokens server-side against an
      // existing profile. Persisting a bare `auth_method=<oauth>` here would
      // point the new profile at a method it has no credentials for, so skip it.
      if (activeMethod && activeMethod.kind !== 'oauth') {
        config[`${p.name}.auth_method`] = p.selectedAuthMethod;
        for (const [key, value] of Object.entries(p.authFieldValues)) {
          if (value && key in activeMethod.fields) {
            config[`${p.name}.${key}`] = value;
          }
        }
      }
    }
    // Legacy: emit api_key if set.
    if (p.apiKey) config[`${p.name}.api_key`] = p.apiKey;
    // Legacy: emit config_fields values.
    for (const [key, value] of Object.entries(p.configValues)) {
      if (value) config[`${p.name}.${key}`] = value;
    }
  }

  // The provider fetch can fail on a revisit (`onMounted`'s catch leaves the
  // list empty). Rebuilding from an empty list would emit a record with no
  // credentials and no auth methods at all — and the host replaces its copy
  // wholesale, so everything typed on an earlier visit would vanish with
  // nothing on screen to show it. Carry the seed's provider keys forward.
  if (providers.value.length === 0) {
    for (const [key, value] of Object.entries(seedConfig)) {
      if (key === 'default_provider' || key.startsWith('model_group.')) continue;
      if (!(key in config)) config[key] = value;
    }
  }

  emit('update:config', config);
  emit('update:validity', buildValidity(config));
}

/**
 * Describe the record for a host that has to gate on it.
 *
 * Credential detection reads the record being emitted rather than component
 * state, so it always describes exactly what the host is about to persist.
 */
function buildValidity(config: Record<string, string>): LLMConfigValidity {
  const mainModel = config['model_group.high'] || '';
  const name = extractProvider(mainModel);
  const provider = providers.value.find(p => p.name === name);
  if (!mainModel || !provider) {
    return { mainModel, mainProviderLabel: name, mainProviderNeedsCredentials: false };
  }

  const label = provider.display_name || name;
  const method = (provider.auth_methods || []).find(m => m.id === provider.selectedAuthMethod);
  if (!method) {
    // Legacy provider shape: a bare API key is all there is to check.
    return {
      mainModel,
      mainProviderLabel: label,
      mainProviderNeedsCredentials: !config[`${name}.api_key`],
    };
  }
  // `none` (ollama, vllm) needs nothing. A device-code sign-in has no fields
  // but very much needs completing: its token reaches the record as an
  // api_key, so its absence is what "not signed in" looks like here.
  let needs = false;
  if (method.kind === 'device_code') {
    needs = !config[`${name}.api_key`];
  } else if (method.kind !== 'none') {
    needs = Object.entries(method.fields || {}).some(
      ([key, field]) => field.required && !config[`${name}.${key}`],
    ) || (Object.keys(method.fields || {}).length === 0 && !config[`${name}.api_key`]);
  }
  return { mainModel, mainProviderLabel: label, mainProviderNeedsCredentials: needs };
}

watch([modelGroups, roleProviders, reasoningEfforts, optionalEnabled], emitConfig, { deep: true });
// Provider credentials/auth methods live inside `providers`; this watcher only
// ever emits — triggering a fetch from here would loop back through itself.
watch(providers, emitConfig, { deep: true });

// ── Self-saving mode: REST actions ──────────────────────────────────────────

type CustomFormPayload = {
  display_name: string;
  base_url: string;
  api_key?: string;
  models: CustomProviderModel[];
};

async function handleCreateCustom(payload: CustomFormPayload) {
  saving.value = true;
  try {
    const res = await createCustomProvider(props.agentUrl, props.token, payload);
    ElMessage.success(`${payload.display_name} created`);
    await reloadProviders(res.name);
  } catch (e) {
    ElMessage.error(`Failed to create: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

async function handleUpdateCustom(payload: CustomFormPayload) {
  const name = apiKeyProvider.value;
  saving.value = true;
  try {
    const body: Record<string, unknown> = {
      display_name: payload.display_name,
      base_url: payload.base_url,
      models: payload.models,
    };
    if (payload.api_key) body.api_key = payload.api_key;
    await updateProvider(props.agentUrl, props.token, name, body);
    ElMessage.success('Custom provider updated');
    await reloadProviders(name);
  } catch (e) {
    ElMessage.error(`Failed to save: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

async function handleDeleteCustom() {
  const name = apiKeyProvider.value;
  const display = selectedApiKeyProvider.value?.display_name || 'this provider';
  if (!confirm(`Delete custom provider "${display}"? This removes its models and stored API key.`)) return;
  saving.value = true;
  try {
    await deleteProviderConfig(props.agentUrl, props.token, name);
    ElMessage.success('Custom provider deleted');
    await reloadProviders('');
  } catch (e) {
    ElMessage.error(`Failed to delete: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

/** Save provider credentials using the new auth_methods flow. */
async function saveProvider(provider: ProviderWithState) {
  const config: Record<string, string> = {};

  // If using auth_methods (new flow)
  if (provider.auth_methods && provider.auth_methods.length > 0) {
    const activeMethod = provider.auth_methods.find(m => m.id === provider.selectedAuthMethod);
    // Browser-OAuth methods ("Sign in with ChatGPT") persist their auth_method
    // server-side only when the flow completes and tokens are captured. Saving a
    // bare auth_method here would flip the active method to a not-yet-signed-in
    // state, so skip the PUT entirely for oauth methods.
    if (activeMethod?.kind === 'oauth') return;
    config.auth_method = provider.selectedAuthMethod;
    // Include field values for the selected auth method
    if (activeMethod) {
      for (const [key, value] of Object.entries(provider.authFieldValues)) {
        if (value && key in activeMethod.fields) {
          config[key] = value;
        }
      }
    }
  }

  if (Object.keys(config).length <= 1 && !config.auth_method) return;
  // Validate JSON fields
  if (provider.auth_methods) {
    const activeMethod = provider.auth_methods.find(m => m.id === provider.selectedAuthMethod);
    if (activeMethod) {
      for (const [key, value] of Object.entries(provider.authFieldValues)) {
        if (value && activeMethod.fields[key]?.type === 'json') {
          try {
            JSON.parse(value);
          } catch {
            ElMessage.error(`Invalid JSON for ${activeMethod.fields[key].description || key}`);
            return;
          }
        }
      }
    }
  }

  saving.value = true;
  try {
    await updateProvider(props.agentUrl, props.token, provider.name, config);
    provider.configured = true;
    ElMessage.success(`${provider.display_name} configuration saved`);
  } catch (e) {
    ElMessage.error(`Failed to save: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

/** Legacy: save API key only. */
async function saveProviderKey(provider: ProviderWithState) {
  if (!provider.apiKey) return;
  saving.value = true;
  try {
    await updateProvider(props.agentUrl, props.token, provider.name, {
      api_key: provider.apiKey,
    });
    provider.configured = true;
    ElMessage.success(`${provider.display_name} API key saved`);
  } catch (e) {
    ElMessage.error(`Failed to save: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

/** Legacy: save config fields. */
async function saveProviderConfig(provider: ProviderWithState) {
  const config: Record<string, string> = {};
  for (const [key, value] of Object.entries(provider.configValues)) {
    if (value) config[key] = value;
  }
  if (Object.keys(config).length === 0) return;

  for (const [key, value] of Object.entries(config)) {
    if (provider.config_fields?.[key]?.type === 'json') {
      try {
        JSON.parse(value);
      } catch {
        ElMessage.error(`Invalid JSON for ${provider.config_fields[key].description || key}`);
        return;
      }
    }
  }

  saving.value = true;
  try {
    await updateProvider(props.agentUrl, props.token, provider.name, config);
    for (const key of Object.keys(config)) {
      if (provider.config_fields?.[key]) {
        provider.config_fields[key].configured = true;
      }
    }
    provider.configured = true;
    ElMessage.success(`${provider.display_name} configuration saved`);
  } catch (e) {
    ElMessage.error(`Failed to save: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

async function removeProviderConfiguration(provider: ProviderWithState) {
  if (!confirm(`Remove all stored credentials for ${provider.display_name}?`)) return;
  saving.value = true;
  try {
    await deleteProviderConfig(props.agentUrl, props.token, provider.name);
    provider.configured = false;
    provider.apiKey = '';
    provider.authFieldValues = {};
    provider.configValues = {};
    // Reset configured status on auth method fields
    if (provider.auth_methods) {
      for (const am of provider.auth_methods) {
        for (const field of Object.values(am.fields)) {
          field.configured = false;
        }
      }
    }
    if (provider.config_fields) {
      for (const field of Object.values(provider.config_fields)) {
        field.configured = false;
      }
    }
    ElMessage.success(`${provider.display_name} configuration removed`);
  } catch (e) {
    ElMessage.error(`Failed to remove: ${e instanceof Error ? e.message : 'Unknown error'}`);
  } finally {
    saving.value = false;
  }
}

async function saveModelGroups() {
  // Everything falls back to the main model, so saving a blank one leaves the
  // profile unable to answer at all — and it is easy to do by accident,
  // because changing a section's Provider clears its Model. The backend
  // refuses this too; catching it here keeps the message next to the field.
  if (!modelGroups.value.high) {
    ElMessage.error('Choose a Model before saving — the assistant needs one to answer.');
    return;
  }
  saving.value = true;
  try {
    // Only the reasoning-capable roles carry an effort; the opt-in roles would
    // otherwise ship stray nulls that ModelGroupFields writes on provider change.
    const efforts: Record<string, string | null> = {};
    for (const role of MODEL_ROLES) {
      if (role.showReasoning) efforts[role.key] = reasoningEfforts.value[role.key] ?? null;
    }
    await updateModelGroups(
      props.agentUrl,
      props.token,
      { ...modelGroups.value },
      // Derive default_provider from the main model's provider.
      roleProviders.value.high || '',
      efforts,
      !!optionalEnabled.value.vision,
      !!optionalEnabled.value.audio,
    );
    ElMessage.success('Model groups updated');
  } catch {
    ElMessage.error('Failed to save model groups');
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="llm-config-form" :class="variant === 'step' ? 'variant-step' : 'variant-page'">
    <div v-if="loading" class="loading-state">Loading providers...</div>

    <template v-else>
      <!-- API Keys / credentials -->
      <div class="section">
        <div class="section-title-row">
          <h2 v-if="variant === 'page'" class="section-title">API Keys</h2>
          <h4 v-else class="section-subtitle">API Keys</h4>
        </div>
        <p class="section-description">{{ apiKeysDescription }}</p>

        <ElForm label-position="top" class="groups-form">
          <ElFormItem label="Provider">
            <ElSelect v-model="apiKeyProvider" placeholder="Select a provider to configure">
              <ElOption v-for="p in providers" :key="p.name" :label="p.display_name" :value="p.name" />
              <ElOption v-if="allowCustomProviders" :value="ADD_CUSTOM" label="➕ Add custom provider" />
            </ElSelect>
          </ElFormItem>

          <!-- Create a new custom provider -->
          <div v-if="addingCustomProvider" class="provider-config-inline">
            <CustomProviderForm
              mode="create"
              :saving="saving"
              @submit="handleCreateCustom"
              @cancel="apiKeyProvider = ''"
            />
          </div>

          <!-- Edit an existing custom provider -->
          <div v-else-if="editingCustomProvider && selectedApiKeyProvider" class="provider-config-inline">
            <CustomProviderForm
              mode="edit"
              :provider="selectedApiKeyProvider"
              :saving="saving"
              @submit="handleUpdateCustom"
              @delete="handleDeleteCustom"
            />
          </div>

          <!-- Built-in provider credentials -->
          <div v-else-if="selectedApiKeyProvider" class="provider-config-inline">
            <!-- Keyed by provider so switching the dropdown mounts a fresh
                 card: a reused instance kept an in-flight sign-in's polling
                 alive while its `provider` prop silently re-pointed, flagging
                 whichever provider happened to be selected when it landed. -->
            <ProviderConfigFields
              :key="selectedApiKeyProvider!.name"
              :provider="selectedApiKeyProvider"
              :show-configured-badge="showConfiguredBadge"
              :show-save-buttons="selfSaving"
              :persist-credentials="selfSaving"
              :saving="saving"
              :allow-browser-oauth="allowBrowserOauth"
              @save-provider="saveProvider(selectedApiKeyProvider!)"
              @save-key="saveProviderKey(selectedApiKeyProvider!)"
              @save-config="saveProviderConfig(selectedApiKeyProvider!)"
              @remove-config="removeProviderConfiguration(selectedApiKeyProvider!)"
              @auth-method-change="refreshProviderModels(selectedApiKeyProvider!)"
              @oauth-complete="handleOauthComplete($event)"
            />
          </div>
        </ElForm>
      </div>

      <!-- One section per model role, driven entirely by MODEL_ROLES. -->
      <div v-for="role in MODEL_ROLES" :key="role.key" class="section role-section">
        <div class="section-title-row">
          <h2 v-if="variant === 'page'" class="section-title">{{ role.title }}</h2>
          <h4 v-else class="section-subtitle">{{ role.title }}</h4>
          <ElSwitch v-if="role.optIn" v-model="optionalEnabled[role.key]" />
        </div>
        <!-- Static module copy (see MODEL_ROLES) — never user input. -->
        <p class="section-description" v-html="descriptionFor(role)"></p>

        <ModelGroupFields
          v-if="!role.optIn || optionalEnabled[role.key]"
          :providers="providers"
          :all-models="allModels"
          :known-models="knownModels"
          :use-vision="role.useVision"
          :use-audio="role.useAudio"
          :show-reasoning="role.showReasoning"
          :clearable="role.clearable"
          :model-placeholder="role.modelPlaceholder"
          :provider="roleProviders[role.key]"
          :model="modelGroups[role.key]"
          :reasoning-effort="reasoningEfforts[role.key] ?? null"
          @update:provider="roleProviders[role.key] = $event"
          @update:model="modelGroups[role.key] = $event"
          @update:reasoning-effort="reasoningEfforts[role.key] = $event"
        />
      </div>

      <ElButton v-if="selfSaving" type="primary" :loading="saving" @click="saveModelGroups">Save</ElButton>
    </template>
  </div>
</template>

<style scoped>
.llm-config-form { width: 100%; }

.loading-state { text-align: center; padding: 40px; color: var(--text-secondary); }

.section { margin-bottom: 32px; }
.variant-step .section { margin-bottom: 24px; }

.section-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.section-title { font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0; }
.section-subtitle { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0; }

.section-description {
  color: var(--text-secondary); font-size: 0.85rem; margin: 0 0 16px 0; line-height: 1.5;
}
.variant-step .section-description { font-size: 0.875rem; margin-bottom: 12px; }

.groups-form { max-width: 480px; }

/* The inline credential editor sits in its own bordered card in both hosts, so
   it reads as belonging to the Provider dropdown above it. */
.provider-config-inline {
  margin: 0 0 16px 0;
  padding: 12px 16px;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  background: var(--surface-color, #fafafa);
  max-width: 480px;
}

/* Wizard only: box each model role so the tall step card stays scannable. The
   settings page uses plain, full-width sections instead. */
.variant-step .role-section {
  padding: 16px;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
  background: var(--surface-color, #fafafa);
}
</style>
