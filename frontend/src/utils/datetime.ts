export function parseServerDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  let text = value.trim();
  if (!text) return null;
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(text)) {
    text += "Z";
  }
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatServerDateTime(
  value: string | null | undefined,
  timeZone = "Asia/Shanghai",
  fallback = "-",
): string {
  if (!value) return fallback;
  const date = parseServerDate(value);
  if (!date) return value;
  try {
    return date.toLocaleString("zh-CN", { timeZone });
  } catch {
    return date.toLocaleString();
  }
}
