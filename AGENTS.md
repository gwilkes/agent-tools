## General

Use en-US spelling only: `serialize`, `color`, `normalize`, `center`, `gray`,
`catalog`, `behavior`, `meter`, etc. No em dashes, and no decorative comment
lines. The `style` script checks all three.

Comments say why, not what: a constraint, a trap, the reason for an odd
choice. Code that reads plainly needs none.

## Tools

There is no shell. Everything goes through `mcp__gci`; `mcp__gci__list_tools`
prints every tool with its parameters (`brief: true` for one line each).

- Read with `read` (a negative `offset` is the tail), search with `grep`,
  list with `ls`.
- Change files with `edit`: exact text, a line range (pass the lines as you
  read them in `expect`), or the region between two marker lines. Several
  files go in one `files` call, so a change lands everywhere or nowhere.
- Rename an identifier across the tree with `sub` and `words`, with
  `dry_run` first. Use `rename` for file names and `json` for JSON values.
- Put temporary files in `tmp/` and empty it with `clean_tmp` when done.

`.gci/`, `.mcp.json`, `.claude/` and `.git/` are read-only to the tools. If
one of them needs to change, say what and why, and the user will make the
change.

## Running things

Run processes with `run_script`; only the scripts in `.gci/settings.json`
exist. Call it with no name to list them. Each run's full output is written
live to `tmp/gci/logs/`, and the result's first line names the file; a
result cut for length says so and points there.

Before finishing a change, run `check` (compile, style, self-test) and fix
what it reports.

## Working on gci

This repo develops the `gci` package itself (`"edit_self": true` lets the
tools edit `gci/`). After an edit:

1. `self_test` runs the self-test against the code on disk.
2. `restart` reloads this session's server with the new code; it refuses if
   the new code does not import, so a broken edit cannot take the tools down.

A new tool or behavior gets a check in `gci/selftest.py`. The house style
applies to test strings too: build a forbidden one at runtime rather than
writing it out.

## Git

The git tools only read: `git_status`, `git_diff`, `git_log`, `git_show`,
`git_blame`, `git_branch`. Never try to commit, push or change branches;
the user does that. Summarize what changed so they can review it.
