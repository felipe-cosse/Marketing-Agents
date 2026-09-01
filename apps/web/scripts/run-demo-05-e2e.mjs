import "./require-pinned-node.mjs";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
function run(name, args, env = process.env) {
  const executable = resolve(root, "node_modules/.bin", name);
  if (!existsSync(executable)) {
    process.stderr.write(
      `DEMO-05 browser evidence requires installed ${name} from make web-bootstrap.\n`,
    );
    process.exit(2);
  }
  const result = spawnSync(executable, args, {
    cwd: root,
    env,
    stdio: "inherit",
  });
  if (result.error) {
    process.stderr.write(
      `DEMO-05 browser evidence could not start ${name}: ${result.error.message}\n`,
    );
    process.exit(2);
  }
  if (result.signal) {
    process.stderr.write(
      `DEMO-05 browser evidence ${name} stopped by signal ${result.signal}.\n`,
    );
    process.exit(2);
  }
  if (result.status !== 0) process.exit(result.status ?? 2);
}
run("tsc", ["-b", "--pretty", "false"]);
run("vite", ["build"]);
run(
  "playwright",
  ["test", "e2e/demo-05-partnership-review.spec.ts"],
  Object.freeze({ ...process.env, PLAYWRIGHT_BROWSERS_PATH: "0" }),
);
