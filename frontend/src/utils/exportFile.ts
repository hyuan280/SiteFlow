export type ImportRecord = Record<string, unknown>;

export function parseExportFile(
  text: string,
  kind: "templates" | "sites",
  label: string,
): ImportRecord[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (cause) {
    throw new Error(`JSON 解析失败：${cause instanceof Error ? cause.message : "格式错误"}`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("文件内容必须是 JSON 对象");
  }
  const data = parsed as Record<string, unknown>;
  if (data.kind !== kind) {
    throw new Error(`文件类型不是${label}配置`);
  }
  const rows = data[kind];
  if (!Array.isArray(rows)) {
    throw new Error(`文件中缺少 ${kind} 数组`);
  }
  return rows.map((row, index) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) {
      throw new Error(`第 ${index + 1} 项格式无效`);
    }
    return row as ImportRecord;
  });
}

export function importText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function importArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
