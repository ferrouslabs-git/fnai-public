# Space keys — one key for your servers and automated checks

A **function key** (`fnk_…`) belongs to one function. A **personal token** (`fnp_…`) belongs to one person. A **space key**
(`fns_…`) belongs to a **space**: one key for every function in it, for servers and CI, doing only what you ticked.

## Making one

In the app: open the space (the Workbench list) → **Space keys** → name it, tick what it may do → **Create key**. The key is
shown **once**; copy it then. Each key lists what it may do and when it was last used. **Revoke** stops it on its very next
request. Anyone who can manage keys (the `keys.write` permission, i.e. space admins) can create and revoke; everyone can look.

Over the API: `POST /api/space-keys {"name": "production servers", "scopes": ["prompts.read", "traces.write"]}`,
`GET /api/space-keys`, `POST /api/space-keys/{id}/revoke`, and `GET /api/space-keys/scopes` for the list below.

## What a key may do

| Permission | What it lets the key do |
|---|---|
| `prompts.read` | Fetch the live (or a named) prompt version of any function in the space (`GET /v1/prompts/{function}`). |
| `traces.write` | Report calls that ran on your own servers (`POST /v1/traces`, naming the function). |
| `functions.run` | Call any deployed function in the space (`POST /v1/run/{function}`). |
| `functions.read` | List the space's functions and read their details and versions. |
| `evals.run` | Create and run experiments run by your own code, and read their results (`fnai.eval`, the user-run routes). |
| `datasets.manage` | Create datasets, add rows, upload, take snapshots. |
| `functions.create` | Create new functions. |
| `versions.save` | Save new prompt versions into a function. |

Deploying, rolling back, deleting, members, billing and keys are **never** available to a space key, whatever it is given.
The routes a key can reach are an explicit list (`backend/app/keys/space_keys.py`, `ALLOWED_ROUTES`); a route nobody listed
is refused, and a test checks every other route.

## Using one

Send it as `Authorization: Bearer fns_…`, the same as the other keys. Rules:

- It only sees **its own space**. A function in another space is "not found"; a header naming another space is refused.
- It names the function itself, because it covers many: the slug in the URL for prompts and runs, `"function": "slug"`
  (or `function_id`) in a trace body.
- Python: `fnai.init_tracing(api_key="fns_…", function="my_function")`, `fnai.get_prompt("my_function")`,
  `fnai.eval("my_function", token="fns_…", …)`. TypeScript: `initTracing({ apiKey, function })`, `getPrompt`, `evaluate(…, { token })`.
  Go: `NewFunction(slug, key)` works as with a function key; for a trace, include `"function": "<slug>"`.
- An experiment started with a space key must be `mode: "user_run"`; it cannot start a hosted one.
- Actions it takes are recorded as `space-key:<name>`, not as a person.
- Calls made through it use the same rate limits and show the same records as any other call.

## Good defaults

- Servers: `prompts.read`, `traces.write`, `functions.run`.
- Automated testing: `evals.run`, `datasets.manage`, `functions.read`.
- Build scripts: add `functions.create` and `versions.save`.

Keep one key per job, so a leak is revoked without breaking the others.
