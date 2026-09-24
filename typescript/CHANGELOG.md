# Changelog

## Unreleased

- **New: `Spaces` class (`client.spaces.create({ name })`).** Didn't exist
  in this SDK before at all — the Python client already had it.
- **New: `client.useSpace(name)` / `client.currentSpace`.** Scope every
  later call to one space by name instead of whatever the token's default
  space resolves to — validated against a fresh `me()` call every time, so
  an unknown name throws `SpaceNotFoundError` rather than silently binding.
  Opt-in and fully backward-compatible: skip it and nothing changes, no
  `X-Space` header is ever sent.
- **New: client-side input validation everywhere.** Every method that
  creates or changes something (`functions.create`, `spaces.create`,
  `datasets.create`/`addRow`, `eval.scores.create`, `eval.experiments.create`,
  …) now validates required fields, ids, and — for `functions.create` /
  `spaces.create` — slug/name uniqueness, throwing before any request goes
  out.
- **`createFromYaml`/`createFromYamlText` now skip a no-op re-import.**
  Re-pushing the exact same document you last pulled (ignoring `meta`,
  which is re-stamped fresh on every pull) returns the existing version
  instead of minting a duplicate. A genuine edit still creates a new
  version exactly as before — versions stay immutable history either way,
  this only avoids wasting a version number on an unchanged file.

## 0.3.0 — 2026-08-26

- **New: `parsePromptFile(path)`.** Reads a `.yaml`/`.yml`/`.json` prompt
  file — the same format the app's canvas Import button reads — and returns
  `{sections, input_schema?, output_schema?}`. Read-and-validate only: no
  network call, nothing created. Throws `PromptFileError` naming exactly
  what's wrong if the file is missing something required or malformed.
  `.json` needs no extra package; `.yaml`/`.yml` needs `js-yaml` installed
  separately (`npm install js-yaml`) — this SDK stays zero-dependency by
  default either way.
- **New: `createFunctionFromDoc(client, { slug, name, doc, ... })`.** Takes
  the object `parsePromptFile()` returned and creates the function + its
  first version. This is the step that talks to the API — the two are
  deliberately separate so you can inspect or edit the parsed data first.
- **New: `createFunction(client, { slug, name, sections, inputSchema?, outputSchema?, ... })`.**
  The manual counterpart — same result, but you supply the sections/schemas
  directly instead of loading them from a file.

## 0.2.0 — 2026-08-19

- Initial `Client` management surface, mirroring the Python SDK
  namespace-for-namespace.
