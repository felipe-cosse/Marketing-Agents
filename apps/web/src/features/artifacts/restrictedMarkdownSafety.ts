export const RESTRICTED_MARKDOWN_LIMITS = Object.freeze({
  maxBlocks: 128,
  maxInlinePartsPerBlock: 64,
  maxLinkLength: 2_048,
});

const HTTP_URL = /^https?:\/\//iu;

function hasControlCharacter(value: string): boolean {
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    if (codePoint !== undefined && (codePoint <= 0x1f || codePoint === 0x7f)) {
      return true;
    }
  }
  return false;
}

export function safeArtifactLinkHref(candidate: string): string | null {
  if (
    candidate.length === 0 ||
    candidate.length > RESTRICTED_MARKDOWN_LIMITS.maxLinkLength ||
    candidate.trim() !== candidate ||
    hasControlCharacter(candidate) ||
    !HTTP_URL.test(candidate)
  ) {
    return null;
  }

  try {
    const url = new URL(candidate);
    if (
      (url.protocol !== "http:" && url.protocol !== "https:") ||
      url.hostname.length === 0 ||
      url.username.length > 0 ||
      url.password.length > 0
    ) {
      return null;
    }
    return url.href;
  } catch {
    return null;
  }
}
