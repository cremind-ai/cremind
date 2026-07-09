// Typed client for the Backup & Restore endpoints.
//
//   POST   /api/backup/create              → start a background backup (202)
//   GET    /api/backup/status              → create-backup status file
//   GET    /api/backup/list                → archives + manifest summaries
//   GET    /api/backup/download/{name}     → stream the archive (auth header)
//   POST   /api/backup/upload              → multipart upload an archive
//   DELETE /api/backup/{name}              → delete an archive
//   POST   /api/backup/restore             → start a restore (202)
//   GET    /api/backup/restore/status      → restore status file
//   GET    /api/backup/restore/report      → restore report + live warnings
//   POST   /api/backup/restore/report/ack  → dismiss the report
//
// Follows the resolveBaseUrl + authHeaders + fetch convention of the other
// services. agentUrl / authToken come from the settings Pinia store.

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

export interface ManifestSummary {
  app_version: string;
  db_provider: string;
  platform: string;
  hostname: string;
  created_at: string;
  profiles: string[];
  encrypted: boolean;
  alembic_revision: string | null;
  db_row_total: number;
}

export interface BackupEntry {
  name: string;
  size_bytes: number;
  created_at: number;
  manifest: ManifestSummary | null;
  manifest_error?: string;
}

export interface StatusFile {
  kind: string;
  phase: string;
  ok: boolean;
  error: string | null;
  detail: Record<string, unknown>;
  log_tail: string[];
  started_at: number | null;
  finished_at: number | null;
}

export interface AutostartFailure {
  id: string;
  profile: string;
  command: string;
  working_dir: string;
  error: string;
}

export interface DisabledChannel {
  id: string;
  profile: string;
  channel_type: string;
  error: string;
}

export interface RestoreReport {
  restore_id?: string;
  ok: boolean;
  finished_at?: number;
  source?: Record<string, unknown> | null;
  db_row_counts?: Record<string, number>;
  relocations?: { relocated: unknown[]; unmapped: unknown[] };
  file_count?: number;
  warnings?: string[];
  error?: string;
  rolled_back?: boolean;
  safety_backup?: string | null;
  acknowledged?: boolean;
}

export interface RestoreReportResponse {
  report: RestoreReport | null;
  warnings: { autostart_failures: AutostartFailure[]; disabled_channels: DisabledChannel[] };
}

async function jsonOrThrow(res: Response, what: string): Promise<any> {
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      if (body?.error) msg = body.error;
    } catch { /* keep statusText */ }
    throw new Error(`${what}: ${msg}`);
  }
  return res.status === 204 ? null : res.json();
}

export async function listBackups(agentUrl: string, authToken: string): Promise<BackupEntry[]> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/list`, {
    headers: authHeaders(authToken),
  });
  const body = await jsonOrThrow(res, 'Failed to list backups');
  return body.backups ?? [];
}

export async function createBackup(agentUrl: string, authToken: string, passphrase?: string): Promise<void> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/create`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify(passphrase ? { passphrase } : {}),
  });
  await jsonOrThrow(res, 'Failed to start backup');
}

export async function fetchBackupStatus(agentUrl: string, authToken: string): Promise<StatusFile> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/status`, {
    headers: authHeaders(authToken),
  });
  return jsonOrThrow(res, 'Failed to fetch backup status');
}

export async function deleteBackup(agentUrl: string, authToken: string, name: string): Promise<void> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: authHeaders(authToken),
  });
  await jsonOrThrow(res, 'Failed to delete backup');
}

// Authorized download → blob → anchor click (a bare href can't carry the bearer).
export async function downloadBackup(agentUrl: string, authToken: string, name: string): Promise<void> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/download/${encodeURIComponent(name)}`, {
    headers: authHeaders(authToken, false),
  });
  if (!res.ok) throw new Error(`Failed to download backup: ${res.statusText}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export async function uploadBackup(
  agentUrl: string, authToken: string, file: File,
): Promise<{ name: string; size_bytes: number; manifest: ManifestSummary }> {
  const form = new FormData();
  form.append('file', file, file.name);
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/upload`, {
    method: 'POST',
    headers: authHeaders(authToken, false),
    body: form,
  });
  return jsonOrThrow(res, 'Failed to upload backup');
}

export async function startRestore(
  agentUrl: string, authToken: string, name: string, passphrase?: string,
): Promise<{ ok: boolean; mode: string }> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/restore`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: JSON.stringify(passphrase ? { name, passphrase } : { name }),
  });
  return jsonOrThrow(res, 'Failed to start restore');
}

export async function fetchRestoreStatus(agentUrl: string, authToken: string): Promise<StatusFile> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/restore/status`, {
    headers: authHeaders(authToken),
  });
  return jsonOrThrow(res, 'Failed to fetch restore status');
}

export async function fetchRestoreReport(agentUrl: string, authToken: string): Promise<RestoreReportResponse> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/restore/report`, {
    headers: authHeaders(authToken),
  });
  return jsonOrThrow(res, 'Failed to fetch restore report');
}

export async function ackRestoreReport(agentUrl: string, authToken: string): Promise<void> {
  const res = await fetch(`${resolveBaseUrl(agentUrl)}/api/backup/restore/report/ack`, {
    method: 'POST',
    headers: authHeaders(authToken),
    body: '{}',
  });
  await jsonOrThrow(res, 'Failed to acknowledge report');
}
