// ARCH-02 source contract proves the checked-in React, TypeScript, and Vite architecture.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const configRoot = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(configRoot, "..");
const repositoryRoot = resolve(webRoot, "../..");

function read(relativePath: string): string {
  return readFileSync(resolve(webRoot, relativePath), "utf8");
}

describe("ARCH-02 frontend architecture", () => {
  it("ARCH-02 pins the React TypeScript Vite stack in the manifest and frozen lock", () => {
    const manifest = JSON.parse(read("package.json")) as {
      readonly scripts: Record<string, string>;
      readonly dependencies: Record<string, string>;
      readonly devDependencies: Record<string, string>;
    };
    expect(manifest.dependencies).toMatchObject({
      react: "19.2.8",
      "react-dom": "19.2.8",
    });
    expect(manifest.devDependencies).toMatchObject({
      "@vitejs/plugin-react": "6.1.1",
      typescript: "6.0.3",
      vite: "8.2.2",
    });
    expect(manifest.scripts.build).toBe("tsc -b && vite build");

    const lock = readFileSync(
      resolve(repositoryRoot, "pnpm-lock.yaml"),
      "utf8",
    );
    for (const boundary of [
      "react:\n        specifier: 19.2.8\n        version: 19.2.8",
      "react-dom:\n        specifier: 19.2.8\n        version: 19.2.8(react@19.2.8)",
      '"@vitejs/plugin-react":\n        specifier: 6.1.1\n        version: 6.1.1(vite@8.2.2',
      "typescript:\n        specifier: 6.0.3\n        version: 6.0.3",
      "vite:\n        specifier: 8.2.2\n        version: 8.2.2",
    ]) {
      expect(lock).toContain(boundary);
    }
  });

  it("ARCH-02 wires strict React JSX through the Vite production entry", () => {
    const config = JSON.parse(read("tsconfig.app.json")) as {
      readonly compilerOptions: Record<string, unknown>;
    };
    expect(config.compilerOptions).toMatchObject({
      strict: true,
      noUncheckedIndexedAccess: true,
      exactOptionalPropertyTypes: true,
      isolatedModules: true,
      noEmit: true,
      jsx: "react-jsx",
    });

    const vite = read("vite.config.ts");
    expect(vite).toMatch(/import react from "@vitejs\/plugin-react"/u);
    expect(vite).toMatch(/plugins:\s*\[react\(\)\]/u);

    const entry = read("src/main.tsx");
    expect(entry).toContain('import { StrictMode } from "react";');
    expect(entry).toContain('import { createRoot } from "react-dom/client";');
    expect(entry).toMatch(/createRoot\(root\)\.render\(\s*<StrictMode>/u);
  });
});
