import { Marked } from 'marked';
import { markedHighlight } from 'marked-highlight';
import hljs from 'highlight.js';

/**
 * Build a configured `Marked` instance for rendering chat/plan markdown:
 * syntax highlighting, `breaks: true`, and image/link hrefs rewritten through
 * `resolveApiUrl` (so `/api/...` and absolute filesystem paths resolve to the
 * backend origin). Shared by the message bubble and the plan approval dialog.
 */
export function createChatMarked(resolveApiUrl: (href: string) => string): Marked {
  const marked = new Marked(
    markedHighlight({
      langPrefix: 'hljs language-',
      highlight(code, lang) {
        const language = hljs.getLanguage(lang) ? lang : 'plaintext';
        return hljs.highlight(code, { language }).value;
      },
    }),
  );
  marked.use({
    silent: true,
    breaks: true,
    renderer: {
      image({ href, title, text }) {
        const src = resolveApiUrl(href);
        const alt = text || '';
        const titleAttr = title ? ` title="${title}"` : '';
        return `<img src="${src}" alt="${alt}"${titleAttr} loading="lazy" style="max-width:100%;border-radius:6px;" />`;
      },
      link({ href, title, tokens }) {
        const url = resolveApiUrl(href);
        const titleAttr = title ? ` title="${title}"` : '';
        const text = this.parser.parseInline(tokens);
        if (url.match(/\/api\/files\//)) {
          return `<a href="${url}"${titleAttr} target="_blank" rel="noopener">${text}</a>`;
        }
        return `<a href="${url}"${titleAttr}>${text}</a>`;
      },
    },
  });
  return marked;
}
