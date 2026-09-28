<script setup lang="ts">
/**
 * The setup code of a bridge or tag, three ways: scan the label's QR code with
 * a camera, upload a photo of the label, or type the 25 characters.
 *
 * Decoding happens here, in the page: the BarcodeDetector API where the
 * browser has one for QR codes, else jsQR (loaded only when needed) on canvas
 * frames. Typed text is checked live (length, characters labels never use,
 * the check symbol, the role) and shown in groups of five.
 *
 * A setup code is a pairing credential: it is never logged, the camera stops
 * as soon as a code is read, frames and photos are wiped from the canvas
 * after use, and the parent clears the text when its dialog closes.
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { ElButton, ElInput } from 'element-plus';
import { Icon } from '@iconify/vue';
import {
  checkSetupCode, formatTypedSetupCode, groupSetupCode, normalizeSetupCode,
  type ParsedSetupCode, type SetupRole,
} from '../../../utils/setupCode';
import { shortSuffix } from '../../../utils/tagsSetupFormat';

const props = withDefaults(defineProps<{
  role: SetupRole;
  modelValue: string;
  disabled?: boolean;
  /** A refusal from the server about this code (shown under the field). */
  serverError?: string;
}>(), { disabled: false, serverError: '' });

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void;
  (e: 'parsed', v: ParsedSetupCode | null): void;
  (e: 'submit'): void;
}>();

type Detector = { detect(source: unknown): Promise<Array<{ rawValue?: string }>> };
type DetectorCtor = { new (opts: { formats: string[] }): Detector; getSupportedFormats?: () => Promise<string[]> };
type JsQr = (data: Uint8ClampedArray, w: number, h: number, o?: { inversionAttempts?: 'dontInvert' | 'attemptBoth' }) => { data: string } | null;

const inputId = `setup-code-${Math.random().toString(36).slice(2, 8)}`;
const statusId = `${inputId}-status`;
const inputRef = ref<InstanceType<typeof ElInput> | null>(null);
const videoRef = ref<HTMLVideoElement | null>(null);
const fileRef = ref<HTMLInputElement | null>(null);
const scanning = ref(false);
const reading = ref(false);
const scanNote = ref('');
const scanError = ref('');

let stream: MediaStream | null = null;
let frameTimer: ReturnType<typeof setTimeout> | null = null;
let canvas: HTMLCanvasElement | null = null;
let detector: Detector | null | undefined;
let jsQr: JsQr | null = null;

const roleWord = computed(() => (props.role === 'bridge' ? 'bridge' : 'tag'));
const check = computed(() => checkSetupCode(props.modelValue, props.role));

/** Live feedback: a hint while the code is still being typed, else the verdict. */
const status = computed<{ tone: 'hint' | 'ok' | 'error'; text: string }>(() => {
  if (props.serverError) return { tone: 'error', text: props.serverError };
  const c = check.value;
  if (c.ok) {
    return { tone: 'ok', text: `The code checks out: ${roleWord.value} ${shortSuffix(c.value.shortId)}.` };
  }
  const { problem } = c.error;
  if (problem === 'empty') return { tone: 'hint', text: '25 letters and numbers, printed next to the QR code.' };
  if (problem === 'short') {
    return { tone: 'hint', text: `${normalizeSetupCode(props.modelValue).length} of 25 characters.` };
  }
  return { tone: 'error', text: c.error.message };
});

watch(check, (c) => emit('parsed', c.ok ? c.value : null), { immediate: true });

const cameraBlockedReason = computed(() => {
  if (typeof window === 'undefined') return 'No camera here.';
  if (!window.isSecureContext) {
    return 'The camera can only be used when Cremind is opened over HTTPS or on this computer. Upload a photo or type the code instead.';
  }
  if (!navigator.mediaDevices?.getUserMedia) return 'This browser cannot use a camera here. Upload a photo or type the code instead.';
  return '';
});

function setText(value: string) {
  emit('update:modelValue', value);
}

function onInput(value: string) {
  const el = inputRef.value?.input as HTMLInputElement | undefined;
  const atEnd = !el || el.selectionStart == null || el.selectionStart >= value.length;
  setText(atEnd ? formatTypedSetupCode(value) : value);
}

function onBlur() {
  setText(formatTypedSetupCode(props.modelValue));
}

function onEnter() {
  if (check.value.ok) emit('submit');
}

// ── decoding ──

async function getDetector(): Promise<Detector | null> {
  if (detector !== undefined) return detector;
  detector = null;
  const Ctor = (window as unknown as { BarcodeDetector?: DetectorCtor }).BarcodeDetector;
  if (!Ctor) return null;
  try {
    const formats = Ctor.getSupportedFormats ? await Ctor.getSupportedFormats() : ['qr_code'];
    if (formats.includes('qr_code')) detector = new Ctor({ formats: ['qr_code'] });
  } catch {
    detector = null;
  }
  return detector;
}

async function getJsQr(): Promise<JsQr> {
  if (!jsQr) jsQr = (await import('jsqr')).default as unknown as JsQr;
  return jsQr;
}

function wipeCanvas() {
  if (!canvas) return;
  canvas.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
  canvas.width = 0;
  canvas.height = 0;
}

/** Decode one image (a video frame, a photo). `maxSide` bounds the work per try. */
async function decode(
  source: CanvasImageSource, width: number, height: number, maxSide: number, thorough: boolean,
): Promise<string | null> {
  const det = await getDetector();
  if (det) {
    try {
      const found = await det.detect(source);
      const text = found.find((f) => typeof f.rawValue === 'string' && f.rawValue)?.rawValue;
      if (text) return text;
      if (!thorough) return null;
    } catch {
      detector = null; // fall back to jsQR from now on
    }
  }
  const decodeQr = await getJsQr();
  const scale = Math.min(1, maxSide / Math.max(width, height));
  const w = Math.max(1, Math.round(width * scale));
  const h = Math.max(1, Math.round(height * scale));
  canvas ??= document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return null;
  ctx.drawImage(source, 0, 0, w, h);
  const pixels = ctx.getImageData(0, 0, w, h);
  const result = decodeQr(pixels.data, w, h, { inversionAttempts: thorough ? 'attemptBoth' : 'dontInvert' });
  pixels.data.fill(0);
  return result?.data ?? null;
}

/** A decoded QR text: accept it when it is this role's setup code. */
function accept(text: string): boolean {
  const c = checkSetupCode(text, props.role);
  if (c.ok) {
    setText(groupSetupCode(c.value.normalized));
    scanError.value = '';
    scanNote.value = 'Code read from the label.';
    return true;
  }
  const looksLikeCode = /^\s*ctag:/i.test(text) || c.error.problem === 'check' || c.error.problem === 'wrong_role';
  scanError.value = looksLikeCode ? c.error.message : 'That QR code is not a Cremind Tag setup label.';
  return false;
}

// ── camera ──

async function startCamera() {
  if (cameraBlockedReason.value || scanning.value) return;
  scanError.value = '';
  scanNote.value = '';
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false });
  } catch (e) {
    const name = e instanceof DOMException ? e.name : '';
    scanError.value = name === 'NotAllowedError' || name === 'SecurityError'
      ? 'Camera access was not allowed. Allow it in the browser, or upload a photo of the label instead.'
      : 'No camera could be opened. Upload a photo of the label or type the code instead.';
    return;
  }
  scanning.value = true;
  await nextTick();
  const video = videoRef.value;
  if (!video) { stopCamera(); return; }
  video.srcObject = stream;
  try {
    await video.play();
  } catch {
    /* autoplay of a muted inline video is allowed; a refusal just shows the first frame late */
  }
  scheduleFrame();
}

function scheduleFrame() {
  frameTimer = setTimeout(scanFrame, 200);
}

async function scanFrame() {
  frameTimer = null;
  const video = videoRef.value;
  if (!scanning.value || !video) return;
  if (video.readyState >= 2 && video.videoWidth) {
    const text = await decode(video, video.videoWidth, video.videoHeight, 720, false).catch(() => null);
    if (!scanning.value) return;
    if (text && accept(text)) {
      stopCamera();
      await nextTick();
      inputRef.value?.focus();
      return;
    }
  }
  scheduleFrame();
}

function stopCamera() {
  if (frameTimer) clearTimeout(frameTimer);
  frameTimer = null;
  scanning.value = false;
  stream?.getTracks().forEach((t) => t.stop());
  stream = null;
  if (videoRef.value) videoRef.value.srcObject = null;
  wipeCanvas();
}

// ── photo ──

function pickPhoto() {
  fileRef.value?.click();
}

async function loadImage(file: File): Promise<{ source: CanvasImageSource; width: number; height: number; release: () => void }> {
  if (typeof createImageBitmap === 'function') {
    const bitmap = await createImageBitmap(file);
    return { source: bitmap, width: bitmap.width, height: bitmap.height, release: () => bitmap.close() };
  }
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.src = url;
  await img.decode();
  return { source: img, width: img.naturalWidth, height: img.naturalHeight, release: () => URL.revokeObjectURL(url) };
}

async function onPhoto(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  stopCamera();
  scanError.value = '';
  scanNote.value = '';
  reading.value = true;
  let image: Awaited<ReturnType<typeof loadImage>> | null = null;
  try {
    image = await loadImage(file);
    let text: string | null = null;
    // A label is often a small part of a large photo: try a moderate size, then a larger one.
    for (const side of [1280, 2400]) {
      text = await decode(image.source, image.width, image.height, side, true);
      if (text) break;
    }
    if (!text) {
      scanError.value = 'No QR code was found in that photo. Try a sharper photo closer to the label, or type the code.';
    } else if (accept(text)) {
      await nextTick();
      inputRef.value?.focus();
    }
  } catch {
    scanError.value = 'That file could not be read as a photo. Try another photo, or type the code.';
  } finally {
    image?.release();
    wipeCanvas();
    reading.value = false;
  }
}

function focus() {
  inputRef.value?.focus();
}

watch(() => props.disabled, (d) => { if (d) stopCamera(); });

onBeforeUnmount(() => {
  stopCamera();
  canvas = null;
});

defineExpose({ focus, stopCamera });
</script>

<template>
  <div class="setup-code">
    <div class="scan-actions">
      <ElButton
        :disabled="disabled || !!cameraBlockedReason || scanning"
        @click="startCamera"
      >
        <Icon icon="mdi:qrcode-scan" class="btn-icon" /> Scan the QR code
      </ElButton>
      <ElButton :disabled="disabled || reading" :loading="reading" @click="pickPhoto">
        <Icon v-if="!reading" icon="mdi:image-outline" class="btn-icon" /> Upload a photo of the label
      </ElButton>
      <input
        ref="fileRef"
        type="file"
        accept="image/*"
        class="visually-hidden"
        tabindex="-1"
        aria-hidden="true"
        @change="onPhoto"
      />
    </div>
    <p v-if="cameraBlockedReason" class="field-hint">{{ cameraBlockedReason }}</p>

    <div v-if="scanning" class="camera">
      <video ref="videoRef" class="camera-video" muted playsinline aria-label="Camera preview" />
      <div class="camera-bar">
        <span>Hold the {{ roleWord }}'s label in front of the camera.</span>
        <ElButton size="small" @click="stopCamera">Stop camera</ElButton>
      </div>
    </div>
    <p v-if="scanError" class="field-error" role="alert">{{ scanError }}</p>
    <p v-else-if="scanNote" class="field-ok" role="status">{{ scanNote }}</p>

    <label class="field-label" :for="inputId">Setup code</label>
    <ElInput
      :id="inputId"
      ref="inputRef"
      :model-value="modelValue"
      class="code-input"
      placeholder="XXXXX-XXXXX-XXXXX-XXXXX-XXXXX"
      maxlength="48"
      autocomplete="off"
      spellcheck="false"
      autocapitalize="characters"
      :disabled="disabled"
      :aria-invalid="status.tone === 'error'"
      :aria-describedby="statusId"
      @update:model-value="onInput"
      @blur="onBlur"
      @keyup.enter="onEnter"
    />
    <p :id="statusId" class="code-status" :class="`tone-${status.tone}`" aria-live="polite">
      <Icon v-if="status.tone === 'ok'" icon="mdi:check-circle" class="status-icon" aria-hidden="true" />
      <Icon v-else-if="status.tone === 'error'" icon="mdi:alert-circle-outline" class="status-icon" aria-hidden="true" />
      <span>{{ status.text }}</span>
    </p>
  </div>
</template>

<style scoped>
.setup-code { display: flex; flex-direction: column; gap: 6px; }
.scan-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.scan-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 6px; }
.visually-hidden {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}
.camera {
  border: 1px solid var(--border-color); border-radius: 10px; overflow: hidden;
  background: #000; margin-top: 4px;
}
.camera-video { display: block; width: 100%; max-height: 280px; object-fit: cover; }
.camera-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;
  padding: 8px 10px; background: var(--surface-color); color: var(--text-secondary); font-size: 0.82rem;
}
.field-label { display: block; margin-top: 10px; font-size: 0.82rem; font-weight: 600; color: var(--text-secondary); }
.code-input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 1.05rem; letter-spacing: 0.06em;
}
.code-status { display: flex; align-items: flex-start; gap: 6px; margin: 2px 0 0; font-size: 0.82rem; line-height: 1.4; }
.status-icon { flex-shrink: 0; margin-top: 2px; }
.tone-hint { color: var(--text-tertiary); }
.tone-ok { color: var(--el-color-success); }
.tone-error { color: var(--el-color-danger); }
.field-hint { margin: 0; font-size: 0.78rem; color: var(--text-tertiary); line-height: 1.4; }
.field-error { margin: 0; font-size: 0.82rem; color: var(--el-color-danger); }
.field-ok { margin: 0; font-size: 0.82rem; color: var(--el-color-success); }
</style>
