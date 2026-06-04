<script setup lang="ts">
import { computed, watch } from 'vue';
import { ElRadioGroup, ElRadio } from 'element-plus';
import type { DeploymentMode, ServiceCapability } from '../../services/configApi';
import type { InstallCatalog } from '../../services/installCatalogApi';
import { renderServiceModeDescription } from '../../services/installCatalogApi';

// A service-aware radio that lets the user pick how a backing service
// (Postgres / Qdrant / Chroma) is deployed: Docker, Native, or
// External. The supported_modes list comes from
// ``GET /api/services/capabilities``; the backend has already applied
// the install-mode rule from install_catalog.toml (see
// app/api/features.py) and removed modes the install can't honour, so
// we render whatever survived. On top of that we mask Docker locally
// when the host can't drive a daemon. SQLite (and any future
// local-only service) is intentionally absent — the parent step skips
// mounting this component when the service has only one possible mode
// in the first place.
//
// When the visible mode set collapses to a single option, we render an
// info line instead of a one-button radio: the choice isn't a choice,
// and saying "Docker — Cremind starts a Postgres container" is clearer
// than asking the user to tick a sole radio.
//
// Labels and descriptions come from the install catalog when it's
// provided as a prop (the common case); a small hardcoded fallback
// keeps the component renderable on backends that don't ship the
// catalog endpoint (older builds, smoke tests).

const props = defineProps<{
  modelValue: DeploymentMode;
  service: ServiceCapability;
  /** Backend reported ``docker_available === false``; mask the Docker
   *  radio so the user can't pick a mode that the host can't honour. */
  dockerAvailable: boolean;
  /** Optional install catalog — when present, labels/descriptions are
   *  read from its ``service_modes`` table so install.sh and the
   *  wizard never drift. */
  catalog?: InstallCatalog | null;
}>();

const emit = defineEmits<{
  'update:modelValue': [mode: DeploymentMode];
}>();

const visibleModes = computed<DeploymentMode[]>(() =>
  props.service.supported_modes.filter((mode) =>
    mode === 'docker' ? props.dockerAvailable : true,
  ),
);

const isSingleMode = computed(() => visibleModes.value.length === 1);

// Belt-and-suspenders: if the parent passes a modelValue that the
// visible mode set doesn't include (saved config from before a
// capability change, etc.), snap it to the first visible mode so the
// form below doesn't render a stale shape.
watch(
  visibleModes,
  (modes) => {
    if (modes.length === 0) return;
    if (!modes.includes(props.modelValue)) {
      emit('update:modelValue', modes[0]);
    }
  },
  { immediate: true },
);

function modeLabel(mode: DeploymentMode): string {
  const entry = props.catalog?.service_modes?.[mode];
  if (entry) return entry.label;
  if (mode === 'docker') return 'Docker';
  if (mode === 'native') return 'Native';
  return 'External';
}

function modeDescription(mode: DeploymentMode): string {
  const name = props.service.display_name;
  const entry = props.catalog?.service_modes?.[mode];
  if (entry) return renderServiceModeDescription(entry, name);
  if (mode === 'docker') {
    return `Cremind starts a ${name} container alongside itself.`;
  }
  if (mode === 'native') {
    return `Cremind runs ${name} locally (no extra container).`;
  }
  return `Connect to an existing ${name} instance.`;
}

function onChange(value: DeploymentMode) {
  emit('update:modelValue', value);
}

const showDockerUnavailableHint = computed(
  () => !props.dockerAvailable && props.service.supported_modes.includes('docker'),
);
</script>

<template>
  <div class="deployment-mode-radio">
    <!-- Single-mode passthrough: no radio, just confirm what will happen. -->
    <div v-if="isSingleMode" class="single-mode">
      <strong>{{ modeLabel(visibleModes[0]) }}</strong>
      — {{ modeDescription(visibleModes[0]) }}
    </div>
    <!-- Multi-mode: render the radio. -->
    <ElRadioGroup
      v-else
      :model-value="modelValue"
      @update:model-value="onChange as any"
    >
      <ElRadio v-for="mode in visibleModes" :key="mode" :value="mode">
        <strong>{{ modeLabel(mode) }}</strong>
        — {{ modeDescription(mode) }}
      </ElRadio>
    </ElRadioGroup>
    <!-- Only shown when Docker is masked for an environment reason
         (no docker CLI / no compose file on this install). The
         backend's install-time policy filter is silent — when it
         hides External, that's an intentional simplification, not
         something the user should be warned about. -->
    <div v-if="showDockerUnavailableHint" class="field-hint">
      Docker mode is not available on this install (Cremind is running natively).
    </div>
  </div>
</template>

<style scoped>
.deployment-mode-radio :deep(.el-radio-group) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  width: 100%;
  gap: 8px;
}

.deployment-mode-radio :deep(.el-radio) {
  display: flex;
  align-items: flex-start;
  width: 100%;
  height: auto;
  margin-right: 0;
  white-space: normal;
}

.deployment-mode-radio :deep(.el-radio__input) {
  margin-top: 3px;
}

.deployment-mode-radio :deep(.el-radio__label) {
  white-space: normal;
  line-height: 1.5;
}

.single-mode {
  padding: 10px 14px;
  background: var(--hover-bg);
  border-radius: 6px;
  font-size: 0.875rem;
  color: var(--text-secondary);
  line-height: 1.5;
}

.single-mode strong {
  color: var(--text-primary);
}

.field-hint {
  margin-top: 6px;
  font-size: 0.78rem;
  color: var(--text-secondary);
  line-height: 1.4;
}
</style>
