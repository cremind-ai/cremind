// Typed client for the Blueprint endpoints.
//
//   GET    /api/blueprints/exportable          → export checklist for this profile
//   POST   /api/blueprints/export              → build an archive (returns file+manifest)
//   GET    /api/blueprints                     → list stored archives
//   GET    /api/blueprints/download/{name}     → stream an archive (auth header)
//   DELETE /api/blueprints/{name}              → delete an archive
//   POST   /api/blueprints/import/upload       → stage an upload → session+plan
//   GET    /api/blueprints/import/session      → current import session
//   POST   /api/blueprints/import/steps/{key}  → apply a step (body = inputs)
//   POST   /api/blueprints/import/steps/{key}/skip → apply with no inputs (skip)
//   POST   /api/blueprints/import/finalize     → build the report, finish
//   POST   /api/blueprints/import/abort        → abort (optionally delete the profile)
//
// Follows the resolveBaseUrl + authHeaders + fetch convention of backupApi.ts.

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(authToken: string, json = true): Record<string, string> {
  const headers: Record<string, string> = {};
  if (json) headers['Content-Type'] = 'application/json';
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
  return headers;
}

async function jsonOrThrow(res: Response, what: string): Promise<any> {
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      if (body?.message) msg = body.message;
      else if (body?.error) msg = body.error;
    } catch { /* keep statusText */ }
    throw new Error(`${what}: ${msg}`);
  }
  return res.status === 204 ? null : res.json();
}

export interface ToolItem {
  tool_id: string;
  name: string;
  kind: string; // "builtin" | "a2a" | "mcp"
  description?: string | null;
  source?: string | null;
  enabled?: boolean | null;
  settings_count?: number;
  secret_variables?: string[];
  has_secret_variables?: boolean;
  disabled_leaves?: number;
}

export interface SkillItem {
  name: string;
  slug: string;
  dir: string;
  builtin: boolean;
  bundled: boolean;
  secret_variables?: string[];
  has_listener?: boolean;
  approx_bytes?: number;
}

export interface SettingItem {
  key: string;
  label: string;
  group: string | null;
  group_label: string | null;
  description?: string | null;
  type: string; // "number" | "string" | "boolean" | "enum" | "unknown"
  enum?: string[] | null;
  value: any;
  default: any;
  unknown: boolean;
}

export interface ScheduleEventItem {
  id: string;
  title: string;
  schedule_kind?: string | null;
  dtstart?: string | null;
  rrule?: string | null;
  timezone?: string | null;
  all_day?: boolean;
}

export interface WatcherEventItem {
  id: string;
  name: string;
  root_path: string;
  recursive?: boolean;
  event_types?: string | null;
  extensions?: string | null;
}

export interface SkillEventItem {
  id: string;
  skill_slug: string;
  skill_name: string;
  event_type: string;
}

export interface EventItemGroups {
  schedule: ScheduleEventItem[];
  file_watcher: WatcherEventItem[];
  skill_event: SkillEventItem[];
}

export interface ListenerItem {
  skill_dir: string;
  skill_name: string;
}

export interface ExportableComponent {
  available: boolean;
  count?: number;
  summary?: Record<string, any>;
  items?: SettingItem[] | ToolItem[] | SkillItem[] | ListenerItem[] | EventItemGroups;
  counts?: Record<string, number>;
  keys?: string[];
  skipped_non_skill?: number;
  excluded?: Record<string, number>;
}

export interface ExportableResponse {
  profile: string;
  components: Record<string, ExportableComponent>;
}

export interface BlueprintManifestSummary {
  name: string;
  display_name: string;
  description: string;
  author: string | null;
  app_version: string;
  min_app_version: string;
  platform: string;
  source_profile: string;
  created_at: string;
  components: Record<string, any>;
  requirements: {
    secrets?: any[];
    paths?: any[];
    listeners?: any[];
  };
}

export interface BlueprintEntry {
  name: string;
  size_bytes: number;
  created_at: number;
  manifest: BlueprintManifestSummary | null;
}

export interface PlanStep {
  key: string;
  title: string;
  kind: string;
  requirements: any[];
  preview?: Record<string, any>;
}

export interface ImportStepState {
  key: string;
  status: string;
  requirements: any[];
  result: Record<string, any>;
}

export interface ImportSession {
  id: string;
  owner: string;
  state: string;
  manifest: BlueprintManifestSummary;
  plan: PlanStep[];
  target_profile: string | null;
  steps: ImportStepState[];
  warnings: { kind: string; message: string }[];
  report: ImportReport | null;
}

export interface ImportReport {
  profile: string | null;
  applied: string[];
  skipped: string[];
  needs_attention: string[];
  warnings: string[];
}

// ── export ─────────────────────────────────────────────────────────────────

export async function getExportable(agentUrl: string, authToken: string): Promise<ExportableResponse> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/exportable`, {
    headers: authHeaders(authToken),
  });
  return jsonOrThrow(res, 'Failed to load exportable components');
}

export async function exportBlueprint(
  agentUrl: string,
  authToken: string,
  body: {
    components: string[];
    skills?: string[];
    tools?: string[];
    settings?: string[];
    events?: string[];
    name?: string;
    display_name?: string;
    description?: string;
  },
): Promise<{ ok: boolean; file: { name: string; bytes: number }; manifest: BlueprintManifestSummary; warnings: string[] }> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/export`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify(body),
  });
  return jsonOrThrow(res, 'Failed to export blueprint');
}

export async function listBlueprints(agentUrl: string, authToken: string): Promise<BlueprintEntry[]> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints`, {
    headers: authHeaders(authToken),
  });
  const body = await jsonOrThrow(res, 'Failed to list blueprints');
  return body.blueprints ?? [];
}

export async function downloadBlueprint(agentUrl: string, authToken: string, name: string): Promise<void> {
  const blob = await fetchBlueprintBlob(agentUrl, authToken, name);
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Fetch a stored blueprint archive as a Blob (for the Publish-to-Hub upload). */
export async function fetchBlueprintBlob(
  agentUrl: string, authToken: string, name: string,
): Promise<Blob> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/download/${encodeURIComponent(name)}`, {
    headers: authHeaders(authToken, false),
  });
  if (!res.ok) throw new Error(`Failed to read blueprint: ${res.statusText}`);
  return res.blob();
}

/** Stage a blueprint downloaded from the Cremind Hub into the import wizard. */
export async function importBlueprintFromHub(
  agentUrl: string, authToken: string, link: string, replace = false,
): Promise<ImportSession> {
  const q = replace ? '?replace=true' : '';
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/import/hub${q}`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify({ link }),
  });
  return jsonOrThrow(res, 'Failed to import blueprint from hub');
}

export async function deleteBlueprint(agentUrl: string, authToken: string, name: string): Promise<void> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: authHeaders(authToken),
  });
  await jsonOrThrow(res, 'Failed to delete blueprint');
}

// ── import ─────────────────────────────────────────────────────────────────

export async function uploadBlueprint(
  agentUrl: string, authToken: string, file: File, replace = false,
): Promise<ImportSession> {
  const form = new FormData();
  form.append('file', file, file.name);
  const q = replace ? '?replace=true' : '';
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/import/upload${q}`, {
    method: 'POST',
    headers: authHeaders(authToken, false),
    body: form,
  });
  return jsonOrThrow(res, 'Failed to upload blueprint');
}

export async function getImportSession(agentUrl: string, authToken: string): Promise<ImportSession | null> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/import/session`, {
    headers: authHeaders(authToken),
  });
  if (res.status === 404) return null;
  return jsonOrThrow(res, 'Failed to load import session');
}

export async function applyStep(
  agentUrl: string, authToken: string, key: string, inputs: Record<string, any>,
): Promise<{ ok: boolean; result: any; session: ImportSession }> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/import/steps/${encodeURIComponent(key)}`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify(inputs || {}),
  });
  return jsonOrThrow(res, 'Step failed');
}

export async function skipStep(
  agentUrl: string, authToken: string, key: string,
): Promise<{ ok: boolean; result: any; session: ImportSession }> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/import/steps/${encodeURIComponent(key)}/skip`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: '{}',
  });
  return jsonOrThrow(res, 'Step failed');
}

export async function finalizeImport(agentUrl: string, authToken: string): Promise<{ ok: boolean; report: ImportReport }> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/import/finalize`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: '{}',
  });
  return jsonOrThrow(res, 'Failed to finalize import');
}

export async function abortImport(agentUrl: string, authToken: string, deleteProfile = true): Promise<void> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/blueprints/import/abort`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify({ delete_profile: deleteProfile }),
  });
  await jsonOrThrow(res, 'Failed to abort import');
}
