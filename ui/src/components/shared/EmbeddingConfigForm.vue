<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { ElForm, ElFormItem, ElInput, ElInputNumber, ElSelect, ElOption, ElSwitch } from 'element-plus';
import DeploymentModeRadio from './DeploymentModeRadio.vue';
import type {
  DeploymentMode,
  EmbeddingSetupConfig,
  ServiceCapability,
  ServiceCapabilitiesResponse,
} from '../../services/configApi';
import type { InstallCatalog } from '../../services/installCatalogApi';

/**
 * Shared Vector Embedding config form: the enable switch, the embedding-model
 * picker + HuggingFace token, and the vector-store (Qdrant / ChromaDB) fields
 * with their deployment-mode radios.
 *
 * Reused by the Setup Wizard's embedding step (`setup/StepEmbeddingConfig.vue`)
 * and the Settings "Vector Embedding" page (`views/EmbeddingSettings.vue`).
 * Those two used to carry field-for-field copies of this markup — plus their
 * own copies of the payload defaults and of `pickInitialMode` — and the copies
 * had already drifted (different model labels, different hints for the same
 * field). This is now the only copy; each host keeps only what is genuinely
 * its own.
 *
 * Deliberately *controlled and persistence-free*: the payload comes in through
 * `modelValue` and every edit goes straight back out via `update:modelValue`,
 * because the two hosts save on completely different transports — the wizard
 * bubbles its payload up to `SetupWizard.vue` (posted later, with the rest of
 * the setup body, to `POST /api/config/setup`), while Settings self-saves
 * through its own Apply/confirm + `applyEmbeddingConfig` flow.
 *
 * Slots carry the host-specific chrome around the shared fields:
 * - `intro` — above the enable switch. The wizard puts its "What you get when
 *   enabled" benefits box here; Settings puts the live status row + error
 *   banner here.
 * - `enable-hint` (scoped: `{ enabled }`) — the hint under the enable switch.
 *   Both hosts override it — the wizard says what skipping costs, Settings
 *   warns that applying reloads and rebuilds the caches — so the built-in text
 *   is only a neutral fallback. The slot is scoped so a host can render the
 *   on/off wording without having to read back the payload it just received.
 * - `store-hint` — under the Vector Store provider select. Only Settings fills
 *   it ("switching stores triggers a full rebuild"); during setup there is
 *   nothing to rebuild yet.
 *
 * Note for hosts: slot content is compiled in the *parent's* scope, so a host
 * that renders a `.field-hint` into one of these slots must keep that rule in
 * its own scoped CSS — this component's scoped styles do not reach it.
 */

/** The embedding block, under the name both hosts already use. It is the same
 *  shape the wizard posts and the Settings page reads/writes back, so it is an
 *  alias of the transport type rather than a third hand-maintained copy. */
export type EmbeddingConfigPayload = EmbeddingSetupConfig;

const props = withDefaults(defineProps<{
  /** The embedding payload. Accepts a partial because this component owns the
   *  defaults: `{}` (first wizard run, nothing saved yet) and a full config off
   *  `GET /api/embedding/config` both normalize to the same shape, which is
   *  emitted straight back on mount. */
  modelValue: Partial<EmbeddingConfigPayload>;
  /** Per-service deployment-mode descriptor from
   *  `/api/services/capabilities`. Null while still loading — the form mounts
   *  with the External fields visible until it arrives. */
  serviceCapabilities?: ServiceCapabilitiesResponse | null;
  /** Install catalog, forwarded to `DeploymentModeRadio` so service-mode
   *  labels stay in sync with the install scripts. Cosmetic: the radio falls
   *  back to its built-in labels when this is null. */
  installCatalog?: InstallCatalog | null;
  /** Freeze every field. Settings passes `isBusy || saving` so the form can't
   *  be edited while a rebuild is running; the wizard has nothing to wait on
   *  and leaves it false. */
  disabled?: boolean;
}>(), {
  serviceCapabilities: null,
  installCatalog: null,
  disabled: false,
});

const emit = defineEmits<{
  'update:modelValue': [config: EmbeddingConfigPayload];
}>();

const qdrantCapability = computed(() => props.serviceCapabilities?.services?.qdrant ?? null);
const chromaCapability = computed(() => props.serviceCapabilities?.services?.chroma ?? null);
const dockerAvailable = computed(() => props.serviceCapabilities?.docker_available ?? false);

// Modes left after both layers of filtering: the backend already trims what
// the install mode can't honour (see app/api/features.py), and we drop Docker
// locally when the host can't drive a daemon.
function effectiveModes(cap: ServiceCapability): DeploymentMode[] {
  return cap.supported_modes.filter((mode) => (mode === 'docker' ? dockerAvailable.value : true));
}

// When the saved deployment_mode doesn't survive that filter (e.g. a previous
// setup saved ``external`` for Qdrant but the install mode now restricts to
// Docker-only), snap to the first effectively allowed mode so the form renders
// the sub-fields that match what will actually be provisioned.
function pickInitialMode(
  saved: DeploymentMode | undefined,
  cap: ServiceCapability | null,
  preferred: DeploymentMode,
): DeploymentMode {
  if (!cap) return saved ?? preferred;
  const effective = effectiveModes(cap);
  // An empty list means "we don't know", not "nothing is supported", so the
  // stored mode has to survive it — same refusal the clamp watchers below
  // make. Without this, normalize() would snap a saved mode to ``preferred``
  // and emit that up as the user's choice, which is what Apply then posts.
  if (!effective.length) return saved ?? preferred;
  if (saved && effective.includes(saved)) return saved;
  if (effective.includes(preferred)) return preferred;
  return effective[0] ?? preferred;
}

function normalize(src: Partial<EmbeddingConfigPayload>): EmbeddingConfigPayload {
  const vs = src.vectorstore;
  const next: EmbeddingConfigPayload = {
    enabled: src.enabled ?? false,
    provider: src.provider ?? 'me5',
    hf_token: src.hf_token ?? '',
    vectorstore: {
      // Default to ChromaDB so the user can complete setup with no extra
      // configuration — Chroma supports Native (in-process persistent
      // file) which works on every install.
      provider: vs?.provider ?? 'chroma',
      deployment_mode: 'external',  // recomputed by syncTopLevelMode below
      qdrant: {
        deployment_mode: pickInitialMode(
          vs?.qdrant?.deployment_mode,
          qdrantCapability.value,
          'external',
        ),
        host: vs?.qdrant?.host ?? 'localhost',
        port: vs?.qdrant?.port ?? 6333,
        api_key: vs?.qdrant?.api_key ?? '',
        https: vs?.qdrant?.https ?? false,
      },
      chroma: {
        deployment_mode: pickInitialMode(
          vs?.chroma?.deployment_mode,
          chromaCapability.value,
          'native',
        ),
        host: vs?.chroma?.host ?? 'localhost',
        port: vs?.chroma?.port ?? 8000,
        ssl: vs?.chroma?.ssl ?? false,
        api_key: vs?.chroma?.api_key ?? '',
        persist_path: vs?.chroma?.persist_path ?? '',
      },
    },
  };
  next.vectorstore.deployment_mode = next.vectorstore[next.vectorstore.provider].deployment_mode;
  return next;
}

const form = ref<EmbeddingConfigPayload>(normalize(props.modelValue));

// Keep the top-level ``vectorstore.deployment_mode`` in sync with the active
// provider's mode — the backend reads either, but having them agree keeps the
// persisted state consistent across edits. Guarded so it doesn't re-trigger
// the deep watcher below when there is nothing to change.
function syncTopLevelMode() {
  const active = form.value.vectorstore[form.value.vectorstore.provider].deployment_mode;
  if (form.value.vectorstore.deployment_mode !== active) {
    form.value.vectorstore.deployment_mode = active;
  }
}

function emitUpdate() {
  emit('update:modelValue', JSON.parse(JSON.stringify(form.value)) as EmbeddingConfigPayload);
}

watch(form, () => {
  syncTopLevelMode();
  emitUpdate();
}, { deep: true });

// Re-hydrate when the host swaps the payload underneath us (Settings replaces
// its ref wholesale once ``GET /api/embedding/config`` resolves). The
// structural compare is what keeps the round-trip from looping: our own emit
// comes straight back down as ``modelValue`` and must not re-normalize.
watch(() => props.modelValue, (next) => {
  // An empty payload has nothing to hydrate *from*, and re-hydrating on one
  // can only clobber: the wizard hands down a fresh ``{}`` literal on every
  // re-render while its own ref is still unset, and a new object identity
  // would otherwise reset the form under the user. ``form`` already holds the
  // defaults from the initial normalize.
  if (!next || Object.keys(next).length === 0) return;
  if (JSON.stringify(next) === JSON.stringify(form.value)) return;
  form.value = normalize(next);
}, { deep: true });

// When capabilities arrive after mount, clamp any mode the live capability
// list no longer supports. We also watch docker_available so flipping it
// triggers re-evaluation.
watch([qdrantCapability, dockerAvailable], () => {
  const cap = qdrantCapability.value;
  if (!cap) return;
  const effective = effectiveModes(cap);
  // An empty list tells us nothing useful, so leave the saved mode alone
  // rather than snapping it to a guess.
  if (effective.length && !effective.includes(form.value.vectorstore.qdrant.deployment_mode)) {
    form.value.vectorstore.qdrant.deployment_mode = pickInitialMode(
      form.value.vectorstore.qdrant.deployment_mode, cap, 'external',
    );
  }
});
watch([chromaCapability, dockerAvailable], () => {
  const cap = chromaCapability.value;
  if (!cap) return;
  const effective = effectiveModes(cap);
  if (effective.length && !effective.includes(form.value.vectorstore.chroma.deployment_mode)) {
    form.value.vectorstore.chroma.deployment_mode = pickInitialMode(
      form.value.vectorstore.chroma.deployment_mode, cap, 'native',
    );
  }
});

const showGemmaToken = computed(() => form.value.enabled && form.value.provider === 'gemma');
const showQdrant = computed(() => form.value.enabled && form.value.vectorstore.provider === 'qdrant');
const showChroma = computed(() => form.value.enabled && form.value.vectorstore.provider === 'chroma');

const qdrantMode = computed(() => form.value.vectorstore.qdrant.deployment_mode);
const chromaMode = computed(() => form.value.vectorstore.chroma.deployment_mode);

onMounted(() => {
  // The initial ``normalize`` runs before any watcher is armed, so hand the
  // host the defaulted/clamped payload once — the wizard relies on this to
  // pick up a config for a step the user never touched.
  syncTopLevelMode();
  emitUpdate();
});
</script>

<template>
  <div class="embedding-config-form">
    <!-- Host chrome above the fields: the wizard's benefits box, Settings'
         live status row + error banner. -->
    <slot name="intro" />

    <ElForm label-position="top" class="config-form" :disabled="disabled">
      <ElFormItem label="Enable Vector Embedding">
        <!-- ElForm's ``disabled`` already propagates here; kept explicit (as
             Settings had it) because this is the one control whose state the
             user is most likely to fight with mid-rebuild. -->
        <ElSwitch v-model="form.enabled" :disabled="disabled" />
        <slot name="enable-hint" :enabled="form.enabled">
          <div class="field-hint">
            {{ form.enabled
              ? 'Configure the embedding model and vector store below.'
              : 'Semantic search stays off. You can enable it later.' }}
          </div>
        </slot>
      </ElFormItem>

      <template v-if="form.enabled">
        <div class="section-divider"></div>
        <h4 class="section-title">Embedding Model</h4>

        <ElFormItem label="Model">
          <ElSelect v-model="form.provider" placeholder="Select an embedding model" style="width: 100%">
            <ElOption value="me5" label="ME5 — Multilingual E5 Base (no auth required, 768 dims)" />
            <ElOption value="gemma" label="Gemma 300M — Google (requires HuggingFace token, 768 dims)" />
          </ElSelect>
        </ElFormItem>

        <ElFormItem v-if="showGemmaToken" label="HuggingFace Token (HF_TOKEN)">
          <ElInput
            v-model="form.hf_token"
            type="password"
            show-password
            placeholder="hf_..."
          />
          <div class="field-hint">
            Required to download the gated <code>google/embeddinggemma-300m</code> model. Generate one at huggingface.co/settings/tokens.
          </div>
        </ElFormItem>

        <div class="section-divider"></div>
        <h4 class="section-title">Vector Store</h4>

        <ElFormItem label="Provider">
          <ElSelect v-model="form.vectorstore.provider" style="width: 100%">
            <ElOption value="qdrant" label="Qdrant" />
            <ElOption value="chroma" label="ChromaDB" />
          </ElSelect>
          <!-- Settings warns here that switching stores forces a full
               rebuild; nothing is built yet during setup. -->
          <slot name="store-hint" />
        </ElFormItem>

        <template v-if="showQdrant">
          <ElFormItem v-if="qdrantCapability" label="Qdrant Deployment">
            <DeploymentModeRadio
              v-model="form.vectorstore.qdrant.deployment_mode"
              :service="qdrantCapability"
              :docker-available="dockerAvailable"
              :catalog="installCatalog ?? null"
            />
          </ElFormItem>
          <template v-if="qdrantMode === 'docker'">
            <div class="info-box">
              Cremind will start a <code>qdrant/qdrant</code> container alongside
              itself and connect to it on <code>qdrant:6333</code>.
            </div>
          </template>
          <!-- A bare v-else, as the Settings page had it: Qdrant has no Native
               mode yet (managed binary is phase 2), so any mode that isn't
               Docker is an external endpoint. Testing for ``external`` instead
               would leave a config carrying an unexpected mode with no fields
               at all — a blank pane the user can't edit their way out of. -->
          <template v-else>
            <ElFormItem label="Qdrant Host">
              <ElInput v-model="form.vectorstore.qdrant.host" placeholder="localhost" />
            </ElFormItem>
            <ElFormItem label="Qdrant Port">
              <ElInputNumber v-model="form.vectorstore.qdrant.port" :min="1" :max="65535" />
            </ElFormItem>
            <ElFormItem label="API Key (optional)">
              <ElInput
                v-model="form.vectorstore.qdrant.api_key"
                type="password"
                show-password
                placeholder="Leave blank if not using auth"
              />
            </ElFormItem>
            <ElFormItem label="Use HTTPS">
              <ElSwitch v-model="form.vectorstore.qdrant.https" />
            </ElFormItem>
          </template>
        </template>

        <template v-if="showChroma">
          <ElFormItem v-if="chromaCapability" label="ChromaDB Deployment">
            <DeploymentModeRadio
              v-model="form.vectorstore.chroma.deployment_mode"
              :service="chromaCapability"
              :docker-available="dockerAvailable"
              :catalog="installCatalog ?? null"
            />
          </ElFormItem>

          <template v-if="chromaMode === 'docker'">
            <div class="info-box">
              Cremind will start a <code>chromadb/chroma</code> container alongside
              itself and connect to it on <code>chroma:8000</code>.
            </div>
          </template>
          <template v-else-if="chromaMode === 'native'">
            <ElFormItem label="Persist Path">
              <ElInput
                v-model="form.vectorstore.chroma.persist_path"
                placeholder="Leave blank for <working_dir>/storage/chroma"
              />
              <div class="field-hint">
                Local directory where Chroma will store its database files.
                Cremind runs the <code>chromadb</code> Python library in-process — no separate service.
              </div>
            </ElFormItem>
          </template>
          <template v-else>
            <ElFormItem label="Chroma Host">
              <ElInput v-model="form.vectorstore.chroma.host" placeholder="localhost" />
            </ElFormItem>
            <ElFormItem label="Chroma Port">
              <ElInputNumber v-model="form.vectorstore.chroma.port" :min="1" :max="65535" />
            </ElFormItem>
            <ElFormItem label="Use SSL">
              <ElSwitch v-model="form.vectorstore.chroma.ssl" />
            </ElFormItem>
            <ElFormItem label="API Key (optional)">
              <ElInput
                v-model="form.vectorstore.chroma.api_key"
                type="password"
                show-password
                placeholder="Leave blank if not using auth"
              />
            </ElFormItem>
          </template>
        </template>
      </template>
    </ElForm>
  </div>
</template>

<style scoped>
.config-form {
  max-width: 540px;
}

.section-divider {
  border-top: 1px solid var(--border-color);
  margin: 18px 0 14px;
}

.section-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0 0 10px 0;
}

.field-hint {
  margin-top: 4px;
  font-size: 0.775rem;
  color: var(--text-secondary);
  line-height: 1.4;
}

.field-hint code {
  background: var(--surface-color);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.78rem;
}

.info-box {
  margin: 8px 0 16px 0;
  padding: 12px 16px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.825rem;
  color: var(--text-secondary);
  line-height: 1.5;
}

.info-box code {
  background: var(--surface-color);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 0.8rem;
}
</style>
