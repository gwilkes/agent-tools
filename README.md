# agent-tools

MCP tools for running a project's scripts and editing its files. See
[Tools](#tools) for the full list.

## Setup

Install the package:

```bash
pip install git+https://github.com/gwilkes/agent-tools.git
```

Add `gci` to the project's `.mcp.json` so new Claude Code sessions start it:

```json
{
  "mcpServers": {
    "gci": {
      "command": "python",
      "args": ["-m", "gci", "mcp"]
    }
  }
}
```

Optionally, allow its tools without prompting in `.claude/settings.json`:

```json
{
    "permissions": {
        "allow": ["mcp__gci"]
    }
}
```

Define the project's scripts, and any other settings, in `.gci/settings.json`:

```json
{
    "scripts": {
        "build": {
            "description": "builds the crate",
            "command": ["cargo", "build"]
        },
        "clean": {
            "description": "remove the target directory",
            "run": ["cargo", "clean"]
        }
    }
}
```

Then tell agents about the tools in `AGENTS.md`, `CLAUDE.md` or similar:

```md
`mcp__gci` has misc ci management tools available which may make your life
easier. Use `mcp__gci__list_tools` to list all available tools.
```

## Tools

```
$ python -m gci list-mcp
read(path, offset=1, limit=2000, tail?)
    Read a text file, numbered like `cat -n`. Long files come a page at a time:
    the header says which lines these are and the offset for the next page. A
    negative offset counts from the end: -50 is the last 50 lines, as is
    `tail` 50. To see only the lines matching a pattern, with these same line
    numbers, search it with `grep -n` instead.
    - path (string, required): The file, relative to the project root.
    - offset (integer): First line to show, 1-based; negative counts from the end.
    - limit (integer): How many lines to show.
    - tail (integer): The last N lines, like `tail -n N`; replaces offset.

edit(path?, old?, new?, replace_all=false, edits?, files?)
    Change files by exact text, by line range, or between marker lines; one
    file or several, all or nothing. `old`/`new` replaces text that must occur
    once (or set replace_all). `edits` makes several changes to `path`. `files`
    makes changes to several files, each {path, edits}: if any edit in any file
    fails, no file is written.

    An edit is one of: {old, new, replace_all?}; {start, end?, expect, new}, a
    line range as you read it, where `expect` must be those lines exactly as
    they are now; or {after, before, new}, replacing everything between the one
    line containing `after` and the next containing `before` (a generated
    block, a section between markers). Line ranges apply first, numbered as the
    file was before the call; the rest apply in order. Answers with the diffs.
    - path (string): The file, for one-file edits.
    - old (string): Text to find, exactly, whitespace included.
    - new (string): Text to put in its place.
    - replace_all (boolean): Replace every occurrence instead of requiring one.
    - edits (object[]): Several edits to `path`.
    - files (object[]): Edits to several files, all or nothing.

sub(pattern?, replacement?, rules?, words?, path=".", glob?, ignore_case=false, literal=false, multiline=false, dry_run=false, grep?, tail?)
    Search and replace across files, like `sed -i`. Python `re` syntax; a
    replacement takes \1 and \g<name>. Three forms: `pattern` with
    `replacement`; `rules`, several {pattern, replacement} applied in order,
    each to the result of the last; or `words`, a map of whole words to their
    new names applied in one pass ({"old_name": "new_name", ...}), so a
    renamed word is never renamed again. A word is bounded by anything that is
    not a letter, digit or underscore. Set dry_run to see the diff first.
    Nothing is written if any file that would change is protected.
    - pattern (string): Regular expression (or plain text with literal).
    - replacement (string): What each match becomes.
    - rules (object[]): Several rules, applied in order.
    - words (object): Whole words to rename: {old: new}.
    - path (string): File or directory, relative to the project root.
    - glob (string): Only files matching this glob: `*.py` matches a name at any depth, `src/**` everything under src/.
    - ignore_case (boolean): Case-insensitive.
    - literal (boolean): Treat pattern and replacement as plain text.
    - multiline (boolean): ^ and $ match at every line; the pattern may span lines with \n.
    - dry_run (boolean): Report the diff without writing.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.

clean_tmp(dry_run=false)
    Empty the project's scratch directory, `tmp/` unless the server's
    instructions name another: the place for temporary files, kept out of git and out
    of every sync. Everything inside goes, the directory itself stays. Refused
    if git tracks anything in it, or it holds a protected or denied file. Set
    dry_run to see what it holds first.
    - dry_run (boolean): Report what would be removed without removing it.

json(path, query?, value?, delete=false, keys=false, grep?, tail?)
    Query or edit a JSON file, like jq. `query` is a path into it: `a.b`,
    `a[0]`, `a[-1]`, `a["key.with.dots"]`; none means the whole document.
    Without `value` or `delete`, answers with what is there (`keys` for just
    the keys of an object, or the length of an array). With `value` (JSON
    text: `"str"`, `3`, `{"k": [1]}`), sets it there, making missing objects
    on the way and appending at an index one past an array's end. With
    `delete`, removes it. An edit rewrites the file in standard layout, keeping
    its indent, and answers with the diff.
    - path (string, required): The file, relative to the project root.
    - query (string): Where in the document; omit for the whole of it.
    - value (string): JSON text to set at the query.
    - delete (boolean): Remove what the query names.
    - keys (boolean): Answer with an object's keys or an array's length only.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.

list_scripts(grep?, tail?)
    List the scripts run_script can run: each one's name, what it is for and
    the command it runs, marked `[args...]` if it takes arguments and with the
    platforms it is limited to, if any. Then the hosts it can run them on.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.

run_script(name, args?, hosts?, sync=true, proxy?, detach=false, grep?, tail?)
    Run one of the project's scripts (list_scripts lists them): the build, the
    tests, anything the project needs a process for. Only those scripts run,
    and only the ones marked `[args...]` take arguments. Answers with the exit
    code, the time taken and the combined stdout/stderr. With `hosts`, it runs
    on each of them at once instead: this tree is synced to each host first,
    and the answer has a section per host.

    `grep` and `tail` narrow the output (each host's, with `hosts`) before it
    is cut to fit; the run's log keeps all of it, and its `log:` line is
    always kept, so grep or read that file to look again without rerunning.

    With `detach`, it answers at once with a job id and the run's log (each
    host's, with `hosts`), and the run goes on in the background; `job`
    reports on it, waits for it or stops it.
    - name (string, required): The script's name, as list_scripts lists it.
    - args (string[]): Arguments appended to its command, for scripts that take them.
    - hosts (string[]): Host names, as list_scripts lists them (or addresses/URLs); `local` is this checkout. Omit to run here alone.
    - sync (boolean): Sync this tree to each host before running.
    - proxy (boolean): true: reach hosts through the project's proxy; false: directly only. Unset: directly, falling back to the proxy.
    - detach (boolean): Answer at once and let the run go on; see `job`.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.

sync(hosts, dry_run=false, proxy?, grep?, tail?)
    Bring each host's tree level with this one, without running anything:
    changed files are sent and files the host has that this tree does not are
    deleted there. With dry_run, report what would be sent and deleted.
    - hosts (string[], required): Host names, as list_scripts lists them (or addresses/URLs); `local` is this checkout.
    - dry_run (boolean): Report without sending or deleting.
    - proxy (boolean): true: reach hosts through the project's proxy; false: directly only. Unset: directly, falling back to the proxy.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.

hosts(proxy?, grep?, tail?)
    Every host run_script can reach: whether it answers, its platform, whether
    it runs this version of the tools, and what it is running now.
    - proxy (boolean): true: reach hosts through the project's proxy; false: directly only. Unset: directly, falling back to the proxy.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.

kill(hosts, proxy?)
    Stop whatever script each host is running, with its whole process tree.
    - hosts (string[], required): Host names, as list_scripts lists them (or addresses/URLs); `local` is this checkout.
    - proxy (boolean): true: reach hosts through the project's proxy; false: directly only. Unset: directly, falling back to the proxy.

job(id?, wait=0, stop=false, grep?, tail?)
    Follow a run that run_script started with `detach`: how it stands and its
    output so far, laid out as run_script answers once a run ends. Alone, an
    id answers at once with what there is. `wait` first waits up to that many
    seconds for the run to end; `stop` first kills it,
    with its whole process tree here or its job on each host. With no id, list
    the detached runs. They live in this server: a restart is refused while
    one is going, and forgets the finished ones (their logs stay).
    - id (string): The job id run_script answered with. Omit to list them.
    - wait (integer): Seconds to wait for the run to end before answering.
    - stop (boolean): Kill the run, then report it.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.

restart()
    Restart this MCP server in place to pick up edits to its own code. The new
    code is started once first and the restart refused if it fails, so a broken
    edit leaves this server running. Refused while other calls are running,
    since their answers would be lost. The tool list is re-announced afterwards.

self_test(timeout=300, grep?, tail?)
    Run the tools' own self-test: every tool against a scratch tree, a real
    `gci serve` over loopback, and a real MCP server restarted mid-session. Run
    it after editing this package, before `restart`, to check the edit. It runs
    in a fresh process, so it tests the code on disk rather than the code this
    server loaded, and touches nothing outside its scratch directories.
    - timeout (integer): Seconds before it is killed.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.

list_tools(names?, brief=false, grep?, tail?)
    List every tool this server offers: its signature, what it does and its
    parameters, plus any switched off for this project. The same tools a client's
    tools/list sees, laid out for reading. Pass `names` for just those, or
    grep: `^\w` keeps the signatures alone, `tail\?` the tools that take tail.
    - names (string[]): Only these tools.
    - brief (boolean): One line per tool: its signature and first sentence.
    - grep (string): Only the lines of the answer matching this Python regex, like `| grep -E`; `(?i)` at its start ignores case.
    - tail (integer): Only the last N lines of the answer (after grep), like `| tail -n N`.
```
