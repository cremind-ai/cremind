import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import Icons from 'unplugin-icons/vite'
import IconsResolver from 'unplugin-icons/resolver'
import Components from 'unplugin-vue-components/vite'
import pkg from './package.json' with { type: 'json' }

// Web-only Vite config (no Electron)
// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    base: './',
    define: {
      __IS_ELECTRON__: false,
      __APP_VERSION__: JSON.stringify(pkg.version),
    },
    build: {
      outDir: 'dist-web',
    },
    server: {
      host: env.HOST || '0.0.0.0',
      port: parseInt(env.PORT) || 1515,
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
    ],
  }
})
