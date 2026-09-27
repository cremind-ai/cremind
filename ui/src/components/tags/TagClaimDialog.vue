<script setup lang="ts">
/**
 * Admin: give a tag to a profile (POST …/tags/{id}/claim). Claiming bumps the
 * tag's epoch, cancels whatever the previous owner still had on its way, and
 * queues `assign_tag` + `clear_tag`: the screen is blanked before the new
 * owner's first card. The bridge may be left to the server when the tag
 * already has one or the companion has only one; otherwise it answers 409
 * `bridge_required`, shown on the bridge field.
 *
 * Bridges whose known capacity is used up are listed but disabled; if the
 * server still answers 409 `bridge_full` (the automatic bridge is full, or a
 * slot was taken meanwhile) it shows on the bridge field too.
 *
 * A claim starts the tag clean (name, previews, revisions): the name field is
 * prefilled only when re-claiming for the SAME owner (e.g. to retry a failed
 * clear) and is then sent explicitly; a new owner never inherits the old name.
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElDialog, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus';
import { useTagsStore } from '../../stores/tags';
import { TagsApiError, type TagDevice } from '../../services/tagsApi';
import { CAPACITY_NOTE, bridgeCapacity, deviceTitle } from '../../utils/tagsFormat';

const props = defineProps<{
  modelValue: boolean;
  tag: TagDevice | null;
  bridges: TagDevice[];
  profiles: string[];
}>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'done'): void;
  (e: 'refused'): void;
}>();

const store = useTagsStore();
const owner = ref('');
const bridgeId = ref('');
const name = ref('');
const saving = ref(false);
const fieldError = ref<{ field: 'owner' | 'bridge' | 'name' | 'other'; message: string } | null>(null);

/** The bridge the server picks when none is named: the tag's own, else the only one. */
const autoBridgeRow = computed<TagDevice | null>(() => {
  if (!props.tag) return null;
  const current = props.bridges.find((b) => b.id === props.tag!.bridge_device_id);
  if (current) return current;
  return props.bridges.length === 1 ? props.bridges[0] : null;
});
const autoBridge = computed(() => {
  const b = autoBridgeRow.value;
  if (!b || !props.tag) return '';
  const full = bridgeCapacity(b, props.tag).full ? ' — full' : '';
  return b.id === props.tag.bridge_device_id ? `Keep ${deviceTitle(b)}${full}` : `Automatic (${deviceTitle(b)}${full})`;
});
/** Warn before submitting when the bridge that would be used is known full. */
const chosenFull = computed(() => {
  if (!props.tag) return null;
  const b = bridgeId.value ? props.bridges.find((x) => x.id === bridgeId.value) : autoBridgeRow.value;
  return b && bridgeCapacity(b, props.tag).full ? b : null;
});

const sameOwner = computed(() => !!props.tag?.owner_profile && owner.value === props.tag.owner_profile);

watch(() => props.modelValue, (open) => {
  if (!open || !props.tag) return;
  owner.value = props.tag.owner_profile ?? '';
  bridgeId.value = '';
  name.value = props.tag.owner_profile ? (props.tag.name ?? '') : '';
  fieldError.value = null;
});

// Picking another owner drops the previous owner's name for the tag.
watch(owner, (next, prev) => {
  if (!props.tag || next === prev) return;
  if (next === props.tag.owner_profile) name.value = props.tag.name ?? '';
  else if (name.value === (props.tag.name ?? '')) name.value = '';
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
    if (name.value.trim()) body.name = name.value.trim();
    await store.claim(tag.id, body);
    ElMessage.success(`${deviceTitle(tag)} now belongs to ${owner.value} — its screen is cleared first`);
    emit('done');
    close();
  } catch (e) {
    if (e instanceof TagsApiError && e.code === 'bridge_full') {
      const b = e.body?.bridge as { id?: string; name?: string; max_tags?: number; assigned?: number } | undefined;
      const known = b?.id ? props.bridges.find((x) => x.id === b.id) : undefined;
      const label = b?.name || (known ? deviceTitle(known) : 'That bridge');
      fieldError.value = {
        field: 'bridge',
        message: b && b.max_tags
          ? `${label} is full (${b.assigned ?? b.max_tags} of ${b.max_tags} tags). Pick another bridge, or free a slot by moving or forgetting a tag.`
          : e.message,
      };
      emit('refused'); // the counts the picker shows are stale: refresh them
    } else if (e instanceof TagsApiError && e.code === 'bridge_required') {
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
      <ElSelect
        v-model="bridgeId" clearable :placeholder="autoBridge || 'Pick the bridge'" class="full" aria-label="Bridge"
        @change="fieldError?.field === 'bridge' && (fieldError = null)"
      >
        <ElOption
          v-for="b in bridges" :key="b.id" :label="deviceTitle(b)" :value="b.id"
          :disabled="!!tag && bridgeCapacity(b, tag).full"
        >
          <span>{{ deviceTitle(b) }}</span>
          <span class="opt-meta" :class="{ 'at-capacity': !!tag && bridgeCapacity(b, tag).full }">
            {{ bridgeCapacity(b).label }}{{ tag && bridgeCapacity(b, tag).full ? ' · full' : '' }}
          </span>
        </ElOption>
      </ElSelect>
      <p v-if="fieldError?.field === 'bridge'" class="field-error">{{ fieldError.message }}</p>
      <p v-else-if="chosenFull" class="field-warn">
        {{ deviceTitle(chosenFull) }} is full ({{ bridgeCapacity(chosenFull).label }}) — pick another bridge.
      </p>
      <p v-else class="field-hint" :title="CAPACITY_NOTE">The bridge on the tag's companion that talks to it over the mesh.</p>
    </div>
    <div class="field">
      <label class="field-label">Name <span class="optional">(optional)</span></label>
      <ElInput v-model="name" maxlength="128" :placeholder="tag?.hw_id" />
      <p v-if="!sameOwner" class="field-hint">A new owner starts with a clean tag: no name unless you give one.</p>
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
.field-warn { margin: 6px 0 0; font-size: 0.8rem; color: var(--el-color-warning); }
.opt-meta { float: right; margin-left: 12px; color: var(--text-tertiary); font-size: 0.8rem; }
.opt-meta.at-capacity { color: var(--el-color-warning); }
.note { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
.full { width: 100%; }
</style>
