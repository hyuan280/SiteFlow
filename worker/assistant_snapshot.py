"""Fixed, read-only DOM collection and bounded immutable snapshot projection.

Text is direct child text, not aggregated innerText. Inspect includes its root
and only descendants captured in the original bounded scan; it never retargets
a selector or expands into a newer DOM. Hidden elements are retained.

Worker API (all POST bodies include the host token):
observe {offset=0, limit=100, frame_id?}: zero creates a new snapshot; nonzero
continues the latest snapshot for that frame. For stable concurrent pagination,
inspect the document root using its explicit snapshot_id instead.
inspect {snapshot_id, node_id, offset=0, limit=100}: immutable, root-inclusive.
Both return session_id, lane_key, expires_at, projected page URL metadata,
snapshot_id, frame_id, elements, offset, next_offset, total, truncated, frames,
frames_truncated, limits. Node IDs are scoped to (snapshot_id, frame_id).
truncated means collection/metadata limits; next_offset means more captured
nodes remain. Selectors are proven at capture time, not on cached-page reads.
check-selector {selector, frame_id?}: returns session/URL metadata, frame_id,
selector, match_count, visible_count, matches, truncated. Counts are capped at
10000; samples at 10. Truncated also signals omitted samples. CSS only, with
explicit ' >>> ' segments traversing open shadow roots. No iframe auto-piercing.
URL metadata is url, url_redacted, redacted_parameters; URL attributes contain
the projected URL string. Unreadable frame reasons are static browser_* codes.
Stale/evicted snapshot or unknown snapshot node: 409 browser_snapshot_stale.
Invalid CSS: 422 browser_selector_invalid. Request validation never echoes input.
Known unreadable frame targets: 403 browser_frame_unreadable; detailed reasons
remain in observation frame metadata. Generated selectors use DOM paths only,
so raw identifiers are never CSS-escaped into output before fingerprint checks.
Raw text/attributes up to 4096 characters are checked in Python before output
truncation; larger fields are omitted whole. Fingerprints stay in the worker.
"""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException

from worker.assistant_urls import project_url

MAX_SNAPSHOT_BYTES = 32 * 1024
MAX_NODES = 5000
MAX_CACHE_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOTS = 4
MAX_FRAMES = 64
MAX_FRAME_BYTES = 12 * 1024
MAX_NODE_BYTES = 8 * 1024
MAX_RAW_FIELD_CHARS = 4096
LIMITS = {
    "page_bytes": MAX_SNAPSHOT_BYTES, "page_nodes": 100,
    "scan_nodes": MAX_NODES, "scan_depth": 64, "snapshot_bytes": MAX_CACHE_BYTES,
    "retained_snapshots": MAX_SNAPSHOTS, "text_chars": 300,
    "attributes_per_node": 24, "attribute_chars": 512, "selector_chars": 2048,
    "url_attribute_chars": 4096, "selector_bytes": 4096,
    "frames": MAX_FRAMES, "frame_bytes": MAX_FRAME_BYTES,
    "node_bytes": MAX_NODE_BYTES,
    "raw_field_chars": MAX_RAW_FIELD_CHARS,
    "selector_matches": 10000, "selector_samples": 10,
    "inspect_root_included": True, "inspect_scope": "captured_subtree",
}
SENSITIVE_KEY = re.compile(
    r"token|password|passwd|secret|auth|nonce|cookie|storage|session|credential|csrf|xsrf|apikey|signature",
    re.I,
)
SENSITIVE_META = re.compile(SENSITIVE_KEY.pattern + r"|security", re.I)
URL_ATTRIBUTES = {"href", "src", "action", "formaction", "poster", "cite", "background", "longdesc", "xlink:href"}
URL_KEY = re.compile(r"(?:^|[-_:])(?:url|href|src|action)$", re.I)
OMIT_ATTRIBUTES = {"style", "srcdoc", "srcset", "imagesrcset", "ping", "selected", "checked"}
NO_TEXT = {"input", "textarea", "select", "option", "script", "style", "template", "noscript"}

# All caller input is passed as data. No eval, page-world functions, field value
# properties, cookie/storage reads, event dispatch, or DOM writes are used here.
SNAPSHOT_SCRIPT = r"""function(args) {
  if (args?.expected_url && location.href !== args.expected_url) return {stale:true};
  const MAX = args?.max_nodes || 5000, DEPTH = 64;
  const sensitive = /token|password|passwd|secret|auth|nonce|cookie|storage|session|credential|csrf|xsrf|apikey|signature/i;
  const urls = new Set(['href','src','action','formaction','poster','cite','background','longdesc','xlink:href']);
  const omit = new Set(['style','srcdoc','srcset','imagesrcset','ping','selected','checked']);
  const noText = new Set(['input','textarea','select','option','script','style','template','noscript']);
  const parent = el => el.parentElement || el.getRootNode()?.host || null;
  const visible = el => {
    for (let n = el; n; n = parent(n)) {
      const s = getComputedStyle(n);
      if (n.hidden || n.getAttribute('aria-hidden') === 'true' ||
          s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse' || Number(s.opacity) === 0) return false;
    }
    return el.getClientRects().length > 0;
  };
  // The separator is recognized only outside CSS strings/brackets/escapes.
  const split = selector => {
    let parts = [], start = 0, quote = '', bracket = 0, paren = 0;
    for (let i = 0; i < selector.length; i++) {
      const c = selector[i];
      if (c === '\\') { i++; continue; }
      if (quote) { if (c === quote) quote = ''; continue; }
      if (c === '"' || c === "'") { quote = c; continue; }
      if (c === '[') bracket++; else if (c === ']') bracket--;
      if (c === '(') paren++; else if (c === ')') paren--;
      if (!bracket && !paren && selector.slice(i,i+3) === '>>>') {
        parts.push(selector.slice(start,i).trim()); start = i + 3; i += 2;
      }
    }
    parts.push(selector.slice(start).trim());
    if (parts.some(p => !p) || parts.length > 32) throw new Error('invalid');
    return parts;
  };
  const match = selector => {
    let roots = [document], found = [], truncated = false;
    const parts = split(selector);
    for (let i = 0; i < parts.length; i++) {
      if (!roots.length) document.createDocumentFragment().querySelector(parts[i]);
      found = [];
      const seen = new Set();
      for (const root of roots) {
        for (const el of root.querySelectorAll(parts[i])) {
          if (seen.has(el)) continue;
          seen.add(el);
          if (found.length === 10000) { truncated = true; break; }
          found.push(el);
        }
        if (truncated) break;
      }
      if (i < parts.length - 1) roots = found.map(el => el.shadowRoot).filter(Boolean);
    }
    return {found, truncated};
  };
  const pathCache = new WeakMap();
  const pathFor = el => {
    if (pathCache.has(el)) return pathCache.get(el);
    const root = el.getRootNode(), prefix = root.host ? pathFor(root.host) + ' >>> ' : '';
    const parts = [];
    for (let n = el; n; n = n.parentElement) {
      let index = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling)
        if (s.localName === n.localName && s.namespaceURI === n.namespaceURI) index++;
      parts.unshift(CSS.escape(n.localName) + ':nth-of-type(' + index + ')');
      if (parts.length > DEPTH + 1) return '';
    }
    const path = prefix + parts.join(' > ');
    pathCache.set(el,path); return path;
  };
  const selectorFor = el => {
    // Never encode raw IDs/names into selectors before Python's secret checks.
    const selector = pathFor(el);
    if (!selector || selector.length > 2048) return {};
    try {
      const {found, truncated} = match(selector);
      if (!truncated && found.length === 1 && found[0] === el)
        return {selector, selector_matches:1, selector_source:'dom_path'};
    } catch {}
    return {};
  };
  const nodeFor = (el, id, parent_id, blocked) => {
    const tag = el.localName, attributes = {};
    const privateMeta = tag === 'meta' && ['name','property'].some(key =>
      /token|password|passwd|secret|auth|nonce|cookie|storage|session|credential|csrf|xsrf|apikey|signature|security/i
        .test((el.getAttribute(key) || '').replace(/[^a-z0-9]/gi,'')));
    for (const attr of el.attributes) {
      const key = attr.name.toLowerCase();
      if (key.length > 128 || key.startsWith('on') || /value/i.test(key) ||
          sensitive.test(key.replace(/[^a-z0-9]/g,'')) || omit.has(key)) continue;
      if (privateMeta && key === 'content') continue;
      if (Object.keys(attributes).length >= 24) break;
      let value = attr.value;
      if (value.length > 4096) continue;
      if (urls.has(key) || (tag === 'object' && key === 'data') ||
          /(?:^|[-_:])(?:url|href|src|action)$/i.test(key) || /^https?:\/\//i.test(value)) {
        try { value = new URL(value, document.baseURI).href; } catch { continue; }
        if (!/^https?:\/\//i.test(value)) continue;
        if (value.length > 4096) continue;
      }
      attributes[key] = value;
    }
    const item = {node_id:id, parent_id, tag, visible:visible(el), attributes, ...selectorFor(el)};
    if (el.shadowRoot) item.has_shadow_root = true;
    if (!blocked && !noText.has(tag) && !el.isContentEditable) {
      let text = '', scanned = 0, overflow = false, child = el.firstChild;
      for (; child && scanned++ < 100; child = child.nextSibling) {
        if (child.nodeType !== 3) continue;
        const part = child.textContent;
        if (part.length > 4096 || text.length + part.length > 4096) { overflow = true; break; }
        text += part;
      }
      if (overflow || child) text = '';
      if (text) item.text = text;
    }
    return item;
  };
  if (args?.mode === 'check') {
    try {
      const {found, truncated} = match(args.selector);
      const matches = found.slice(0,10).map((el,i) => {
        let blocked = false;
        for (let n = el; n; n = parent(n)) if (noText.has(n.localName) || n.isContentEditable) blocked = true;
        return nodeFor(el,'m'+i,null,blocked);
      });
      return {matches, match_count:found.length, visible_count:found.filter(visible).length, truncated};
    } catch { return {invalid_selector:true}; }
  }
  const elements = [], stack = [];
  if (document.documentElement) stack.push([document.documentElement,null,0,false]);
  let truncated = false, bytes = 0;
  while (stack.length && elements.length < MAX) {
    const [el,parent_id,depth,inherited] = stack.pop();
    const blocked = inherited || noText.has(el.localName) || el.isContentEditable;
    const id = 'n'+elements.length, item = nodeFor(el,id,parent_id,blocked);
    bytes += JSON.stringify(item).length * 6;
    if (bytes > 2*1024*1024) { truncated = true; break; }
    elements.push(item);
    if (depth >= DEPTH) {
      if (el.children.length || el.shadowRoot?.children.length) truncated = true;
      continue;
    }
    // Linked sibling iteration avoids materializing arbitrarily wide subtrees.
    // Pending work is capped too, independently of the emitted node count.
    const children = [];
    for (const root of [el,el.shadowRoot,el.localName === 'template' ? el.content : null].filter(Boolean)) {
      for (let child = root.firstElementChild; child; child = child.nextElementSibling) {
        if (children.length + stack.length >= MAX) { truncated = true; break; }
        children.push([child,id,depth+1,blocked]);
      }
    }
    for (let i=children.length-1;i>=0;i--) stack.push(children[i]);
  }
  return {elements, truncated:truncated || stack.length > 0};
}"""


def _snapshot(raw: Any, frame_id: str = "main", known: set | None = None) -> dict[str, Any]:
    """Defensive projection; only fixed-script selector proofs are accepted."""
    from worker.assistant_step import secret_in_url

    known = known or set()
    result: dict[str, Any] = {"elements": [], "truncated": False}
    if not isinstance(raw, dict) or not isinstance(raw.get("elements"), list):
        return result
    result["truncated"] = raw.get("truncated") is True or len(raw["elements"]) > MAX_NODES
    size = 0
    seen = set()
    for source in raw["elements"][:MAX_NODES]:
        if not isinstance(source, dict):
            continue
        tag, node_id = source.get("tag"), source.get("node_id")
        if (not isinstance(tag, str) or not 1 <= len(tag) <= 128
                or not isinstance(node_id, str) or not re.fullmatch(r"[nm][0-9]{1,5}", node_id)
                or node_id in seen):
            continue
        parent_id = source.get("parent_id")
        item = {"node_id": node_id, "parent_id": parent_id if parent_id in seen else None,
                "frame_id": frame_id, "tag": tag, "visible": source.get("visible") is True,
                "attributes": {}}
        text = source.get("text")
        if tag not in NO_TEXT and isinstance(text, str) and len(text) <= MAX_RAW_FIELD_CHARS:
            item["text"] = "[redacted]" if secret_in_url(text, known) else " ".join(text.split())[:300]
        attrs = source.get("attributes")
        private_identifier = isinstance(attrs, dict) and any(
            isinstance(attrs.get(key), str) and secret_in_url(attrs[key], known) for key in ("id", "name"))
        if (isinstance(source.get("selector"), str) and len(source["selector"]) <= 2048
                and len(json.dumps(source["selector"], ensure_ascii=True).encode()) <= 4096
                and not secret_in_url(source["selector"], known)
                and not (private_identifier and source.get("selector_source") in {"id", "name"})
                and source.get("selector_matches") == 1
                and source.get("selector_source") in {"id", "name", "dom_path"}):
            item.update(selector=source["selector"], selector_matches=1,
                        selector_source=source["selector_source"])
        if source.get("has_shadow_root") is True:
            item["has_shadow_root"] = True
        if isinstance(attrs, dict):
            private_meta = tag == "meta" and any(
                isinstance(attrs.get(key), str)
                and SENSITIVE_META.search(re.sub(r"[^a-z0-9]", "", attrs[key].lower()))
                for key in ("name", "property"))
            for key, value in attrs.items():
                if (not isinstance(key, str) or not isinstance(value, str) or len(key) > 128
                        or len(value) > MAX_RAW_FIELD_CHARS or (private_meta and key.lower() == "content")
                        or key.lower().startswith("on") or "value" in key.lower()
                        or key.lower() in OMIT_ATTRIBUTES
                        or SENSITIVE_KEY.search(re.sub(r"[^a-z0-9]", "", key.lower()))):
                    continue
                if len(item["attributes"]) >= 24:
                    break
                if secret_in_url(value, known):
                    value = "[redacted]"
                elif (key.lower() in URL_ATTRIBUTES or (tag == "object" and key.lower() == "data") or URL_KEY.search(key)
                        or re.match(r"https?://", value, re.I)):
                    try:
                        value = project_url(value)["url"]
                    except ValueError:
                        continue
                else:
                    value = value[:512]
                item["attributes"][key] = value
                if len(json.dumps(item, ensure_ascii=True).encode()) > MAX_NODE_BYTES:
                    del item["attributes"][key]
        size += len(json.dumps(item, ensure_ascii=True).encode())
        if size > MAX_CACHE_BYTES:
            result["truncated"] = True
            break
        seen.add(node_id)
        result["elements"].append(item)
    return result


def paginate(metadata: dict, nodes: list[dict], offset: int, limit: int, truncated: bool,
             max_bytes: int = MAX_SNAPSHOT_BYTES) -> dict:
    """Fit the complete JSON envelope, not just nodes, within the wire budget."""
    result = {**metadata, "elements": [], "offset": offset, "next_offset": None,
              "total": len(nodes), "truncated": truncated, "limits": dict(LIMITS)}
    result["limits"]["page_bytes"] = max_bytes
    for node in nodes[offset:offset + limit]:
        result["elements"].append(node)
        result["next_offset"] = offset + len(result["elements"])
        if len(json.dumps(result, ensure_ascii=True).encode()) > max_bytes:
            result["elements"].pop()
            break
    end = offset + len(result["elements"])
    result["next_offset"] = end if end < len(nodes) else None
    if end < len(nodes) and not result["elements"]:
        raise HTTPException(502, {"code": "browser_snapshot_limit"})
    if len(json.dumps(result, ensure_ascii=True).encode()) > max_bytes:
        raise HTTPException(502, {"code": "browser_snapshot_limit"})
    return result
