// vite.config.web.ts
import { defineConfig, loadEnv } from "file:///C:/Users/lyntc/DATA/Personal/OpenPA/openpa/ui/node_modules/vite/dist/node/index.js";
import vue from "file:///C:/Users/lyntc/DATA/Personal/OpenPA/openpa/ui/node_modules/@vitejs/plugin-vue/dist/index.mjs";
import Icons from "file:///C:/Users/lyntc/DATA/Personal/OpenPA/openpa/ui/node_modules/unplugin-icons/dist/vite.mjs";
import IconsResolver from "file:///C:/Users/lyntc/DATA/Personal/OpenPA/openpa/ui/node_modules/unplugin-icons/dist/resolver.mjs";
import Components from "file:///C:/Users/lyntc/DATA/Personal/OpenPA/openpa/ui/node_modules/unplugin-vue-components/dist/vite.mjs";

// package.json
var package_default = {
  name: "openpa-client-ui",
  private: true,
  version: "0.2.9",
  type: "module",
  scripts: {
    "version:sync": "python ../scripts/sync_ui_version.py",
    predev: "npm run version:sync",
    prebuild: "npm run version:sync",
    "prebuild:test": "npm run version:sync",
    "prebuild:renderer": "npm run version:sync",
    "preweb:dev": "npm run version:sync",
    "preweb:build": "npm run version:sync",
    dev: "vite",
    build: "vue-tsc && vite build && electron-builder",
    "build:test": "vue-tsc && vite build --mode test && electron-builder --config electron-builder.test.json5",
    "build:renderer": "vue-tsc && vite build",
    preview: "vite preview",
    "web:dev": "vite --config vite.config.web.ts",
    "web:build": "vue-tsc && vite build --config vite.config.web.ts && tsup server/index.ts --format esm --external express --external compression --out-dir dist-server",
    "web:start": "node dist-server/index.js"
  },
  dependencies: {
    "@a2a-js/sdk": "^0.3.10",
    "@xterm/addon-fit": "^0.10.0",
    "@xterm/xterm": "^5.5.0",
    compression: "^1.7.4",
    "electron-updater": "^6.3.9",
    "element-plus": "^2.13.1",
    express: "^4.18.2",
    "highlight.js": "^11.11.1",
    marked: "^17.0.4",
    "marked-highlight": "^2.2.3",
    pinia: "^3.0.4",
    vue: "^3.4.21",
    "vue-router": "4"
  },
  devDependencies: {
    "@iconify-json/logos": "^1.2.10",
    "@iconify-json/mdi": "^1.2.3",
    "@iconify-json/vscode-icons": "^1.2.48",
    "@iconify/vue": "^5.0.0",
    "@types/compression": "^1.7.5",
    "@types/express": "^4.17.21",
    "@vitejs/plugin-vue": "^5.0.4",
    electron: "^30.0.1",
    "electron-builder": "^24.13.3",
    "http-proxy-middleware": "^2.0.6",
    tsup: "^8.0.1",
    typescript: "^5.2.2",
    "unplugin-icons": "^23.0.1",
    "unplugin-vue-components": "^31.0.0",
    vite: "^5.1.6",
    "vite-plugin-electron": "^0.28.6",
    "vite-plugin-electron-renderer": "^0.14.5",
    "vue-tsc": "^2.0.26"
  },
  optionalDependencies: {
    "@rollup/rollup-linux-x64-gnu": "^4.0.0",
    "@rollup/rollup-linux-arm64-gnu": "^4.0.0",
    "@rollup/rollup-darwin-x64": "^4.0.0",
    "@rollup/rollup-darwin-arm64": "^4.0.0",
    "@rollup/rollup-win32-x64-msvc": "^4.0.0"
  },
  main: "dist-electron/main.js"
};

// vite.config.web.ts
var vite_config_web_default = defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    base: "./",
    define: {
      __IS_ELECTRON__: false,
      __APP_VERSION__: JSON.stringify(package_default.version)
    },
    build: {
      outDir: "dist-web"
    },
    server: {
      host: env.HOST || "0.0.0.0",
      port: parseInt(env.PORT) || 1515
    },
    plugins: [
      vue(),
      Components({
        resolvers: [
          IconsResolver()
        ]
      }),
      Icons({
        autoInstall: true
      })
    ]
  };
});
export {
  vite_config_web_default as default
};
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsidml0ZS5jb25maWcud2ViLnRzIiwgInBhY2thZ2UuanNvbiJdLAogICJzb3VyY2VzQ29udGVudCI6IFsiY29uc3QgX192aXRlX2luamVjdGVkX29yaWdpbmFsX2Rpcm5hbWUgPSBcIkM6XFxcXFVzZXJzXFxcXGx5bnRjXFxcXERBVEFcXFxcUGVyc29uYWxcXFxcT3BlblBBXFxcXG9wZW5wYVxcXFx1aVwiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9maWxlbmFtZSA9IFwiQzpcXFxcVXNlcnNcXFxcbHludGNcXFxcREFUQVxcXFxQZXJzb25hbFxcXFxPcGVuUEFcXFxcb3BlbnBhXFxcXHVpXFxcXHZpdGUuY29uZmlnLndlYi50c1wiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9pbXBvcnRfbWV0YV91cmwgPSBcImZpbGU6Ly8vQzovVXNlcnMvbHludGMvREFUQS9QZXJzb25hbC9PcGVuUEEvb3BlbnBhL3VpL3ZpdGUuY29uZmlnLndlYi50c1wiO2ltcG9ydCB7IGRlZmluZUNvbmZpZywgbG9hZEVudiB9IGZyb20gJ3ZpdGUnXG5pbXBvcnQgdnVlIGZyb20gJ0B2aXRlanMvcGx1Z2luLXZ1ZSdcbmltcG9ydCBJY29ucyBmcm9tICd1bnBsdWdpbi1pY29ucy92aXRlJ1xuaW1wb3J0IEljb25zUmVzb2x2ZXIgZnJvbSAndW5wbHVnaW4taWNvbnMvcmVzb2x2ZXInXG5pbXBvcnQgQ29tcG9uZW50cyBmcm9tICd1bnBsdWdpbi12dWUtY29tcG9uZW50cy92aXRlJ1xuaW1wb3J0IHBrZyBmcm9tICcuL3BhY2thZ2UuanNvbicgd2l0aCB7IHR5cGU6ICdqc29uJyB9XG5cbi8vIFdlYi1vbmx5IFZpdGUgY29uZmlnIChubyBFbGVjdHJvbilcbi8vIGh0dHBzOi8vdml0ZWpzLmRldi9jb25maWcvXG5leHBvcnQgZGVmYXVsdCBkZWZpbmVDb25maWcoKHsgbW9kZSB9KSA9PiB7XG4gIGNvbnN0IGVudiA9IGxvYWRFbnYobW9kZSwgcHJvY2Vzcy5jd2QoKSwgJycpXG5cbiAgcmV0dXJuIHtcbiAgICBiYXNlOiAnLi8nLFxuICAgIGRlZmluZToge1xuICAgICAgX19JU19FTEVDVFJPTl9fOiBmYWxzZSxcbiAgICAgIF9fQVBQX1ZFUlNJT05fXzogSlNPTi5zdHJpbmdpZnkocGtnLnZlcnNpb24pLFxuICAgIH0sXG4gICAgYnVpbGQ6IHtcbiAgICAgIG91dERpcjogJ2Rpc3Qtd2ViJyxcbiAgICB9LFxuICAgIHNlcnZlcjoge1xuICAgICAgaG9zdDogZW52LkhPU1QgfHwgJzAuMC4wLjAnLFxuICAgICAgcG9ydDogcGFyc2VJbnQoZW52LlBPUlQpIHx8IDE1MTUsXG4gICAgfSxcbiAgICBwbHVnaW5zOiBbXG4gICAgICB2dWUoKSxcbiAgICAgIENvbXBvbmVudHMoe1xuICAgICAgICByZXNvbHZlcnM6IFtcbiAgICAgICAgICBJY29uc1Jlc29sdmVyKCksXG4gICAgICAgIF0sXG4gICAgICB9KSxcbiAgICAgIEljb25zKHtcbiAgICAgICAgYXV0b0luc3RhbGw6IHRydWUsXG4gICAgICB9KSxcbiAgICBdLFxuICB9XG59KVxuIiwgIntcclxuICBcIm5hbWVcIjogXCJvcGVucGEtY2xpZW50LXVpXCIsXHJcbiAgXCJwcml2YXRlXCI6IHRydWUsXHJcbiAgXCJ2ZXJzaW9uXCI6IFwiMC4yLjlcIixcclxuICBcInR5cGVcIjogXCJtb2R1bGVcIixcclxuICBcInNjcmlwdHNcIjoge1xyXG4gICAgXCJ2ZXJzaW9uOnN5bmNcIjogXCJweXRob24gLi4vc2NyaXB0cy9zeW5jX3VpX3ZlcnNpb24ucHlcIixcclxuICAgIFwicHJlZGV2XCI6IFwibnBtIHJ1biB2ZXJzaW9uOnN5bmNcIixcclxuICAgIFwicHJlYnVpbGRcIjogXCJucG0gcnVuIHZlcnNpb246c3luY1wiLFxyXG4gICAgXCJwcmVidWlsZDp0ZXN0XCI6IFwibnBtIHJ1biB2ZXJzaW9uOnN5bmNcIixcclxuICAgIFwicHJlYnVpbGQ6cmVuZGVyZXJcIjogXCJucG0gcnVuIHZlcnNpb246c3luY1wiLFxyXG4gICAgXCJwcmV3ZWI6ZGV2XCI6IFwibnBtIHJ1biB2ZXJzaW9uOnN5bmNcIixcclxuICAgIFwicHJld2ViOmJ1aWxkXCI6IFwibnBtIHJ1biB2ZXJzaW9uOnN5bmNcIixcclxuICAgIFwiZGV2XCI6IFwidml0ZVwiLFxyXG4gICAgXCJidWlsZFwiOiBcInZ1ZS10c2MgJiYgdml0ZSBidWlsZCAmJiBlbGVjdHJvbi1idWlsZGVyXCIsXHJcbiAgICBcImJ1aWxkOnRlc3RcIjogXCJ2dWUtdHNjICYmIHZpdGUgYnVpbGQgLS1tb2RlIHRlc3QgJiYgZWxlY3Ryb24tYnVpbGRlciAtLWNvbmZpZyBlbGVjdHJvbi1idWlsZGVyLnRlc3QuanNvbjVcIixcclxuICAgIFwiYnVpbGQ6cmVuZGVyZXJcIjogXCJ2dWUtdHNjICYmIHZpdGUgYnVpbGRcIixcclxuICAgIFwicHJldmlld1wiOiBcInZpdGUgcHJldmlld1wiLFxyXG4gICAgXCJ3ZWI6ZGV2XCI6IFwidml0ZSAtLWNvbmZpZyB2aXRlLmNvbmZpZy53ZWIudHNcIixcclxuICAgIFwid2ViOmJ1aWxkXCI6IFwidnVlLXRzYyAmJiB2aXRlIGJ1aWxkIC0tY29uZmlnIHZpdGUuY29uZmlnLndlYi50cyAmJiB0c3VwIHNlcnZlci9pbmRleC50cyAtLWZvcm1hdCBlc20gLS1leHRlcm5hbCBleHByZXNzIC0tZXh0ZXJuYWwgY29tcHJlc3Npb24gLS1vdXQtZGlyIGRpc3Qtc2VydmVyXCIsXHJcbiAgICBcIndlYjpzdGFydFwiOiBcIm5vZGUgZGlzdC1zZXJ2ZXIvaW5kZXguanNcIlxyXG4gIH0sXHJcbiAgXCJkZXBlbmRlbmNpZXNcIjoge1xyXG4gICAgXCJAYTJhLWpzL3Nka1wiOiBcIl4wLjMuMTBcIixcclxuICAgIFwiQHh0ZXJtL2FkZG9uLWZpdFwiOiBcIl4wLjEwLjBcIixcclxuICAgIFwiQHh0ZXJtL3h0ZXJtXCI6IFwiXjUuNS4wXCIsXHJcbiAgICBcImNvbXByZXNzaW9uXCI6IFwiXjEuNy40XCIsXHJcbiAgICBcImVsZWN0cm9uLXVwZGF0ZXJcIjogXCJeNi4zLjlcIixcclxuICAgIFwiZWxlbWVudC1wbHVzXCI6IFwiXjIuMTMuMVwiLFxyXG4gICAgXCJleHByZXNzXCI6IFwiXjQuMTguMlwiLFxyXG4gICAgXCJoaWdobGlnaHQuanNcIjogXCJeMTEuMTEuMVwiLFxyXG4gICAgXCJtYXJrZWRcIjogXCJeMTcuMC40XCIsXHJcbiAgICBcIm1hcmtlZC1oaWdobGlnaHRcIjogXCJeMi4yLjNcIixcclxuICAgIFwicGluaWFcIjogXCJeMy4wLjRcIixcclxuICAgIFwidnVlXCI6IFwiXjMuNC4yMVwiLFxyXG4gICAgXCJ2dWUtcm91dGVyXCI6IFwiNFwiXHJcbiAgfSxcclxuICBcImRldkRlcGVuZGVuY2llc1wiOiB7XHJcbiAgICBcIkBpY29uaWZ5LWpzb24vbG9nb3NcIjogXCJeMS4yLjEwXCIsXHJcbiAgICBcIkBpY29uaWZ5LWpzb24vbWRpXCI6IFwiXjEuMi4zXCIsXHJcbiAgICBcIkBpY29uaWZ5LWpzb24vdnNjb2RlLWljb25zXCI6IFwiXjEuMi40OFwiLFxyXG4gICAgXCJAaWNvbmlmeS92dWVcIjogXCJeNS4wLjBcIixcclxuICAgIFwiQHR5cGVzL2NvbXByZXNzaW9uXCI6IFwiXjEuNy41XCIsXHJcbiAgICBcIkB0eXBlcy9leHByZXNzXCI6IFwiXjQuMTcuMjFcIixcclxuICAgIFwiQHZpdGVqcy9wbHVnaW4tdnVlXCI6IFwiXjUuMC40XCIsXHJcbiAgICBcImVsZWN0cm9uXCI6IFwiXjMwLjAuMVwiLFxyXG4gICAgXCJlbGVjdHJvbi1idWlsZGVyXCI6IFwiXjI0LjEzLjNcIixcclxuICAgIFwiaHR0cC1wcm94eS1taWRkbGV3YXJlXCI6IFwiXjIuMC42XCIsXHJcbiAgICBcInRzdXBcIjogXCJeOC4wLjFcIixcclxuICAgIFwidHlwZXNjcmlwdFwiOiBcIl41LjIuMlwiLFxyXG4gICAgXCJ1bnBsdWdpbi1pY29uc1wiOiBcIl4yMy4wLjFcIixcclxuICAgIFwidW5wbHVnaW4tdnVlLWNvbXBvbmVudHNcIjogXCJeMzEuMC4wXCIsXHJcbiAgICBcInZpdGVcIjogXCJeNS4xLjZcIixcclxuICAgIFwidml0ZS1wbHVnaW4tZWxlY3Ryb25cIjogXCJeMC4yOC42XCIsXHJcbiAgICBcInZpdGUtcGx1Z2luLWVsZWN0cm9uLXJlbmRlcmVyXCI6IFwiXjAuMTQuNVwiLFxyXG4gICAgXCJ2dWUtdHNjXCI6IFwiXjIuMC4yNlwiXHJcbiAgfSxcclxuICBcIm9wdGlvbmFsRGVwZW5kZW5jaWVzXCI6IHtcclxuICAgIFwiQHJvbGx1cC9yb2xsdXAtbGludXgteDY0LWdudVwiOiBcIl40LjAuMFwiLFxyXG4gICAgXCJAcm9sbHVwL3JvbGx1cC1saW51eC1hcm02NC1nbnVcIjogXCJeNC4wLjBcIixcclxuICAgIFwiQHJvbGx1cC9yb2xsdXAtZGFyd2luLXg2NFwiOiBcIl40LjAuMFwiLFxyXG4gICAgXCJAcm9sbHVwL3JvbGx1cC1kYXJ3aW4tYXJtNjRcIjogXCJeNC4wLjBcIixcclxuICAgIFwiQHJvbGx1cC9yb2xsdXAtd2luMzIteDY0LW1zdmNcIjogXCJeNC4wLjBcIlxyXG4gIH0sXHJcbiAgXCJtYWluXCI6IFwiZGlzdC1lbGVjdHJvbi9tYWluLmpzXCJcclxufVxyXG4iXSwKICAibWFwcGluZ3MiOiAiO0FBQWlWLFNBQVMsY0FBYyxlQUFlO0FBQ3ZYLE9BQU8sU0FBUztBQUNoQixPQUFPLFdBQVc7QUFDbEIsT0FBTyxtQkFBbUI7QUFDMUIsT0FBTyxnQkFBZ0I7OztBQ0p2QjtBQUFBLEVBQ0UsTUFBUTtBQUFBLEVBQ1IsU0FBVztBQUFBLEVBQ1gsU0FBVztBQUFBLEVBQ1gsTUFBUTtBQUFBLEVBQ1IsU0FBVztBQUFBLElBQ1QsZ0JBQWdCO0FBQUEsSUFDaEIsUUFBVTtBQUFBLElBQ1YsVUFBWTtBQUFBLElBQ1osaUJBQWlCO0FBQUEsSUFDakIscUJBQXFCO0FBQUEsSUFDckIsY0FBYztBQUFBLElBQ2QsZ0JBQWdCO0FBQUEsSUFDaEIsS0FBTztBQUFBLElBQ1AsT0FBUztBQUFBLElBQ1QsY0FBYztBQUFBLElBQ2Qsa0JBQWtCO0FBQUEsSUFDbEIsU0FBVztBQUFBLElBQ1gsV0FBVztBQUFBLElBQ1gsYUFBYTtBQUFBLElBQ2IsYUFBYTtBQUFBLEVBQ2Y7QUFBQSxFQUNBLGNBQWdCO0FBQUEsSUFDZCxlQUFlO0FBQUEsSUFDZixvQkFBb0I7QUFBQSxJQUNwQixnQkFBZ0I7QUFBQSxJQUNoQixhQUFlO0FBQUEsSUFDZixvQkFBb0I7QUFBQSxJQUNwQixnQkFBZ0I7QUFBQSxJQUNoQixTQUFXO0FBQUEsSUFDWCxnQkFBZ0I7QUFBQSxJQUNoQixRQUFVO0FBQUEsSUFDVixvQkFBb0I7QUFBQSxJQUNwQixPQUFTO0FBQUEsSUFDVCxLQUFPO0FBQUEsSUFDUCxjQUFjO0FBQUEsRUFDaEI7QUFBQSxFQUNBLGlCQUFtQjtBQUFBLElBQ2pCLHVCQUF1QjtBQUFBLElBQ3ZCLHFCQUFxQjtBQUFBLElBQ3JCLDhCQUE4QjtBQUFBLElBQzlCLGdCQUFnQjtBQUFBLElBQ2hCLHNCQUFzQjtBQUFBLElBQ3RCLGtCQUFrQjtBQUFBLElBQ2xCLHNCQUFzQjtBQUFBLElBQ3RCLFVBQVk7QUFBQSxJQUNaLG9CQUFvQjtBQUFBLElBQ3BCLHlCQUF5QjtBQUFBLElBQ3pCLE1BQVE7QUFBQSxJQUNSLFlBQWM7QUFBQSxJQUNkLGtCQUFrQjtBQUFBLElBQ2xCLDJCQUEyQjtBQUFBLElBQzNCLE1BQVE7QUFBQSxJQUNSLHdCQUF3QjtBQUFBLElBQ3hCLGlDQUFpQztBQUFBLElBQ2pDLFdBQVc7QUFBQSxFQUNiO0FBQUEsRUFDQSxzQkFBd0I7QUFBQSxJQUN0QixnQ0FBZ0M7QUFBQSxJQUNoQyxrQ0FBa0M7QUFBQSxJQUNsQyw2QkFBNkI7QUFBQSxJQUM3QiwrQkFBK0I7QUFBQSxJQUMvQixpQ0FBaUM7QUFBQSxFQUNuQztBQUFBLEVBQ0EsTUFBUTtBQUNWOzs7QUR4REEsSUFBTywwQkFBUSxhQUFhLENBQUMsRUFBRSxLQUFLLE1BQU07QUFDeEMsUUFBTSxNQUFNLFFBQVEsTUFBTSxRQUFRLElBQUksR0FBRyxFQUFFO0FBRTNDLFNBQU87QUFBQSxJQUNMLE1BQU07QUFBQSxJQUNOLFFBQVE7QUFBQSxNQUNOLGlCQUFpQjtBQUFBLE1BQ2pCLGlCQUFpQixLQUFLLFVBQVUsZ0JBQUksT0FBTztBQUFBLElBQzdDO0FBQUEsSUFDQSxPQUFPO0FBQUEsTUFDTCxRQUFRO0FBQUEsSUFDVjtBQUFBLElBQ0EsUUFBUTtBQUFBLE1BQ04sTUFBTSxJQUFJLFFBQVE7QUFBQSxNQUNsQixNQUFNLFNBQVMsSUFBSSxJQUFJLEtBQUs7QUFBQSxJQUM5QjtBQUFBLElBQ0EsU0FBUztBQUFBLE1BQ1AsSUFBSTtBQUFBLE1BQ0osV0FBVztBQUFBLFFBQ1QsV0FBVztBQUFBLFVBQ1QsY0FBYztBQUFBLFFBQ2hCO0FBQUEsTUFDRixDQUFDO0FBQUEsTUFDRCxNQUFNO0FBQUEsUUFDSixhQUFhO0FBQUEsTUFDZixDQUFDO0FBQUEsSUFDSDtBQUFBLEVBQ0Y7QUFDRixDQUFDOyIsCiAgIm5hbWVzIjogW10KfQo=
