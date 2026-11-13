import type { AssistantMessage } from "@/types/assistant";

// Selection-local only: never persist tool results or screenshot payloads.
export function createAssistantMessageCache() {
  const entries = new Map<number, { content: string; tool?: string; parsed: ReturnType<typeof JSON.parse>; pretty?: string }>();
  function entry(message: AssistantMessage) {
    const cached = entries.get(message.id);
    if (cached?.content === message.content && cached.tool === message.tool_name) return cached;
    let parsed;
    try { parsed = JSON.parse(message.content); } catch { /* Plaintext tool logs remain readable. */ }
    const next = { content: message.content, tool: message.tool_name, parsed, pretty: undefined as string | undefined };
    entries.delete(message.id);
    entries.set(message.id, next);
    if (entries.size > 200) entries.delete(entries.keys().next().value!);
    return next;
  }
  return {
    parse: (message: AssistantMessage) => entry(message).parsed,
    pretty(message: AssistantMessage): string {
      const cached = entry(message);
      if (cached.pretty !== undefined) return cached.pretty;
      if (message.tool_name === "browser_screenshot") {
        cached.pretty = cached.parsed === undefined ? "截图工具结果格式无效，未显示图像。" : JSON.stringify(Object.fromEntries(
          ["ok", "code", "mime_type", "width", "height", "masked", "image_id", "url_redacted"].filter(key =>
            ["string", "number", "boolean"].includes(typeof cached.parsed?.[key])).map(key => [key, cached.parsed[key]])), null, 2);
      } else cached.pretty = cached.parsed === undefined ? message.content : JSON.stringify(cached.parsed, null, 2);
      return cached.pretty!;
    },
    prune(messages: AssistantMessage[]): void {
      const current = new Map(messages.map(message => [message.id, message]));
      for (const [id, cached] of entries) {
        const message = current.get(id);
        if (!message || message.content !== cached.content || message.tool_name !== cached.tool) entries.delete(id);
      }
    },
    clear: () => entries.clear(),
  };
}
