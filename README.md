Misc MCP tools for agents to use instead of bash, powershell, etc.

```bash
pip install git+https://github.com/gwilkes/agent-tools.git
```

An ideal `.claude/settings.json` allows this MCP server while denying
Bash and PowerShell:

```json
{
    "permissions": {
        "defaultMode": "dontAsk",
        "allow": [
            "mcp__gci"
        ],
        "ask": [
            "mcp__gci__delete"
        ],
        "deny": [
            "Bash",
            "PowerShell",
            "Read(./.env)",
            "Read(./.env.*)",
            "Read(./secrets/**)",
            "mcp__terminal"
        ]
    }
}
```

`.mcp.json` stands up endpoints for new Claude sessions:
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

**Tools:**
```
$ python -m gci list-mcp
read(path, offset=1, limit=2000)
    Read a text file, numbered like `cat -n`. Long files come a page at a time:
    the header says which lines these are and the offset for the next page. A
    negative offset counts from the end, like tail: -50 is the last 50 lines.
    - path (string, required): File or directory, relative to the project root.
    - offset (integer): First line to show, 1-based; negative counts from the end.
    - limit (integer): How many lines to show.

write(path, content)
    Create or replace a file with `content`, making its directories. Prefer edit
    for a change to an existing file: it shows what changed and cannot clobber
    the parts you did not mean to touch.
    - path (string, required): File or directory, relative to the project root.
    - content (string, required): The whole new file.

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

sub(pattern?, replacement?, rules?, words?, path=".", glob?, ignore_case=false, literal=false, multiline=false, dry_run=false)
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

grep(pattern, path=".", glob?, mode="content", ignore_case=false, literal=false, word=false, multiline=false, context=0, max_results=200)
    Search file contents with a Python regex, like grep -rn. Walks the tree
    skipping binaries, denied paths and the skip list (.git, node_modules...).
    mode `content` shows `path:line:text` (context lines use `-`), `files` only
    the paths, `count` matches per file.
    - pattern (string, required): Regular expression (or plain text with literal).
    - path (string): File or directory, relative to the project root.
    - glob (string): Only files matching this glob: `*.py` matches a name at any depth, `src/**` everything under src/.
    - mode (content|files|count): What to report.
    - ignore_case (boolean): Case-insensitive.
    - literal (boolean): Treat the pattern as plain text.
    - word (boolean): Match whole words only.
    - multiline (boolean): Search whole files, so a pattern can span lines with \n; ^ and $ still match at each line.
    - context (integer): Lines of context around each match (content mode).
    - max_results (integer): Stop after this many matches (or files).

ls(path=".", glob?, depth=1, max_results=500)
    List a directory, like ls/find. Without a glob: its entries to `depth`
    levels, directories marked with `/`, files with their sizes. With a glob:
    every matching file under it, one path per line.
    - path (string): File or directory, relative to the project root.
    - glob (string): Only files matching this glob: `*.py` matches a name at any depth, `src/**` everything under src/.
    - depth (integer): Levels to descend without a glob.
    - max_results (integer): Stop after this many entries.

move(src, dst, copy=false, overwrite=false)
    Move or rename a file or directory, like mv; with copy, copy it instead,
    like cp -r. A destination that is an existing directory receives the source
    inside it.
    - src (string, required): What to move, relative to the project root.
    - dst (string, required): Where it goes.
    - copy (boolean): Copy rather than move.
    - overwrite (boolean): Replace a file already at the destination.

rename(pattern, replacement, path=".", glob?, dry_run=false)
    Rename many files at once by a regex over their names, the loop of `mv`s a
    shell would write. Each file's name (not its directory) under `path` is
    matched; a match is renamed in place to the name the replacement makes.
    Refused as a whole if two files would land on one name, a name is taken,
    or any file involved is protected. Set dry_run to see the list first.
    - pattern (string, required): Regular expression over file names, e.g. `^(.*)\.jpeg$`.
    - replacement (string, required): The new name; takes \1 and \g<name>, e.g. `\1.jpg`.
    - path (string): File or directory, relative to the project root.
    - glob (string): Only files matching this glob: `*.py` matches a name at any depth, `src/**` everything under src/.
    - dry_run (boolean): Report the renames without making them.

delete(path, recursive=false)
    Delete a file, or a directory with recursive. Permanent: there is no trash.
    - path (string, required): File or directory, relative to the project root.
    - recursive (boolean): Delete a non-empty directory and everything in it.

clean_tmp(dry_run=false)
    Empty the project's scratch directory, `tmp/` unless the settings'
    `tmp` names another: the place for temporary files, kept out of git and out
    of every sync. Everything inside goes, the directory itself stays. Refused
    if git tracks anything in it, or it holds a protected or denied file. Set
    dry_run to see what it holds first.
    - dry_run (boolean): Report what would be removed without removing it.

mkdir(path)
    Make a directory and any missing parents, like mkdir -p.
    - path (string, required): File or directory, relative to the project root.

diff(a, b?, content?, context=3)
    Unified diff of two files, or of a file against `content` (what it would
    become). Empty when they match.
    - a (string, required): The first file.
    - b (string): The second file.
    - content (string): Text to compare `a` against, instead of b.
    - context (integer): Lines of context.

json(path, query?, value?, delete=false, keys=false)
    Query or edit a JSON file, like jq. `query` is a path into it: `a.b`,
    `a[0]`, `a[-1]`, `a["key.with.dots"]`; none means the whole document.
    Without `value` or `delete`, answers with what is there (`keys` for just
    the keys of an object, or the length of an array). With `value` (JSON
    text: `"str"`, `3`, `{"k": [1]}`), sets it there, making missing objects
    on the way and appending at an index one past an array's end. With
    `delete`, removes it. An edit rewrites the file in standard layout, keeping
    its indent, and answers with the diff.
    - path (string, required): File or directory, relative to the project root.
    - query (string): Where in the document; omit for the whole of it.
    - value (string): JSON text to set at the query.
    - delete (boolean): Remove what the query names.
    - keys (boolean): Answer with an object's keys or an array's length only.

git_status(paths?)
    The working tree's state, like `git status --short --branch`: the branch and
    how far it is from its upstream, then one line per changed, staged or
    untracked file (XY codes: first column staged, second unstaged).
    - paths (string[]): Only these files or directories, relative to the project root.

git_diff(staged=false, base?, target?, paths?, stat=false, context=3)
    A diff, like `git diff`. Unstaged changes by default; `staged` for what is
    in the index; `base` (and `target`) to compare commits instead, e.g. base
    `main` for everything since main, or base `HEAD~3` target `HEAD`. `stat`
    for the per-file summary alone.
    - staged (boolean): Diff the index against HEAD (or `base`) instead of the work tree.
    - base (string): Commit to diff from.
    - target (string): Commit to diff to; needs base.
    - paths (string[]): Only these files or directories, relative to the project root.
    - stat (boolean): Only the --stat summary.
    - context (integer): Lines of context.

git_log(ref?, paths?, max_count=20, full=false, patch=false)
    Commit history, like `git log`. One line per commit (hash, date, author,
    subject) unless `full` (whole messages) or `patch` (each commit's diff).
    `ref` is where to start, or a range like `main..HEAD`; `paths` keeps
    commits that touched them.
    - ref (string): Branch, commit or range; HEAD if omitted.
    - paths (string[]): Only these files or directories, relative to the project root.
    - max_count (integer): At most this many commits.
    - full (boolean): Whole commit messages.
    - patch (boolean): Each commit's diff as well.

git_show(ref="HEAD", paths?, stat=false)
    One commit, like `git show`: its message and its diff (or `stat` for the
    summary only). Files the settings deny are left out of the diff.
    - ref (string): The commit; HEAD if omitted.
    - paths (string[]): Only these files or directories, relative to the project root.
    - stat (boolean): The --stat summary instead of the diff.

git_blame(path, start?, end?, ref?)
    Who last changed each line of a file, and in which commit, like `git
    blame`. `start` and `end` limit it to a range of lines.
    - path (string, required): The file, relative to the project root.
    - start (integer): First line, 1-based.
    - end (integer): Last line.
    - ref (string): Blame as of this commit instead of the work tree.

git_branch(all=false)
    Branches, like `git branch -vv`: the current one starred, each with its
    last commit and the upstream it tracks. `all` adds remote-tracking
    branches (as last fetched; nothing is fetched).
    - all (boolean): Include remote-tracking branches.

run_script(name?, args?, hosts?, sync=true, proxy?)
    Run a script defined in .gci/settings.json -- the build, the tests, git, anything the
    project needs a process for. Only the scripts listed there run, and extra
    args are accepted only by entries that allow them. Answers with the exit
    code, the time taken and the combined stdout/stderr. With `hosts`, it runs
    on each of them at once instead: this tree is synced to each host first,
    and the answer has a section per host. Call with no name to list the
    scripts and hosts.
    - name (string): The script's key in .gci/settings.json.
    - args (string[]): Arguments appended to its command, for scripts that take them.
    - hosts (string[]): Host names from .gci/settings.json's remote.hosts (or addresses/URLs); `local` is this checkout. Omit to run here alone.
    - sync (boolean): Sync this tree to each host before running.
    - proxy (boolean): true: reach hosts through .gci/settings.json's remote.proxy; false: directly only. Unset: directly, falling back to the proxy.

sync(hosts, dry_run=false, proxy?)
    Bring each host's tree level with this one, without running anything:
    changed files are sent and files the host has that this tree does not are
    deleted there. With dry_run, report what would be sent and deleted.
    - hosts (string[], required): Host names from .gci/settings.json's remote.hosts (or addresses/URLs); `local` is this checkout.
    - dry_run (boolean): Report without sending or deleting.
    - proxy (boolean): true: reach hosts through .gci/settings.json's remote.proxy; false: directly only. Unset: directly, falling back to the proxy.

hosts(proxy?)
    Every host in .gci/settings.json: whether it answers, its platform, whether it runs
    this version of the tools, and what it is running now.
    - proxy (boolean): true: reach hosts through .gci/settings.json's remote.proxy; false: directly only. Unset: directly, falling back to the proxy.

kill(hosts, proxy?)
    Stop whatever script each host is running, with its whole process tree.
    - hosts (string[], required): Host names from .gci/settings.json's remote.hosts (or addresses/URLs); `local` is this checkout.
    - proxy (boolean): true: reach hosts through .gci/settings.json's remote.proxy; false: directly only. Unset: directly, falling back to the proxy.

restart()
    Restart this MCP server in place to pick up edits to its own code. The new
    code is started once first and the restart refused if it fails, so a broken
    edit leaves this server running. Refused while other calls are running,
    since their answers would be lost. The tool list is re-announced afterwards.

self_test(timeout=300)
    Run the tools' own self-test: every tool against a scratch tree, a real
    `gci serve` over loopback, and a real MCP server restarted mid-session. Run
    it after editing this package, before `restart`, to check the edit. It runs
    in a fresh process, so it tests the code on disk rather than the code this
    server loaded, and touches nothing outside its scratch directories.
    - timeout (integer): Seconds before it is killed.

list_tools(names?, brief=false)
    List every tool this server offers: its signature, what it does and its
    parameters, plus any the settings switch off. The same tools a client's
    tools/list sees, laid out for reading. Pass `names` for just those.
    - names (string[]): Only these tools.
    - brief (boolean): One line per tool: its signature and first sentence.
```
