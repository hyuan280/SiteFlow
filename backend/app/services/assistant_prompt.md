# Assistant Prompt Resource Manifest

The English runtime prompt lives in `assistant_prompts/*.md`, relative to
`assistant_prompt.py`, not the working directory. This file is maintenance
documentation, not a second copy of the prompt.

`build_system_prompt(*, takeover=False, tool_names=None) -> str` reads resources
on demand. `SYSTEM_PROMPT` is the full ordinary compatibility constant assembled
at import, including the historical Failure Takeover section.

Ordinary assembly order and ownership:

1. `common.md`: purpose, host authority, secret boundaries.
2. `consent.md`: feature discovery, target selection, interactive consent.
3. `browser.md`: URLs, lanes, cookie handling.
4. `evidence.md`: DOM, selectors, frames, images.
5. `steps.md`: single steps, auth limits, recovery diagnostics.
6. `workflow.md`: discovery, authoring, import, full-run lifecycle.
7. `post.md`: standing post policy and bounded approvals.
8. `settings.md`: explicit settings and template changes.
9. `scheduler.md`: scheduler and run-result pagination.
10. `context.md`: historical artifacts and compacted context.
11. `takeover.md`: ordinary compatibility explanation of failure takeover.

Takeover assembly reads `common.md` and `takeover_context.md` only. The latter
contains `<!-- capability:always -->` and explicit tool-name blocks. Always
blocks retain binding, authoring principles, provenance, uncertainty, and post
prohibitions. Tool blocks are included only for names in `tool_names`; omitted
or empty names expose no tool instructions. Permissions remain host-managed.
Never add interactive approvals, settings, scheduler changes, template creation,
full runs, or post execution to takeover blocks.

Keep ordinary wording/order stable for existing consumers. Keep common rules
free of instructions to call tools. Add capability-specific instructions only
under their matching block, without cross-references requiring hidden tools.
Resource names are code-owned; caller input is never a path. Missing selected
resources raise rather than falling back to a partial prompt. There is no
cached per-session policy or authorization state. Tool definitions are separate.
