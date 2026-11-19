# Which key do I use?

Three kinds of key, each shown **once** when you make it and revocable at any time. Keep them in environment variables or a
secret store, never in code.

| Key | Looks like | Belongs to | Made at | Use it for |
|---|---|---|---|---|
| **Personal token** | `fnp_…` | you | avatar → **API tokens** | setting things up from your computer: creating functions, datasets, checks; everything you can do in the app |
| **Function key** | `fnk_…` | one function | the function → **Deployments** → API keys | calling that function, fetching its prompts, sending its traces |
| **Space key** | `fns_…` | one space | the space → **Space keys** | servers and automated tests that need several functions: traces, prompts, test runs, datasets |

## Choosing

- **Setting up, trying things:** a personal token. It acts as you, with your permissions.
- **One production service, one function:** a function key. Smallest blast radius if it leaks.
- **Servers, CI or scheduled tests across a space:** a **space key**, with only the boxes it needs:
  - servers: **Read prompts**, **Send traces**, **Use functions**
  - automated tests: **Run tests**, **Manage datasets**, **Read functions**
  - build scripts: add **Create functions** and **Save versions**
  Make one key per job so a leak is revoked without breaking the others. Details and the full list: [space-keys.md](space-keys.md).

A space key can never deploy, delete, manage members, billing or keys, whatever it is given.

## Where the SDK looks

| Setting | Used by |
|---|---|
| `FNAI_TOKEN` | `fnai.eval`, `evaluate` (and the `fn` command): a personal token or a space key |
| `FNAI_API_KEY` | `get_prompt`, tracing: a function key or a space key |
| `FNAI_BASE_URL` | all of them: which Functional AI to talk to |

An explicit argument always beats the environment (`token=`, `api_key=`, `base_url=`).

## Model keys are different

The keys above are for talking to **Functional AI**. A key for a model provider (OpenAI, Anthropic, …) is a separate thing. Your
own code uses its own provider key and Functional AI never sees it. You only add a provider key in the app (**API keys**) if you
want the AI judges that score your answers to run on your own account instead of Functional AI's.
