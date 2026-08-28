// WEB-01 gates fail closed unless the executing Node runtime exactly matches repository authority.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../..",
);
const expectedNodeVersion = readFileSync(
  resolve(repositoryRoot, ".nvmrc"),
  "utf8",
).trim();

if (process.versions.node !== expectedNodeVersion) {
  process.stderr.write(
    `WEB-01 evidence requires Node ${expectedNodeVersion}; received ${process.versions.node}.\n`,
  );
  process.exit(2);
}
