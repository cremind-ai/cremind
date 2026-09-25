<script setup lang="ts">
/**
 * Who "me" is, for questions like "the doc I wrote" or "photos I took": the
 * names and emails a document's author field may carry, and the cameras or
 * phones (EXIF make/model) the user shoots with. Search treats a match as a
 * boost, never a filter — a file without an author or EXIF is not excluded.
 *
 * Each list is edited as comma-separated text and saved together.
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElInput } from 'element-plus';
import type { UserDocsOptions } from '../../services/userdocsApi';

type Identity = UserDocsOptions['identity'];

const props = withDefaults(defineProps<{
  identity: Identity;
  saving?: boolean;
}>(), { saving: false });

const emit = defineEmits<{ save: [identity: Identity] }>();

function join(list: string[]): string {
  return list.join(', ');
}

function split(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of text.split(/[,\n;]/)) {
    const v = part.trim();
    if (v && !seen.has(v.toLowerCase())) {
      seen.add(v.toLowerCase());
      out.push(v);
    }
  }
  return out;
}

const names = ref(join(props.identity.author_names));
const emails = ref(join(props.identity.emails));
const cameras = ref(join(props.identity.camera_devices));

const edited = computed<Identity>(() => ({
  author_names: split(names.value),
  emails: split(emails.value),
  camera_devices: split(cameras.value),
}));

const dirty = computed(() => JSON.stringify(edited.value) !== JSON.stringify({
  author_names: props.identity.author_names,
  emails: props.identity.emails,
  camera_devices: props.identity.camera_devices,
}));

function reset() {
  names.value = join(props.identity.author_names);
  emails.value = join(props.identity.emails);
  cameras.value = join(props.identity.camera_devices);
}

watch(() => props.identity, () => { if (!dirty.value) reset(); }, { deep: true });

function save() {
  if (dirty.value) emit('save', edited.value);
}
</script>

<template>
  <div class="ident">
    <label class="ident-field">
      <span>Names you write as</span>
      <ElInput v-model="names" size="small" placeholder="Ann Nguyen, Nguyễn Thị An" :disabled="saving" />
    </label>
    <label class="ident-field">
      <span>Your email addresses</span>
      <ElInput v-model="emails" size="small" placeholder="ann@example.com" :disabled="saving" />
    </label>
    <label class="ident-field">
      <span>Your cameras and phones</span>
      <ElInput v-model="cameras" size="small" placeholder="iPhone 14, Canon EOS R6" :disabled="saving" />
    </label>
    <p class="hint">
      Separate entries with commas. Used for "documents I wrote" (the author recorded in Word, PDF and
      other files) and "photos I took" (the camera recorded in the photo). A match ranks a file higher;
      files without this information are still found.
    </p>
    <div v-if="dirty" class="ident-actions">
      <ElButton size="small" type="primary" :loading="saving" @click="save">Save</ElButton>
      <ElButton size="small" :disabled="saving" @click="reset">Cancel</ElButton>
    </div>
  </div>
</template>

<style scoped>
.ident { display: flex; flex-direction: column; gap: 10px; }
.ident-field { display: flex; flex-direction: column; gap: 4px; font-size: 0.8rem; color: var(--text-secondary); }
.hint { margin: 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
.ident-actions { display: flex; gap: 8px; }
.ident-actions :deep(.el-button + .el-button) { margin-left: 0; }
</style>
