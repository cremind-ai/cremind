import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { watchEffect } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import hljsDarkUrl from 'highlight.js/styles/github-dark.css?url'
import hljsLightUrl from 'highlight.js/styles/github.css?url'
import './style.css'
import App from './App.vue'
import router from './router'
import { useSettingsStore } from './stores/settings'
import { installExternalLinkInterceptor } from './utils/externalLinks'

document.title = __IS_ELECTRON__ ? 'Cremind App' : 'Cremind Web UI'

// Bundle fingerprint — one glance at the console tells you whether this tab is
// running the freshly-built SPA or a stale cached one (an SPA tab never
// refetches index.html on its own).
console.info(`[cremind-ui] build ${__BUILT_AT__} · v${__APP_VERSION__}`)

// Route clicked external links to the OS browser instead of a new Electron
// window. Installed at module scope (not in a component) so it survives the
// file:// → backend pivot reload below, which re-executes this module, and so
// it's active before Vue mounts. No-ops in the web build.
installExternalLinkInterceptor()

// Electron: leave the asar-bundled SPA snapshot in favor of the
// wheel-served SPA whenever a backend is reachable. The asar copy is
// frozen at electron-builder time, so wheel-only test releases ship
// new UI that this Electron shell never picks up otherwise (e.g. the
// About page added in v0.1.9-test19, invisible in Electron until the
// renderer leaves file://). Once we pivot, all future wheel updates
// auto-deliver their UI through the same HTTP origin.
//
// We deliberately stay on the asar in three situations:
//   - The web build (location.protocol !== 'file:') — no pivot needed.
//   - First-run install before the Setup Wizard has configured a
//     backend (bridge.config.agentUrl empty). The wizard itself runs
//     from the asar and configures the backend it'll then pivot to.
//   - Mid-Setup-Wizard (route under /setup). Navigating away here
//     would wipe in-flight form state.
async function maybePivotToBackend(): Promise<boolean> {
  if (window.location.protocol !== 'file:') return false
  const bridge = window.cremind
  if (!bridge) return false
  const agentUrl = bridge.config?.agentUrl
  if (!agentUrl || !/^https?:\/\//.test(agentUrl)) return false
  const route = window.location.hash.slice(1) || '/'
  if (route.startsWith('/setup')) return false
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 2000)
    const r = await fetch(`${agentUrl}/health`, { signal: controller.signal })
    clearTimeout(timer)
    if (!r.ok) return false
  } catch {
    return false
  }
  // agentUrl is the single public origin (default port 1515) that serves the
  // API, the SPA, and the /electron-renderer mount all together, so navigate to
  // it directly — no port swap.
  let spaUrl: URL
  try {
    spaUrl = new URL(agentUrl)
  } catch {
    return false
  }
  // Prefer the /electron-renderer/ mount (__IS_ELECTRON__: true so the
  // custom titlebar / drag region renders). When the probe fails we
  // pivot to the web bundle at ``/`` instead of staying on the asar
  // copy — same http origin as /electron-renderer/, so the auth token
  // written by SetupWizard.vue's pivot is visible and the user stays
  // logged in. Asar is a different origin (file://), so leaving the
  // window there would force a re-login.
  const hash = window.location.hash || '#/'
  let targetPath = '/electron-renderer/'
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 2000)
    const probe = await fetch(`${spaUrl.origin}/electron-renderer/index.html`, {
      method: 'HEAD',
      signal: controller.signal,
    })
    clearTimeout(timer)
    if (!probe.ok) targetPath = '/'
  } catch {
    targetPath = '/'
  }
  window.location.replace(`${spaUrl.origin}${targetPath}${hash}`)
  return true
}

async function boot() {
  if (await maybePivotToBackend()) return  // navigation in flight; do not mount.

  const app = createApp(App)
  const pinia = createPinia()

  app.use(pinia)
  app.use(ElementPlus)
  app.use(router)

  app.mount('#app')

  // Apply theme attribute and highlight.js stylesheet reactively
  const settingsStore = useSettingsStore()

  function applyHljsTheme(theme: string) {
    const id = 'hljs-theme'
    let link = document.getElementById(id) as HTMLLinkElement | null
    if (!link) {
      link = document.createElement('link')
      link.id = id
      link.rel = 'stylesheet'
      document.head.appendChild(link)
    }
    link.href = theme === 'dark' ? hljsDarkUrl : hljsLightUrl
  }

  watchEffect(() => {
    document.documentElement.setAttribute('data-theme', settingsStore.theme)
    applyHljsTheme(settingsStore.theme)
  })
}

void boot()
