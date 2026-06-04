// Map a file-tree entry to a VS Code-style Iconify icon slug.
//
// Returns icons from the ``vscode-icons`` set, which mirrors the popular
// VS Code File Icons theme. Unknown extensions fall back to the default
// file icon; folders use the standard folder/folder-opened pair.

interface IconEntry {
  name: string;
  is_dir: boolean;
}

const EXTENSION_MAP: Record<string, string> = {
  // TypeScript / JavaScript
  ts: 'vscode-icons:file-type-typescript',
  tsx: 'vscode-icons:file-type-reactts',
  js: 'vscode-icons:file-type-js',
  jsx: 'vscode-icons:file-type-reactjs',
  mjs: 'vscode-icons:file-type-js',
  cjs: 'vscode-icons:file-type-js',
  // Vue / web
  vue: 'vscode-icons:file-type-vue',
  html: 'vscode-icons:file-type-html',
  htm: 'vscode-icons:file-type-html',
  css: 'vscode-icons:file-type-css',
  scss: 'vscode-icons:file-type-scss',
  sass: 'vscode-icons:file-type-sass',
  less: 'vscode-icons:file-type-less',
  // Python / data
  py: 'vscode-icons:file-type-python',
  ipynb: 'vscode-icons:file-type-jupyter',
  // Markdown / docs
  md: 'vscode-icons:file-type-markdown',
  markdown: 'vscode-icons:file-type-markdown',
  rst: 'vscode-icons:file-type-text',
  txt: 'vscode-icons:file-type-text',
  pdf: 'vscode-icons:file-type-pdf2',
  // Config / data
  json: 'vscode-icons:file-type-json',
  yml: 'vscode-icons:file-type-yaml',
  yaml: 'vscode-icons:file-type-yaml',
  toml: 'vscode-icons:file-type-toml',
  xml: 'vscode-icons:file-type-xml',
  ini: 'vscode-icons:file-type-config',
  env: 'vscode-icons:file-type-dotenv',
  // Shell / scripting
  sh: 'vscode-icons:file-type-shell',
  bash: 'vscode-icons:file-type-shell',
  zsh: 'vscode-icons:file-type-shell',
  ps1: 'vscode-icons:file-type-powershell',
  bat: 'vscode-icons:file-type-bat',
  cmd: 'vscode-icons:file-type-bat',
  // Other languages
  go: 'vscode-icons:file-type-go',
  rs: 'vscode-icons:file-type-rust',
  java: 'vscode-icons:file-type-java',
  kt: 'vscode-icons:file-type-kotlin',
  swift: 'vscode-icons:file-type-swift',
  rb: 'vscode-icons:file-type-ruby',
  php: 'vscode-icons:file-type-php',
  c: 'vscode-icons:file-type-c',
  h: 'vscode-icons:file-type-c',
  cpp: 'vscode-icons:file-type-cpp',
  hpp: 'vscode-icons:file-type-cpp',
  cs: 'vscode-icons:file-type-csharp',
  // Images / media
  png: 'vscode-icons:file-type-image',
  jpg: 'vscode-icons:file-type-image',
  jpeg: 'vscode-icons:file-type-image',
  gif: 'vscode-icons:file-type-image',
  svg: 'vscode-icons:file-type-svg',
  webp: 'vscode-icons:file-type-image',
  ico: 'vscode-icons:file-type-image',
  mp4: 'vscode-icons:file-type-video',
  mov: 'vscode-icons:file-type-video',
  webm: 'vscode-icons:file-type-video',
  mp3: 'vscode-icons:file-type-audio',
  wav: 'vscode-icons:file-type-audio',
  flac: 'vscode-icons:file-type-audio',
  // Archives / lock
  zip: 'vscode-icons:file-type-zip',
  tar: 'vscode-icons:file-type-zip',
  gz: 'vscode-icons:file-type-zip',
  '7z': 'vscode-icons:file-type-zip',
  lock: 'vscode-icons:file-type-lock',
  // SQL / db
  sql: 'vscode-icons:file-type-sql',
  db: 'vscode-icons:file-type-db',
  sqlite: 'vscode-icons:file-type-db',
  // Logs / misc
  log: 'vscode-icons:file-type-log',
  csv: 'vscode-icons:file-type-excel',
  xls: 'vscode-icons:file-type-excel',
  xlsx: 'vscode-icons:file-type-excel',
  doc: 'vscode-icons:file-type-word',
  docx: 'vscode-icons:file-type-word',
  ppt: 'vscode-icons:file-type-powerpoint',
  pptx: 'vscode-icons:file-type-powerpoint',
};

const FILENAME_MAP: Record<string, string> = {
  'package.json': 'vscode-icons:file-type-npm',
  'package-lock.json': 'vscode-icons:file-type-npm',
  'pnpm-lock.yaml': 'vscode-icons:file-type-pnpm',
  'yarn.lock': 'vscode-icons:file-type-yarn',
  'tsconfig.json': 'vscode-icons:file-type-tsconfig',
  'tsconfig.app.json': 'vscode-icons:file-type-tsconfig',
  'tsconfig.node.json': 'vscode-icons:file-type-tsconfig',
  'vite.config.ts': 'vscode-icons:file-type-vite',
  'vite.config.js': 'vscode-icons:file-type-vite',
  'Dockerfile': 'vscode-icons:file-type-docker2',
  'docker-compose.yml': 'vscode-icons:file-type-docker2',
  'docker-compose.yaml': 'vscode-icons:file-type-docker2',
  '.gitignore': 'vscode-icons:file-type-git',
  '.gitattributes': 'vscode-icons:file-type-git',
  '.env': 'vscode-icons:file-type-dotenv',
  '.env.local': 'vscode-icons:file-type-dotenv',
  '.eslintrc': 'vscode-icons:file-type-eslint',
  '.eslintrc.json': 'vscode-icons:file-type-eslint',
  '.eslintrc.js': 'vscode-icons:file-type-eslint',
  '.prettierrc': 'vscode-icons:file-type-prettier',
  'README.md': 'vscode-icons:file-type-markdown',
  'pyproject.toml': 'vscode-icons:file-type-python',
  'requirements.txt': 'vscode-icons:file-type-python',
  'Pipfile': 'vscode-icons:file-type-python',
  'CLAUDE.md': 'vscode-icons:file-type-markdown',
};

const FOLDER_NAME_MAP: Record<string, [string, string]> = {
  // [closed, opened]
  '.git': ['vscode-icons:folder-type-git', 'vscode-icons:folder-type-git-opened'],
  'node_modules': [
    'vscode-icons:folder-type-node',
    'vscode-icons:folder-type-node-opened',
  ],
  'src': ['vscode-icons:folder-type-src', 'vscode-icons:folder-type-src-opened'],
  'dist': ['vscode-icons:folder-type-dist', 'vscode-icons:folder-type-dist-opened'],
  'build': ['vscode-icons:folder-type-dist', 'vscode-icons:folder-type-dist-opened'],
  'public': ['vscode-icons:folder-type-public', 'vscode-icons:folder-type-public-opened'],
  'assets': ['vscode-icons:folder-type-resource', 'vscode-icons:folder-type-resource-opened'],
  'tests': ['vscode-icons:folder-type-test', 'vscode-icons:folder-type-test-opened'],
  'test': ['vscode-icons:folder-type-test', 'vscode-icons:folder-type-test-opened'],
  '__tests__': ['vscode-icons:folder-type-test', 'vscode-icons:folder-type-test-opened'],
  'docs': ['vscode-icons:folder-type-docs', 'vscode-icons:folder-type-docs-opened'],
  'components': [
    'vscode-icons:folder-type-component',
    'vscode-icons:folder-type-component-opened',
  ],
  '.vscode': ['vscode-icons:folder-type-vscode', 'vscode-icons:folder-type-vscode-opened'],
  '.github': ['vscode-icons:folder-type-github', 'vscode-icons:folder-type-github-opened'],
};

export function iconFor(entry: IconEntry, expanded = false): string {
  if (entry.is_dir) {
    const folderHit = FOLDER_NAME_MAP[entry.name];
    if (folderHit) return expanded ? folderHit[1] : folderHit[0];
    return expanded ? 'vscode-icons:default-folder-opened' : 'vscode-icons:default-folder';
  }
  const named = FILENAME_MAP[entry.name];
  if (named) return named;
  const dot = entry.name.lastIndexOf('.');
  if (dot > 0) {
    const ext = entry.name.slice(dot + 1).toLowerCase();
    const hit = EXTENSION_MAP[ext];
    if (hit) return hit;
  }
  return 'vscode-icons:default-file';
}
