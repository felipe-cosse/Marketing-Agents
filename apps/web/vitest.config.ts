import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    // AC-15: isolated threads avoid fork SIGTERM shutdown races under syscall
    // tracing. Shared configuration also covers the real denial-canary runner.
    pool: "threads",
    maxWorkers: 1,
    isolate: true,
    environment: "jsdom",
    globals: true,
    include: ["config/**/*.test.ts", "src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/main.tsx", "src/test/**"],
    },
  },
});
