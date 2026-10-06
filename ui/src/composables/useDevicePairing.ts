/**
 * Adding a bridge or a tag from its label (connect-setup.md §8.2, §8.3), as
 * the Add bridge / Add tag dialogs run it: the setup code → a discovery (a
 * bridge: its gateway listens; a tag: the gateway itself, when it reaches tags
 * on its own radio, and every ready bridge) → pair at once when exactly one
 * candidate can take it, else let the person choose → follow the pairing
 * until the device is ready.
 *
 * The code text lives here only while the dialog is open: `reset()` (on
 * close and on unmount) drops it. The server gets the normalized code; the
 * store keeps no copy of it.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useTagsSetupStore } from '../stores/tagsSetup';
import { TagsApiError } from '../services/tagsApi';
import type { ParsedSetupCode } from '../utils/setupCode';
import {
  discoveryDecision, sortCandidates, setupErrorMessage, tagReach,
} from '../utils/tagsSetupFormat';

export type PairingStep = 'code' | 'searching' | 'choose' | 'pairing' | 'done' | 'problem';
export type PairingResume = { kind: 'pairing' | 'discovery'; id: string };

export function useDevicePairing(role: 'bridge' | 'tag') {
  const store = useTagsSetupStore();
  const codeText = ref('');
  const parsed = ref<ParsedSetupCode | null>(null);
  const name = ref('');
  const codeError = ref('');
  const discoveryId = ref<string | null>(null);
  const pairingId = ref<string | null>(null);
  const chosen = ref<string | null>(null);
  const busy = ref<'' | 'find' | 'pair' | 'cancel'>('');
  /** A refusal of a request (not a device outcome): shown with Try again. */
  const failure = ref<{ code: string | null; message: string } | null>(null);
  /** Why the chosen candidate was refused (it filled up meanwhile): shown on the choice. */
  const choiceError = ref('');
  /** The last search's device id, to say which device was looked for. */
  const lookingFor = ref('');
  /** Stop was asked for; the server is undoing (or keeping) what the device already did. */
  const stopping = ref(false);
  /** Pairing at once happens once per discovery, never again after a refusal. */
  let autoPaired: string | null = null;

  const discovery = computed(() => (discoveryId.value ? store.discoveries[discoveryId.value] ?? null : null));
  const pairing = computed(() => (pairingId.value ? store.pairings[pairingId.value] ?? null : null));
  const decision = computed(() => (discovery.value ? discoveryDecision(discovery.value) : null));
  const candidates = computed(() => (discovery.value ? sortCandidates(discovery.value) : []));
  /** What a tag should be close to: "your gateway or one of your bridges". */
  const near = computed(() => tagReach(store.readiness));
  const say = (code: string | null | undefined, fallback?: string | null) =>
    setupErrorMessage(code, { role, fallback, near: near.value });

  const step = computed<PairingStep>(() => {
    if (failure.value) return 'problem';
    const p = pairing.value;
    if (p) {
      if (p.state === 'succeeded') return 'done';
      if (p.state === 'failed' || p.state === 'cancelled') return 'problem';
      return 'pairing';
    }
    if (pairingId.value || busy.value === 'pair') return 'pairing';
    if (!discoveryId.value) return 'code';
    const d = decision.value;
    if (!d || d.kind === 'searching') return 'searching';
    if (d.kind === 'choose' || d.kind === 'pair') return 'choose';
    return 'problem';
  });

  /** What went wrong, in words, and what the page offers next. */
  const problem = computed<{ title: string; text: string; action: 'search' | 'retry' } | null>(() => {
    if (step.value !== 'problem') return null;
    if (failure.value) return { title: 'That did not work', text: failure.value.message, action: 'retry' };
    const p = pairing.value;
    if (p?.state === 'cancelled') return { title: 'Pairing cancelled', text: 'Nothing was added.', action: 'retry' };
    if (p?.state === 'failed') {
      return { title: `The ${role} was not added`, text: say(p.error?.code, p.error?.message), action: 'retry' };
    }
    const d = decision.value;
    if (d?.kind === 'not_found') return { title: `No ${role} found`, text: say('not_found'), action: 'search' };
    if (d?.kind === 'unavailable') {
      return {
        title: `Found the ${role}, but it cannot be added yet`,
        text: say(d.reason ?? 'candidate_not_eligible'),
        action: 'search',
      };
    }
    if (d?.kind === 'failed') {
      return { title: 'The search stopped', text: say(d.error?.code, d.error?.message), action: 'retry' };
    }
    return { title: 'The search was cancelled', text: 'Nothing was added.', action: 'retry' };
  });

  function describe(e: unknown): { code: string | null; message: string } {
    if (e instanceof TagsApiError) return { code: e.code, message: say(e.code, e.message) };
    return { code: null, message: 'Cremind could not be reached. Check the connection and try again.' };
  }

  async function find(gatewayId?: string): Promise<void> {
    const code = parsed.value;
    if (!code || busy.value) return;
    busy.value = 'find';
    codeError.value = '';
    failure.value = null;
    try {
      const d = await store.startDiscovery(role, code.normalized, gatewayId);
      lookingFor.value = code.shortId;
      pairingId.value = null;
      chosen.value = null;
      discoveryId.value = d.id;
      store.follow('discovery', d.id);
    } catch (e) {
      const why = describe(e);
      if (why.code === 'setup_code_invalid' || why.code === 'setup_code_wrong_role') codeError.value = why.message;
      else failure.value = why;
    } finally {
      busy.value = '';
    }
  }

  async function pair(candidateId: string): Promise<void> {
    const dId = discoveryId.value;
    if (!dId || busy.value === 'pair') return;
    busy.value = 'pair';
    failure.value = null;
    try {
      const p = await store.startPairing(dId, candidateId, name.value);
      store.unfollow('discovery', dId);
      pairingId.value = p.id;
      store.follow('pairing', p.id);
    } catch (e) {
      const why = describe(e);
      if (why.code === 'candidate_not_eligible' || why.code === 'bridge_full') {
        // Someone took the slot meanwhile: stay on the choice, with the reason.
        chosen.value = null;
        codeError.value = '';
        failure.value = null;
        choiceError.value = why.message;
      } else {
        failure.value = why;
      }
    } finally {
      busy.value = '';
    }
  }

  /** A tag enrolled with the hardware tools (no label): imported by its tag id, then followed as a pairing. */
  async function importTag(tagId: string): Promise<void> {
    if (busy.value) return;
    busy.value = 'pair';
    failure.value = null;
    codeError.value = '';
    try {
      const p = await store.importTag(tagId, name.value);
      lookingFor.value = tagId.trim().toUpperCase();
      pairingId.value = p.id;
      store.follow('pairing', p.id);
    } catch (e) {
      const why = describe(e);
      if (why.code === 'invalid_tag_id') codeError.value = why.message;
      else failure.value = why;
    } finally {
      busy.value = '';
    }
  }

  async function cancel(): Promise<void> {
    const id = pairingId.value;
    if (!id) return;
    busy.value = 'cancel';
    try {
      await store.cancelPairing(id);
      stopping.value = true;
    } catch (e) {
      failure.value = describe(e);
    } finally {
      busy.value = '';
    }
  }

  /** Back to the code (kept), for another try. */
  function again(): void {
    if (discoveryId.value) store.unfollow('discovery', discoveryId.value);
    if (pairingId.value) store.unfollow('pairing', pairingId.value);
    discoveryId.value = null;
    pairingId.value = null;
    chosen.value = null;
    failure.value = null;
    choiceError.value = '';
    stopping.value = false;
  }

  /** Pick a setup up again after a refresh. */
  function resume(r: PairingResume): void {
    reset();
    if (r.kind === 'pairing') {
      pairingId.value = r.id;
      store.follow('pairing', r.id);
    } else {
      discoveryId.value = r.id;
      store.follow('discovery', r.id);
    }
  }

  /** Forget everything, the code first. */
  function reset(): void {
    again();
    codeText.value = '';
    parsed.value = null;
    name.value = '';
    codeError.value = '';
    lookingFor.value = '';
    busy.value = '';
  }

  // Exactly one candidate can take the device: pair at once. Several: preselect the recommended one.
  watch(decision, (d) => {
    if (!d) return;
    if (d.kind === 'pair' && !pairingId.value && !busy.value && autoPaired !== discoveryId.value) {
      autoPaired = discoveryId.value;
      void pair(d.candidateId);
    }
    if (d.kind === 'pair' && !chosen.value) chosen.value = d.candidateId;
    if (d.kind === 'choose' && !chosen.value) chosen.value = d.preselect;
  });

  // A new code clears the server's refusal of the old one.
  watch(codeText, () => { codeError.value = ''; });

  onBeforeUnmount(reset);

  return {
    codeText,
    parsed,
    name,
    codeError,
    choiceError,
    discovery,
    pairing,
    decision,
    candidates,
    chosen,
    busy,
    stopping,
    step,
    problem,
    lookingFor,
    near,
    find,
    pair,
    importTag,
    cancel,
    again,
    resume,
    reset,
  };
}
