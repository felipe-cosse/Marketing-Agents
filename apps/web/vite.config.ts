import react from "@vitejs/plugin-react";
import { defineConfig, type ProxyOptions } from "vite";

import { stripUntrustedUpstreamHeaders } from "./config/proxyHeaders.ts";

function apiProxy(): Record<string, string | ProxyOptions> {
  const port = process.env.MARKETING_AGENTS_NATIVE_API_PORT ?? "8000";
  if (!/^\d{4,5}$/.test(port) || Number(port) < 1024 || Number(port) > 65535) {
    throw new Error(
      "MARKETING_AGENTS_NATIVE_API_PORT must be a local unprivileged port",
    );
  }
  return {
    "/api": {
      target: `http://127.0.0.1:${port}`,
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
