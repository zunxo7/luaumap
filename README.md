# Luaumap

A token-cheap map of your Roblox/Luau codebase for AI coding agents: Claude Code, Codex, Cursor, Gemini CLI, GitHub Copilot, Windsurf and any other agent that supports [Agent Skills](https://agentskills.io).

Coding agents burn most of their context reading whole files and grepping to answer simple questions: what does this module expose, who fires this remote, who sets this attribute. Luaumap indexes a Rojo project once and answers those questions in a few lines, so the agent opens only the lines it actually needs.

| Question (measured on a ~500-file Roblox game) | Luaumap | Usual way |
|---|---|---|
| What does a 56 KB server module expose | 2.8K chars | 55.6K (read the file) |
| Who fires and listens to a remote | 202 | 602 (grep, then grep the variable) |
| Who reads a config field | 82 | 952 |
| Who calls an object method (`profile:EndSession()`) | 46 | 3.6K (grep, then read the file) |
| Project overview | 5.3K | 17.8K (file list alone) |

It knows Roblox: Rojo paths, `require(script.Parent.X)`, remotes and their roles (`FireServer`, `OnServerEvent`, `OnServerInvoke`…), attributes, CollectionService tags, server/client/shared sides, and objects built with `Class.new` or `setmetatable`.

## Requirements

- **Python 3.8 or newer.** That's the only dependency: no pip packages, and no Rojo, Luau or Studio install, since it reads your files directly. Check with `python --version` (or `python3`/`py`); get it from [python.org](https://www.python.org/downloads/) if needed. If Python is missing, the skill tells you and the agent falls back to normal file reading.
- **A Rojo project** (`default.project.json`, or any Rojo-compatible `*.project.json` such as Argon's) gives full results. Without one it still indexes the folders, but requires that go through game services (`ReplicatedStorage.Shared.X`) can't be linked.

## Install

Luaumap is an [Agent Skill](https://agentskills.io), the open skill format that most coding agents now read. The agent picks it up on its own when it works in a Luau project.

### Any agent

```
npx skills add zunxo7/luaumap
```

The [skills CLI](https://github.com/vercel-labs/skills) (needs Node.js) detects the agents you have and installs Luaumap into each one: Claude Code, Codex, Cursor, Gemini CLI, GitHub Copilot, Windsurf, Cline, OpenCode, Roo Code, Kiro and [many more](https://github.com/vercel-labs/skills/blob/main/docs/agents.md). Add `-g` to install it for all your projects instead of the current one.

### Claude Code plugin

```
/plugin marketplace add zunxo7/luaumap
/plugin install luaumap@luaumap
```

Updates arrive through `/plugin`.

### Manual

Download this repository and copy its `skills/luaumap` folder into your agent's skills folder, for example `.claude/skills/`, `.agents/skills/` (Codex, Cursor, Gemini CLI, Copilot, OpenCode) or `.windsurf/skills/`.

### Plain terminal

It is a normal CLI that needs only Python, so you can run it yourself too:

```
python path/to/skills/luaumap map
```

## Use

Run commands from your project root. The first query builds the index in `.luaumap/` (add it to `.gitignore`); later queries refresh changed files by themselves in well under a second.

| Question | Command |
|---|---|
| Project overview, ranked by importance | `map` |
| What a module exposes and requires | `summary Shop` (`--docs`, `--types`, `--all`) |
| Where a function is defined | `def Shop.buy` or `def buy` |
| Who calls it / what it calls | `callers Shop.buy`, `callers Gun.fire` (finds `gun:fire()`), `calls Shop.buy` |
| Remote wiring | `remote ItemBought`, or `remote` to list all |
| Attribute and tag sites | `attr Health`, `tag Enemy` |
| Who reads a config field | `reads Config.MaxHealth` |
| Dependencies | `requires X`, `required-by X` (`--deep`) |
| Name search | `find slot` (`--docs` searches comments too) |
| What the index can't see | `doctor` |

Example (`luaumap` here is `python path/to/skills/luaumap`):

```
$ luaumap summary Gun
shared/Gun.luau module shared 28L used-by:1
req: Net Sight
exports:
 new():11
 fire():18
 reload(self):23
remotes: Shoot(FireServer)

$ luaumap remote Shoot
shared/Gun.luau [sh]: get:13 FireServer:19,24

$ luaumap def fire
shared/Gun.luau:18 Gun:fire()
shared/Net.luau:11 Net.fire(name, ...)
```

Output is grouped by file as `function:line`. `top` means module level, `[S]`/`[C]`/`[sh]` mean server/client/shared, `op~` means through a wrapper function, `op?` means the name was inferred from a field name and should be checked. Partly known names are kept as patterns (`"Slot" .. i .. "Tier"` becomes `Slot*Tier`), so `attr Slot1Tier` and `attr Slot*` both find them. `--budget N` (tokens) and `--limit N` cap any answer.

### Config

Optional `.luaumap.json` at the repo root:

```json
{
  "project": "default.project.json",
  "roots": ["extra/dir"],
  "exclude": ["src/x/*"],
  "include_packages": false,
  "remote_getters": ["Net.remote"],
  "constructors": ["new", "New", "create", "Create"],
  "map_exclude": ["*.story.lua*", "*/Stories/*"]
}
```

Everything has a default; most projects need no config.

## How it works

- Its own Luau lexer (type annotations, backtick strings, compound operators, long brackets), so Luau-only syntax never breaks a parse.
- Maps every file to its Roblox instance path from the Rojo project file and resolves `require`s the way Roblox and luau-lsp do.
- Two passes: each file is scanned on its own (in parallel), then a link step connects calls, remotes, attributes and object types across modules.
- Names built at runtime are resolved statically where possible: constants, concatenation, string interpolation, literal loops, wrapper functions with literal arguments, remote registry modules (`Remotes.X = makeEvent("X")`), values passed between modules.
- Objects are typed from constructors, `setmetatable`, type annotations, call-site arguments and table inserts, so `obj:method()` links to its class.
- String requires that the Rojo tree can't place fall back to a path relative to the file, as in Luau's require-by-string.
- Output is ranked and trimmed to a token budget, signatures only, never function bodies.

## What it does on your machine

- The agent runs one local Python script, `skills/luaumap/__main__.py`, with the command it picked (`map`, `summary`, …).
- The script reads your project's `.lua`/`.luau` files, the Rojo project file and the optional `.luaumap.json`.
- It writes one file, the index at `.luaumap/index.json` in your project.
- It makes no network connections, sends no data anywhere, and needs no account or API key.

## Limits

- Names that only exist at runtime stay unknown (loops over `GetAttributes()`/`GetTags()`, names looked up from data, user input). `doctor` lists every one so the agent knows to fall back to grep.
- Methods on values whose type can't be traced statically aren't linked to a class.
- It indexes structure, not behaviour: read the function body when you need logic.

Tested on two real Roblox games of 224 and 518 files: a full build takes about a second, a query about 0.3 s. Run the test suite with `python -m unittest discover -s skills/luaumap/tests`.

## Credits

Luaumap was built by [Zun](https://github.com/zunxo7) with [Claude Code](https://claude.com/claude-code). It borrows ideas, not code, from these projects:

- **[graphify](https://github.com/Graphify-Labs/graphify)**: the original inspiration. The two-pass design with a project-wide call-resolution pass, conservative type inference, ambiguity tie-breakers, EXTRACTED/INFERRED confidence labels, and the query-CLI-plus-skill model.
- **[aider's repo map](https://github.com/Aider-AI/aider/blob/main/aider/repomap.py)** by Paul Gauthier: signature-only output, PageRank ranking, and fitting the answer to a token budget.
- **[luau-lsp](https://github.com/JohnnyMorganz/luau-lsp)** by JohnnyMorganz: which require forms to accept, the Roblox service mappings (PlayerScripts, PlayerGui, StarterGear), `.luaurc` aliases, and the filesystem fallback for string requires.
- **[Luau](https://luau.org/types/object-oriented-programs)** docs and RFCs ([metatable types](https://rfcs.luau.org/metatable-type-functions.html), [require by string](https://rfcs.luau.org/new-require-by-string-semantics.html)): the class type patterns used to type `obj:method()` calls, and relative require resolution.
- **[TypeScript template literal types](https://www.typescriptlang.org/docs/handbook/2/template-literal-types.html)**: keeping a partly known string as a pattern that concrete names match.
- **[Pyright](https://github.com/microsoft/pyright/blob/main/docs/type-inference.md)** by Microsoft: call-site type inference, adapted to type function parameters.
- **[Moonwave](https://eryn.io/moonwave/)** by evaera: doc comment styles and placement.
- **[full-moon](https://github.com/Kampfkarren/full-moon)** by Kampfkarren: its changelog served as the checklist of Luau syntax edge cases.
- **[Universal Ctags](https://docs.ctags.io)**: scope-qualified names (`Class.member`) to tell same-named symbols apart.
- **[SCIP](https://github.com/sourcegraph/scip)** by Sourcegraph: string symbol keys and keeping bad data contained.
- **[Rojo](https://rojo.space)**: the project format the whole index is built on.

## License

[MIT](LICENSE)
