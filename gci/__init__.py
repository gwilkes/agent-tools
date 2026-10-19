"""Tools for working in a project, from the command line or over MCP: running
its own scripts, here or on other hosts, and reading and editing its files.

    gci TOOL [--PARAM VALUE ...] [--input FILE]
                              any tool below; `gci TOOL --help` describes it
    gci run [--host H ...] [--no-sync] NAME [ARG ...]
                              run a script, its output printed live
    gci serve [--port N] [--bind ADDR]
                              run scripts for remote callers (on each host)
    gci mcp                   serve the tools over stdio (what .mcp.json runs)

`gci` is the console script `pip install` makes; `python -m gci` is the same
thing from a checkout.

Each tool has a fixed shape, so what an agent can do is the tool list:

    read                                          (tools/read.py)
    edit                                          (tools/files.py)
    scripts run_script job sync hosts kill        (tools/run.py)
    tools restart                                 (tools/meta.py)

Each is a CLI word too, built from its schema: a flag per param, JSON text
for an object, and `--input` for the whole call as one JSON object. `job`,
`restart` and run_script's `wait` are not: they need a process that outlives
the call, and a CLI process ends with it, so a run there is waited out.

The tools are the ones that save an agent calls or context over its own
shell. `read` reads a block by its first line (`at`) or several slices at
once (`ranges`), where a shell would grep for a line number first. `edit`
takes several files at once, all or nothing, and three kinds of edit: exact
text, a line range (which must quote the lines as they are now, so a stale
line number cannot clobber the wrong ones), and the region between two marker
lines. run_script narrows a build's output before it lands.

There is deliberately no tool that runs arbitrary code. Anything that needs a
process is a script in the settings, where a person wrote it.

The root is the directory the server starts in (Claude Code starts a project's
servers in the project), or GCI_ROOT if set. Every path a tool takes is
relative to it, and one that resolves outside it -- `..`, an absolute path, a
symlink pointing away -- is refused. Paths in output are root-relative.

.gci/settings.json, under the root, is the whole configuration, and it is read
on every call, so an edit to it lands without a restart. settings.schema.json,
beside this file, describes every key and its default; a key it does not
list is refused when a script runs, so a typo is never quietly ignored:

    {
        "deny":    [".env", ".env.*", "secrets/**"],
        "protect": ["Cargo.lock"],
        "skip":    [".git", "node_modules"],
        "tmp":     "tmp",
        "max_output": 40000,
        "defaults": {"tail": 80, "timeout": 900},
        "scripts": {
            "test": {"description": "run the tests", "command": ["cargo", "test"],
                     "args": true, "timeout": 1200, "cwd": "."}
        },
        "remote":  {...}
    }

`deny` is what no tool here reads, writes or syncs. It is not a sandbox: the
client's own tools and shell answer to the client's permissions, so a file
that must stay unread needs the same globs there too (in Claude Code, `Read`
and `Edit` deny rules). Here they also keep secrets off every host a sync
reaches. `protect` is what tools may read but never change. .gci/, .mcp.json,
.claude/, every .git/ and this package (when it lives in the tree) are always
protected: a tool that can rewrite the script table can run anything, one
that can rewrite the permissions can grant itself the rest, and git's hooks
and config are code the next git command runs. In the repo that develops this
package, `"edit_self": true` lifts the protection from the package itself.
`skip` names directories (and files) a recursive walk does not enter -- a path
that names one directly still works.

A glob without a `/` matches a name at any depth (`.env`), one with a `/`
matches the root-relative path (`secrets/**`, everything under secrets/).

A script is an argv, `command`, or a list of them, `commands`, run as given
in order and stopping at the first failure; an entry has one or the other.
The caller's `args` are appended to the last only when the entry says
`"args": true`, so run_script runs only the commands a person wrote. A
script that wants a shell says so in the settings (`["sh", "-c", "..."]`),
where a person wrote it. Output is stdout and stderr together; a run past
its `timeout` (600 s unless the settings say otherwise, shared by all its
commands) is killed with its whole process tree.

Instead of either, an entry can be `run` (a gauntlet: other scripts in
order, an OK/FAIL line each and the failing one's output, stopping at the first failure unless
`keep_going`), or `forbid` (regex rules checked over the files a sync would
carry, `files`/`exclude` globs narrowing them -- a house-style lint with no
code). `{name}` in an argument is a var: the settings' `vars`, which can
differ per platform, or a built-in such as `{python}` (the interpreter running
this package, since its name on PATH differs per host), `{exe}` or `{tmp}`.
`{args}` alone as an argument is where a caller's arguments go. `env`, `path`
(prepended to PATH) and `platforms` round it out; scripts.py has the details.

`hosts`, `sync`, `wait`, `grep`, `after`, `tail` and `timeout` are defaults,
for whatever a caller leaves out: the settings' `defaults` sets them for every
script, and a script's own key of the same name wins, null included, so a
script can clear a shared grep. `job` reports on a run with its script's grep,
tail and after unless told otherwise. A caller overrides default hosts with
`["local"]`, which runs the script here as if no hosts were named.

Temporary files go in tmp/ (or the directory the settings' `tmp` names),
which should be in .gitignore; that also keeps it out of every sync.
tmp/ never travels in a sync, whatever .gitignore says.

A tool result arrives whole when the call ends -- MCP has no streamed result
-- so a long run is watched two other ways. Every script run writes its
output, line by line as it lands, to tmp/gci/logs/NAME-TIME.log (HOST-NAME-
TIME.log for a run on a host, which a host also keeps of its own), and the
result's first line names the file: follow it with `tail -f`, or `read` with
a negative offset. And a client that sends a progressToken gets
`notifications/progress` carrying the latest lines, rate limited, with every
gauntlet OK/FAIL line among them; whether a client shows those is up to it.
A gauntlet's result holds its OK/FAIL lines and a failure's tail; its
members' full output is in the log.

Every run is a job in a thread of the MCP server, so a call need not last as
long as its run. run_script waits for the run to end unless given `wait`, the
seconds after which it answers with the output so far and the job's id; `job`
then reports on the run, waits for it or stops it. A call the client gave up
on leaves its run to `job` the same way. A restart is refused while a run is
going, and a client that disconnects stops them all.

A result is at most `max_output` characters (40000 unless the settings say
otherwise). A script's output past that is cut from the middle, keeping more
of the end, with a line saying how much went and naming the log that holds
all of it.

The `tools` tool is the same list tools/list gives a client, laid out for
reading: each tool's signature, its description, its parameters, and any
tools the current settings switch off. `gci tools` and `gci scripts` print
their lists in a layout of their own for a terminal (listing.py): a block per
tool or script, its head line, a summary, and aligned rows; a tool's rows are
its flags and their defaults, a script's are its commands and every default
it runs with, marked with where each comes from. Their `--grep` keeps whole
blocks rather than lines.

After an edit to this package, the tests under tests/ (`python -m unittest`)
check the code on disk, and `restart` loads it: it re-execs the MCP server in place -- same pid, same pipes, so the
client sees no disconnect -- after importing every module of the new code in a
separate process first, so a broken edit leaves the running server alone. It
re-announces the tool list afterwards.

Files are edited here; other machines only run scripts. The `remote` section
of the settings names them:

    "remote": {
        "port": 9001,
        "key_env": "GCI_API_KEY",
        "key_file": "~/.gci_key",
        "hosts": {"macos": "192.168.0.24", "windows": "192.168.0.5"},
        "ignore": ["vendor/"]
    }

Each host runs `gci serve` in its own checkout. run_script with `hosts` reaches
each one, syncs this tree to it, runs the script there and follows its output,
all hosts at once; `local` among them runs it here as well. A host is its
address on `port`, or a URL.

Every request carries the API key, from the environment variable `key_env`
names or else from `key_file`, and a server refuses to start without one: it
runs whatever its settings say, and a sync can rewrite them. The key belongs
in neither the settings nor the tree, since both travel.

A sync is the tree as every .gitignore in it filters it, nested ones included,
with `ignore` lines (gitignore syntax) after the root's, and without .git,
symlinks or anything `deny` names. .gci/ always travels, whatever a .gitignore
says, since a host runs the scripts its own copy of the settings defines. The
server deletes what it holds in that domain and the client did not send, so
`sync` with dry_run is the way to check a domain change before it lands. Only changed files travel, by content
hash, and they land with fresh mtimes so build tools see them change.

A run is a job: the POST that starts it answers with an id, and the client
long-polls `/job` for the lines after the last one it has, so no request
outlives a proxy's timeout and a dropped poll resumes where it was. A server
runs one job at a time and refuses a sync during one. A job nobody has polled
for ten minutes is killed.

A client and a server must run the same code: the version is a hash of every
module in this package. A client that sees a different one sends its modules
to /self-update, and the server compiles them all, replaces its own (removing
any the new version dropped, and putting the old ones back if that fails)
and restarts into them. A sync never touches the
server's own package directory; that is /self-update's alone.

Text is read and written as UTF-8 bytes, so line endings and a missing final
newline survive an edit. A file with a NUL in its first 8 KiB, or that is not
UTF-8, is binary: read says what it is, and edit refuses it.
"""
