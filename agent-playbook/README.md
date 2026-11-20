# Agent playbook

The working order for a coding assistant (Claude Code, Cursor, any MCP client) that is testing and improving an AI agent with
Functional AI: add tracing, build a dataset from real traffic, choose the checks, run `fn eval`, read the failed rows, propose
a fix, and **ask before going live or spending credits**.

`PLAYBOOK.md` is the one source. Everything else here is generated from it by `tools/gen_playbook.py` and must not be edited by
hand.

| For | File | How to use it |
|---|---|---|
| Any assistant | `PLAYBOOK.md` | Point it at this file, or paste it into its instructions. |
| Claude Code | `claude-code-plugin/` | `/plugin marketplace add ferrouslabs-git/fnai-public`, then `/plugin install functional-ai@fnai`. Or load the folder directly: `claude --plugin-dir agent-playbook/claude-code-plugin`. Its skill loads when you talk about testing your agent. |
| Cursor | `cursor/functional-ai.mdc` | Copy it to `.cursor/rules/` in your project (or download it from Org → Connect AI Tools in the app). |
| MCP clients | prompts on the server | Our MCP server offers `where_are_we`, `set_up_testing`, `investigate_a_drop` and `fix_a_failing_row`. |

The plugin carries the playbook only. To give the assistant the tools, connect it to Functional AI first: in the app, Org →
**Connect AI Tools** shows the steps for Claude, Claude Code and Cursor.
