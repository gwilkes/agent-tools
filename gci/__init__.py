"""Tools for working in a project, served over MCP: files, search, git and the
project's own scripts.

    gci mcp                   serve the tools over stdio (what .mcp.json runs)
    gci scripts               list the scripts and hosts .gci/settings.json defines
    gci list-mcp [--json]     every MCP tool, with its description and parameters
    gci run [--host H ...] [--proxy | --no-proxy] [--no-sync] NAME [ARG ...]
                              run one of them here, or on each host at once
    gci serve [--port N] [--bind ADDR]
                              run scripts for remote callers (on each host)
    gci sync --host H ... [--dry-run]
                              bring a host's tree level with this one
    gci hosts                 which hosts answer, and what they run
    gci kill --host H ...     stop what a host is running
    gci self-test             drive every tool once in scratch directories

`gci` is the console script `pip install` makes; `python -m gci` is the same
thing from a checkout.

Each tool has a fixed shape, so what an agent can do is the tool list:

    read edit sub clean_tmp                       (tools/files.py)
    json                                          (tools/data.py)
    list_scripts run_script job sync hosts kill   (tools/run.py)
    self_test restart list_tools                  (mcp.py)

The bulk forms sit inside two of those rather than beside them, to keep the
tool list short. `edit` takes several files at once, all or nothing, and
three kinds of edit: exact text, a line range (which must quote the lines as
they are now, so a stale line number cannot clobber the wrong ones), and the
region between two marker lines. `sub` takes one pattern, a list of rules
applied in order, or a `words` map renaming whole words in one pass.

There is deliberately no tool that runs arbitrary code. What an agent would
otherwise write a throwaway script for gets a tool of its own instead: `json`
for querying and editing JSON, `read` with a negative offset for the end of a
log, `sub` for editing across files. Anything
else that needs a process is a script in the settings, where a person wrote it.

The root is the directory the server starts in (Claude Code starts a project's
servers in the project), or GCI_ROOT if set. Every path a tool takes is
relative to it, and one that resolves outside it -- `..`, an absolute path, a
symlink pointing away -- is refused. Paths in output are root-relative.

.gci/settings.json, under the root, is the whole configuration, and it is read
on every call, so an edit to it lands without a restart:

    {
        "deny":    [".env", ".env.*", "secrets/**"],
        "protect": ["Cargo.lock"],
        "skip":    [".git", "node_modules"],
        "tmp":     "tmp",
        "max_output": 40000,
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

A script is an argv: `command` is run as given, with the caller's `args`
appended only when the entry says `"args": true`, so run_script runs only the
commands a person wrote. A script that wants a shell says so in the settings (`["sh", "-c", "..."]`), where a person wrote it. Output
is stdout and stderr together; a run past its `timeout` (600 s unless the
entry says otherwise) is killed with its whole process tree.

Beyond one `command`, an entry can be `steps` (argvs in order, stopping at the
first failure), `run` (a gauntlet: other scripts in order, an OK/FAIL line
each and the failing one's output, stopping at the first failure unless
`keep_going`), or `forbid` (regex rules checked over the files a sync would
carry, `files`/`exclude` globs narrowing them -- a house-style lint with no
code). `{name}` in an argument is a var: the settings' `vars`, which can
differ per platform, or a built-in such as `{python}` (the interpreter running
this package, since its name on PATH differs per host), `{exe}` or `{tmp}`.
`{args}` alone as an argument is where a caller's arguments go. `env`, `path`
(prepended to PATH) and `platforms` round it out; scripts.py has the details.

Temporary files go in tmp/ (or the directory the settings' `tmp` names),
which should be in .gitignore; that also keeps it out of every sync.
`clean_tmp` empties it, and refuses when git tracks anything there, since
then it is not scratch. tmp/ never travels in a sync, whatever .gitignore says.

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

A result is at most `max_output` characters (40000 unless the settings say
otherwise). A script's output past that is cut from the middle, keeping more
of the end, with a line saying how much went and naming the log that holds
all of it.

`list_tools` (and `gci list-mcp` from a terminal) is the same list tools/list
gives a client, laid out for reading: each tool's signature, its description,
its parameters, and any tools the current settings switch off.

`self_test` runs `gci self-test` in a fresh process, so it checks the code on
disk -- run it after an edit to this package, then `restart` to load it.
`restart` re-execs the MCP server in place -- same pid, same pipes, so the
client sees no disconnect -- after importing every module of the new code in a
separate process first, so a broken edit leaves the running server alone. It
re-announces the tool list afterwards.

Files are edited here; other machines only run scripts. The `remote` section
of the settings names them:

    "remote": {
        "port": 9001,
        "proxy": "https://example.com/gci",
        "key_env": "GCI_API_KEY",
        "key_file": "~/.gci_key",
        "hosts": {"macos": "192.168.0.24", "windows": "192.168.0.5"},
        "ignore": ["vendor/"]
    }

Each host runs `gci serve` in its own checkout. run_script with `hosts` reaches
each one, syncs this tree to it, runs the script there and follows its output,
all hosts at once; `local` among them runs it here as well. A host is its
address on `port`, or a URL. Off the home network, `proxy` reaches a named host
at PROXY/<name>: a direct connection that fails falls back to it unless the
caller said which to use.

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
any the new version dropped) and restarts into them. A sync never touches the
server's own package directory; that is /self-update's alone.

Text is read and written as UTF-8 bytes, so line endings and a missing final
newline survive an edit. A file with a NUL in its first 8 KiB, or that is not
UTF-8, is binary: sub passes over it and read says what it is.
"""
