/**
 * How a file-tree status button asks its own file-tree panel to show a file's
 * indexed content. Provided per FileTreePanel (not a global store), so two
 * panels on screen at once — a chat's and an event run's — each open their
 * own pane.
 */
import type { InjectionKey } from 'vue';

export interface IndexedContentTarget {
  /** The absolute path the tree lists. */
  path: string;
  name: string;
  /** The button that opened it; focus returns there on Back. */
  originEl: HTMLElement | null;
}

export interface IndexedContentOpener {
  open(target: IndexedContentTarget): void;
}

export const INDEXED_CONTENT_OPENER: InjectionKey<IndexedContentOpener> = Symbol('indexed-content-opener');
