# wutchatwusdat

Search Claude Code, Codex and OpenCode conversations from one terminal. Press Enter to resume a result in the original client and project directory.

```sh
nix run
nix run . -- 'database migration'
nix run . -- --provider codex
```

To install from another flake, add this input and use its package in your Home Manager configuration:

```nix
inputs.wutchatwusdat = {
  url = "github:phonkd/wutchatwusdat";
  inputs.nixpkgs.follows = "nixpkgs";
};

# In a Home Manager module with inputs and pkgs in scope:
home.packages = [ inputs.wutchatwusdat.packages.${pkgs.system}.default ];
```

The installed command is `wutchatwusdat` on Linux and macOS.

The clients must already be on your `PATH`. The flake supplies Python and SQLite; no Python packages or services are needed.

Type to search all user and assistant text, titles and project paths. Keystrokes appear immediately; search runs in the background after a 200 ms typing pause. Colored client tabs, bordered panes and highlighted key controls separate the interface. Wide terminals show results and the preview side by side. Results use SQLite FTS5 relevance ranking with boosted titles, English stemming, accent folding and prefix matching. Multiple words are combined with AND; use `"quoted phrases"` for consecutive words. Chats appear only after you enter a search. `--raw` enables FTS5 operators such as `sqlite OR postgres`.

- Up/Down: select a conversation
- Enter: resume the selected conversation
- Tab: cycle all clients / Claude / Codex / OpenCode
- Page Up/Down: scroll the preview (matching excerpt followed by transcript)
- Ctrl-U: clear the query
- Ctrl-R: refresh the index
- Escape or Ctrl-C: exit

```sh
nix run . -- --index-only
nix run . -- 'sqlite' --json
nix run . -- --no-refresh
nix run . -- --help
```

Search displays up to 1,000 results, with more terminal space devoted to the result list. JSON output includes the transcript and metadata. Indexing runs automatically on launch and skips unchanged conversations. Source files are only read. The cache contains searchable chat text and is stored with owner-only permissions at `$XDG_CACHE_HOME/wutchatwusdat/index.sqlite3` (default `~/.cache/wutchatwusdat/index.sqlite3`). Delete it to rebuild from scratch, or use `--db PATH` for a separate index.

Sources:

| Client | Default storage | Resume command |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/*/*.jsonl` | `claude --resume ID` |
| Codex | `~/.codex/sessions/**/*.jsonl` and `archived_sessions` | `codex resume ID` |
| OpenCode | `~/.local/share/opencode/opencode.db` | `opencode --session ID` |

`CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `XDG_DATA_HOME` are respected. Override paths with `--claude-home`, `--codex-home` and `--opencode-home`. OpenCode support targets its SQLite storage format. Claude subagent transcripts, tool output, reasoning and system/developer instructions are excluded. Only locally stored conversations are indexed. If a project's directory was removed, resuming reports the missing directory.

Development:

```sh
nix develop
python3 -m unittest discover -s tests -v
nix build
```

The Nix build runs the test suite. Tests cover source parsing, updates/deletions, search semantics and client launch arguments without launching a real chat.
