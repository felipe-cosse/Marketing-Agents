function pointerToken(pointer: string): string {
  return encodeURIComponent(pointer).replaceAll("%", "_");
}

export function fieldIdForPointer(formId: string, pointer: string): string {
  return `${formId}-field-${pointerToken(pointer)}`;
}

export function fieldErrorIdForPointer(
  formId: string,
  pointer: string,
): string {
  return `${fieldIdForPointer(formId, pointer)}-error`;
}

export function fieldDescriptionIdForPointer(
  formId: string,
  pointer: string,
): string {
  return `${fieldIdForPointer(formId, pointer)}-description`;
}

export function fieldHelpIdForPointer(formId: string, pointer: string): string {
  return `${fieldIdForPointer(formId, pointer)}-help`;
}

export function fieldUriWarningIdForPointer(
  formId: string,
  pointer: string,
): string {
  return `${fieldIdForPointer(formId, pointer)}-uri-warning`;
}

export function fieldSensitiveNoteIdForPointer(
  formId: string,
  pointer: string,
): string {
  return `${fieldIdForPointer(formId, pointer)}-sensitive-note`;
}

export function escapeJsonPointerSegment(segment: string): string {
  return segment.replaceAll("~", "~0").replaceAll("/", "~1");
}
