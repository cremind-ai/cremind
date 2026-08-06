<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElCard, ElInput, ElMessage, ElTable, ElTableColumn, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  cancelDriveGrant, completeDriveGrant, getDriveGrant, getDriveStatus,
  listDriveFiles, startDriveGrant,
  type DriveFile, type DriveStatus,
} from '../services/googleDriveApi';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settings = useSettingsStore();

const loading = ref(true);
const status = ref<DriveStatus | null>(null);
const files = ref<DriveFile[]>([]);
const filesMessage = ref('');
const nextPageToken = ref<string | null>(null);

const granting = ref(false);
const grantState = ref('');
const grantUrl = ref('');
const captureHint = ref('');
const fileRef = ref('');
const pastedRedirect = ref('');

// Polling a grant round: the grant lands with Google on approval, so the server
// discovers it by re-listing reachable files even when the redirect never
// arrives. That is why this polls instead of waiting on a callback.
const POLL_MS = 2500;
const MAX_POLLS = 120;
let pollTimer: number | undefined;
let popup: Window | null = null;

const linked = computed(() => status.value?.linked === true);
const stale = computed(() => status.value?.scopes_stale === true);

function goBack() {
  router.push(`/${props.profile}/settings`);
}

function shortType(mime: string): string {
  if (!mime) return '';
  if (mime === 'application/vnd.google-apps.folder') return 'Folder';
  if (mime === 'application/vnd.google-apps.document') return 'Google Doc';
  if (mime === 'application/vnd.google-apps.spreadsheet') return 'Google Sheet';
  if (mime === 'application/vnd.google-apps.presentation') return 'Google Slides';
  return mime.split('/').pop() || mime;
}

async function loadStatus() {
  if (!settings.agentUrl || !settings.authToken) return;
  try {
    status.value = await getDriveStatus(settings.agentUrl, settings.authToken);
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

async function loadFiles(pageToken?: string) {
  if (!settings.agentUrl || !settings.authToken) return;
  const page = await listDriveFiles(settings.agentUrl, settings.authToken, pageToken);
  filesMessage.value = page.message || '';
  files.value = pageToken ? [...files.value, ...page.files] : page.files;
  nextPageToken.value = page.next_page_token ?? null;
}

async function refresh() {
  loading.value = true;
  try {
    await loadStatus();
    if (linked.value) await loadFiles();
  } finally {
    loading.value = false;
  }
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

function finishGrant(count: number) {
  stopPolling();
  granting.value = false;
  grantUrl.value = '';
  captureHint.value = '';
  pastedRedirect.value = '';
  fileRef.value = '';
  if (popup && !popup.closed) popup.close();
  popup = null;
  if (count > 0) {
    ElMessage.success(`Granted access to ${count} file${count === 1 ? '' : 's'}.`);
  }
  void loadFiles();
}

async function onGrant() {
  if (!settings.agentUrl || !settings.authToken) return;
  granting.value = true;
  const refs = fileRef.value.trim() ? [fileRef.value.trim()] : undefined;
  const started = await startDriveGrant(settings.agentUrl, settings.authToken, { fileIds: refs });
  if (started.error || !started.authorize_url || !started.state) {
    granting.value = false;
    ElMessage.warning(started.message || 'Could not start a Drive grant.');
    return;
  }
  grantState.value = started.state;
  grantUrl.value = started.authorize_url;
  captureHint.value = started.capture_hint || '';
  popup = window.open(started.authorize_url, 'cremind-google-drive', 'width=620,height=700');

  let polls = 0;
  pollTimer = window.setInterval(async () => {
    polls += 1;
    if (polls > MAX_POLLS) {
      stopPolling();
      granting.value = false;
      ElMessage.warning('Timed out waiting for the file picker. Try again, or paste the redirect URL.');
      return;
    }
    try {
      const out = await getDriveGrant(settings.agentUrl, settings.authToken, grantState.value);
      if (out.status === 'error') {
        stopPolling();
        granting.value = false;
        ElMessage.warning(out.error || 'The Google consent was denied.');
        return;
      }
      if (out.status === 'completed' && out.files.length) finishGrant(out.files.length);
    } catch {
      // A transient poll failure is not fatal; the next tick retries.
    }
  }, POLL_MS);
}

async function onPasteRedirect() {
  if (!pastedRedirect.value.trim() || !settings.agentUrl || !settings.authToken) return;
  try {
    const out = await completeDriveGrant(
      settings.agentUrl, settings.authToken, pastedRedirect.value.trim(),
    );
    if (!out.files.length) {
      ElMessage.warning(out.note || 'No files were granted from that URL.');
      return;
    }
    finishGrant(out.files.length);
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

function onCancelGrant() {
  if (settings.agentUrl && settings.authToken && grantState.value) {
    void cancelDriveGrant(settings.agentUrl, settings.authToken, grantState.value);
  }
  finishGrant(0);
}

onMounted(() => { void refresh(); });
onUnmounted(() => { stopPolling(); });
</script>

<template>
  <div class="drive-page">
    <div class="drive-container">
      <div class="drive-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="drive-title">Google Drive Access</h1>
        <p class="drive-subtitle">
          Cremind has <strong>per-file</strong> Drive access: it can only open files you
          pick here, plus files it creates itself. Pasting a Drive link is not enough —
          the file has to be granted. Spreadsheets and documents are different: the
          gsheets and gdocs skills read and write those straight from a URL, with no
          grant needed.
        </p>
      </div>

      <ElCard class="section" shadow="never">
        <div v-if="loading" class="muted">Loading…</div>
        <template v-else-if="!linked">
          <div class="row">
            <Icon icon="mdi:link-off" class="row-icon" />
            <div>
              <strong>Not linked</strong>
              <p class="muted">
                Ask the agent to link the <code>gdrive</code> skill to your Google
                account, then come back to grant files.
              </p>
            </div>
          </div>
        </template>
        <template v-else>
          <div class="row">
            <Icon icon="mdi:google-drive" class="row-icon" />
            <div class="grow">
              <strong>{{ status?.email || 'Linked' }}</strong>
              <p class="muted">
                {{ status?.whole_drive
                  ? 'Whole-Drive access — every file is reachable, so no grants are needed.'
                  : 'Per-file access — granted files plus files Cremind created.' }}
              </p>
            </div>
            <ElButton :loading="granting" type="primary" @click="onGrant">
              Grant access
            </ElButton>
          </div>

          <div v-if="stale" class="banner warn">
            <Icon icon="mdi:alert" />
            <span>{{ status?.hint }}</span>
          </div>

          <div class="grant-row">
            <ElInput
              v-model="fileRef"
              placeholder="Optional: paste a Drive link to pre-select that file"
              clearable
            />
          </div>

          <div v-if="granting" class="banner">
            <div class="grow">
              <p>
                Waiting for you to pick files in the Google window. If it didn't open,
                <a :href="grantUrl" target="_blank" rel="noopener">open it here</a>.
              </p>
              <p v-if="captureHint" class="muted">{{ captureHint }}</p>
              <div class="paste-row">
                <ElInput
                  v-model="pastedRedirect"
                  placeholder="…or paste the URL your browser landed on"
                  clearable
                  @keyup.enter="onPasteRedirect"
                />
                <ElButton :disabled="!pastedRedirect.trim()" @click="onPasteRedirect">
                  Use URL
                </ElButton>
              </div>
            </div>
            <ElButton text @click="onCancelGrant">Cancel</ElButton>
          </div>
        </template>
      </ElCard>

      <ElCard v-if="linked && !loading" class="section" shadow="never">
        <template #header>
          <div class="card-head">
            <span>Files Cremind can open</span>
            <ElButton text size="small" @click="() => loadFiles()">
              <Icon icon="mdi:refresh" /> Refresh
            </ElButton>
          </div>
        </template>

        <p v-if="filesMessage" class="muted">{{ filesMessage }}</p>
        <p v-else-if="!files.length" class="muted">
          Nothing granted yet. Use <strong>Grant access</strong> above to pick files.
        </p>
        <ElTable v-else :data="files" size="small" style="width: 100%">
          <ElTableColumn prop="name" label="Name" min-width="220" />
          <ElTableColumn label="Type" width="130">
            <template #default="{ row }">{{ shortType(row.mime_type) }}</template>
          </ElTableColumn>
          <ElTableColumn label="Granted via" width="120">
            <template #default="{ row }">
              <ElTag v-if="row.origin === 'picker'" size="small" type="success">Picked</ElTag>
              <ElTag v-else size="small" type="info">Created</ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="" width="60">
            <template #default="{ row }">
              <a v-if="row.web_view_link" :href="row.web_view_link" target="_blank" rel="noopener">
                <Icon icon="mdi:open-in-new" />
              </a>
            </template>
          </ElTableColumn>
        </ElTable>
        <div v-if="nextPageToken" class="more-row">
          <ElButton text size="small" @click="() => loadFiles(nextPageToken!)">Load more</ElButton>
        </div>
      </ElCard>

      <ElCard v-if="linked && !loading" class="section" shadow="never">
        <template #header><span>Revoking access</span></template>
        <p class="muted">
          Google offers no per-file revoke. Removing Cremind at
          <a :href="status?.revoke_url" target="_blank" rel="noopener">your Google
          account connections</a> revokes <strong>every</strong> file grant at once,
          and unlinks the account.
        </p>
      </ElCard>
    </div>
  </div>
</template>

<style scoped>
.drive-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.drive-container { max-width: 860px; margin: 0 auto; }
.drive-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none; border: none;
  color: var(--text-secondary); cursor: pointer; font-size: 0.875rem;
  padding: 4px 0; margin-bottom: 16px;
}
.back-btn:hover { color: var(--primary-color); }
.drive-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px; }
.drive-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; max-width: 640px; }
.section { margin-bottom: 16px; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.row { display: flex; align-items: center; gap: 12px; }
.row-icon { font-size: 1.5rem; color: var(--text-secondary); flex: none; }
.grow { flex: 1; min-width: 0; }
.muted { color: var(--text-secondary); font-size: 0.875rem; margin: 4px 0 0; }
.banner {
  display: flex; align-items: flex-start; gap: 10px; margin-top: 14px;
  padding: 10px 12px; border-radius: 6px; background: var(--bg-color-page, rgba(0, 0, 0, 0.03));
  font-size: 0.875rem;
}
.banner.warn { color: var(--el-color-warning); }
.banner p { margin: 0 0 6px; }
.grant-row { margin-top: 14px; }
.paste-row { display: flex; gap: 8px; margin-top: 8px; }
.more-row { margin-top: 10px; text-align: center; }
</style>
