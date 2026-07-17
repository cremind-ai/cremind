/**
 * Shared normalization + diffing for plan-mode todo snapshots.
 *
 * The backend always sends a FULL todo snapshot (per `update_todos` call and in
 * persisted `metadata.plan_mode.todos`). Three sites consume that raw shape —
 * the chat SSE handler, the reload restore path, and the floating-panel store —
 * so the normalization lives here to stay in lockstep.
 */

import type { TodoItem } from '../stores/chat';

/** Coerce a raw todo array (SSE payload or persisted metadata) into TodoItems. */
export function normalizeTodos(raw: unknown): TodoItem[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((t: any, i: number) => ({
    id: String(t?.id ?? `t${i}`),
    content: t?.content ?? '',
    status:
      t?.status === 'in_progress' || t?.status === 'completed'
        ? t.status
        : 'pending',
  }));
}

/**
 * Ids whose status or content changed between the previous and next snapshot —
 * drives the highlight-then-fade in the panel.
 */
export function changedTodoIds(
  prev: TodoItem[] | undefined,
  next: TodoItem[],
): string[] {
  const prevById = new Map((prev ?? []).map(t => [t.id, t]));
  return next
    .filter(t => {
      const p = prevById.get(t.id);
      return !p || p.status !== t.status || p.content !== t.content;
    })
    .map(t => t.id);
}

/** True when the snapshot is non-empty and every item is completed. */
export function allTodosCompleted(items: TodoItem[]): boolean {
  return items.length > 0 && items.every(t => t.status === 'completed');
}
