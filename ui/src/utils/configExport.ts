/**
 * Cremind configuration export.
 *
 * Three pure builders that turn a collected snapshot into a downloadable
 * file in Markdown, JSON, or .env form. The Markdown form is the default —
 * it groups secrets under a "Sensitive" callout and shows ready-to-paste
 * connection strings.
 *
 * ``assembleConfigSnapshot`` builds that snapshot from raw sources. It lives
 * here rather than in the Setup Wizard because the wizard is not the only
 * place the file is produced: the Developer page re-downloads it long after
 * setup, from the equivalent facts read back off the server. Both callers
 * must emit the same file for the same install, so the assembly rules
 * (which DB block, which Chroma mode, whether a VNC section exists at all)
 * have exactly one home.
 */

import type {
  EmbeddingConfig,
  InstallSecrets,
  VncPortForwardCommand,
} from '../services/configApi';
import { novncUrlForDockerHost, novncUrlOnOrigin } from './vncDesktop';

export type ExportFormat = 'md' | 'json' | 'env';

export interface ConfigExportSnapshot {
  profile: string;
  token: string;
  tokenExpiresAt: string;
  agentUrl: string;
  // True when ``agentUrl`` is the HTTPS origin the server pivots to on the
  // post-setup restart rather than the origin this page is served from —
  // the ``after-setup`` TLS mode runs the whole wizard on plain HTTP.
  agentUrlPendingHttps?: boolean;
  generatedAt: string;
  deployment: {
    // ``kubernetes`` is a deployment of its own rather than a flavour of
    // ``server``: the chart owns the origin, the ports and the noVNC route,
    // so an export that could only say local/server/custom had no way to
    // describe a Helm install at all.
    type: 'local' | 'server' | 'custom' | 'kubernetes';
    mode: 'docker' | 'native';
    customFields?: Record<string, string>;
  };
  // Which cluster objects this install *is*, so the file can be used to get
  // back to it: a Helm install is reached through a tunnel the reader has to
  // re-open, and `<namespace>` / `<release>` placeholders are useless once the
  // browser tab that knew them is gone. Present only on Kubernetes — the
  // server reports the block as null everywhere else, so its presence is the
  // signal and there is no second "is this Helm?" flag to keep in sync.
  // Individual fields stay absent on a chart too old to state them; ``source``
  // says how much was guessed.
  kubernetes?: {
    namespace?: string;
    release?: string;
    /** Deployment name; the chart uses it for the Service too. */
    deployment?: string;
    service?: string;
    servicePort?: number;
    source?: 'chart' | 'inferred';
    /** Ready-to-paste `kubectl port-forward` line. */
    portForward?: string;
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
    // The tunnel(s) that have to exist before the desktop answers at all —
    // the Kubernetes relay shape, where noVNC lives on its own Service port
    // and nothing on the app origin proxies it. Empty/absent everywhere else.
    port_forward_commands?: VncPortForwardCommand[];
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

/** Everything ``assembleConfigSnapshot`` needs, named explicitly so both
 *  callers can see what a complete export costs them. The Setup Wizard holds
 *  these in local refs; the Developer page reads the same facts back from
 *  /api/system/environment, /api/config/install-secrets, /api/config/server,
 *  /api/config/embedding and the channels store. */
export interface ConfigSnapshotSources {
  profile: string;
  token: string;
  tokenExpiresAt: string;
  agentUrl: string;
  agentUrlPendingHttps: boolean;
  generatedAt: string;
  installDeployment: ConfigExportSnapshot['deployment']['type'];
  // ``kubernetes`` only reaches here from the Developer page: the wizard's
  // own install-mode ref is docker/native, because a Helm install reports
  // itself as a container to the wizard.
  installMode: 'docker' | 'native' | 'kubernetes';
  installCustomValues: Record<string, string>;
  installSecrets: InstallSecrets | null;
  // The ``config`` object of GET /api/config/server (plus a nested
  // ``postgres`` object during first setup). Loosely typed on purpose — it
  // is a free-form key/value store, and only four keys are read here.
  serverConfig: Record<string, any>;
  embeddingConfig: EmbeddingConfig | null;
  channels: Array<{ type: string; mode: string; id: string }>;
}

/** A server field that is optional in three different ways — absent, null, or
 *  the empty string the chart injects for a value it could not resolve —
 *  narrowed to "known" or "not known". */
function optionalText(value: string | null | undefined): string | undefined {
  const text = (value ?? '').trim();
  return text === '' ? undefined : text;
}

export function assembleConfigSnapshot(sources: ConfigSnapshotSources): ConfigExportSnapshot {
  const server = sources.serverConfig as Record<string, any>;
  const dbProvider: 'postgres' | 'sqlite' = server.db_provider === 'postgres'
    ? 'postgres'
    : 'sqlite';

  let postgres: ConfigExportSnapshot['database']['postgres'] | undefined;
  if (dbProvider === 'postgres') {
    const pg = (server.postgres ?? {}) as Record<string, any>;
    const secrets = sources.installSecrets;
    postgres = {
      host: String(pg.host ?? secrets?.pg_host ?? 'localhost'),
      port: Number(pg.port ?? secrets?.pg_port ?? 5432),
      database: String(pg.database ?? secrets?.pg_database ?? 'cremind'),
      user: String(pg.user ?? secrets?.pg_user ?? 'cremind'),
      password: String(pg.password ?? secrets?.pg_password ?? ''),
      sslmode: pg.sslmode ? String(pg.sslmode)
        : (secrets?.pg_sslmode ?? undefined),
    };
  }

  const workingDir = String(server.user_working_dir ?? '~/Documents');
  const systemDir = String(server.system_dir ?? '~/.cremind');
  const sqlitePath = `${systemDir.replace(/[\\/]+$/, '')}/storage/cremind.db`;

  // Vector store info lives in the embedding config (the wizard's local ref
  // submitted to /api/config/setup, or what /api/config/embedding reads back
  // post-setup) — NOT serverConfig. The flat ``vectorstore.*`` keys only land
  // in the SQLite server_config table after persist_embedding_config runs,
  // and the wizard never refetches.
  const vs = sources.embeddingConfig?.vectorstore;
  const vectorProvider: 'qdrant' | 'chroma' | 'none' = (
    sources.embeddingConfig?.enabled
    && (vs?.provider === 'qdrant' || vs?.provider === 'chroma')
  ) ? vs!.provider : 'none';

  let qdrant: ConfigExportSnapshot['vectorStore']['qdrant'] | undefined;
  if (vectorProvider === 'qdrant' && vs) {
    qdrant = {
      host: String(vs.qdrant.host ?? 'localhost'),
      port: Number(vs.qdrant.port ?? 6333),
      api_key: vs.qdrant.api_key ? String(vs.qdrant.api_key) : undefined,
      https: Boolean(vs.qdrant.https),
    };
  }

  let chroma: ConfigExportSnapshot['vectorStore']['chroma'] | undefined;
  if (vectorProvider === 'chroma' && vs) {
    // Same persist-vs-http translation as
    // app/lib/embedding_lifecycle.py:120 (Native → persistent file;
    // Docker / External → http endpoint).
    const mode: 'http' | 'persistent' =
      vs.chroma.deployment_mode === 'native' ? 'persistent' : 'http';
    chroma = {
      mode,
      host: vs.chroma.host || undefined,
      port: vs.chroma.port ?? undefined,
      ssl: vs.chroma.ssl ?? undefined,
      api_key: vs.chroma.api_key ? String(vs.chroma.api_key) : undefined,
      persist_path: vs.chroma.persist_path
        ? String(vs.chroma.persist_path) : undefined,
    };
  }

  // Only a container install has a desktop to describe. Kubernetes counts:
  // it runs the same desktop image, and the wizard only ever sees it as
  // 'docker' because install-secrets reports the container deployment.
  const containerInstall = sources.installMode === 'docker'
    || sources.installMode === 'kubernetes';
  const includeVnc = containerInstall
    && Boolean(sources.installSecrets?.vnc_password);

  let vnc: ConfigExportSnapshot['vnc'] | undefined;
  if (includeVnc) {
    const secrets = sources.installSecrets!;
    // Derive the host from APP_URL so server / custom deployments show a
    // host the user can actually reach. APP_URL is provided by the
    // install-secrets endpoint from the cremind container's env. Falls
    // back to localhost for local installs (APP_URL absent or missing
    // host portion).
    let host = 'localhost';
    if (secrets.app_url) {
      const m = secrets.app_url.match(/^[a-z]+:\/\/([^/:]+)/i);
      if (m && m[1]) host = m[1];
    }
    // Kubernetes usually runs a single pod behind an nginx proxy that fronts
    // the SPA, API and noVNC on ONE port, with the desktop at /vnc/vnc.html on
    // the app origin (e.g. http://localhost:1515/vnc/vnc.html for the
    // documented `port-forward svc/cremind 1515:80`, or https://<host>/...
    // behind an Ingress). But the chart bypasses that sidecar when the pod
    // terminates TLS itself, and noVNC then answers on its own Service port
    // instead — indistinguishable from here, so the chart tells us via
    // CREMIND_NOVNC_URL and that wins whenever it is set.
    // See helm/cremind/templates/{proxy-configmap,configmap}.yaml.
    if (secrets.install_mode === 'kubernetes') {
      let origin = 'http://localhost:1515';
      if (secrets.app_url) {
        try { origin = new URL(secrets.app_url).origin; } catch { /* keep default */ }
      }
      vnc = {
        password: secrets.vnc_password as string,
        host,
        novnc_url: secrets.novnc_url || novncUrlOnOrigin(origin),
        resolution: secrets.resolution || undefined,
        environment: 'kubernetes',
        port_forward_commands: secrets.vnc?.port_forward_commands?.length
          ? secrets.vnc.port_forward_commands
          : undefined,
      };
    } else {
      const novncPort = secrets.novnc_port ?? 6080;
      const vncPort = secrets.vnc_port ?? 5900;
      vnc = {
        password: secrets.vnc_password as string,
        host,
        novnc_port: novncPort,
        vnc_port: vncPort,
        novnc_url: novncUrlForDockerHost(host, novncPort),
        vnc_endpoint: `${host}:${vncPort}`,
        resolution: secrets.resolution || undefined,
        environment: 'docker',
      };
    }
  }

  const customFields = sources.installDeployment === 'custom'
    ? { ...sources.installCustomValues } : undefined;

  // Off Kubernetes the server sends no block at all, so this is also the test
  // for "did a Helm install produce this file?".
  const k8s = sources.installSecrets?.kubernetes;
  const kubernetes: ConfigExportSnapshot['kubernetes'] | undefined = k8s
    ? {
      namespace: optionalText(k8s.namespace),
      release: optionalText(k8s.release),
      // ``workload`` on the wire, because the pod infers one name that is
      // both; here it is split into the two objects the reader looks for.
      deployment: optionalText(k8s.workload),
      service: optionalText(k8s.service),
      servicePort: k8s.service_port ?? undefined,
      source: k8s.source ?? undefined,
      portForward: optionalText(k8s.port_forward),
    }
    : undefined;

  return {
    profile: sources.profile,
    token: sources.token,
    tokenExpiresAt: sources.tokenExpiresAt,
    agentUrl: sources.agentUrl,
    agentUrlPendingHttps: sources.agentUrlPendingHttps,
    generatedAt: sources.generatedAt,
    deployment: {
      type: sources.installDeployment,
      // ``mode`` answers "container or host process?", so Kubernetes folds
      // into 'docker' — the deployment type above is what names the chart.
      mode: sources.installMode === 'kubernetes' ? 'docker' : sources.installMode,
      customFields,
    },
    kubernetes,
    vnc,
    workingDir,
    systemDir,
    database: {
      provider: dbProvider,
      postgres,
      sqlite: dbProvider === 'sqlite' ? { path: sqlitePath } : undefined,
    },
    vectorStore: {
      provider: vectorProvider,
      qdrant,
      chroma,
    },
    embedding: {
      enabled: Boolean(sources.embeddingConfig?.enabled),
      provider: sources.embeddingConfig?.provider,
    },
    channels: sources.channels,
  };
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
  if (s.agentUrlPendingHttps) {
    lines.push('  - _This HTTPS address becomes active once setup finishes and the server restarts._');
  }
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

  if (s.kubernetes) {
    const k = s.kubernetes;
    lines.push('## Kubernetes');
    lines.push('');
    if (k.namespace) lines.push(`- **Namespace:** \`${k.namespace}\``);
    if (k.release) lines.push(`- **Helm release:** \`${k.release}\``);
    if (k.deployment) lines.push(`- **Deployment:** \`${k.deployment}\``);
    if (k.service) lines.push(`- **Service:** \`${k.service}\``);
    if (k.servicePort !== undefined) lines.push(`- **Service port:** \`${k.servicePort}\``);
    if (k.source) lines.push(`- **Identity source:** ${k.source}`);
    // An inferred identity is a real answer with one hole in it: the pod name
    // gives the Deployment away, nothing gives the release away. Say so, or
    // the reader pastes a `helm upgrade` at the wrong name.
    if (k.source === 'inferred') {
      lines.push('');
      lines.push('_Read off the pod rather than stated by the chart — confirm the release name with `helm list --all-namespaces`._');
    }
    if (k.portForward) {
      lines.push('');
      lines.push('Reconnect from your machine:');
      lines.push('');
      lines.push('```');
      lines.push(k.portForward);
      lines.push('```');
      lines.push('');
      lines.push('Add `1455:1455` as a second port on that command before signing in to Codex with a ChatGPT account — the browser sends the OAuth callback there, and it has to reach the pod.');
    }
    lines.push('');
  }

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
    // Fallbacks only — the wizard passes ``novnc_url`` whenever the
    // deployment knows it (on Kubernetes the chart states it outright,
    // because whether noVNC sits behind the proxy or on its own port is
    // invisible from here). This guess covers older charts: the Kubernetes
    // shape reaches noVNC through the *app* origin, so it inherits whatever
    // scheme this page is served over. ``v.host`` is a bare hostname (the
    // wizard strips the scheme off app_url), so the page is the only scheme
    // source available. The Docker shape is NOT derived: 6080 is the
    // container's own noVNC port, published directly and always plain http
    // regardless of any TLS on the app port.
    const appScheme = typeof window !== 'undefined'
      && window.location?.protocol?.startsWith('http')
      ? window.location.protocol.replace(':', '')
      : 'http';
    const novncUrl = v.novnc_url || (isK8s
      ? novncUrlOnOrigin(`${appScheme}://${host}:1515`)
      : novncUrlForDockerHost(host, novncPort));
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
    // Relay mode: nothing on the app origin proxies noVNC, so the URL above is
    // dead until one of these tunnels exists. Printing the command is the whole
    // point of carrying it into the file — the reader is offline from the
    // cluster by the time they open it.
    if (v.port_forward_commands?.length) {
      lines.push('');
      lines.push('**Reach it from your machine**');
      for (const cmd of v.port_forward_commands) {
        lines.push('');
        lines.push(`${cmd.label}:`);
        lines.push('');
        lines.push('```');
        lines.push(cmd.command);
        lines.push('```');
        lines.push('');
        lines.push(`Then open ${cmd.open_url}`);
      }
    }
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
  // CREMIND_SERVER is the name the `cremind` CLI reads (app/cli/config.py).
  if (s.agentUrlPendingHttps) {
    out.push('# HTTPS address; active after the post-setup restart.');
  }
  push(envLine('CREMIND_SERVER', s.agentUrl));
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

  if (s.kubernetes) {
    out.push('# --- Kubernetes ---');
    push(envLine('K8S_NAMESPACE', s.kubernetes.namespace));
    push(envLine('K8S_RELEASE', s.kubernetes.release));
    push(envLine('K8S_DEPLOYMENT', s.kubernetes.deployment));
    push(envLine('K8S_SERVICE', s.kubernetes.service));
    push(envLine('K8S_SERVICE_PORT', s.kubernetes.servicePort));
    push(envLine('K8S_IDENTITY_SOURCE', s.kubernetes.source));
    push(envLine('K8S_PORT_FORWARD', s.kubernetes.portForward));
    out.push('');
  }

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

// ── Download ─────────────────────────────────────────────────────────

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

/** Render a snapshot and download it under the wizard's filename convention.
 *  The name is stable across both producers so a re-download from the
 *  Developer page overwrites/versions alongside the original wizard export
 *  instead of looking like a different artefact. */
export function downloadConfigExport(
  format: ExportFormat,
  snapshot: ConfigExportSnapshot,
  profile: string,
): void {
  downloadTextFile(
    `cremind-${profile}-config.${format}`,
    buildExport(format, snapshot),
    exportMimeType(format),
  );
}
