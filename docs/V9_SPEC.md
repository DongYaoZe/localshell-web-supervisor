# LWS 0.11 / Schema v9: public identity and provider-neutral surface

## Purpose

LWS 0.11 is the public rebrand of the project as **LocalShell Web Supervisor**. The durable worker, recovery, child-scheduler, and Local Shell takeover semantics from 0.10 remain intact. The release changes the public identity and removes machine/private development details from the current tree without rewriting Git history.

## Public identity

- project: `localshell-web-supervisor`
- short name: `LWS`
- Python package: `lws`
- CLI: `lws`
- local runtime directory: `.lws/`
- environment prefix: `LWS_`

The previous project name remains visible only in Git history. No history rewrite is required for the rebrand.

## Provider boundary

The control plane is described in provider-neutral terms: browser worker, web conversation, web project, delivery lifecycle, Local Shell logical session, and workspace/Git state.

The current browser adapter still targets `chatgpt.com` URL and accessibility behavior. Those provider-specific constants remain where the implementation genuinely depends on them. Generic unit tests use synthetic origins; provider-adapter tests retain the real host shape.

LWS does not copy authentication material or reconstruct private web-service endpoints.

## Schema v9

The current public child-dispatch field is `web_project_url`.

Existing schema-v8 databases may contain the earlier provider-named project URL column. On open, schema v9:

1. creates `web_project_url` when absent;
2. detects the legacy column without exposing it as a public model/CLI name;
3. copies any non-null legacy value into `web_project_url` when the new field is empty;
4. advances `PRAGMA user_version` to 9.

The migration is additive and does not delete legacy database data.

## Privacy/publication cleanup

The current tracked tree must not contain:

- user-specific workspace roots;
- home-directory/user-profile paths;
- live Local Shell session/run/action/spawn identifiers;
- real acceptance Project or Conversation identifiers;
- browser profiles or storage state;
- cookies, tokens, passwords, API credentials, or session secrets;
- `.lws/` runtime databases/logs/observations.

Development-only parallel task briefs are removed from the current tree. Their historical commits remain available in Git history.

## Compatibility decision

0.11 intentionally does not ship a parallel package or CLI compatibility layer under the previous project identity. The current public API uses `lws`. This keeps the earlier identity out of the long-term public surface while Git history remains the migration record.

## Release gates

Before publication:

- full test suite passes;
- editable install and `lws --version` smoke pass;
- `git diff --check` passes;
- current-tree secret scan passes or all matches are manually classified as synthetic fixtures;
- privacy grep finds no user-specific paths or live acceptance IDs;
- `.lws/` remains ignored and untracked;
- GitHub publication uses existing authenticated tooling/session without reading or exporting credentials.
