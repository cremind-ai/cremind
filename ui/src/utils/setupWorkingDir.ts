/**
 * The admin's working directory the first-setup payload carries, if any.
 *
 * The Server step shows the server's suggestion (``suggested_working_dir``)
 * as the field's placeholder only, never as its value: it was worked out
 * before the admin could change the System Directory on that same step, and
 * the default lives under it, so sending it back would pin the admin to a
 * folder under the old location. Only a folder the admin typed that differs
 * from the suggestion is sent. Blank — or the suggestion itself — leaves it to
 * the server: its default once the System Directory has settled (on a
 * Reconfigure re-run, the admin's current folder).
 *
 * Pure, so node:test covers it (tests/setup-working-dir.test.mjs).
 */
export function chosenWorkingDir(
  typed: string,
  suggested: string | null | undefined,
): string | null {
  const value = typed.trim();
  if (!value || value === (suggested ?? '').trim()) return null;
  return value;
}
