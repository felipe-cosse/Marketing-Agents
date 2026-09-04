// ARCH-02 build evidence executes the exact installed TypeScript and Vite toolchain.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(
  readFileSync(resolve(webRoot, "package.json"), "utf8"),
);
const expectedPackages = Object.freeze({
  react: manifest.dependencies.react,
  "react-dom": manifest.dependencies["react-dom"],
  "@vitejs/plugin-react": manifest.devDependencies["@vitejs/plugin-react"],
  typescript: manifest.devDependencies.typescript,
  vite: manifest.devDependencies.vite,
});

for (const [packageName, expectedVersion] of Object.entries(expectedPackages)) {
  const installedManifest = resolve(
    webRoot,
    "node_modules",
    packageName,
    "package.json",
  );
  if (!existsSync(installedManifest)) {
    process.stderr.write(
      `ARCH-02 build evidence requires installed ${packageName} from make web-bootstrap.\n`,
    );
    process.exit(2);
  }
  const installed = JSON.parse(readFileSync(installedManifest, "utf8"));
  if (installed.version !== expectedVersion) {
    process.stderr.write(
      `ARCH-02 expected ${packageName} ${expectedVersion}; installed ${String(installed.version)}.\n`,
    );
    process.exit(2);
  }
}

function runLocalTool(name, args) {
  const executable = resolve(webRoot, "node_modules/.bin", name);
  if (!existsSync(executable)) {
    process.stderr.write(
      `ARCH-02 build evidence requires installed ${name} from make web-bootstrap.\n`,
    );
    process.exit(2);
  }
  const result = spawnSync(executable, args, {
    cwd: webRoot,
    env: process.env,
    stdio: "inherit",
  });
  if (result.error !== undefined) {
    process.stderr.write(
      `ARCH-02 build evidence could not start ${name}: ${result.error.message}\n`,
    );
    process.exit(2);
  }
  if (result.signal !== null) {
    process.stderr.write(
      `ARCH-02 build evidence ${name} stopped by signal ${result.signal}.\n`,
    );
    process.exit(2);
  }
  if (result.status !== 0) process.exit(result.status ?? 2);
}

runLocalTool("tsc", ["--version"]);
runLocalTool("vite", ["--version"]);
runLocalTool("tsc", ["-b", "--pretty", "false"]);
runLocalTool("vite", ["build"]);
process.stdout.write(
  `ARCH-02 build evidence passed: React ${expectedPackages.react}, React DOM ${expectedPackages["react-dom"]}, TypeScript ${expectedPackages.typescript}, Vite ${expectedPackages.vite}.\n`,
);
