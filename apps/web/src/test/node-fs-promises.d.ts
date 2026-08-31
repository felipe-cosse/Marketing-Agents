declare module "node:fs/promises" {
  export function readFile(path: URL, encoding: "utf8"): Promise<string>;
}
