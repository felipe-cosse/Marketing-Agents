import http from "node:http";
import { vi } from "vitest";

// These delegates make the intentionally failing fixture safe even if the
// guard is deleted: no canary can fall through to a real network implementation.
vi.spyOn(http, "get").mockImplementation(() => {
  throw new Error("in-memory HTTP tripwire");
});
vi.stubGlobal("fetch", () => {
  throw new Error("in-memory fetch tripwire");
});
