export interface BrowserSettings {
  backend: "cloak-manager" | "unsupported";
  connected: boolean;
  license: {
    configured: boolean;
    source: "env" | "manager" | "unset" | "unknown";
    editable: boolean;
  };
  license_tier: "keyless" | "free" | "pro" | "none" | "unknown";
  binary_version: string | null;
  error: string | null;
}

export const KEYLESS_VERSION_REFERENCE = "146.0.7680.177.5";

export function compareBrowserVersion(version: unknown): -1 | 0 | 1 | null {
  if (typeof version !== "string" || version.length > 64 || /[^0-9.]/.test(version) ||
    !/^[0-9]+(?:\.[0-9]+){3,4}$/.test(version)) return null;
  const parts = version.split(".").map(part => BigInt(part));
  if (parts.length === 4) parts.push(0n);
  const reference = KEYLESS_VERSION_REFERENCE.split(".").map(part => BigInt(part));
  for (let index = 0; index < reference.length; index++) {
    if (parts[index] < reference[index]) return -1;
    if (parts[index] > reference[index]) return 1;
  }
  return 0;
}

export function isBrowserSettings(value: unknown): value is BrowserSettings {
  const object = (value: unknown, keys: string[]): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  if (!object(value, ["backend", "connected", "license", "license_tier", "binary_version", "error"]) ||
    !(value.backend === "cloak-manager" || value.backend === "unsupported") ||
    typeof value.connected !== "boolean" ||
    !["keyless", "free", "pro", "none", "unknown"].includes(value.license_tier as string) ||
    !(value.binary_version === null || typeof value.binary_version === "string" &&
      value.binary_version.length <= 64 && !/[^0-9.]/.test(value.binary_version) &&
      /^[0-9]+(?:\.[0-9]+)*$/.test(value.binary_version)) ||
    !(value.error === null || typeof value.error === "string" && value.error.length <= 2048)) return false;
  const license = value.license;
  if (!object(license, ["configured", "source", "editable"]) ||
    typeof license.configured !== "boolean" || typeof license.editable !== "boolean" ||
    !["env", "manager", "unset", "unknown"].includes(license.source as string)) return false;
  if ((license.source === "env" || license.source === "manager") && !license.configured ||
    (license.source === "unset" || license.source === "unknown") && license.configured) return false;
  const editable = value.backend === "cloak-manager" && value.connected && value.error === null &&
    (license.source === "manager" || license.source === "unset");
  if (license.editable !== editable) return false;
  if (value.backend === "unsupported" && (value.connected || license.source !== "unknown" ||
    value.license_tier !== "unknown" || value.binary_version !== null)) return false;
  return true;
}
