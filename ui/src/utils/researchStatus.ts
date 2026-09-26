/**
 * How the Research activity panel names a job's state.
 *
 * `partial` covers two different endings, and the panel must not blur them:
 * a job a limit stopped (the token budget, the time limit) and a job that
 * finished its work but found too little evidence to answer from — no
 * candidate document, none relevant, no verified finding, a named law that is
 * not indexed. The dossier's `outcome.reason` says which; a snapshot saved
 * before outcomes existed carries none and gets the neutral wording.
 */

/** Why a settled job ended as it did: the dossier's outcome, without its trace. */
export interface ResearchOutcome {
  reason: string;
  detail?: string | null;
  queries?: number;
  candidates?: number;
  selected?: number;
  files_read?: number;
  provisions_read?: number;
  findings?: number;
  unresolved?: number;
  stopped_early?: boolean;
  incomplete_provisions?: string[];
}

const STATUS_LABELS: Record<string, string> = {
  queued: 'Queued',
  planning: 'Planning',
  running: 'Running',
  needs_clarification: 'Waiting for your answer',
  needs_confirmation: 'Waiting for your confirmation',
  complete: 'Complete',
  partial: 'Partial — incomplete or insufficient evidence',
  failed: 'Failed',
  cancelled: 'Cancelled',
  interrupted: 'Interrupted — ask the agent to continue it',
};

/** Outcome reasons of a job a limit stopped. */
const STOPPED_EARLY = new Set(['budget', 'time']);

export function researchStatusLabel(status: string, outcome?: ResearchOutcome | null): string {
  if (status === 'partial' && outcome?.reason) {
    return STOPPED_EARLY.has(outcome.reason) ? 'Partial — stopped early' : 'Partial — insufficient evidence';
  }
  return STATUS_LABELS[status] ?? status;
}
