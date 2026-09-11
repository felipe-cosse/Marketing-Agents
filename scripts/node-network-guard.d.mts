export class NetworkAccessBlocked extends Error {
  readonly code: "MARKETING_AGENTS_EXTERNAL_NETWORK_BLOCKED";
}

export function isLoopbackHost(host: unknown): boolean;

export function installNetworkGuard(options?: {
  allowLoopback?: boolean;
  onBlocked?: (error: NetworkAccessBlocked) => void;
}): () => void;
