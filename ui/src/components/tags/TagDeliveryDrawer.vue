<script setup lang="ts">
/**
 * One delivery in full: the card it carries, how far it got (stage timeline
 * from `stage_times`), how long the hop took (the companion's `timing`), and
 * the receipt fields (status code, revision, digest). Loaded fresh from
 * GET /api/tags/deliveries/{id} each time it opens; a delivery that has not
 * finished can be cancelled from here, even one the companion already holds.
 */
import { computed, ref, watch } from 'vue';
import { ElButton, ElDrawer, ElMessage, ElMessageBox, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import { TagsApiError, type TagDelivery } from '../../services/tagsApi';
import { formatTimestamp } from '../../utils/usageFormat';
import {
  TIMING_PARTS, cancelPrompt, cancelledMessage, cardIconGlyph, formatMs, kindLabel, stageLabel,
  stagePillType, stageTimeline,
} from '../../utils/tagsFormat';

const props = defineProps<{ deliveryId: number | null; tagName?: string }>();
const emit = defineEmits<{ (e: 'close'): void; (e: 'changed', d: TagDelivery): void }>();

const store = useTagsStore();
const delivery = ref<TagDelivery | null>(null);
const loading = ref(false);
const failed = ref('');
const cancelling = ref(false);

const open = computed({
  get: () => props.deliveryId != null,
  set: (v: boolean) => { if (!v) emit('close'); },
});

async function load() {
  const id = props.deliveryId;
  if (id == null) return;
  loading.value = true;
  failed.value = '';
  try {
    const d = await store.delivery(id);
    if (props.deliveryId === id) delivery.value = d;
  } catch (e) {
    if (props.deliveryId === id) {
      delivery.value = null;
      failed.value = e instanceof Error ? e.message : 'Failed to load the delivery';
    }
  } finally {
    loading.value = false;
  }
}

watch(() => props.deliveryId, (id) => {
  if (id == null) return;
  if (delivery.value?.id !== id) delivery.value = null;
  void load();
}, { immediate: true });

const timeline = computed(() => (delivery.value ? stageTimeline(delivery.value) : []));

const timing = computed(() => {
  const t = delivery.value?.timing || {};
  const parts = TIMING_PARTS.map((p) => ({ ...p, ms: typeof t[p.key] === 'number' ? t[p.key] as number : null }));
  const total = parts.reduce((sum, p) => sum + (p.ms ?? 0), 0);
  const extra = Object.entries(t)
    .filter(([k, v]) => !TIMING_PARTS.some((p) => p.key === k) && typeof v === 'number')
    .map(([k, v]) => ({ key: k, label: k.replace(/_ms$/, '').replace(/_/g, ' '), ms: v as number }));
  return { parts, total, extra, any: parts.some((p) => p.ms != null) || extra.length > 0 };
});

const card = computed(() => delivery.value?.card || null);

async function cancel() {
  const d = delivery.value;
  if (!d) return;
  try {
    await ElMessageBox.confirm(cancelPrompt(d), 'Cancel delivery',
      { type: 'warning', confirmButtonText: 'Cancel delivery', cancelButtonText: 'Keep it' });
  } catch { return; }
  cancelling.value = true;
  try {
    const res = await store.cancelDelivery(d.id);
    delivery.value = res.delivery;
    emit('changed', res.delivery);
    ElMessage.success(cancelledMessage(res));
  } catch (e) {
    if (e instanceof TagsApiError && e.code === 'already_terminal') {
      if (e.body?.delivery) {
        delivery.value = e.body.delivery as TagDelivery;
        emit('changed', delivery.value);
      }
      ElMessage.info(e.message);
    } else {
      ElMessage.error(e instanceof Error ? e.message : 'Failed to cancel');
    }
  } finally {
    cancelling.value = false;
  }
}

function segmentStyle(ms: number | null, total: number) {
  return { flexGrow: String(ms && total ? ms / total : 0) };
}
</script>

<template>
  <ElDrawer v-model="open" direction="rtl" size="min(520px, 94vw)" append-to-body :with-header="false">
    <div class="drawer">
      <div class="drawer-head">
        <div class="drawer-title-row">
          <Icon :icon="cardIconGlyph(card?.icon)" class="head-icon" />
          <div class="drawer-title">
            <div class="title-text">{{ card?.title || (delivery ? kindLabel(delivery.kind) : 'Delivery') }}</div>
            <div class="sub">
              Delivery #{{ deliveryId }}<template v-if="tagName"> · {{ tagName }}</template>
            </div>
          </div>
          <ElButton size="small" text :loading="loading" aria-label="Reload" @click="load">
            <Icon icon="mdi:refresh" />
          </ElButton>
          <ElButton size="small" text aria-label="Close" @click="open = false">
            <Icon icon="mdi:close" />
          </ElButton>
        </div>
        <div v-if="delivery" class="pill-row">
          <ElTag :type="stagePillType(delivery.stage)" size="small" effect="plain">{{ stageLabel(delivery.stage) }}</ElTag>
          <ElTag type="info" size="small" effect="plain">{{ kindLabel(delivery.kind) }}</ElTag>
          <span class="sub">priority {{ delivery.priority }}</span>
          <ElButton
            v-if="!delivery.terminal"
            size="small" type="danger" plain class="cancel-btn"
            :loading="cancelling"
            @click="cancel"
          >Cancel delivery</ElButton>
        </div>
      </div>

      <div v-if="failed" class="drawer-body"><p class="error-text">{{ failed }}</p></div>
      <div v-else-if="!delivery" class="drawer-body"><p class="muted">Loading…</p></div>
      <div v-else class="drawer-body">
        <section class="block">
          <h3 class="block-title">Card</h3>
          <div class="card-box">
            <div class="card-box-title">
              <Icon :icon="cardIconGlyph(card?.icon)" />
              <span>{{ card?.title || '(no title)' }}</span>
            </div>
            <p v-if="card?.body" class="card-box-body">{{ card.body }}</p>
            <dl class="facts compact">
              <template v-if="card?.severity"><dt>Severity</dt><dd>{{ card.severity }}</dd></template>
              <template v-if="card?.icon"><dt>Icon</dt><dd>{{ card.icon }}</dd></template>
              <template v-if="card?.lang"><dt>Language</dt><dd>{{ card.lang }}</dd></template>
              <template v-if="card?.progress != null"><dt>Progress</dt><dd>{{ typeof card.progress === 'object' ? JSON.stringify(card.progress) : card.progress }}</dd></template>
              <template v-if="card?.link"><dt>Link</dt><dd class="mono break">{{ card.link }}</dd></template>
              <template v-if="card?.source?.type"><dt>Source</dt><dd>{{ card.source.type }}<span v-if="card.source.id" class="mono"> · {{ card.source.id }}</span></dd></template>
            </dl>
          </div>
        </section>

        <section class="block">
          <h3 class="block-title">Stages</h3>
          <ol class="timeline">
            <li
              v-for="row in timeline"
              :key="row.stage"
              :class="{ reached: row.reached, current: row.current, bad: row.current && ['failed', 'uncertain'].includes(row.stage) }"
            >
              <span class="dot" aria-hidden="true" />
              <span class="t-stage">{{ stageLabel(row.stage) }}</span>
              <span class="t-time">{{ row.at ? formatTimestamp(row.at) : row.reached ? '' : '—' }}</span>
            </li>
          </ol>
          <p v-if="delivery.detail" class="detail-text">{{ delivery.detail }}</p>
        </section>

        <section class="block">
          <h3 class="block-title">Timing</h3>
          <template v-if="timing.any">
            <div v-if="timing.total > 0" class="timing-bar" aria-hidden="true">
              <span
                v-for="(p, i) in timing.parts"
                :key="p.key"
                class="seg"
                :class="`seg-${i}`"
                :style="segmentStyle(p.ms, timing.total)"
              />
            </div>
            <dl class="facts">
              <template v-for="(p, i) in timing.parts" :key="p.key">
                <dt><span class="swatch" :class="`seg-${i}`" />{{ p.label }}</dt>
                <dd class="num">{{ formatMs(p.ms) }}</dd>
              </template>
              <template v-for="p in timing.extra" :key="p.key">
                <dt>{{ p.label }}</dt><dd class="num">{{ formatMs(p.ms) }}</dd>
              </template>
              <dt>Total</dt><dd class="num strong">{{ formatMs(timing.total) }}</dd>
            </dl>
          </template>
          <p v-else class="muted">The companion reports timing once the tag has woken and refreshed.</p>
        </section>

        <section class="block">
          <h3 class="block-title">Receipt</h3>
          <dl class="facts">
            <dt>Status code</dt><dd class="num">{{ delivery.status_code ?? '—' }}</dd>
            <dt>Revision</dt><dd class="num">{{ delivery.revision ?? '—' }}</dd>
            <dt>Digest</dt><dd class="mono">{{ delivery.digest || '—' }}</dd>
            <dt>Outcome</dt><dd>{{ delivery.outcome ? stageLabel(delivery.outcome) : 'not finished' }}</dd>
            <dt>Epoch</dt><dd class="num">{{ delivery.epoch }}</dd>
            <dt>Sequence</dt><dd class="num">{{ delivery.seq }}</dd>
            <template v-if="delivery.replace_key"><dt>Replaces</dt><dd class="mono break">{{ delivery.replace_key }}</dd></template>
            <template v-if="delivery.resolves"><dt>Resolves</dt><dd class="mono break">{{ delivery.resolves }}</dd></template>
            <dt>Created</dt><dd>{{ formatTimestamp(delivery.created_at) }}</dd>
            <dt>Expires</dt><dd>{{ formatTimestamp(delivery.expires_at) }}</dd>
            <template v-if="delivery.finished_at"><dt>Finished</dt><dd>{{ formatTimestamp(delivery.finished_at) }}</dd></template>
          </dl>
        </section>
      </div>
    </div>
  </ElDrawer>
</template>

<style scoped>
.drawer { display: flex; flex-direction: column; height: 100%; color: var(--text-primary); }
.drawer-head { padding: 14px 16px 10px; border-bottom: 1px solid var(--border-color); }
.drawer-title-row { display: flex; align-items: center; gap: 10px; }
.head-icon { font-size: 22px; color: var(--primary-color); flex-shrink: 0; }
.drawer-title { flex: 1; min-width: 0; }
.title-text { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub { font-size: 0.78rem; color: var(--text-tertiary); }
.pill-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.cancel-btn { margin-left: auto; }
.drawer-body { flex: 1; overflow-y: auto; padding: 8px 16px 24px; }
.block { margin-top: 14px; }
.block-title {
  margin: 0 0 8px; font-size: 0.78rem; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; color: var(--text-tertiary);
}
.card-box {
  border: 1px solid var(--border-color); border-radius: 10px; padding: 12px;
  background: var(--surface-color);
}
.card-box-title { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.card-box-body { margin: 6px 0 0; color: var(--text-secondary); white-space: pre-wrap; font-size: 0.9rem; }
.facts {
  display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px;
  margin: 10px 0 0; font-size: 0.85rem;
}
.facts.compact { margin-top: 10px; }
.facts dt { color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }
.facts dd { margin: 0; color: var(--text-primary); min-width: 0; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.8rem; }
.break { word-break: break-all; }
.num { font-variant-numeric: tabular-nums; }
.strong { font-weight: 600; }
.muted { color: var(--text-tertiary); font-size: 0.85rem; }
.error-text { color: var(--el-color-danger); }
.detail-text { margin: 8px 0 0; font-size: 0.85rem; color: var(--text-secondary); }

.timeline { list-style: none; margin: 0; padding: 0; }
.timeline li {
  position: relative; display: flex; align-items: center; gap: 10px;
  padding: 4px 0 4px 2px; font-size: 0.85rem; color: var(--text-tertiary);
}
.timeline li + li::before {
  content: ''; position: absolute; left: 6px; top: -6px; height: 12px; width: 2px;
  background: var(--border-color);
}
.timeline li.reached { color: var(--text-primary); }
.timeline .dot {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  border: 2px solid var(--border-color); background: var(--surface-color);
}
.timeline li.reached .dot { border-color: var(--primary-color); background: var(--primary-color); }
.timeline li.current .dot { box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-color) 30%, transparent); }
.timeline li.bad .dot { border-color: var(--el-color-danger); background: var(--el-color-danger); }
.t-stage { flex: 1; }
.t-time { color: var(--text-secondary); font-size: 0.78rem; font-variant-numeric: tabular-nums; }
.timeline li:not(.reached) .t-time { color: var(--text-tertiary); }

.timing-bar {
  display: flex; height: 10px; border-radius: 5px; overflow: hidden;
  background: var(--hover-bg); margin-bottom: 4px;
}
.seg { flex-basis: 0; min-width: 0; }
.seg-0 { background: #6366f1; }
.seg-1 { background: #0ea5e9; }
.seg-2 { background: #14b8a6; }
.seg-3 { background: #f59e0b; }
.swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
</style>
