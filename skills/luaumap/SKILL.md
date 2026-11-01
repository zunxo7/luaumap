---
name: luaumap
description: Structural index of a Rojo/Luau Roblox codebase. Use BEFORE reading whole .luau files or grepping across src - module API summaries, where a function is defined, who calls it (incl. object methods), remote/attribute/tag wiring, who reads a config field.
---

# luaumap

Run `python "${CLAUDE_SKILL_DIR}" <cmd>` from the repo root. Needs Python 3.8+ (stdlib only): try `python3`
or `py -3` if `python` fails. If none works, tell the user once that luaumap needs Python 3.8+
(python.org) and use Grep/Read meanwhile. The index lives in `.luaumap/index.json` (gitignore it); every query refreshes changed
files itself. `build` forces a full rebuild.

**Session start:** run `map` once (ranked module overview, ~1.5k tokens) instead of listing folders or opening files.

**Ask the index before Reading a whole file or a wide grep:**

| question | command |
|---|---|
| what a module exposes / requires | `summary Shop` (`--docs`, `--types`, `--all`) |
| where a function is defined | `def Shop.buy` / `def buy` |
| who calls it / what it calls | `callers Shop.buy`, `callers Gun.fire` (obj:fire()), `calls Shop.buy`, `callers Shop` |
| remote wiring, who fires/listens | `remote ItemBought` (no name = list all) |
| attribute / CollectionService tag sites | `attr Health`, `tag Enemy` |
| who reads a config field | `reads Config.MaxHealth` |
| dependencies | `requires X`, `required-by X` (`--deep`) |
| name search | `find slot` (`--docs` searches comments too) |

Then Read only the lines you need (`offset`/`limit` around the reported `:line`).

- Modules: name, folder+name (`server/Util`) or path. Same-named modules are picked automatically (first line
  says `picked ... (also: ...)`); pass `--from <file you are working in>` to prefer its side/folder.
- Output: grouped by file, `fn:line`, `top` = module level, `fn&` = passed as a value, `op~` = through a
  wrapper function, `op?` = name only inferred from a field (verify), `x=` = assignment,
  [S]/[C]/[sh] = server/client/shared. `--budget N` / `--limit N` cap it.
- Partly known names are globs (`Slot*Tier`); `attr Slot1Tier` and `attr Slot*` both find them.
- Comments can be stale: signatures and wiring come from code.

**Fall back to Grep/Read** for names that only exist at runtime (built from data, `GetAttributes()`/`GetTags()`
loops, user input) and methods on values from data structures (`list[i]:fn()`) - `doctor` lists these - and for
function bodies or files outside the Rojo tree.
