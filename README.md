# agent-tools

A command line for agents and people working in a project: it runs
the project's own scripts, here or on other hosts at once, with their output
narrowed before it reaches the context, and it reads and edits files in ways
the usual tools take several calls for. The same tools can also be served over
MCP, if you prefer (see [MCP](#mcp-optional)).

## Setup

Install the package:

```bash
pip install git+https://github.com/gwilkes/agent-tools.git
```

Define the project's scripts, and any other settings, in `.gci/settings.json`:

```json
{
    "defaults": {
        "tail": 80,
        "timeout": 900
    },
    "scripts": {
        "build": {
            "description": "builds the crate",
            "command": ["cargo", "build"],
            "args": true,
            "grep": "^(test |error)",
            "after": 5
        },
        "clean": {
            "description": "remove the target directory and the shader cache",
            "commands": [["cargo", "clean"], ["{python}", "tools/clear_shaders.py"]]
        },
        "flicker-check": {
            "description": "score frames for TAA-jitter-tied flicker",
            "command": ["{python}", "flicker_check.py"],
            "args": true,
            "timeout": 1800
        }
    }
}
```

Then tell agents about it in `AGENTS.md`, `CLAUDE.md` or similar:

```md
`gci` runs this project's scripts and reads and edits its files.
`gci scripts` lists the scripts; run one with `gci run_script NAME`, with
`--tail N` or `--grep RE` to keep only the lines you need (the full output is
in the log it names), and a script's own flags after `--`.
Prefer `gci read PATH --at "fn name"` (or `--ranges JSON`) over chained
`sed -n` or `grep -A` to read a function by name or several slices at once,
and `gci edit` for several exact-text edits in one call, all or nothing:
pass the call as JSON with `gci edit --input - <<'EOF'`.
`gci WORD --help` describes any of them.
```

## Tools

```
gci read [PATH] [--offset N] [--limit N] [--tail N] [--at AT] [--ranges JSON]
  Read a text file, numbered like `cat -n`.
    PATH           The file, relative to the project root.
    --offset N     First line to show, 1-based; negative counts from the end. (default 1)
    --limit N      Most lines to show (per slice, with ranges). (default 2000)
    --tail N       The last N lines, like `tail -n N`; replaces offset.
    --at AT        Text on the first line of the block to read.
    --ranges JSON  Several slices, each {path, start?, end?} or {path, at}; replaces path.

gci edit [PATH] [--old OLD] [--new NEW] [--replace-all] [--edits JSON] [--files JSON]
  Change files by exact text, by line range, or between marker lines.
    PATH           The file, for one-file edits.
    --old OLD      Text to find, exactly, whitespace included.
    --new NEW      Text to put in its place.
    --replace-all  Replace every occurrence instead of requiring one.
    --edits JSON   Several edits to `path`.
    --files JSON   Edits to several files, all or nothing.

gci scripts [--grep GREP] [--tail N]
  List the scripts run_script can run.
    --grep GREP  Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)`
                 at its start ignores case.
    --tail N     Only the last N lines of the answer (after grep), like `| tail -n N`.

gci run_script NAME [ARGS...] [--hosts HOSTS...] [--[no-]sync] [--grep GREP] [--tail N] [--after N]
  Run one of the project's scripts (the scripts tool lists them).
    NAME              The script's name, as the scripts tool lists it.
    ARGS...           Arguments appended to its last command, for scripts that take them.
    --hosts HOSTS...  Host names, as the scripts tool lists them. `local` among them runs it here
                      too, and `["local"]` alone runs it only here; omit for the script's default,
                      else here alone.
    --[no-]sync       Sync this tree to each host before running; omit for the script's default,
                      else true.
    --grep GREP       Only the lines of the answer matching this Python regex, like `| grep -E`;
                      `(?i)` at its start ignores case.
    --tail N          Only the last N lines of the answer (after grep), like `| tail -n N`.
    --after N         Lines to keep after each grep match, like `grep -A N`: an error's detail.

gci sync --hosts HOSTS... [--dry-run] [--grep GREP] [--tail N]
  Bring each host's tree level with this one, without running anything.
    --hosts HOSTS...  Host names, as the scripts tool lists them.
    --dry-run         Report without sending or deleting.
    --grep GREP       Only the lines of the answer matching this Python regex, like `| grep -E`;
                      `(?i)` at its start ignores case.
    --tail N          Only the last N lines of the answer (after grep), like `| tail -n N`.

gci hosts [--grep GREP] [--tail N]
  Every host run_script can reach.
    --grep GREP  Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)`
                 at its start ignores case.
    --tail N     Only the last N lines of the answer (after grep), like `| tail -n N`.

gci kill --hosts HOSTS...
  Stop whatever script each host is running, with its whole process tree, whoever started it.
    --hosts HOSTS...  Host names, as the scripts tool lists them.

gci tools [NAMES...] [--brief] [--grep GREP] [--tail N]
  List every tool this server offers.
    NAMES...     Only these tools.
    --brief      One line per tool: its signature and first sentence.
    --grep GREP  Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)`
                 at its start ignores case.
    --tail N     Only the last N lines of the answer (after grep), like `| tail -n N`.

over MCP only: job, restart

`gci WORD --help` gives a word's whole description.
```

## MCP (optional)

The same tools can be served over MCP instead. Add `gci` to the project's
`.mcp.json` so new Claude Code sessions start it:

```json
{
  "mcpServers": {
    "gci": {
      "command": "gci",
      "args": ["mcp"]
    }
  }
}
```

Then tell agents about the tools in `AGENTS.md`, `CLAUDE.md` or similar:

```md
`mcp__gci` has misc tools available which may make your life easier.
Use `mcp__gci__tools` to list all available tools and
`mcp__gci__scripts` to list all scripts defined in this project.
Prefer `mcp__gci__edit` over a one-off script for exact text replacements:
it takes several edits and files in one call, and applies all or none.
Prefer `mcp__gci__read` with `at` or `ranges` over chained `sed -n` or
`grep -A` to read a function by name or several slices at once.
```

A server outlives each call, so over MCP two things more are possible: a
`run_script` call with `wait` answers after that many seconds and leaves the
run going, for the `job` tool to report on, wait for or stop; and `restart`
reloads the server's own code in place. On the command line a run is always
waited out, and a Bash tool's own background mode does what `wait` does.
