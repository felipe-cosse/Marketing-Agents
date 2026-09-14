// OBJ-04: observe the installed Corepack shim, never replace its download logic.
import { writeFileSync } from "node:fs";
import { createRequire } from "node:module";

import { installNetworkGuard } from "../../scripts/node-network-guard.mjs";

const [shim, reportPath, expectedCorepackHome, expectedXdgCache, ...forwarded] =
  process.argv.slice(2);
let blockedAttempts = 0;
installNetworkGuard({
  allowLoopback: false,
  onBlocked() {
    // Count even a transport error that Corepack catches or wraps internally.
    blockedAttempts += 1;
  },
});

const networkRefusal = "Network access disabled by the environment";
let outputTail = "";
let corepackRefusedNetwork = false;
let corepackLoaded = false;
// Retain only a bounded detection window, never emit raw Corepack output,
// URLs, environment values or credentials in the test evidence.
function observeOutput(chunk, encoding, callback) {
  const text = outputTail + String(chunk);
  corepackRefusedNetwork ||= text.includes(networkRefusal);
  outputTail = text.slice(-(networkRefusal.length - 1));
  const complete = typeof encoding === "function" ? encoding : callback;
  if (typeof complete === "function") queueMicrotask(complete);
  return true;
}
process.stdout.write = observeOutput;
process.stderr.write = observeOutput;

const credentialVariables = [
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "OPENAI_API_KEY",
  "COREPACK_NPM_TOKEN",
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "NODE_OPTIONS",
];
const flags = {
  network: process.env.COREPACK_ENABLE_NETWORK === "0",
  downloadPrompt: process.env.COREPACK_ENABLE_DOWNLOAD_PROMPT === "0",
  autoPin: process.env.COREPACK_ENABLE_AUTO_PIN === "0",
};
const versionArgumentOnly =
  forwarded.length === 1 && forwarded[0] === "--version";
process.on("exit", (exitCode) => {
  writeFileSync(
    reportPath,
    JSON.stringify({
      exitCode,
      blockedAttempts,
      corepackLoaded,
      corepackRefusedNetwork,
      versionArgumentOnly,
      flags,
      corepackHomePreserved: process.env.COREPACK_HOME === expectedCorepackHome,
      xdgCachePreserved: process.env.XDG_CACHE_HOME === expectedXdgCache,
      credentialVariableCount: credentialVariables.filter(
        (name) => process.env[name] !== undefined,
      ).length,
    }),
    { encoding: "utf8", flag: "wx", mode: 0o600 },
  );
});

if (
  !shim ||
  !reportPath ||
  !expectedCorepackHome ||
  !expectedXdgCache ||
  !versionArgumentOnly
) {
  process.exit(2);
}
// The real pnpm.js calls runMain(['pnpm', ...process.argv.slice(2)]).
// Restore that precise argv shape before evaluating it, after guard installation.
process.argv = [process.execPath, shim, ...forwarded];
createRequire(import.meta.url)(shim);
corepackLoaded = true;
