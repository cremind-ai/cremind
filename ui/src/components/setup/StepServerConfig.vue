<script setup lang="ts">
import { ref, watch, computed, onMounted } from 'vue';
import { ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioGroup, ElRadio, ElSelect, ElOption } from 'element-plus';
import DeploymentModeRadio from './DeploymentModeRadio.vue';
import type { DeploymentMode, ServiceCapabilitiesResponse } from '../../services/configApi';
import type { InstallCatalog } from '../../services/installCatalogApi';

const props = defineProps<{
  config: Record<string, any>;
  // True only during the very first setup wizard pass for the admin. The DB
  // provider choice is exposed only when this is true; on every subsequent
  // wizard run (e.g. after reconfigure, or for any non-admin profile) the
  // backend silently rejects changes anyway, but we hide the UI as well so
  // it doesn't look configurable.
  firstSetup?: boolean;
  // Per-service deployment-mode descriptor. Null while still loading —
  // we fall back to the External-only form (today's behaviour) until
  // it arrives. The Postgres entry drives the Deployment radio below.
  serviceCapabilities?: ServiceCapabilitiesResponse | null;
  // Install catalog (deployment / mode labels). Forwarded to
  // DeploymentModeRadio so service-mode labels stay in sync with the
  // install scripts.
  installCatalog?: InstallCatalog | null;
}>();

const emit = defineEmits<{
  update: [config: Record<string, any>];
}>();

const isFirstSetup = computed(() => props.firstSetup ?? true);
const postgresCapability = computed(() => props.serviceCapabilities?.services?.postgres ?? null);
const dockerAvailable = computed(() => props.serviceCapabilities?.docker_available ?? false);
// Kubernetes deployments scale horizontally, so a pod-local SQLite file would
// break data consistency. The backend rejects SQLite in this mode (Seam A);
// here we hide the SQLite option and force PostgreSQL so the user never hits
// that error. INSTALL_MODE=kubernetes is reported via service capabilities.
const isKubernetes = computed(() => props.serviceCapabilities?.install_mode === 'kubernetes');

function pickInitialDeploymentMode(saved: string | undefined): DeploymentMode {
  const cap = postgresCapability.value;
  if (!cap) return (saved as DeploymentMode) || 'external';
  // ``effective`` accounts for both layers of filtering:
  //   - backend already trimmed External when this install is Docker;
  //   - locally drop Docker when the host can't drive a daemon (native
  //     install). Without the local drop the saved value could be
  //     ``docker`` from a prior Docker install whose .env we no longer
  //     control.
  const effective = cap.supported_modes.filter((m) =>
    m === 'docker' ? dockerAvailable.value : true,
  );
  if (saved && effective.includes(saved as DeploymentMode)) {
    return saved as DeploymentMode;
  }
  // Prefer External in native installs (form mounts with editable
  // host/port fields, which is what the user expects); Docker
  // otherwise (typical Docker install — no fields to fill).
  if (effective.includes('external')) return 'external';
  if (effective.includes('docker')) return 'docker';
  return effective[0] ?? 'external';
}

const form = ref({
  service_name: props.config.service_name || 'cremind-agent',
  agent_name: props.config.agent_name || 'Cremind Agent',
  // CREMIND_WORKING_DIR — the user's interaction folder. Default for
  // every built-in tool's ``_working_directory`` and root of the
  // Tree Dir file panel. Distinct from the System Directory below.
  user_working_dir: props.config.user_working_dir || '~/Documents',
  // CREMIND_SYSTEM_DIR — Cremind's internal storage. Editable on first
  // setup (the backend relocates before bootstrap.toml is written);
  // read-only thereafter (requires env var + restart to change).
  system_dir: props.config.system_dir || '~/.cremind',
  sqlite_db_path: props.config.sqlite_db_path || 'cremind.db',
  db_provider: ((props.config.db_provider as 'sqlite' | 'postgres') ?? 'sqlite') as 'sqlite' | 'postgres',
  postgres: {
    deployment_mode: pickInitialDeploymentMode(props.config.postgres?.deployment_mode),
    host: props.config.postgres?.host || 'localhost',
    port: props.config.postgres?.port ?? 5432,
    database: props.config.postgres?.database || 'cremind',
    user: props.config.postgres?.user || 'cremind',
    password: props.config.postgres?.password || '',
    sslmode: props.config.postgres?.sslmode || 'prefer',
  },
});

// When capabilities arrive *after* the form has mounted, recompute the
// initial deployment_mode so an unsupported saved value gets clamped.
// We watch the docker_available flag too because the local Docker
// filter depends on it — flipping that flag should re-pick the mode.
watch([postgresCapability, dockerAvailable], () => {
  const cap = postgresCapability.value;
  if (!cap) return;
  const effective = cap.supported_modes.filter((m) =>
    m === 'docker' ? dockerAvailable.value : true,
  );
  if (!effective.includes(form.value.postgres.deployment_mode)) {
    form.value.postgres.deployment_mode = pickInitialDeploymentMode(form.value.postgres.deployment_mode);
  }
});

// Force PostgreSQL on Kubernetes. ``serviceCapabilities`` (which carries
// ``install_mode``) can arrive after the form mounts with its ``sqlite``
// default, so watch immediately and clamp the moment the mode is known.
watch(isKubernetes, (k) => {
  if (k && form.value.db_provider !== 'postgres') {
    form.value.db_provider = 'postgres';
  }
}, { immediate: true });

const postgresIsDocker = computed(() => form.value.postgres.deployment_mode === 'docker');

// Emit only the keys relevant to the chosen backend so the wizard payload
// stays clean (and the server-side filter doesn't have to second-guess us).
function buildPayload() {
  const base: Record<string, any> = {
    service_name: form.value.service_name,
    agent_name: form.value.agent_name,
    user_working_dir: form.value.user_working_dir,
  };
  // ``system_dir`` is applied by the backend only on first setup
  // (relocation runs before bootstrap.toml is written). On reconfigure
  // runs the field is disabled and we omit it from the payload.
  if (isFirstSetup.value) {
    base.system_dir = form.value.system_dir;
  }
  if (!isFirstSetup.value || form.value.db_provider === 'sqlite') {
    base.sqlite_db_path = form.value.sqlite_db_path;
  }
  if (isFirstSetup.value) {
    base.db_provider = form.value.db_provider;
    if (form.value.db_provider === 'postgres') {
      // For Docker mode the backend generates credentials and uses
      // ``postgres:5432`` as the connection host; the host/port/user/
      // password fields are ignored. For External mode the user-typed
      // values are required.
      base.postgres = {
        deployment_mode: form.value.postgres.deployment_mode,
        ...(form.value.postgres.deployment_mode === 'external'
          ? {
              host: form.value.postgres.host,
              port: form.value.postgres.port,
              database: form.value.postgres.database,
              user: form.value.postgres.user,
              password: form.value.postgres.password,
              sslmode: form.value.postgres.sslmode,
            }
          : {}),
      };
    }
  }
  return base;
}

watch(form, () => {
  emit('update', buildPayload());
}, { deep: true });

onMounted(() => {
  emit('update', buildPayload());
});
</script>

<template>
  <div class="step-server-config">
    <h3 class="step-title">Server Configuration</h3>
    <p class="step-description">
      Configure your Cremind server identity. Host and port are managed via the <code>.env</code> file on the server.
    </p>

    <ElForm label-position="top" class="config-form">
      <ElFormItem label="Service Name">
        <ElInput v-model="form.service_name" placeholder="cremind-agent" />
      </ElFormItem>
      <ElFormItem label="Agent Display Name">
        <ElInput v-model="form.agent_name" placeholder="Cremind Agent" />
      </ElFormItem>
      <ElFormItem label="User Working Directory">
        <ElInput v-model="form.user_working_dir" placeholder="~/Documents" />
        <div class="field-hint">
          Your working folder. The agent reads, writes, and lists files
          here by default; this is also the root of the Tree Dir panel.
          Created on first run if missing.
        </div>
      </ElFormItem>
      <ElFormItem label="Cremind System Working Directory">
        <ElInput
          v-model="form.system_dir"
          placeholder="~/.cremind"
          :disabled="!isFirstSetup"
        />
        <div class="field-hint">
          Cremind's internal storage — <code>bootstrap.toml</code>,
          <code>credentials.toml</code>, the SQLite database, tokens,
          and per-profile state. Lives separately from your working
          folder.
          <template v-if="!isFirstSetup">
            <br />
            Set at install time. To change, set
            <code>CREMIND_SYSTEM_DIR</code> in
            <code>{{ form.system_dir }}/.env</code> and restart Cremind.
          </template>
        </div>
      </ElFormItem>

      <template v-if="isFirstSetup">
        <ElFormItem label="Database Provider">
          <template v-if="isKubernetes">
            <div class="single-mode">
              <strong>PostgreSQL</strong> — required on Kubernetes. SQLite is
              disabled because pods scale horizontally and pod-local storage
              breaks data consistency across replicas.
            </div>
          </template>
          <template v-else>
            <ElRadioGroup v-model="form.db_provider">
              <ElRadio value="sqlite">SQLite (default)</ElRadio>
              <ElRadio value="postgres">PostgreSQL</ElRadio>
            </ElRadioGroup>
            <div class="field-hint">
              Chosen once during first setup and locked thereafter — adding or migrating
              data to a different backend later requires manual export/import.
              <span v-if="form.db_provider === 'sqlite'">
                SQLite is local-only; no deployment options apply.
              </span>
            </div>
          </template>
        </ElFormItem>
      </template>

      <template v-if="form.db_provider === 'sqlite'">
        <ElFormItem label="Database Name">
          <ElInput v-model="form.sqlite_db_path" placeholder="cremind.db" />
          <div class="field-hint">SQLite database filename, stored inside the Cremind system directory.</div>
        </ElFormItem>
      </template>

      <template v-else>
        <ElFormItem v-if="postgresCapability" label="PostgreSQL Deployment">
          <DeploymentModeRadio
            v-model="form.postgres.deployment_mode"
            :service="postgresCapability"
            :docker-available="dockerAvailable"
            :catalog="installCatalog ?? null"
          />
        </ElFormItem>

        <template v-if="!postgresIsDocker">
          <ElFormItem label="Postgres Host">
            <ElInput v-model="form.postgres.host" placeholder="localhost" />
          </ElFormItem>
          <ElFormItem label="Postgres Port">
            <ElInputNumber v-model="form.postgres.port" :min="1" :max="65535" />
          </ElFormItem>
          <ElFormItem label="Database Name">
            <ElInput v-model="form.postgres.database" placeholder="cremind" />
          </ElFormItem>
          <ElFormItem label="Username">
            <ElInput v-model="form.postgres.user" placeholder="cremind" />
          </ElFormItem>
          <ElFormItem label="Password">
            <ElInput v-model="form.postgres.password" type="password" show-password />
            <div class="field-hint">
              Stored in <code>~/.cremind/bootstrap.toml</code> in plaintext —
              same trust model as <code>~/.pgpass</code>.
            </div>
          </ElFormItem>
          <ElFormItem label="SSL Mode">
            <ElSelect v-model="form.postgres.sslmode">
              <ElOption label="disable" value="disable" />
              <ElOption label="allow" value="allow" />
              <ElOption label="prefer" value="prefer" />
              <ElOption label="require" value="require" />
              <ElOption label="verify-ca" value="verify-ca" />
              <ElOption label="verify-full" value="verify-full" />
            </ElSelect>
          </ElFormItem>
        </template>
        <template v-else>
          <div class="info-box">
            Cremind will start a <code>postgres:16</code> container alongside itself.
            The database, user, and password are generated automatically and
            stored in <code>~/.cremind/bootstrap.toml</code>.
          </div>
        </template>
      </template>
    </ElForm>

    <div class="info-box">
      <strong>Note:</strong> Server host, port, and URL are configured in the <code>.env</code> file on the server machine.
      These cannot be changed from the UI as they require a server restart.
    </div>
  </div>
</template>

<style scoped>
.step-server-config {
  padding: 8px 0;
}

.step-title {
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0 0 8px 0;
}

.step-description {
  color: var(--text-secondary);
  font-size: 0.875rem;
  margin: 0 0 24px 0;
  line-height: 1.5;
}

.config-form {
  max-width: 480px;
}

.field-hint {
  margin-top: 4px;
  font-size: 0.775rem;
  color: var(--text-secondary);
  line-height: 1.4;
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

.info-box {
  margin-top: 24px;
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
