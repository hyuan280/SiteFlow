import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse } from "@vue/compiler-dom";

const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, hmr: false } });
const originalFetch = globalThis.fetch;
let requests = 0;
globalThis.fetch = () => { requests++; throw new Error("Markdown must not fetch resources"); };
try {
  const { default: Markdown } = await server.ssrLoadModule("/src/components/AssistantMarkdown.vue");
  const render = content => renderToString(createSSRApp({ render: () => h(Markdown, { content }) }));
  const html = await render('# 标题\n\n段落 **粗体** 与 *强调*。\n\n- 一\n- 二\n\n1. 步骤\n\n> 引用\n\n| 列 | 值 |\n| --- | --- |\n| A | B |\n\n`<img onerror=alert(1)>`\n\n```html\n<script>alert("x")</script>\n```');
  for (const tag of ["h1", "p", "strong", "em", "ul", "ol", "li", "blockquote", "table", "th", "td", "pre", "code"]) {
    assert.match(html, new RegExp(`<${tag}(?:>| )`));
  }
  assert.match(html, /<code>&lt;img onerror=alert\(1\)&gt;<\/code>/);
  assert.match(html, /<pre><code class="language-html">&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script|<img/);

  for (const href of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "jav&#x61;script:alert(1)", "javascript&#58;alert(1)",
    "java&#x09;script:alert(1)", "java&#10;script:alert(1)", "&#106;avascript:alert(1)", "javascript%3Aalert(1)",
    "%6aavascript:alert(1)", "data:text/html,evil", "data:image/png;base64,AAAA", "vbscript:evil", "file:///etc/passwd",
    "ftp://example.com", "//example.com/path", "/local", "#anchor"]) {
    const result = await render(`[unsafe](<${href}>)`);
    assert.doesNotMatch(result, /<a\b|\shref=/, href);
  }
  for (const href of ["https://example.com/path?q=1&b=2", "http://example.com", "mailto:hello@example.com"]) {
    const result = await render(`[安全](<${href}>)`);
    assert.match(result, /<a href="(?:https?:\/\/|mailto:)/);
    assert.match(result, /target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer"/);
  }
  const hostile = await render('<script>alert(1)</script>\n\n<img src="https://tracker.invalid/raw" onerror="alert(1)">\n\n<svg onload="alert(1)"><a href="javascript:evil">x</a></svg>\n\n<iframe src="https://tracker.invalid"></iframe>\n\n[link](https://example.com/ "\\\" onclick=\\\"evil")\n\n```\" onmouseover=\"evil\ncode\n```');
  assert.doesNotMatch(hostile, /<(?:script|img|svg|iframe)\b/i);
  const checkAttributes = node => {
    for (const prop of node.props ?? []) assert.doesNotMatch(prop.name, /^on/i, "no actual event-handler attributes");
    for (const child of node.children ?? []) checkAttributes(child);
  };
  checkAttributes(parse(hostile));
  assert.match(hostile, /title="&quot; onclick=&quot;evil"/, "quotes in link titles remain escaped inside the attribute");
  assert.match(hostile, /&lt;script&gt;/);

  for (const image of ['![说明](https://tracker.invalid/pixel "title")', '![说明](/local.png)',
    '![说明](data:image/png;base64,AAAA)', '![说明](javascript:evil)',
    '![**粗体** `<img>`](https://tracker.invalid/pixel)',
    '![说明][pixel]\n\n[pixel]: https://tracker.invalid/pixel',
    '[![说明](https://tracker.invalid/pixel)](https://example.com)']) {
    const result = await render(image);
    assert.doesNotMatch(result, /<(?:img|source|video|audio|iframe|object|embed)\b|\s(?:src|srcset)=/i);
    assert.match(result, /说明|粗体/);
  }
  const altHtml = await render('![<img src=x onerror=evil>](https://tracker.invalid)');
  assert.match(altHtml, /<p>&lt;img src=x onerror=evil&gt;<\/p>/);
  assert.doesNotMatch(altHtml, /<img\b/);
  assert.equal(requests, 0, "rendering never requests model-supplied images");

  // HTML insertion belongs only to the isolated renderer, never the view or raw model content.
  const root = new URL("../src/", import.meta.url);
  const sinks = [];
  for (const entry of await readdir(root, { recursive: true })) {
    if (!entry.endsWith(".vue")) continue;
    const source = await readFile(new URL(entry, root), "utf8");
    if (/v-html|innerHTML/.test(source)) sinks.push(entry);
  }
  assert.deepEqual(sinks, ["components/AssistantMarkdown.vue"]);
  const component = await readFile(new URL("../src/components/AssistantMarkdown.vue", import.meta.url), "utf8");
  assert.match(component, /v-html="safeHtml"/);
  assert.match(component, /safeHtml = computed\(\(\) => renderAssistantMarkdown\(props.content\)\)/);
  console.log("Assistant Markdown SSR and safety regression passed");
} finally {
  globalThis.fetch = originalFetch;
  await server.close();
}
