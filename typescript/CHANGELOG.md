# Changelog

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
