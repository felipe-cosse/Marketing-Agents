const SOURCE_CHART_VENDOR_SUFFIX =
  /\s*;\s*the source chart names [^.;\r\n]+\.?\s*$/iu;

export function presentPurpose(purpose: string): string {
  const vendorNeutral = purpose.replace(SOURCE_CHART_VENDOR_SUFFIX, "").trim();
  if (vendorNeutral === purpose.trim() || vendorNeutral.length === 0) {
    return purpose;
  }
  return vendorNeutral.endsWith(".") ? vendorNeutral : `${vendorNeutral}.`;
}
