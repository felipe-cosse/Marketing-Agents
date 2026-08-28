const FORBIDDEN_IDENTITY_HEADER_PREFIXES = [
  "x-actor",
  "x-user",
  "x-role",
  "x-scope",
  "x-principal",
  "x-auth-",
] as const;

const FORBIDDEN_IDENTITY_HEADERS = new Set([
  "remote-user",
  "x-forwarded-user",
  "x-forwarded-email",
  "x-forwarded-actor",
  "x-forwarded-role",
  "x-forwarded-roles",
  "x-forwarded-scope",
  "x-forwarded-scopes",
]);

export interface MutableProxyRequestHeaders {
  getHeaderNames(): string[];
  removeHeader(name: string): void;
}

export function isUntrustedUpstreamHeader(headerName: string): boolean {
  const normalized = headerName.toLowerCase();
  return (
    normalized === "forwarded" ||
    normalized.startsWith("x-forwarded-") ||
    FORBIDDEN_IDENTITY_HEADERS.has(normalized) ||
    FORBIDDEN_IDENTITY_HEADER_PREFIXES.some((prefix) =>
      normalized.startsWith(prefix),
    )
  );
}

export function stripUntrustedUpstreamHeaders(
  proxyRequest: MutableProxyRequestHeaders,
): void {
  for (const header of proxyRequest.getHeaderNames()) {
    if (isUntrustedUpstreamHeader(header)) {
      proxyRequest.removeHeader(header);
    }
  }
}
