import {
  ClientFactory,
  ClientFactoryOptions,
  DefaultAgentCardResolver,
  JsonRpcTransportFactory,
  createAuthenticatingFetchWithRetry,
  type AuthenticationHandler,
} from '@a2a-js/sdk/client';
import { useSettingsStore } from '../stores/settings';
import { getAgentUrl } from './runtimeConfig';

// Declare Electron flag
declare const __IS_ELECTRON__: boolean;

// Get the agent URL — prefer the live Pinia ref over the bridge snapshot.
//
// ``getAgentUrl()`` reads ``window.cremind.config.agentUrl``, which is a
// snapshot the Electron preload captured synchronously at startup. When
// the Setup Wizard updates the URL via ``setAgentUrl``, the snapshot
// stays stale for the rest of the session (the IPC persists to disk and
// updates the main-process cache, but never refreshes the renderer's
// copy). The Pinia ref does get updated, so we prefer it when present
// and only fall back to the bridge/env path during very-early calls
// before the store has been initialised.
function getBaseUrl(): string {
  try {
    const fromStore = useSettingsStore().agentUrl;
    if (fromStore) return fromStore;
  } catch {
    // Pinia not active yet (module-load-time call). Fall through.
  }
  return getAgentUrl();
}

/**
 * Extracts the origin (protocol + host + port) from a URL.
 * The A2A agent card is served at {origin}/.well-known/agent-card.json,
 * so we need just the origin, not the full endpoint path.
 */
function extractOrigin(url: string): string {
  // If it's a relative path (e.g. '/a2a'), return just the origin of the current page
  if (url.startsWith('/')) {
    return window.location.origin;
  }
  try {
    const parsed = new URL(url);
    return parsed.origin;
  } catch {
    return url;
  }
}

/**
 * Resolves the agent server's origin for plain REST calls
 * (e.g. /api/tasks/{id}/cancel) outside the A2A SDK transport.
 */
export function getApiOrigin(): string {
  return extractOrigin(getBaseUrl());
}

/**
 * Creates an AuthenticationHandler that reads the active JWT token
 * from the settings store and injects it as a Bearer token.
 */
function createAuthHandler(): AuthenticationHandler {
  return {
    async headers(): Promise<Record<string, string>> {
      // Read the active token from the Pinia settings store
      const settingsStore = useSettingsStore();
      const token = settingsStore.authToken;
      if (token) {
        return { 'Authorization': `Bearer ${token}` };
      }
      return {};
    },
    async shouldRetryWithHeaders(_req: RequestInit, _res: Response) {
      // No automatic retry — user must regenerate token manually
      return undefined;
    },
  };
}

/**
 * Creates a ClientFactory with authentication support.
 * The authenticated fetch wrapper injects Bearer tokens into all requests.
 */
function createAuthenticatedFactory(): ClientFactory {
  const authHandler = createAuthHandler();
  const authFetch = createAuthenticatingFetchWithRetry(fetch, authHandler);

  return new ClientFactory(
    ClientFactoryOptions.createFrom(ClientFactoryOptions.default, {
      transports: [new JsonRpcTransportFactory({ fetchImpl: authFetch })],
      cardResolver: new DefaultAgentCardResolver({ fetchImpl: authFetch }),
    })
  );
}

// Export a function that returns the client promise with the current URL
export const getA2AClient = () => {
  const fullUrl = getBaseUrl();
  const origin = extractOrigin(fullUrl);
  console.log('Creating A2A client with URL:', origin);
  const factory = createAuthenticatedFactory();
  return factory.createFromUrl(origin);
};
