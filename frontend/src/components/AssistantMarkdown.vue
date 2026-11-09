<script setup lang="ts">
import { computed } from "vue";
import { renderAssistantMarkdown } from "@/utils/assistant-markdown";

const props = defineProps<{ content: string }>();
const safeHtml = computed(() => renderAssistantMarkdown(props.content));
</script>

<template>
  <div class="assistant-markdown" v-html="safeHtml" />
</template>

<style scoped>
.assistant-markdown { min-width: 0; max-width: 100%; margin-top: 12px; font-size: 14px; line-height: 1.7; overflow-wrap: anywhere; }
.assistant-markdown :deep(> :first-child) { margin-top: 0; }
.assistant-markdown :deep(> :last-child) { margin-bottom: 0; }
.assistant-markdown :deep(p), .assistant-markdown :deep(ul), .assistant-markdown :deep(ol), .assistant-markdown :deep(blockquote), .assistant-markdown :deep(pre) { margin: 0.75em 0; }
.assistant-markdown :deep(h1), .assistant-markdown :deep(h2), .assistant-markdown :deep(h3), .assistant-markdown :deep(h4), .assistant-markdown :deep(h5), .assistant-markdown :deep(h6) { margin: 1em 0 0.5em; line-height: 1.4; font-weight: 600; }
.assistant-markdown :deep(h1) { font-size: 1.4em; }
.assistant-markdown :deep(h2) { font-size: 1.25em; }
.assistant-markdown :deep(h3), .assistant-markdown :deep(h4), .assistant-markdown :deep(h5), .assistant-markdown :deep(h6) { font-size: 1.1em; }
.assistant-markdown :deep(ul), .assistant-markdown :deep(ol) { padding-left: 1.6em; }
.assistant-markdown :deep(li + li) { margin-top: 0.25em; }
.assistant-markdown :deep(blockquote) { margin-left: 0; padding-left: 12px; border-left: 3px solid #c5cfdb; color: #596c80; }
.assistant-markdown :deep(a) { color: #2f7ed8; text-decoration: underline; text-underline-offset: 2px; }
.assistant-markdown :deep(code) { padding: 0.15em 0.3em; border-radius: 4px; background: #f0f3f7; font-family: ui-monospace, monospace; font-size: 0.9em; white-space: pre-wrap; }
.assistant-markdown :deep(pre) { max-width: 100%; max-height: 440px; overflow: auto; padding: 12px; border-radius: 8px; background: #f0f3f7; white-space: pre-wrap; overflow-wrap: anywhere; }
.assistant-markdown :deep(pre code) { padding: 0; background: transparent; }
.assistant-markdown :deep(table) { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; font-size: 0.95em; }
.assistant-markdown :deep(th), .assistant-markdown :deep(td) { padding: 6px 10px; border: 1px solid #e4e9f0; text-align: left; }
.assistant-markdown :deep(th) { background: #f8fafc; }
.assistant-markdown :deep(hr) { margin: 1em 0; border: 0; border-top: 1px solid #e4e9f0; }
</style>
