import react from "@vitejs/plugin-react";
import { defineConfig, type ProxyOptions } from "vite";

import { stripUntrustedUpstreamHeaders } from "./config/proxyHeaders.ts";

function apiProxy(): Record<string, string | ProxyOptions> {
  return {
    "/api": {
      target: "http://127.0.0.1:8000",
      changeOrigin: true,
      xfwd: false,
      configure(proxy) {
        proxy.on("proxyReq", (proxyRequest) => {
          stripUntrustedUpstreamHeaders(proxyRequest);
        });
      },
    },
  };
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: apiProxy(),
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
    proxy: apiProxy(),
  },
});
