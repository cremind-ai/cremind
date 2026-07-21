<script setup lang="ts">
/**
 * Timezone picker for the `system.timezone` config field.
 *
 * The user chooses ONE of two mutually-exclusive formats via a toggle:
 *   - IANA name (e.g. Asia/Tokyo), or
 *   - UTC offset (e.g. +07:00), chosen from a whole-hour offsets dropdown.
 *
 * The "auto" sentinel (inherit / server default) is offered in BOTH modes and
 * is format-agnostic — selecting it never changes the toggle.
 *
 * The stored value is a single string (an IANA name, an offset, or "auto");
 * the active mode is inferred from the value's shape, so this component emits
 * exactly one string via `update:modelValue` — the parent config plumbing is
 * unchanged.
 */
import { computed, ref, watch } from 'vue';
import { ElRadioGroup, ElRadioButton, ElSelect, ElOption } from 'element-plus';

const props = defineProps<{
  /** Current value: an IANA name, a "+HH:MM" offset, or "auto". */
  modelValue: string;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void;
}>();

type Mode = 'iana' | 'offset';

/** Canonical stored offset shape, e.g. "+07:00" / "-05:00". */
const OFFSET_RE = /^[+-]\d{2}:\d{2}$/;
function isOffset(v: string | null | undefined): boolean {
  return OFFSET_RE.test((v ?? '').trim());
}

const AUTO = 'auto';
/** Display label for an option value (the "auto" sentinel gets a friendly name). */
function optionLabel(v: string): string {
  return v === AUTO ? 'Auto (inherit / server default)' : v;
}

/** IANA zones from the browser, with the "auto" inherit sentinel first. */
const ianaOptions = computed<string[]>(() => {
  let zones: string[] = [];
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf;
    if (typeof supported === 'function') zones = supported('timeZone');
  } catch {
    zones = [];
  }
  return [AUTO, ...zones];
});

function formatOffset(totalMin: number): string {
  const sign = totalMin >= 0 ? '+' : '-';
  const abs = Math.abs(totalMin);
  const hh = String(Math.floor(abs / 60)).padStart(2, '0');
  const mm = String(abs % 60).padStart(2, '0');
  return `${sign}${hh}:${mm}`;
}

/** The "auto" sentinel plus whole-hour offsets -12:00 → +14:00. */
const offsetOptions = computed<string[]>(() => {
  const out: string[] = [AUTO];
  for (let h = -12; h <= 14; h += 1) out.push(formatOffset(h * 60));
  return out;
});

/** The viewer's browser offset, rounded to a whole hour (a sensible default). */
function browserOffset(): string {
  const hours = Math.round(-new Date().getTimezoneOffset() / 60);
  const clamped = Math.max(-12, Math.min(14, hours));
  return formatOffset(clamped * 60);
}

const mode = ref<Mode>(isOffset(props.modelValue) ? 'offset' : 'iana');
// Keep the toggle in sync when the value changes externally (e.g. Reset). A
// concrete value pins its mode; "auto" (or empty) is format-agnostic, so leave
// the user's chosen mode untouched.
watch(
  () => props.modelValue,
  (v) => {
    if (isOffset(v)) mode.value = 'offset';
    else if (v && v !== AUTO) mode.value = 'iana';
  },
);

// Values bound to each select; guarded so a select never shows a value that
// doesn't belong to its mode. "auto" is valid in either mode.
const ianaValue = computed(() =>
  isOffset(props.modelValue) ? AUTO : (props.modelValue || AUTO),
);
const offsetValue = computed(() => {
  if (isOffset(props.modelValue)) return props.modelValue;
  if (props.modelValue === AUTO || !props.modelValue) return AUTO;
  return browserOffset();
});

function onModeChange(next: string | number | boolean | undefined) {
  const m = next as Mode;
  // Emit a value that fits the newly selected mode so the stored string always
  // matches the active format. "auto" fits both modes, so it is left as-is.
  const v = props.modelValue;
  if (m === 'offset' && !isOffset(v) && v !== AUTO) {
    emit('update:modelValue', browserOffset());
  } else if (m === 'iana' && isOffset(v)) {
    emit('update:modelValue', AUTO);
  }
}

function onIana(v: string) {
  emit('update:modelValue', v);
}
function onOffset(v: string) {
  emit('update:modelValue', v);
}
</script>

<template>
  <div class="tz-field">
    <ElRadioGroup v-model="mode" size="small" @change="onModeChange">
      <ElRadioButton value="iana">IANA name</ElRadioButton>
      <ElRadioButton value="offset">UTC offset</ElRadioButton>
    </ElRadioGroup>

    <ElSelect
      v-if="mode === 'iana'"
      :model-value="ianaValue"
      filterable
      size="small"
      class="tz-select"
      @update:model-value="onIana"
    >
      <ElOption
        v-for="tz in ianaOptions"
        :key="tz"
        :label="optionLabel(tz)"
        :value="tz"
      />
    </ElSelect>
    <ElSelect
      v-else
      :model-value="offsetValue"
      filterable
      size="small"
      class="tz-select"
      @update:model-value="onOffset"
    >
      <ElOption v-for="off in offsetOptions" :key="off" :label="optionLabel(off)" :value="off" />
    </ElSelect>
  </div>
</template>

<style scoped>
.tz-field {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}
.tz-select {
  width: 220px;
}
</style>
