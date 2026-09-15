# fn — the functionalAI command line

**Status: built, not yet verified.** This was written without a Go toolchain
available to compile or run it — reviewed carefully by hand, but nobody has
actually built or smoke-tested this binary yet. Do that before relying on it:

```bash
cd sdk/go
go build ./...          # compiles the SDK and this command together
go vet ./...
go test ./...
CGO_ENABLED=0 go build -o fn ./cmd/fn
./fn help
```

Fix whatever that first build turns up, then this note should come out.

## What it does

Ten subcommands, wrapping the existing REST API via the hand-maintained Go
SDK (`sdk/go/fnai.go`) one directory up:

| Command | What it does |
|---|---|
| `fn login` | store a personal access token (`fnp_...`) in `~/.fnai/config.json` |
| `fn init [path]` | write a working example function to a file — no login needed |
| `fn new <path.yaml>` | push a function file up as a brand-new function |
| `fn ls` | list your functions |
| `fn run <slug> --key <fnk_...> --input '{...}'` | invoke a **deployed** function |
| `fn eval <function-id>` | run the function's checks; **exits nonzero if any fail** (for CI) |
| `fn deploy <function-id> <version-id>` | make a version the live deployment |
| `fn rollback <function-id> [--to-version <id>]` | roll back the live deployment |
| `fn pin <function-id> <version-id>` | remember a version for the current folder |
| `fn diff <version-id-a> <version-id-b>` | line-based diff of two versions' snapshots |
| `fn logs <function-id> [--limit N]` | recent calls (traces) |

## Three decisions this makes, and why

The ticket (REQ-55) left three things open. Here's what was picked, and the
reasoning, so it's easy to revisit:

1. **Language: Go.** The ticket asks for a static binary; Go produces
   exactly that with nothing else to install, and there's already a
   hand-maintained Go SDK in this repo to build on instead of
   reimplementing HTTP/auth from scratch.
2. **Auth: the existing personal access token (`fnp_*`).** Same mechanism
   the Python/TS/Go SDKs and the MCP server already use — not a new auth
   system. `fn run` (invoking an already-deployed function) uses a
   different, narrower credential — an `fnk_*` invoke key, passed via
   `--key`/`FNAI_INVOKE_KEY`, never stored — because that's genuinely a
   different, narrower kind of access than authoring.
3. **`pin` and `diff` operate on *versions*, not deployments** — and `pin`
   is stored **locally**, in a `.fnai/pin.json` file in whatever folder you
   run it from (like a version-control tool scoping a checked-out ref to
   the folder you're standing in), not as new state on the server. This
   avoids inventing new backend surface for a CLI convenience.

**Not decided here:** how someone actually *gets* this binary (a release
pipeline — GitHub Releases, Homebrew, etc.). That's a separate distribution
decision. Build it locally with the command above until that's settled.

## `fn eval`'s default dataset/checks

If you don't pass `--snapshot`/`--scores` explicitly, `fn eval` uses the
function's most recently created dataset's latest snapshot, and every score
defined on the function. (An earlier draft of this defaulted to scores
"marked as a gate" — that concept was removed from the product a while
back and the API never returns it, so that filter could never have
selected anything; fixed while verifying the Python port against a live
server, see `sdk/python/fn_cli.py`.) This default is reasonable, not a
promise it's the *right* dataset or check set for every function — pass
the flags explicitly once you know which ones you mean, especially in CI.
