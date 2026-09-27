<script setup lang="ts">
/**
 * Admin: give a tag to a profile (POST …/tags/{id}/claim). Claiming bumps the
 * tag's epoch, cancels whatever the previous owner still had on its way, and
 * queues `assign_tag` + `clear_tag`: the screen is blanked before the new
 * owner's first card. The bridge may be left to the server when the tag
 * already has one or the companion has only one; otherwise it answers 409
 * `bridge_required`, shown on the bridge field.
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElDialog, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus';
import { useTagsStore } from '../../stores/tags';
import { TagsApiError, type TagDevice } from '../../services/tagsApi';
import { deviceTitle } from '../../utils/tagsFormat';

const props = defineProps<{
  modelValue: boolean;
  tag: TagDevice | null;
  bridges: TagDevice[];
  profiles: string[];
}>();
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void; (e: 'done'): void }>();

const store = useTagsStore();
const owner = ref('');
const bridgeId = ref('');
const name = ref('');
const saving = ref(false);
const fieldError = ref<{ field: 'owner' | 'bridge' | 'name' | 'other'; message: string } | null>(null);

const autoBridge = computed(() => {
  if (!props.tag) return '';
  const current = props.bridges.find((b) => b.id === props.tag!.bridge_device_id);
  if (current) return `Keep ${deviceTitle(current)}`;
  if (props.bridges.length === 1) return `Automatic (${deviceTitle(props.bridges[0])})`;
  return '';
});

watch(() => props.modelValue, (open) => {
  if (!open || !props.tag) return;
  owner.value = props.tag.owner_profile ?? '';
  bridgeId.value = '';
  name.value = props.tag.name ?? '';
  fieldError.value = null;
});

function close() { emit('update:modelValue', false); }

async function submit() {
  const tag = props.tag;
  if (!tag || !owner.value) return;
  saving.value = true;
  fieldError.value = null;
  try {
    const body: { owner: string; bridge_id?: string; name?: string } = { owner: owner.value };
    if (bridgeId.value) body.bridge_id = bridgeId.value;
    if (name.value.trim() && name.value.trim() !== (tag.name ?? '')) body.name = name.value.trim();
    await store.claim(tag.id, body);
    ElMessage.success(`${deviceTitle(tag)} now belongs to ${owner.value} — its screen is cleared first`);
    emit('done');
    close();
  } catch (e) {
    if (e instanceof TagsApiError && e.code === 'bridge_required') {
      fieldError.value = {
        field: 'bridge',
        message: `This companion has ${props.bridges.length} bridges — pick the one the tag should use.`,
      };
    } else if (e instanceof TagsApiError && e.code === 'bridge_not_found') {
      fieldError.value = { field: 'bridge', message: e.message };
    } else if (e instanceof TagsApiError && (e.code === 'unknown_profile' || e.code === 'invalid_owner')) {
      fieldError.value = { field: 'owner', message: e.message };
    } else if (e instanceof TagsApiError && e.code === 'invalid_name') {
      fieldError.value = { field: 'name', message: e.message };
    } else {
      fieldError.value = { field: 'other', message: e instanceof Error ? e.message : 'Failed to claim' };
    }
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <ElDialog
    :model-value="modelValue"
    :title="tag ? `${tag.owner_profile ? 'Change the owner of' : 'Claim'} ${deviceTitle(tag)}` : 'Claim tag'"
    width="480px"
    append-to-body
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <div class="field">
      <label class="field-label">Owner profile</label>
      <ElSelect v-model="owner" filterable placeholder="Pick a profile" class="full" aria-label="Owner profile">
        <ElOption v-for="p in profiles" :key="p" :label="p" :value="p" />
      </ElSelect>
      <p v-if="fieldError?.field === 'owner'" class="field-error">{{ fieldError.message }}</p>
    </div>
    <div class="field">
      <label class="field-label">Bridge</label>
      <ElSelect v-model="bridgeId" clearable :placeholder="autoBridge || 'Pick the bridge'" class="full" aria-label="Bridge">
        <ElOption v-for="b in bridges" :key="b.id" :label="deviceTitle(b)" :value="b.id" />
      </ElSelect>
      <p v-if="fieldError?.field === 'bridge'" class="field-error">{{ fieldError.message }}</p>
      <p v-else class="field-hint">The bridge on the tag's companion that talks to it over the mesh.</p>
    </div>
    <div class="field">
      <label class="field-label">Name <span class="optional">(optional)</span></label>
      <ElInput v-model="name" maxlength="128" :placeholder="tag?.hw_id" />
      <p v-if="fieldError?.field === 'name'" class="field-error">{{ fieldError.message }}</p>
    </div>
    <p class="note">
      The tag's screen is cleared before the new owner's first card, and anything still on its
      way to it is cancelled.
    </p>
    <p v-if="fieldError?.field === 'other'" class="field-error">{{ fieldError.message }}</p>
    <template #footer>
      <ElButton @click="close">Cancel</ElButton>
      <ElButton type="primary" :loading="saving" :disabled="!owner" @click="submit">
        {{ tag?.owner_profile ? 'Change owner' : 'Claim' }}
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.field { margin-bottom: 14px; }
.field-label { display: block; margin-bottom: 6px; font-size: 0.82rem; font-weight: 600; color: var(--text-secondary); }
.optional { font-weight: 400; color: var(--text-tertiary); }
.field-hint { margin: 6px 0 0; font-size: 0.78rem; color: var(--text-tertiary); }
.field-error { margin: 6px 0 0; font-size: 0.8rem; color: var(--el-color-danger); }
.note { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
.full { width: 100%; }
</style>
