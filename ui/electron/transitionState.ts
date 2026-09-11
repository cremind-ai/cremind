export type TransitionState = {
  local: Record<string, string>
  session: Record<string, string>
}

const PREFERENCES = [
  'theme', 'auto_connect', 'conversations_panel_collapsed', 'sidebar_collapsed',
  'usage_chip_hover', 'events_view_mode', 'terminalPanelWidth', 'rightPanelSplitRatio',
  'rightPanelShowHidden', 'rightPanelViewMode', 'rightPanelCollapsed',
  'agent_activity_panel_maximized', 'eventRunDrawerMaximized',
]

export function transitionProfile(url: string, fallback?: string): string | null {
  try {
    const route = new URL(new URL(url).hash.slice(1) || '/', 'http://cremind.invalid')
    const segments = route.pathname.split('/').filter(Boolean).map(decodeURIComponent)
    const first = segments[0]
    const profile = (first === 'login' || first === 'setup' ? segments[1] : first) || (typeof fallback === 'string' ? fallback : undefined)
    // Public routes whose first segment is a route name, not a profile.
    if (!profile || profile === 'setup-handoff' || profile === 'https-handoff' || profile === 'tls-handoff'
      || profile === 'oauth-return' || profile.length > 128 || /[\/\\\u0000-\u001f]/.test(profile)) return null
    return profile
  } catch { return null }
}

export function transitionLocalKeys(url: string, fallback?: string): string[] {
  const profile = transitionProfile(url, fallback)
  return profile
    ? [...PREFERENCES, `agent_token_${profile}`, `chat_mode_${profile}`, `reasoning_enabled_${profile}`]
    : [...PREFERENCES]
}

/** Bound and allowlist both directions; one window never exports another profile. */
export function filterTransitionState(url: string, state: unknown): TransitionState {
  const result: TransitionState = { local: {}, session: {} }
  if (!state || typeof state !== 'object') return result
  const raw = state as Partial<TransitionState>
  const profile = transitionProfile(url, raw.local?.profile_id)
  for (const key of transitionLocalKeys(url, profile ?? undefined)) {
    const value = raw.local?.[key]
    if (typeof value === 'string' && value.length <= 32768) result.local[key] = value
  }
  if (profile && result.local[`agent_token_${profile}`]) {
    result.local.profile_id = profile
    result.local.logged_in_profiles = JSON.stringify([profile])
  }
  const grace = raw.session?.['cremind:just_updated']
  if (typeof grace === 'string' && /^\d{10,16}$/.test(grace)) result.session['cremind:just_updated'] = grace
  if (profile && raw.session && typeof raw.session === 'object') {
    let bytes = 0
    for (const [key, value] of Object.entries(raw.session)) {
      if (!key.startsWith(`cremind:draft:${profile}:`) || typeof value !== 'string') continue
      bytes += value.length
      if (bytes > 4 * 1024 * 1024) throw new Error('Draft state is too large to migrate safely')
      result.session[key] = value
    }
  }
  return result
}
