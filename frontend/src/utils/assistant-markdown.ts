import MarkdownIt from "markdown-it";

const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false });
const validateLink = markdown.validateLink.bind(markdown);
markdown.validateLink = href => {
  if (!validateLink(href) || !/^(https?:\/\/|mailto:)/i.test(href) || /[\u0000-\u0020\u007f]/.test(href)) return false;
  try {
    const url = new URL(href);
    return ["http:", "https:", "mailto:"].includes(url.protocol);
  } catch { return false; }
};
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  tokens[index].attrSet("referrerpolicy", "no-referrer");
  return renderer.renderToken(tokens, index, options);
};
// Never emit image elements, even for otherwise safe URLs: model output must not trigger requests.
markdown.renderer.rules.image = (tokens, index, options, env, renderer) =>
  markdown.utils.escapeHtml(renderer.renderInlineAsText(tokens[index].children ?? [], options, env));

export function renderAssistantMarkdown(content: string): string {
  return markdown.render(content);
}
