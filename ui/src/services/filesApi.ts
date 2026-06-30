// Wrappers around the file-tree backend endpoints in app/api/files.py.

export interface DirectoryEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  modified: number;
}

export interface DirectoryListing {
  path: string;
  entries: DirectoryEntry[];
  truncated: boolean;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function authHeaders(token: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export class DirectoryAccessError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'DirectoryAccessError';
  }
}

export async function listDirectory(
  agentUrl: string,
  token: string,
  path: string,
  showHidden: boolean,
  signal?: AbortSignal,
  conversationId?: string,
): Promise<DirectoryListing> {
  const base = resolveBaseUrl(agentUrl);
  let url = `${base}/api/files/list?path=${encodeURIComponent(path)}&show_hidden=${
    showHidden ? '1' : '0'
  }`;
  if (conversationId) {
    url += `&conversation_id=${encodeURIComponent(conversationId)}`;
  }
  const res = await fetch(url, { headers: authHeaders(token), signal });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new DirectoryAccessError(
      res.status,
      data.error || `Failed to list directory: ${res.statusText}`,
    );
  }
  return res.json();
}

export async function getInitialCwd(agentUrl: string, token: string): Promise<string> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/files/cwd`, { headers: authHeaders(token) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Failed to fetch cwd: ${res.statusText}`);
  }
  const data = await res.json();
  return data.cwd as string;
}

export function fileOpenUrl(
  agentUrl: string,
  path: string,
  conversationId?: string,
): string {
  const base = resolveBaseUrl(agentUrl);
  let url = `${base}/api/files/open?path=${encodeURIComponent(path)}`;
  if (conversationId) {
    url += `&conversation_id=${encodeURIComponent(conversationId)}`;
  }
  return url;
}

// Extract the parent directory of a path. Backend returns native OS paths
// (Windows uses ``\``); we don't normalize, just split on whichever
// separator appears last so the same logic works on both platforms.
export function parentDir(p: string): string {
  if (!p) return '';
  const sepIdx = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return sepIdx <= 0 ? '' : p.slice(0, sepIdx);
}

export type FileWatchEventType = 'ready' | 'created' | 'deleted' | 'modified' | 'moved';

export interface FileWatchEvent {
  type: FileWatchEventType;
  path: string;
  is_dir?: boolean;
  dest_path?: string;
}

export interface FileWatchHandle {
  close(): void;
}

export interface UploadResult {
  name: string;
  saved_as: string;
  status: string;
  error?: string;
}

export async function uploadFiles(
  agentUrl: string,
  token: string,
  targetDir: string,
  files: File[],
  conversationId?: string,
): Promise<UploadResult[]> {
  if (!files.length) return [];
  const base = resolveBaseUrl(agentUrl);
  const form = new FormData();
  form.append('path', targetDir);
  if (conversationId) form.append('conversation_id', conversationId);
  for (const f of files) form.append('files', f, f.name);
  // Note: do NOT set Content-Type — the browser writes the multipart boundary.
  const res = await fetch(`${base}/api/files/upload`, {
    method: 'POST',
    headers: authHeaders(token),
    body: form,
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new DirectoryAccessError(
      res.status,
      data.error || `Upload failed: ${res.statusText}`,
    );
  }
  const data = await res.json();
  return (data.results || []) as UploadResult[];
}

export interface TempUploadResult extends UploadResult {
  // Absolute server path of the saved file (temp uploads only). Attached to
  // the next message so the agent can read/convert/move it.
  path?: string;
}

// Upload files into the conversation's temporary folder. The destination is
// computed server-side from the authenticated profile + conversation, so —
// unlike uploadFiles — no path is sent. Returns each file's absolute path.
export async function uploadTempFiles(
  agentUrl: string,
  token: string,
  conversationId: string,
  files: File[],
): Promise<TempUploadResult[]> {
  if (!files.length) return [];
  const base = resolveBaseUrl(agentUrl);
  const form = new FormData();
  form.append('conversation_id', conversationId);
  for (const f of files) form.append('files', f, f.name);
  // Note: do NOT set Content-Type — the browser writes the multipart boundary.
  const res = await fetch(`${base}/api/files/upload-temp`, {
    method: 'POST',
    headers: authHeaders(token),
    body: form,
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new DirectoryAccessError(
      res.status,
      data.error || `Upload failed: ${res.statusText}`,
    );
  }
  const data = await res.json();
  return (data.results || []) as TempUploadResult[];
}

export async function deleteEntry(
  agentUrl: string,
  token: string,
  path: string,
  conversationId?: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/files/delete`, {
    method: 'DELETE',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, conversation_id: conversationId }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new DirectoryAccessError(
      res.status,
      data.error || `Delete failed: ${res.statusText}`,
    );
  }
}

export async function moveEntry(
  agentUrl: string,
  token: string,
  src: string,
  dest: string,
  conversationId?: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/files/move`, {
    method: 'POST',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify({ src, dest, conversation_id: conversationId }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new DirectoryAccessError(
      res.status,
      data.error || `Move failed: ${res.statusText}`,
    );
  }
}

export async function mkdir(
  agentUrl: string,
  token: string,
  path: string,
  conversationId?: string,
): Promise<void> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/files/mkdir`, {
    method: 'POST',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, conversation_id: conversationId }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new DirectoryAccessError(
      res.status,
      data.error || `mkdir failed: ${res.statusText}`,
    );
  }
}

export async function setConversationCwd(
  agentUrl: string,
  token: string,
  conversationId: string,
  path: string,
): Promise<string> {
  const base = resolveBaseUrl(agentUrl);
  const res = await fetch(`${base}/api/files/cwd`, {
    method: 'POST',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: conversationId, path }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new DirectoryAccessError(
      res.status,
      data.error || `Set cwd failed: ${res.statusText}`,
    );
  }
  const data = await res.json();
  return (data.working_directory as string) || path;
}

// Triggers a browser download by fetching the file as a blob (since the
// open endpoint requires a bearer header that ``<a download>`` can't carry)
// and clicking a synthetic link.
export async function downloadFile(
  agentUrl: string,
  token: string,
  path: string,
  name: string,
  conversationId?: string,
): Promise<void> {
  const url = fileOpenUrl(agentUrl, path, conversationId);
  const res = await fetch(url, { headers: authHeaders(token) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new DirectoryAccessError(
      res.status,
      data.error || `Download failed: ${res.statusText}`,
    );
  }
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 5_000);
}

export function watchDirectory(
  agentUrl: string,
  token: string,
  path: string,
  onEvent: (ev: FileWatchEvent) => void,
  onError?: (e: unknown) => void,
  conversationId?: string,
): FileWatchHandle {
  const controller = new AbortController();
  let closed = false;

  const run = async () => {
    try {
      const base = resolveBaseUrl(agentUrl);
      let url = `${base}/api/files/watch?path=${encodeURIComponent(path)}`;
      if (conversationId) {
        url += `&conversation_id=${encodeURIComponent(conversationId)}`;
      }
      const headers: Record<string, string> = { Accept: 'text/event-stream' };
      if (token) headers['Authorization'] = `Bearer ${token}`;
      const res = await fetch(url, { headers, signal: controller.signal });
      if (!res.ok || !res.body) {
        throw new Error(`watch SSE failed: ${res.status} ${res.statusText}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      while (!closed) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while (
          (idx = (() => {
            const a = buffer.indexOf('\n\n');
            const b = buffer.indexOf('\r\n\r\n');
            if (a === -1) return b;
            if (b === -1) return a;
            return Math.min(a, b);
          })()) !== -1
        ) {
          const sep = buffer[idx] === '\r' ? 4 : 2;
          const frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + sep);
          const dataLines: string[] = [];
          for (const rawLine of frame.split(/\r?\n/)) {
            if (rawLine.startsWith('data:')) {
              dataLines.push(rawLine.slice(5).replace(/^ /, ''));
            }
          }
          if (dataLines.length === 0) continue;
          try {
            const payload = JSON.parse(dataLines.join('\n')) as FileWatchEvent;
            onEvent(payload);
          } catch (err) {
            // Ignore malformed frames — keepalive comments don't reach here.
            void err;
          }
        }
      }
    } catch (err) {
      if (closed || (err as Error)?.name === 'AbortError') return;
      onError?.(err);
    }
  };

  run();

  return {
    close() {
      if (closed) return;
      closed = true;
      controller.abort();
    },
  };
}
