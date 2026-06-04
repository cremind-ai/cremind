import { defineConfig, loadEnv } from 'vite'
import path from 'node:path'
import electron from 'vite-plugin-electron/simple'
import vue from '@vitejs/plugin-vue'
import Icons from 'unplugin-icons/vite'
import IconsResolver from 'unplugin-icons/resolver'
import Components from 'unplugin-vue-components/vite'
import pkg from './package.json' with { type: 'json' }

// https://vitejs.dev/config/
export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), '')

  // Channel: which Cremind release stream the bundled installer points at.
  //   development (vite serve)         → dev    (uses local checkout)
  //   test        (vite build --mode test) → test   (Test PyPI; build-only)
  //   production  (vite build, default)    → production
  //
  // The test channel is build-only: ``vite --mode test`` (dev serve) is
  // refused below, because running the Electron app against a Test PyPI
  // installer isn't a supported developer workflow — devs use ``npm run
  // dev`` (which routes to --dev for the local checkout) and the test
  // channel exists only to produce installer artifacts.
  if (mode === 'test' && command !== 'build') {
    throw new Error(
      "vite mode 'test' is build-only. Use `npm run dev` for local " +
      "development (dev channel) or `npm run build:test` to produce a " +
      "test-channel installer.",
    )
  }
  const installChannel: 'production' | 'test' | 'dev' =
    mode === 'test' ? 'test'
      : command === 'serve' ? 'dev'
      : 'production'

  return {
    base: './',
    define: {
      __IS_ELECTRON__: true,
      __APP_VERSION__: JSON.stringify(pkg.version),
      __CREMIND_INSTALL_CHANNEL__: JSON.stringify(installChannel),
    },
    server: {
      host: env.HOST || '0.0.0.0',
      port: parseInt(env.PORT) || 0,
    },
    plugins: [
    vue(),
    Components({
      resolvers: [
        IconsResolver(),
      ],
    }),
    Icons({
      autoInstall: true,
    }),
    electron({
      main: {
        // Shortcut of `build.lib.entry`.
        entry: 'electron/main.ts',
        // The Electron-main bundle is built by vite-plugin-electron with
        // its own vite config — the top-level ``define`` does not
        // propagate. We re-inject the channel constant here so the main
        // process sees the same baked-in value as the renderer.
        vite: {
          define: {
            __CREMIND_INSTALL_CHANNEL__: JSON.stringify(installChannel),
          },
        },
      },
      preload: {
        // Shortcut of `build.rollupOptions.input`.
        // Preload scripts may contain Web assets, so use the `build.rollupOptions.input` instead `build.lib.entry`.
        input: path.join(__dirname, 'electron/preload.ts'),
      },
      // Ployfill the Electron and Node.js API for Renderer process.
      // If you want use Node.js in Renderer process, the `nodeIntegration` needs to be enabled in the Main process.
      // See 👉 https://github.com/electron-vite/vite-plugin-electron-renderer
      renderer: process.env.NODE_ENV === 'test'
        // https://github.com/electron-vite/vite-plugin-electron-renderer/issues/78#issuecomment-2053600808
        ? undefined
        : {},
    }),
  ],
  }
})
