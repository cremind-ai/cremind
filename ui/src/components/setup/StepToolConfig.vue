<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { ElCard, ElDivider } from 'element-plus';
import { Icon } from '@iconify/vue';
import {
  listTools, listLLMProviders, getProviderModels,
  type ToolStatus, type LLMProvider,
} from '../../services/configApi';
import type { JsonSchema } from '../../services/agentApi';
import type { ProfileValue } from '../../stores/settings';
import { useLLMModels } from '../../composables/useLLMModels';
import ItemCardHeader from '../shared/ItemCardHeader.vue';
import ToolVariablesForm from '../shared/ToolVariablesForm.vue';
import ToolArgumentsForm from '../shared/ToolArgumentsForm.vue';
import LLMParametersForm from '../shared/LLMParametersForm.vue';

const props = defineProps<{
  agentUrl: string;
  configs: Record<string, Record<string, string>>;
  profile: string;
  isFirstSetup: boolean;
}>();

const emit = defineEmits<{
  update: [configs: Record<string, Record<string, string>>];
  'update:agentConfigs': [configs: Record<string, Record<string, string>>];
}>();

const { rebuildModelList, getFilteredModels, getReasoningOptions } = useLLMModels();

interface SetupToolItem {
  name: string;
  displayName: string;
  isSkill: boolean;

  // Tool config fields (secrets)
  configFields: Record<string, { description: string; type: string; secret: boolean; configured: boolean }>;
  configValues: Record<string, string>;

  // Tool arguments
  argumentsSchema: JsonSchema | null;
  argValues: Record<string, ProfileValue>;

  // Agent LLM config
  description: string;
  systemPrompt: string;
  llmProvider: string;
  llmModel: string;
  reasoningEffort: string;
  fullReasoning: boolean;

  // Code-level defaults from TOOL_CONFIG.llm_parameters (placeholders only).
  llmDefaults: NonNullable<ToolStatus['llm_defaults']>;

  enabled: boolean;
  expanded: boolean;

  /** Optional pip-extras group name (``"browser"``, ``"documents"``,
   *  ``"google"``). Surfaced as a hint next to the toggle so the user
   *  knows their selection will trigger a pip install on submit. */
  requiresFeature: string | null;
}

const items = ref<SetupToolItem[]>([]);
const llmProviders = ref<LLMProvider[]>([]);
const loading = ref(false);

const builtinItems = computed(() => items.value.filter(i => !i.isSkill));
const skillItems = computed(() => items.value.filter(i => i.isSkill));
const hasSkills = computed(() => skillItems.value.length > 0);

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

function initArgValues(
  schema: JsonSchema | null,
  storedValues?: Record<string, unknown> | null,
): Record<string, ProfileValue> {
  if (!schema || !schema.properties) return {};
  const stored = storedValues || {};
  const values: Record<string, ProfileValue> = {};
  for (const [propName, propDef] of Object.entries(schema.properties)) {
    values[propName] = (stored[propName] as ProfileValue) ?? getDefaultForType(propDef.type);
  }
  return values;
}

function getStatusTag(item: SetupToolItem) {
  // Built-in tools report `llm_bound=false` until setup completes, so using it
  // here would flag every tool as "Setup required" during the wizard. Instead,
  // surface whether the tool still has unfilled required variables — that's
  // the piece the user can act on from this screen.
  const hasUnfilledRequired = Object.keys(item.configFields).some(
    key => !item.configValues[key],
  );
  if (hasUnfilledRequired) {
    return { label: 'Needs Config', type: 'warning' as const };
  }
  if (item.enabled) return { label: 'Enabled', type: 'success' as const };
  return { label: 'Disabled', type: 'info' as const };
}

onMounted(async () => {
  loading.value = true;
  try {
    // Fetch tools (always available, no auth needed during setup)
    const toolRes = await listTools(props.agentUrl, '');

    // Built-in tools and skills are returned directly by /api/tools (with
    // tool_type 'builtin' or 'skill'). /api/agents only carries a2a/mcp
    // tools and is post-storage-only, so the wizard doesn't need it.

    // Load LLM providers and models
    try {
      const provRes = await listLLMProviders(props.agentUrl, '');
      llmProviders.value = provRes.providers;
      const providersWithModels: { name: string; display_name: string; models: any[] }[] = [];
      for (const p of provRes.providers) {
        try {
          const modelRes = await getProviderModels(props.agentUrl, '', p.name);
          providersWithModels.push({ name: p.name, display_name: p.display_name, models: modelRes.models });
        } catch {
          providersWithModels.push({ name: p.name, display_name: p.display_name, models: [] });
        }
      }
      rebuildModelList(providersWithModels);
    } catch { /* LLM providers may not be available yet */ }

    // Build items -- the new /api/tools response already includes everything
    // we need (display name, required_fields, current variable values, llm
    // overrides via config.llm, etc.).
    const existingConfigs = props.configs;
    const localTools = toolRes.tools.filter(
      (t: ToolStatus) => t.tool_type === 'builtin' || t.tool_type === 'skill',
    );
    items.value = localTools.map((tool: ToolStatus) => {
      const schema = (tool.arguments_schema as JsonSchema | null) ?? null;
      const toolConf = existingConfigs[tool.tool_id] || {};
      const llmCfg = (tool.config?.llm ?? {}) as Record<string, unknown>;
      const metaCfg = (tool.config?.meta ?? {}) as Record<string, string>;

      return {
        name: tool.tool_id,
        displayName: tool.name,
        isSkill: tool.tool_type === 'skill',
        configFields: { ...(tool.required_fields ?? {}) },
        configValues: { ...initVarValues(tool.required_fields, tool.config?.variables), ...toolConf },
        argumentsSchema: schema,
        argValues: initArgValues(schema, tool.config?.arguments),
        // User override only; code defaults are shown as placeholders via llmDefaults.
        description: metaCfg.description || '',
        systemPrompt: (metaCfg.system_prompt as string) || '',
        llmProvider: (llmCfg.llm_provider as string) || '',
        llmModel: (llmCfg.llm_model as string) || '',
        reasoningEffort: (llmCfg.reasoning_effort as string) || '',
        fullReasoning: !!tool.full_reasoning,
        llmDefaults: tool.llm_defaults ?? {},
        enabled: tool.enabled,
        expanded: false,
        requiresFeature: tool.requires_feature ?? null,
      };
    });
  } catch {
    // Tools endpoint may not be available yet during setup
  } finally {
    loading.value = false;
  }
});

function emitConfigs() {
  const configs: Record<string, Record<string, string>> = {};
  const agentConfigs: Record<string, Record<string, string>> = {};

  for (const item of items.value) {
    // Tool configs: secrets, _enabled, _full_reasoning, _arg.*
    const toolConfig: Record<string, string> = {};
    for (const [key, value] of Object.entries(item.configValues)) {
      if (value) toolConfig[key] = value;
    }
    toolConfig['_enabled'] = String(item.enabled);
    toolConfig['_full_reasoning'] = String(item.fullReasoning);

    // Tool arguments (stored as _arg.* keys in tool_configs)
    if (item.argumentsSchema && Object.keys(item.argValues).length > 0) {
      for (const [key, value] of Object.entries(item.argValues)) {
        const stored = typeof value === 'string' ? value : JSON.stringify(value);
        if (stored) toolConfig[`_arg.${key}`] = stored;
      }
    }

    if (Object.keys(toolConfig).length > 0) {
      configs[item.name] = toolConfig;
    }

    // Agent configs: LLM parameters (stored in mcp_server_storage)
    if (!item.isSkill) {
      const ac: Record<string, string> = {};
      if (item.description) ac.description = item.description;
      if (item.systemPrompt) ac.system_prompt = item.systemPrompt;
      if (item.llmProvider) ac.llm_provider = item.llmProvider;
      if (item.llmModel) ac.llm_model = item.llmModel;
      if (item.reasoningEffort) ac.reasoning_effort = item.reasoningEffort;
      if (Object.keys(ac).length > 0) {
        agentConfigs[item.name] = ac;
      }
    }
  }

  emit('update', configs);
  emit('update:agentConfigs', agentConfigs);
}

watch(items, emitConfigs, { deep: true });
</script>

<template>
  <div class="step-tool-config">
    <h3 class="step-title">Tool Configuration</h3>
    <p class="step-description">
      Configure built-in tools and skills. Expand each item to set variables, arguments, and LLM parameters.
    </p>

    <div v-if="loading" class="loading-state">Loading tools...</div>

    <template v-else>
      <!-- Built-in Tools -->
      <div class="section">
        <h4 class="section-subtitle">
          <Icon icon="mdi:toolbox" class="section-icon" /> Built-in Tools
        </h4>

        <div class="items-list">
          <ElCard v-for="item in builtinItems" :key="item.name" class="item-card" shadow="hover">
            <ItemCardHeader
              :name="item.displayName"
              :status-tag="getStatusTag(item)"
              :expanded="item.expanded"
              :enabled="item.enabled"
              @toggle-expand="item.expanded = !item.expanded"
              @update:enabled="item.enabled = $event"
            />

            <div
              v-if="item.requiresFeature && item.enabled"
              class="requires-feature-hint"
            >
              <Icon icon="mdi:package-down" /> Installs:
              <code>cremind[{{ item.requiresFeature }}]</code>
              <span class="requires-feature-note">
                — pip extras for this tool will be installed when you submit.
              </span>
            </div>

            <div v-if="item.expanded" class="item-config">
              <!-- Tool Variables -->
              <div v-if="Object.keys(item.configFields).length > 0" class="config-section">
                <ToolVariablesForm
                  :fields="item.configFields"
                  :values="item.configValues"
                  @update:values="item.configValues = $event"
                />
              </div>

              <!-- LLM Parameters -->
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
                  :default-description="item.llmDefaults.description || item.llmDefaults.server_instructions || ''"
                  :default-system-prompt="item.llmDefaults.system_prompt || ''"
                  :default-llm-provider="item.llmDefaults.llm_provider || ''"
                  :default-llm-model="item.llmDefaults.llm_model || ''"
                  :default-reasoning-effort="item.llmDefaults.reasoning_effort || ''"
                  :default-full-reasoning="item.llmDefaults.full_reasoning"
                />
              </div>

              <!-- Tool Arguments -->
              <div v-if="item.argumentsSchema && Object.keys(item.argumentsSchema.properties).length > 0" class="config-section">
                <ToolArgumentsForm
                  :schema="item.argumentsSchema"
                  :arg-values="item.argValues"
                  @update:arg-values="item.argValues = $event"
                />
              </div>
            </div>
          </ElCard>
        </div>
      </div>

      <!-- Skills -->
      <template v-if="hasSkills">
        <ElDivider />

        <div class="section">
          <h4 class="section-subtitle">
            <Icon icon="mdi:creation" class="section-icon" /> Skills
          </h4>

          <div class="items-list">
            <ElCard v-for="item in skillItems" :key="item.name" class="item-card" shadow="hover">
              <ItemCardHeader
                :name="item.displayName"
                :status-tag="getStatusTag(item)"
                :expanded="item.expanded"
                :enabled="item.enabled"
                @toggle-expand="item.expanded = !item.expanded"
                @update:enabled="item.enabled = $event"
              />

              <div v-if="item.expanded" class="item-config">
                <!-- Tool Variables -->
                <div v-if="Object.keys(item.configFields).length > 0" class="config-section">
                  <ToolVariablesForm
                    :fields="item.configFields"
                    :values="item.configValues"
                    @update:values="item.configValues = $event"
                  />
                </div>

                <!-- Tool Arguments -->
                <div v-if="item.argumentsSchema && Object.keys(item.argumentsSchema.properties).length > 0" class="config-section">
                  <ToolArgumentsForm
                    :schema="item.argumentsSchema"
                    :arg-values="item.argValues"
                    @update:arg-values="item.argValues = $event"
                  />
                </div>
              </div>
            </ElCard>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.step-tool-config { padding: 8px 0; }

.step-title {
  font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0;
}

.step-description {
  color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 20px 0; line-height: 1.5;
}

.loading-state { text-align: center; padding: 40px; color: var(--text-secondary); }

.section { margin-bottom: 8px; }

.section-subtitle {
  font-size: 1rem; font-weight: 600; color: var(--text-primary);
  margin: 0 0 12px 0; display: flex; align-items: center; gap: 8px;
}

.section-icon { font-size: 18px; color: var(--primary-color); }

.items-list { display: flex; flex-direction: column; gap: 10px; }

.item-card { background: var(--surface-color); }

.item-config {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-color, #e4e7ed);
}

.config-section { margin-bottom: 12px; }
.config-section + .config-section {
  padding-top: 12px;
  border-top: 1px dashed var(--border-color, #e4e7ed);
}

.config-section-title {
  font-size: 0.8rem; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.03em; margin: 0 0 8px 0;
}

/* Surfaces the optional pip extras a built-in tool needs (e.g. browser,
   documents, google). Shown only when the tool is enabled, so the user
   can see what their submit-time pip install will pull in. */
.requires-feature-hint {
  margin-top: 8px;
  font-size: 0.8rem;
  color: var(--text-secondary, #606266);
  display: flex; align-items: center; gap: 4px; flex-wrap: wrap;
}
.requires-feature-hint code {
  background: var(--el-fill-color-light, rgba(0, 0, 0, 0.04));
  padding: 1px 6px; border-radius: 3px;
  font-family: var(--el-font-family-monospace, ui-monospace, Menlo, monospace);
}
.requires-feature-note { color: var(--text-secondary, #909399); }
</style>
