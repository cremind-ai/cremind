/**
 * Download helper for the server-rendered configuration export.
 *
 * The file itself is assembled and rendered by the backend
 * (``app/config/config_export.py``, served by ``GET /api/config/export``). It
 * used to be built here, in the browser, which meant only a profile that could
 * read four admin-only endpoints could ever produce one — and the wizard, the
 * Profile page card and the CLI would each have had to agree on the same
 * assembly rules. One renderer, three callers; this file is what is left of the
 * browser's half: turning the bytes it gets back into a download.
 */

export type ExportFormat = 'md' | 'json' | 'env';

/** Hand the browser a generated text file. The object URL is revoked as soon
 *  as the click has been dispatched — the browser has already taken its own
 *  reference to the blob by then, and leaving it alive pins the whole string
 *  in memory for the life of the tab. */
export function downloadTextFile(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
