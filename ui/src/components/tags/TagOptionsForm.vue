<script setup lang="ts">
/**
 * The Tags options form, shared by Settings → Tags (a profile's own
 * overrides over the admin defaults) and the admin's defaults editor (over
 * the built-ins). Every field can inherit: `null` in the draft means "use the
 * layer below", shown as that value, greyed. Unticking "Use default" starts
 * the override from the inherited value.
 *
 * Routing picks, per card kind, which of the profile's tags receive it. A
 * profile may name specific tags (`devices`); the admin's defaults cover
 * every profile, so they only offer All / None (`devices` null).
 */
import { computed } from 'vue';
import {
  ElCheckbox, ElInput, ElInputNumber, ElOption, ElSelect, ElSwitch,
} from 'element-plus';
import type { TagDevice, TagEffectiveOptions, TagRoute } from '../../services/tagsApi';
import {
  deviceTitle, formatCadence, kindHint, kindLabel, routeSummary, type ScalarOptionKey, type TagOptionsDraft,
} from '../../utils/tagsFormat';

const props = defineProps<{
  modelValue: TagOptionsDraft;
  inherited: TagEffectiveOptions;
  kinds: string[];
  layouts: string[];
  /** The profile's own tags, for "specific tags" routes; null hides that choice. */
  devices: TagDevice[] | null;
  /** What the inherited layer is called: "admin default" / "built-in". */
  inheritLabel: string;
  /** Hint for an inherited empty timezone ("your profile's timezone (…)"). */
  ownTimezoneHint?: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ (e: 'update:modelValue', v: TagOptionsDraft): void }>();

type RouteMode = 'inherit' | 'all' | 'none' | 'specific';

function update(patch: Partial<TagOptionsDraft>) {
  emit('update:modelValue', { ...props.modelValue, ...patch });
}

function setRoute(kind: string, route: TagRoute | null) {
  update({ routes: { ...props.modelValue.routes, [kind]: route } });
}

function routeMode(kind: string): RouteMode {
  const r = props.modelValue.routes[kind];
  if (r == null) return 'inherit';
  if (Array.isArray(r)) return 'specific';
  return r;
}

function onRouteMode(kind: string, mode: RouteMode) {
  if (mode === 'inherit') return setRoute(kind, null);
  if (mode === 'all' || mode === 'none') return setRoute(kind, mode);
  // Start "specific" from what the inherited route already names, else every tag.
  const inherited = props.inherited.routes[kind];
  const ids = (props.devices ?? []).map((d) => d.id);
  const start = Array.isArray(inherited) ? inherited.filter((id) => ids.includes(id))
    : inherited === 'none' ? [] : ids;
  setRoute(kind, start);
}

function specificIds(kind: string): string[] {
  const r = props.modelValue.routes[kind];
  return Array.isArray(r) ? r : [];
}

const deviceName = (id: string) => {
  const d = (props.devices ?? []).find((x) => x.id === id);
  return d ? deviceTitle(d) : `unknown tag (${id.slice(0, 8)})`;
};

function inheritedRouteText(kind: string): string {
  return routeSummary(props.inherited.routes[kind], deviceName);
}

// ── scalar fields ──

function isInherited(key: ScalarOptionKey): boolean {
  return props.modelValue[key] === null;
}

function setInherited(key: ScalarOptionKey, inherit: boolean) {
  if (inherit) update({ [key]: null } as Partial<TagOptionsDraft>);
  else update({ [key]: props.inherited[key] } as Partial<TagOptionsDraft>);
}

function valueOf<K extends ScalarOptionKey>(key: K): TagEffectiveOptions[K] {
  const v = props.modelValue[key];
  return (v === null ? props.inherited[key] : v) as TagEffectiveOptions[K];
}
const numValue = (key: 'progress_cadence_s'): number => Number(valueOf(key));
const strValue = (key: 'language' | 'timezone' | 'layout'): string => String(valueOf(key) ?? '');
const boolValue = (key: 'show_excerpts' | 'qr_links'): boolean => valueOf(key) === true;

function setValue(key: ScalarOptionKey, value: unknown) {
  update({ [key]: value } as Partial<TagOptionsDraft>);
}

const tzInheritedText = computed(() => {
  const tz = props.inherited.timezone;
  if (tz) return tz;
  return props.ownTimezoneHint || "the profile's own timezone";
});
/** Short enough for the input's placeholder. */
const tzPlaceholder = computed(() => props.inherited.timezone || "Profile's own");

function inheritedText(key: ScalarOptionKey): string {
  const v = props.inherited[key];
  if (key === 'timezone') return tzInheritedText.value;
  if (key === 'progress_cadence_s') return formatCadence(v as number);
  if (typeof v === 'boolean') return v ? 'on' : 'off';
  return String(v);
}
</script>

<template>
  <div class="options-form" :class="{ disabled }">
    <section class="form-section">
      <h3 class="section-heading">Which tags get which cards</h3>
      <p class="section-hint">
        Each kind of card goes to the tags you pick. "Default" follows the {{ inheritLabel }}.
      </p>
      <div class="route-table" role="table" aria-label="Card routing">
        <div v-for="kind in kinds" :key="kind" class="route-row" role="row">
          <div class="route-kind" role="rowheader">
            <div class="route-label">{{ kindLabel(kind) }}</div>
            <div class="route-hint">{{ kindHint(kind) }}</div>
          </div>
          <div class="route-controls" role="cell">
            <ElSelect
              :model-value="routeMode(kind)"
              :disabled="disabled"
              class="route-mode"
              :aria-label="`Route for ${kindLabel(kind)}`"
              @update:model-value="(v: RouteMode) => onRouteMode(kind, v)"
            >
              <ElOption :label="`Default (${inheritedRouteText(kind)})`" value="inherit" />
              <ElOption label="All tags" value="all" />
              <ElOption label="None" value="none" />
              <ElOption v-if="devices" label="Specific tags…" value="specific" :disabled="devices.length === 0" />
            </ElSelect>
            <ElSelect
              v-if="devices && routeMode(kind) === 'specific'"
              :model-value="specificIds(kind)"
              multiple
              collapse-tags
              collapse-tags-tooltip
              :disabled="disabled"
              class="route-tags"
              placeholder="Pick tags"
              :aria-label="`Tags for ${kindLabel(kind)}`"
              @update:model-value="(v: string[]) => setRoute(kind, v)"
            >
              <ElOption v-for="d in devices" :key="d.id" :label="deviceTitle(d)" :value="d.id" />
            </ElSelect>
          </div>
        </div>
      </div>
    </section>

    <section class="form-section">
      <h3 class="section-heading">How cards look</h3>

      <div class="opt-row">
        <div class="opt-text">
          <div class="opt-label">Reply excerpts</div>
          <div class="opt-hint">Show the start of a finished reply on the tag, not just that it is ready.</div>
        </div>
        <div class="opt-control">
          <ElSwitch
            :model-value="boolValue('show_excerpts')"
            :disabled="disabled || isInherited('show_excerpts')"
            aria-label="Reply excerpts"
            @update:model-value="(v) => setValue('show_excerpts', !!v)"
          />
          <ElCheckbox
            :model-value="isInherited('show_excerpts')" :disabled="disabled"
            @update:model-value="(v) => setInherited('show_excerpts', !!v)"
          >Use {{ inheritLabel }} ({{ inheritedText('show_excerpts') }})</ElCheckbox>
        </div>
      </div>

      <div class="opt-row">
        <div class="opt-text">
          <div class="opt-label">QR links</div>
          <div class="opt-hint">Add a QR code that opens the conversation or run in Cremind.</div>
        </div>
        <div class="opt-control">
          <ElSwitch
            :model-value="boolValue('qr_links')"
            :disabled="disabled || isInherited('qr_links')"
            aria-label="QR links"
            @update:model-value="(v) => setValue('qr_links', !!v)"
          />
          <ElCheckbox
            :model-value="isInherited('qr_links')" :disabled="disabled"
            @update:model-value="(v) => setInherited('qr_links', !!v)"
          >Use {{ inheritLabel }} ({{ inheritedText('qr_links') }})</ElCheckbox>
        </div>
      </div>

      <div class="opt-row">
        <div class="opt-text">
          <div class="opt-label">Progress cadence</div>
          <div class="opt-hint">How often a progress card may redraw the screen (seconds, 60–3600). Each redraw costs battery.</div>
        </div>
        <div class="opt-control">
          <ElInputNumber
            :model-value="numValue('progress_cadence_s')"
            :min="60" :max="3600" :step="60"
            :disabled="disabled || isInherited('progress_cadence_s')"
            controls-position="right"
            class="num-input"
            aria-label="Progress cadence in seconds"
            @update:model-value="(v) => setValue('progress_cadence_s', v ?? 300)"
          />
          <ElCheckbox
            :model-value="isInherited('progress_cadence_s')" :disabled="disabled"
            @update:model-value="(v) => setInherited('progress_cadence_s', !!v)"
          >Use {{ inheritLabel }} ({{ inheritedText('progress_cadence_s') }})</ElCheckbox>
        </div>
      </div>

      <div class="opt-row">
        <div class="opt-text">
          <div class="opt-label">Language</div>
          <div class="opt-hint">The language tag the companion renders words like "Updated" in, e.g. en or vi.</div>
        </div>
        <div class="opt-control">
          <ElInput
            :model-value="strValue('language')"
            maxlength="16"
            :disabled="disabled || isInherited('language')"
            class="text-input"
            aria-label="Language"
            @update:model-value="(v: string) => setValue('language', v)"
          />
          <ElCheckbox
            :model-value="isInherited('language')" :disabled="disabled"
            @update:model-value="(v) => setInherited('language', !!v)"
          >Use {{ inheritLabel }} ({{ inheritedText('language') }})</ElCheckbox>
        </div>
      </div>

      <div class="opt-row">
        <div class="opt-text">
          <div class="opt-label">Timezone</div>
          <div class="opt-hint">For the times on the screen. Leave empty to follow the profile's own timezone.</div>
        </div>
        <div class="opt-control">
          <ElInput
            :model-value="strValue('timezone')"
            :placeholder="tzPlaceholder"
            :disabled="disabled || isInherited('timezone')"
            class="text-input"
            aria-label="Timezone"
            @update:model-value="(v: string) => setValue('timezone', v)"
          />
          <ElCheckbox
            :model-value="isInherited('timezone')" :disabled="disabled"
            @update:model-value="(v) => setInherited('timezone', !!v)"
          >Use {{ inheritLabel }} ({{ inheritedText('timezone') }})</ElCheckbox>
        </div>
      </div>

      <div v-if="layouts.length > 1" class="opt-row">
        <div class="opt-text">
          <div class="opt-label">Layout</div>
          <div class="opt-hint">How the screen is arranged.</div>
        </div>
        <div class="opt-control">
          <ElSelect
            :model-value="strValue('layout')"
            :disabled="disabled || isInherited('layout')"
            class="text-input"
            aria-label="Layout"
            @update:model-value="(v: string) => setValue('layout', v)"
          >
            <ElOption v-for="l in layouts" :key="l" :label="l" :value="l" />
          </ElSelect>
          <ElCheckbox
            :model-value="isInherited('layout')" :disabled="disabled"
            @update:model-value="(v) => setInherited('layout', !!v)"
          >Use {{ inheritLabel }} ({{ inheritedText('layout') }})</ElCheckbox>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.options-form { color: var(--text-primary); }
.form-section + .form-section { margin-top: 22px; }
.section-heading { margin: 0 0 4px; font-size: 0.95rem; font-weight: 600; color: var(--text-primary); }
.section-hint { margin: 0 0 12px; font-size: 0.8rem; color: var(--text-tertiary); }

.route-table { border: 1px solid var(--border-color); border-radius: 10px; overflow: hidden; }
.route-row {
  display: flex; align-items: center; gap: 12px; padding: 10px 12px; flex-wrap: wrap;
}
.route-row + .route-row { border-top: 1px solid var(--border-color); }
.route-kind { flex: 1 1 240px; min-width: 0; }
.route-label { font-weight: 600; font-size: 0.875rem; }
.route-hint { font-size: 0.78rem; color: var(--text-tertiary); }
.route-controls { display: flex; gap: 8px; flex: 0 1 auto; flex-wrap: wrap; justify-content: flex-end; }
.route-mode { width: 210px; }
.route-tags { width: 220px; }

.opt-row {
  display: flex; align-items: flex-start; gap: 16px; padding: 12px 0; flex-wrap: wrap;
}
.opt-row + .opt-row { border-top: 1px solid var(--border-color); }
.opt-text { flex: 1 1 260px; min-width: 0; }
.opt-label { font-weight: 600; font-size: 0.875rem; }
.opt-hint { font-size: 0.78rem; color: var(--text-tertiary); line-height: 1.45; margin-top: 2px; }
.opt-control { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; flex: 0 1 300px; min-width: 0; }
.num-input { width: 160px; }
.text-input { width: 240px; }
.opt-control :deep(.el-checkbox) { height: auto; min-height: 22px; align-items: flex-start; white-space: normal; }
.opt-control :deep(.el-checkbox__input) { margin-top: 3px; }
.opt-control :deep(.el-checkbox__label) {
  color: var(--text-secondary); font-size: 0.8rem; line-height: 1.45; white-space: normal;
}
</style>
