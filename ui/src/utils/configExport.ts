/**
 * Setup Wizard configuration export.
 *
 * Three pure builders that turn the wizard's collected state into a
 * downloadable file in Markdown, JSON, or .env form. The Markdown form
 * is the default — it groups secrets under a "Sensitive" callout and
 * shows ready-to-paste connection strings.
 */

export type ExportFormat = 'md' | 'json' | 'env';

export interface ConfigExportSnapshot {
  profile: string;
  token: string;
  tokenExpiresAt: string;
  agentUrl: string;
  generatedAt: string;
  deployment: {
    type: 'local' | 'server' | 'custom';
    mode: 'docker' | 'native';
    customFields?: Record<string, string>;
  };
  vnc?: {
    password: string;
    host?: string;
    novnc_url?: string;
    vnc_endpoint?: string;
    novnc_port?: number;
    vnc_port?: number;
    resolution?: string;
    // Deployment environment for the VNC desktop. Docker exposes separate
    // noVNC/VNC ports (6080/5900); Kubernetes fronts everything through one
    // nginx proxy port and serves noVNC at <origin>/vnc/vnc.html instead.
    // Absent is treated as 'docker' (the historical behaviour).
    environment?: 'docker' | 'kubernetes';
  };
  workingDir: string;
  systemDir: string;
  database: {
    provider: 'sqlite' | 'postgres';
    postgres?: {
      host: string;
      port: number;
      database: string;
      user: string;
      password: string;
      sslmode?: string;
    };
    sqlite?: { path: string };
  };
  vectorStore: {
    provider: 'qdrant' | 'chroma' | 'none';
    qdrant?: { host: string; port: number; api_key?: string; https: boolean };
    chroma?: {
      mode: 'http' | 'persistent';
      host?: string;
      port?: number;
      ssl?: boolean;
      api_key?: string;
      persist_path?: string;
    };
  };
  embedding: { enabled: boolean; provider?: string };
  channels: Array<{ type: string; mode: string; id: string }>;
}

function pgConnectionString(p: NonNullable<ConfigExportSnapshot['database']['postgres']>): string {
  const sslSuffix = p.sslmode ? `?sslmode=${encodeURIComponent(p.sslmode)}` : '';
  return `postgresql://${encodeURIComponent(p.user)}:${encodeURIComponent(p.password)}@${p.host}:${p.port}/${encodeURIComponent(p.database)}${sslSuffix}`;
}

function qdrantUrl(q: NonNullable<ConfigExportSnapshot['vectorStore']['qdrant']>): string {
  const scheme = q.https ? 'https' : 'http';
  return `${scheme}://${q.host}:${q.port}`;
}

// ── Markdown ─────────────────────────────────────────────────────────

export function buildMarkdownExport(s: ConfigExportSnapshot): string {
  const lines: string[] = [];
  lines.push(`# Cremind Configuration — ${s.profile}`);
  lines.push('');
  lines.push(`_Generated at ${s.generatedAt}_`);
  lines.push('');
  lines.push('> **Sensitive.** This file contains secrets (JWT token, passwords, API keys). Store it somewhere safe and avoid sharing.');
  lines.push('');

  lines.push('## Profile & Token');
  lines.push('');
  lines.push(`- **Profile:** \`${s.profile}\``);
  lines.push(`- **Agent URL:** \`${s.agentUrl}\``);
  if (s.tokenExpiresAt) lines.push(`- **Token expires:** ${s.tokenExpiresAt}`);
  lines.push(`- **Token recovery path on server:** \`~/.cremind/tokens/${s.profile}.token\``);
  lines.push('');
  lines.push('```');
  lines.push(s.token);
  lines.push('```');
  lines.push('');

  lines.push('## Deployment');
  lines.push('');
  lines.push(`- **Type:** ${s.deployment.type}`);
  lines.push(`- **Mode:** ${s.deployment.mode}`);
  if (s.deployment.customFields) {
    const entries = Object.entries(s.deployment.customFields).filter(([, v]) => v);
    if (entries.length > 0) {
      lines.push('- **Custom fields:**');
      for (const [k, v] of entries) lines.push(`  - \`${k}\` = \`${v}\``);
    }
  }
  lines.push('');

  lines.push('## Project Paths');
  lines.push('');
  lines.push(`- **User working directory:** \`${s.workingDir}\``);
  lines.push(`- **System directory:** \`${s.systemDir}\``);
  lines.push('');

  if (s.vnc) {
    const v = s.vnc;
    const host = v.host || 'localhost';
    const isK8s = v.environment === 'kubernetes';
    const novncPort = v.novnc_port ?? 6080;
    const vncPort = v.vnc_port ?? 5900;
    const novncUrl = v.novnc_url || (isK8s
      ? `http://${host}:1515/vnc/vnc.html`
      : `http://${host}:${novncPort}/vnc.html`);
    const vncEndpoint = v.vnc_endpoint || `${host}:${vncPort}`;
    lines.push(`## VNC Desktop (${isK8s ? 'Kubernetes' : 'Docker'})`);
    lines.push('');
    lines.push('> **Sensitive.**');
    lines.push('');
    lines.push(`- **Password:** \`${v.password}\``);
    lines.push(`- **Host:** \`${host}\``);
    lines.push(`- **Web client (noVNC):** ${novncUrl}`);
    // Kubernetes fronts noVNC/VNC through a single nginx proxy port — the
    // raw 6080/5900 ports and a direct VNC endpoint aren't exposed, so omit
    // those lines there.
    if (!isK8s) {
      lines.push(`- **VNC client:** \`${vncEndpoint}\``);
      lines.push(`- **noVNC port:** \`${novncPort}\``);
      lines.push(`- **VNC port:** \`${vncPort}\``);
    }
    if (v.resolution) lines.push(`- **Resolution:** \`${v.resolution}\``);
    lines.push('');
  }

  lines.push('## Database');
  lines.push('');
  if (s.database.provider === 'postgres' && s.database.postgres) {
    const p = s.database.postgres;
    lines.push('- **Provider:** PostgreSQL');
    lines.push(`- **Host:** \`${p.host}\``);
    lines.push(`- **Port:** \`${p.port}\``);
    lines.push(`- **Database:** \`${p.database}\``);
    lines.push(`- **User:** \`${p.user}\``);
    lines.push(`- **Password:** \`${p.password}\``);
    if (p.sslmode) lines.push(`- **SSL mode:** \`${p.sslmode}\``);
    lines.push('');
    lines.push('Connection string:');
    lines.push('');
    lines.push('```');
    lines.push(pgConnectionString(p));
    lines.push('```');
  } else if (s.database.sqlite) {
    lines.push('- **Provider:** SQLite');
    lines.push(`- **Path:** \`${s.database.sqlite.path}\``);
  }
  lines.push('');

  if (s.vectorStore.provider !== 'none') {
    lines.push('## Vector Store');
    lines.push('');
    if (s.vectorStore.provider === 'qdrant' && s.vectorStore.qdrant) {
      const q = s.vectorStore.qdrant;
      lines.push('- **Provider:** Qdrant');
      lines.push(`- **Host:** \`${q.host}\``);
      lines.push(`- **Port:** \`${q.port}\``);
      lines.push(`- **HTTPS:** ${q.https ? 'yes' : 'no'}`);
      if (q.api_key) lines.push(`- **API key:** \`${q.api_key}\``);
      lines.push('');
      lines.push('URL:');
      lines.push('');
      lines.push('```');
      lines.push(qdrantUrl(q));
      lines.push('```');
    } else if (s.vectorStore.provider === 'chroma' && s.vectorStore.chroma) {
      const c = s.vectorStore.chroma;
      lines.push('- **Provider:** Chroma');
      lines.push(`- **Mode:** ${c.mode}`);
      if (c.mode === 'http') {
        if (c.host) lines.push(`- **Host:** \`${c.host}\``);
        if (c.port !== undefined) lines.push(`- **Port:** \`${c.port}\``);
        lines.push(`- **SSL:** ${c.ssl ? 'yes' : 'no'}`);
        if (c.api_key) lines.push(`- **API key:** \`${c.api_key}\``);
      } else if (c.persist_path) {
        lines.push(`- **Persist path:** \`${c.persist_path}\``);
      }
    }
    lines.push('');
  }

  lines.push('## Embedding');
  lines.push('');
  lines.push(`- **Enabled:** ${s.embedding.enabled ? 'yes' : 'no'}`);
  if (s.embedding.provider) lines.push(`- **Provider:** \`${s.embedding.provider}\``);
  lines.push('');

  if (s.channels.length > 0) {
    lines.push('## Channels');
    lines.push('');
    for (const ch of s.channels) {
      lines.push(`- \`${ch.type}\` (${ch.mode}) — id: \`${ch.id}\``);
    }
    lines.push('');
  }

  return lines.join('\n');
}

// ── JSON ─────────────────────────────────────────────────────────────

export function buildJsonExport(s: ConfigExportSnapshot): string {
  return JSON.stringify(s, null, 2) + '\n';
}

// ── .env ─────────────────────────────────────────────────────────────

function quoteEnvValue(v: string): string {
  if (v === '') return '';
  if (/[\s"'#$`\\]/.test(v)) {
    return `"${v.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
  }
  return v;
}

function envLine(key: string, value: string | number | boolean | undefined | null): string | null {
  if (value === undefined || value === null || value === '') return null;
  return `${key}=${quoteEnvValue(String(value))}`;
}

export function buildEnvExport(s: ConfigExportSnapshot): string {
  const out: string[] = [];
  const push = (line: string | null) => { if (line !== null) out.push(line); };

  out.push('# Cremind configuration export');
  out.push(`# Generated at ${s.generatedAt}`);
  out.push('# Sensitive: contains JWT token, passwords, and API keys.');
  out.push('');

  out.push('# --- Profile & Token ---');
  push(envLine('CREMIND_PROFILE', s.profile));
  push(envLine('CREMIND_AGENT_URL', s.agentUrl));
  push(envLine('CREMIND_TOKEN', s.token));
  push(envLine('CREMIND_TOKEN_EXPIRES_AT', s.tokenExpiresAt));
  out.push('');

  out.push('# --- Deployment ---');
  push(envLine('CREMIND_DEPLOYMENT_TYPE', s.deployment.type));
  push(envLine('CREMIND_DEPLOYMENT_MODE', s.deployment.mode));
  if (s.deployment.customFields) {
    for (const [k, v] of Object.entries(s.deployment.customFields)) {
      push(envLine(`CREMIND_${k.toUpperCase()}`, v));
    }
  }
  out.push('');

  out.push('# --- Project Paths ---');
  push(envLine('CREMIND_USER_WORKING_DIR', s.workingDir));
  push(envLine('CREMIND_SYSTEM_DIR', s.systemDir));
  out.push('');

  if (s.vnc) {
    out.push('# --- VNC Desktop ---');
    push(envLine('VNC_PASSWORD', s.vnc.password));
    push(envLine('VNC_HOST', s.vnc.host));
    push(envLine('VNC_NOVNC_PORT', s.vnc.novnc_port));
    push(envLine('VNC_PORT', s.vnc.vnc_port));
    push(envLine('VNC_NOVNC_URL', s.vnc.novnc_url));
    push(envLine('VNC_ENDPOINT', s.vnc.vnc_endpoint));
    push(envLine('VNC_RESOLUTION', s.vnc.resolution));
    out.push('');
  }

  out.push('# --- Database ---');
  push(envLine('DB_PROVIDER', s.database.provider));
  if (s.database.provider === 'postgres' && s.database.postgres) {
    const p = s.database.postgres;
    push(envLine('PG_HOST', p.host));
    push(envLine('PG_PORT', p.port));
    push(envLine('PG_DATABASE', p.database));
    push(envLine('PG_USER', p.user));
    push(envLine('PG_PASSWORD', p.password));
    push(envLine('PG_SSLMODE', p.sslmode));
    push(envLine('PG_URL', pgConnectionString(p)));
  } else if (s.database.sqlite) {
    push(envLine('SQLITE_DB_PATH', s.database.sqlite.path));
  }
  out.push('');

  if (s.vectorStore.provider !== 'none') {
    out.push('# --- Vector Store ---');
    push(envLine('VECTORSTORE_PROVIDER', s.vectorStore.provider));
    if (s.vectorStore.provider === 'qdrant' && s.vectorStore.qdrant) {
      const q = s.vectorStore.qdrant;
      push(envLine('QDRANT_HOST', q.host));
      push(envLine('QDRANT_PORT', q.port));
      push(envLine('QDRANT_HTTPS', q.https));
      push(envLine('QDRANT_API_KEY', q.api_key));
      push(envLine('QDRANT_URL', qdrantUrl(q)));
    } else if (s.vectorStore.provider === 'chroma' && s.vectorStore.chroma) {
      const c = s.vectorStore.chroma;
      push(envLine('CHROMA_MODE', c.mode));
      push(envLine('CHROMA_HOST', c.host));
      push(envLine('CHROMA_PORT', c.port));
      push(envLine('CHROMA_SSL', c.ssl));
      push(envLine('CHROMA_API_KEY', c.api_key));
      push(envLine('CHROMA_PERSIST_PATH', c.persist_path));
    }
    out.push('');
  }

  out.push('# --- Embedding ---');
  push(envLine('EMBEDDING_ENABLED', s.embedding.enabled));
  push(envLine('EMBEDDING_PROVIDER', s.embedding.provider));
  out.push('');

  if (s.channels.length > 0) {
    out.push('# --- Channels ---');
    s.channels.forEach((ch, i) => {
      push(envLine(`CHANNEL_${i}_TYPE`, ch.type));
      push(envLine(`CHANNEL_${i}_MODE`, ch.mode));
      push(envLine(`CHANNEL_${i}_ID`, ch.id));
    });
    out.push('');
  }

  return out.join('\n');
}

export function buildExport(format: ExportFormat, snapshot: ConfigExportSnapshot): string {
  if (format === 'json') return buildJsonExport(snapshot);
  if (format === 'env') return buildEnvExport(snapshot);
  return buildMarkdownExport(snapshot);
}

export function exportMimeType(format: ExportFormat): string {
  if (format === 'json') return 'application/json';
  if (format === 'env') return 'text/plain';
  return 'text/markdown';
}
