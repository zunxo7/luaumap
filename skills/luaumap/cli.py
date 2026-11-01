"""luaumap: token-cheap structural queries over a Rojo/Luau codebase.

usage: python <luaumap-folder> <command> [args] [--budget N] [--limit N] [--root DIR] [--from FILE] [--no-refresh]

  build [--changed]        rebuild the index (queries auto-refresh changed files anyway)
  map [--dir PREFIX]       project overview ranked by importance (~1500 tokens; --all keeps stories)
  summary <module|path>    signatures + wiring of one module, instead of reading it
          [--docs] first doc line per item   [--types] typed params   [--all] locals/tables/types too
  doc <module|Mod.member>  full doc comment
  def <name>               where defined: Shop.buy | buy | server/Shop.buy
  callers <Mod.fn|Mod>     who calls it (cross-module + same-file), grouped by file
  calls <Mod.fn|Mod>       what it calls / fires / sets
  requires <mod> [--deep]  required-by <mod> [--deep]
  remote [Name]            remote sites + role (FireServer/OnServerEvent/...); no name = list all
  attr [Name]              attribute get/set/changed sites; no name = list all
  tag [Name]               CollectionService tag sites; no name = list all
  reads <Alias.Field|Field> who reads a module field (Config.MaxHealth, MaxHealth)
  find <text> [--docs]     search module/def/remote/attr/tag names (and docs)
  stats | doctor           index size / blind spots (unresolved names, untyped method calls, requires)

Ambiguous module names are resolved automatically (exact path > same side/folder as --from FILE >
non-story > most required); the pick and the alternatives are printed on the first line.

Output: paths are repo-relative, grouped by file; `name:line`; [S]=server [C]=client [sh]=shared;
  op~ = via a wrapper function, op? = remote name inferred from a field; Name*Glob = partly known name.
Config (optional) .luaumap.json at repo root: {"remote_getters": ["Net.remote"], "exclude": ["src/x/*"],
  "roots": ["extra/dir"], "project": "default.project.json", "include_packages": false,
  "constructors": ["new", "New", "create", "Create"], "map_exclude": ["*.story.lua*", "*/Stories/*"]}
"""
import fnmatch
import math
import os
import re
import sys
import time

import index as ix
import project

CPT = 3.5  # chars per token, for --budget
DEFAULT_BUDGET = {"map": 1500, "summary": 2500, "doc": 800}
MAP_EXCLUDE = ["*.story.lua", "*.story.luau", "*.storybook.lua", "*.storybook.luau", "*/Stories/*", "*/stories/*"]
# Roblox engine / Luau library methods: an untyped `x:Connect()` is almost always the engine's, so the doctor's
# "unlinked project method" count leaves these out (the raw total is printed too).
ENGINE_METHODS = frozenset("""Connect Once Wait Disconnect Fire Destroy Clone GetChildren GetDescendants FindFirstChild
FindFirstChildOfClass FindFirstChildWhichIsA FindFirstAncestor FindFirstAncestorOfClass FindFirstAncestorWhichIsA
WaitForChild IsA IsDescendantOf IsAncestorOf GetAttribute SetAttribute GetAttributes GetAttributeChangedSignal
GetPropertyChangedSignal ClearAllChildren GetFullName Play Stop Pause Cancel AdjustSpeed LoadAnimation MoveTo Move
Kick GetPivot PivotTo ScaleTo GetBoundingBox GetExtentsSize ApplyImpulse ApplyAngularImpulse SetNetworkOwner GetMass
AddTag RemoveTag HasTag GetTags Lerp Inverse ToWorldSpace ToObjectSpace PointToWorldSpace PointToObjectSpace
VectorToWorldSpace VectorToObjectSpace Dot Cross Abs Floor Ceil Max Min FuzzyEq GetAsync SetAsync UpdateAsync
RemoveAsync IncrementAsync GetService InvokeServer InvokeClient FireServer FireClient FireAllClients Invoke TakeDamage
ChangeState GetState EquipTool UnequipTools LoadCharacter GetMouse Raycast GetPartsInPart GetPartBoundsInBox
GetPartBoundsInRadius Create GetPlayerFromCharacter GetPlayers GetUserIdFromNameAsync PromptProductPurchase
PromptGamePassPurchase UserOwnsGamePassAsync GetRankInGroup IsInGroup BindToClose GetDataStore GetOrderedDataStore
GetSortedAsync GetCurrentPage AdvanceToNextPageAsync Resume Emit Clear JSONEncode JSONDecode GenerateGUID
format sub gsub match gmatch find lower upper len rep split byte reverse""".split())
SIDE = {"server": "S", "client": "C", "shared": "sh"}


# ---------------------------------------------------------------- output
class Out:
    def __init__(self, budget, limit):
        self.lines = []
        self.budget = budget
        self.limit = limit

    def __call__(self, line=""):
        self.lines.append(line)

    def wrap(self, prefix, items, width=200, sep=" "):
        """prefix + items joined, wrapped at width (continuation lines indented)."""
        cur = prefix
        first = True
        for it in items:
            add = it if first else sep + it
            if len(cur) + len(add) > width and not first:
                self.lines.append(cur)
                cur = "  " + it
            else:
                cur += add
            first = False
        if cur.strip():
            self.lines.append(cur)

    def emit(self):
        lines = self.lines
        cut = len(lines)
        if self.limit and self.limit > 0:
            cut = min(cut, self.limit)
        if self.budget and self.budget > 0:
            chars = 0
            maxc = int(self.budget * CPT)
            for i in range(cut):
                chars += len(lines[i]) + 1
                if chars > maxc:
                    cut = max(i, 1)
                    break
        out = lines[:cut]
        if cut < len(lines):
            out.append("+%d more lines (raise --budget/--limit)" % (len(lines) - cut))
        sys.stdout.write("\n".join(out) + ("\n" if out else ""))


def lines_fmt(pairs):
    """[(label, line)] -> 'label:1,2 other:3' keeping first-seen label order."""
    order, by = [], {}
    for label, ln in pairs:
        if label not in by:
            by[label] = []
            order.append(label)
        if ln not in by[label]:
            by[label].append(ln)
    return " ".join("%s:%s" % (lb, ",".join(str(x) for x in by[lb])) if lb else ",".join(str(x) for x in by[lb])
                    for lb in order)


def norm(q):
    return q.replace(":", ".")


# ---------------------------------------------------------------- query model
class Q:
    def __init__(self, idx):
        self.idx = idx
        self.mods = idx["modules"]
        self.res = idx.get("res", {})
        self.getters = idx.get("getters", [])
        self._disp = None
        self._defs = None
        self._rev = None
        self.ctx = None  # module path given with --from

    def calls(self, path):
        """Call records: scanned + object/receiver calls the linker typed (unlinked 'o'/'x' inputs dropped)."""
        m = self.mods[path]
        return [c for c in m["calls"] if c[4] not in ("o", "x")] + m.get("x", {}).get("c", [])

    def sites(self, path, field):
        """(line, caller, name, op) for remotes/attrs/tags: literal names + names the linker resolved."""
        m = self.mods[path]
        out = [(r[0], r[1], r[2], r[3]) for r in m[field] if r[2]]
        xk = {"remotes": "r", "attrs": "a", "tags": "t"}[field]
        for r in m.get("x", {}).get(xk, []):  # op~ = through a wrapper function, op? = name inferred from a field
            mark = "~" if str(r[-1]).startswith("via ") else "?" if r[-1] == "inferred" else ""
            out.append((r[0], r[1], r[2], r[3] + mark))
        return out

    def required_by(self):
        if self._rev is None:
            self._rev = {}
            for p, m in self.mods.items():
                for r in m["requires"]:
                    t = self.resolve(r[2])
                    if t:
                        self._rev.setdefault(t, set()).add(p)
        return self._rev

    def mname(self, path):
        return self.mods[path]["inst"].rsplit(".", 1)[-1]

    def resolve(self, target):
        r = self.res.get(target, "")
        return r[1:] if r.startswith("~") else r

    def side(self, path):
        return SIDE.get(self.mods[path]["side"], "?")

    def disp(self, path):
        """Short unique name for a module: Units, or server/Units when the name is ambiguous (valid query)."""
        if self._disp is None:
            byname = {}
            for p, m in self.mods.items():
                byname.setdefault(m["inst"].rsplit(".", 1)[-1], []).append(p)
            self._disp = {}
            for name, ps in byname.items():
                for p in ps:
                    if len(ps) == 1:
                        self._disp[p] = name
                        continue
                    dirs = p.split("/")[:-1]
                    best = p
                    for d in reversed(dirs):  # nearest folder that tells it apart
                        if sum(1 for o in ps if d in o.split("/")[:-1]) == 1:
                            best = d + "/" + name
                            break
                    self._disp[p] = best
        if path.startswith("ext:"):
            return os.path.splitext(os.path.basename(path[4:]))[0]
        return self._disp.get(path, path)

    def find_modules(self, q):
        q = q.strip().replace("\\", "/")
        if os.path.isabs(q):
            q = os.path.relpath(q, self.idx["root"]).replace("\\", "/")
        if q in self.mods:
            return [q]
        for ext in (".server.luau", ".client.luau", ".server.lua", ".client.lua", ".luau", ".lua"):
            if q.endswith(ext):
                q = q[: -len(ext)]
                break
        parts = [p.lower() for p in re.split(r"[/.]", q) if p]
        if not parts:
            return []
        out = []
        for p, m in self.mods.items():
            name = m["inst"].rsplit(".", 1)[-1].lower()
            stem = os.path.basename(p).split(".")[0].lower()
            if parts[-1] not in (name, stem):
                continue
            pool = [c.lower() for c in p.split("/")[:-1]] + [c.lower() for c in m["inst"].split(".")[:-1]] + [stem]
            pos = 0
            ok = True
            for want in parts[:-1]:
                try:
                    pos = pool.index(want, pos) + 1
                except ValueError:
                    try:
                        pos = pool.index(want) + 1
                    except ValueError:
                        ok = False
                        break
            if ok:
                out.append(p)
        return sorted(out)

    def defs(self, path):
        return self.mods[path]["defs"]

    def short(self, path, d):
        """Member name without the module-table root (Shop.buy -> buy)."""
        nm = norm(d[2])
        table = self.mods[path].get("table")
        if table and nm.startswith(table + "."):
            return nm[len(table) + 1:]
        return nm

    def find_defs(self, q, fuzzy=True):
        qn = norm(q)
        parts = qn.split(".")
        hits = []
        seen = set()

        def add(path, d):
            key = (path, d[0], d[2])
            if key not in seen:
                seen.add(key)
                hits.append((path, d))

        for s in range(len(parts) - 1, 0, -1):
            mq, mem = ".".join(parts[:s]), ".".join(parts[s:])
            for path in self.find_modules(mq):
                for d in self.defs(path):
                    if self.short(path, d) == mem or norm(d[2]) == mem:
                        add(path, d)
        if not hits:
            for path, m in self.mods.items():
                for d in m["defs"]:
                    dn = norm(d[2])
                    if dn == qn or (len(parts) == 1 and dn.rsplit(".", 1)[-1] == qn):
                        add(path, d)
        if not hits and fuzzy:
            ql = qn.lower()
            for path, m in self.mods.items():
                for d in m["defs"]:
                    if ql in norm(d[2]).lower():
                        add(path, d)
        hits.sort(key=lambda h: (-h[1][8], h[1][7], h[1][3] in ("value", "const", "field"), h[0], h[1][0]))
        return hits

    def sig(self, d, types=False):
        """'(a, b)' for functions (d[5] holds the raw typed signature), '' for values/tables/types."""
        if not d[5]:
            return ""
        return d[5] if types else "(%s)" % d[4]


# ---------------------------------------------------------------- commands
def cmd_build(q, a, out):
    where = os.path.relpath(ix.index_path(q.idx["root"], q.idx["config"]), q.idx["root"]).replace("\\", "/")
    out("indexed %d files in %.2fs (%d rescanned) -> %s" % (len(q.mods), q.idx["secs"], q.idx["scanned"], where))


def pagerank(q):
    nodes = list(q.mods)
    pos = {p: i for i, p in enumerate(nodes)}
    n = len(nodes)
    edges = [dict() for _ in range(n)]
    for p, m in q.mods.items():
        i = pos[p]
        for r in m["requires"]:
            t = q.resolve(r[2])
            if t in pos and t != p:
                edges[i][pos[t]] = edges[i].get(pos[t], 0) + 1.0
        for c in q.calls(p):
            if c[2]:
                t = q.resolve(c[2])
                if t in pos and t != p:
                    edges[i][pos[t]] = edges[i].get(pos[t], 0) + 0.25
        for r in m["reads"]:
            if r[2]:
                t = q.resolve(r[2])
                if t in pos and t != p:
                    edges[i][pos[t]] = edges[i].get(pos[t], 0) + 0.25
    for e in edges:
        for k in e:
            e[k] = 1 + math.log1p(e[k])
    rank = [1.0 / n] * n if n else []
    for _ in range(40):
        nxt = [0.15 / n] * n
        dangling = 0.0
        for i in range(n):
            tot = sum(edges[i].values())
            if not tot:
                dangling += rank[i]
                continue
            share = 0.85 * rank[i] / tot
            for j, w in edges[i].items():
                nxt[j] += share * w
        dshare = 0.85 * dangling / n
        rank = [x + dshare for x in nxt]
    return {nodes[i]: rank[i] for i in range(n)}


def usage_counts(q):
    """(module path, first member) -> references from OTHER modules."""
    cnt = {}
    for p, m in q.mods.items():
        for c in q.calls(p):
            if c[2]:
                t = q.resolve(c[2])
                if t and t != p:
                    key = (t, c[3].split(".")[0])
                    cnt[key] = cnt.get(key, 0) + 1
        for r in m["reads"]:
            if r[2]:
                t = q.resolve(r[2])
                if t and t != p:
                    key = (t, r[3].split(".")[0])
                    cnt[key] = cnt.get(key, 0) + 1
    return cnt


def exports(q, path):
    m = q.mods[path]
    seen = set()
    out = []
    for d in m["defs"]:
        if not d[8]:
            continue
        s = q.short(path, d)
        if s in seen:
            continue
        seen.add(s)
        out.append(d)
    return out


def cmd_map(q, a, out):
    pr = pagerank(q)
    use = usage_counts(q)
    skip = [] if a.all else (q.idx["config"].get("map_exclude") or MAP_EXCLUDE)
    mods = [p for p in q.mods if (not a.dir or p.startswith(a.dir.replace("\\", "/")))
            and not any(fnmatch.fnmatch(p, g) for g in skip)]
    # Fair share per source root (src/server, src/client, ...): a root with 40% of the files gets ~40% of the
    # lines, ranked inside the root. Pure PageRank lets shared utility hubs crowd out whole sides of the game.
    groups = {}
    for p in mods:
        groups.setdefault("/".join(p.split("/")[:2]), []).append(p)
    for g in groups.values():
        g.sort(key=lambda p: -pr.get(p, 0))
    keyed = []
    for g in groups.values():
        w = len(g) / max(1, len(mods))
        keyed += [((i + 1) / w, -pr.get(p, 0), p) for i, p in enumerate(g)]
    mods = [p for _, _, p in sorted(keyed)]
    names = {}
    for p in mods:
        ex = exports(q, p)
        ranked = sorted(ex, key=lambda d: (-use.get((p, q.short(p, d).split(".")[0]), 0), d[0]))
        nm = [q.short(p, d) for d in ranked
              if q.short(p, d) != q.mods[p].get("table") and d[3] != "export type" and not q.short(p, d).startswith("__")]
        m = q.mods[p]
        if m.get("module_fn"):
            nm = ["(fn)"] + nm
        names[p] = nm

    def render(k):
        chosen = mods[:k]
        order = []
        groups = {}
        for p in chosen:
            d = p.rsplit("/", 1)[0] + "/"
            if d not in groups:
                groups[d] = []
                order.append(d)
            groups[d].append(p)
        lines = []
        for d in order:
            lines.append(d)
            fnonly = [p for p in groups[d] if names[p] == ["(fn)"]]
            if fnonly:
                lines.append(" (fn modules) " + " ".join(q.mname(p) for p in fnonly))
            for p in groups[d]:
                if p in fnonly:
                    continue
                nm = names[p]
                shown = nm[:6]
                more = " +%d" % (len(nm) - 6) if len(nm) > 6 else ""
                cls = q.mods[p]["cls"]
                tag = "" if cls == "ModuleScript" else " [%s]" % cls
                label = os.path.basename(p) if cls != "ModuleScript" else q.mname(p)
                lines.append(" %s%s%s%s" % (label, tag, (": " + " ".join(shown)) if shown else "", more))
        return lines

    budget = int((a.budget if a.budget is not None else DEFAULT_BUDGET["map"]) * CPT)
    lo, hi = 1, len(mods)
    if budget <= 0:
        lo = hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(len(x) + 1 for x in render(mid)) <= budget - 60:
            lo = mid
        else:
            hi = mid - 1
    for line in render(lo):
        out(line)
    if lo < len(mods):
        out("+%d lower-ranked modules (ranked by dependents; --budget N or --dir PREFIX)" % (len(mods) - lo))
    out.budget = 0


def rank_candidates(q, cands):
    """Order same-named modules: same side/folder as --from (graphify's path-proximity tie-break),
    then not a story/test file, then most required (aider's "more references, more relevant")."""
    rev = q.required_by()
    ctx = q.ctx
    ctx_side = q.mods[ctx]["side"] if ctx in q.mods else None
    ctx_dirs = ctx.split("/")[:-1] if ctx else []

    def key(p):
        dirs = p.split("/")[:-1]
        common = 0
        for x, y in zip(dirs, ctx_dirs):
            if x != y:
                break
            common += 1
        story = any(fnmatch.fnmatch(p, g) for g in MAP_EXCLUDE) or "/test" in p.lower() or "/spec" in p.lower()
        return (0 if ctx_side and q.mods[p]["side"] == ctx_side else 1, -common, story, -len(rev.get(p, ())), p)

    return sorted(cands, key=key)


def pick_module(q, arg, out, quiet=False):
    """One module for a query; ambiguity is resolved, never asked back - the alternatives are printed."""
    c = q.find_modules(arg)
    if len(c) == 1:
        return c[0]
    if not c:
        out("no module matches %r" % arg)
        return None
    c = rank_candidates(q, c)
    if not quiet:
        out("picked %s (also: %s)" % (c[0], " | ".join(c[1:])))
    return c[0]


def cmd_summary(q, a, out):
    path = pick_module(q, a.name, out)
    if not path:
        return
    m = q.mods[path]
    used = sum(1 for p, mm in q.mods.items() if p != path and any(q.resolve(r[2]) == path for r in mm["requires"]))
    kind = {"ModuleScript": "module", "Script": "Script", "LocalScript": "LocalScript"}[m["cls"]]
    out("%s %s %s %dL used-by:%d" % (path, kind, m["side"], m["lines"], used))
    if a.docs and m["doc"]:
        out("doc: " + first_line(m["doc"]))
    reqs, inline = [], []
    for r in m["requires"]:
        t = q.resolve(r[2])
        if not t:
            (reqs if r[1] else inline).append((r[1] or r[3]) + "?")
            continue
        d = q.disp(t)
        if not r[1]:
            inline.append(d)
        elif r[1] == d or r[1] == (q.mname(t) if t in q.mods else d):
            reqs.append(r[1])  # alias = module name: exact target via `requires`, not worth tokens here
        else:
            reqs.append("%s=%s" % (r[1], d))
    if reqs:
        out.wrap("req: ", dedupe(reqs))
    if inline:
        out.wrap("req inline: ", dedupe(inline))
    if m["services"]:
        out.wrap("svc: ", m["services"])
    ex = exports(q, path)
    if ex:
        out("exports:")
        for d in ex:
            out(" " + item(q, path, d, a))
    fns = [d for d in m["defs"] if d[7] == 0 and not d[8] and d[5]]
    if fns:
        if a.docs or a.types:
            out("locals:")
            for d in fns:
                out(" " + item(q, path, d, a))
        else:
            out.wrap("locals: ", ["%s:%d" % (d[2], d[0]) for d in fns])
    if a.all:
        other = [d for d in m["defs"] if not d[8] and d not in fns and d[7] == 0]
        if other:
            out.wrap("other: ", ["%s:%d(%s)" % (d[2], d[0], d[3]) for d in other])
        nested = [d for d in m["defs"] if d[7] > 0 and d[3] in ("local", "member", "method", "var", "global")]
        if nested:
            out.wrap("nested: ", ["%s:%d<%s" % (d[2], d[0], d[9] or "?") for d in nested])
    wiring(q, path, out, lines=False)


def wiring(q, path, out, caller=None, lines=True):
    """Remote/attribute/tag use. lines=False: names + roles only (details via remote/attr/tag commands)."""
    rem = {}
    for r in q.sites(path, "remotes"):
        if caller is None or norm(r[1]) == caller:
            rem.setdefault(r[2], []).append((r[3], r[0]))
    if rem and lines:
        out.wrap("remotes: ", ["%s(%s)" % (k, lines_fmt(sorted(v, key=lambda x: x[1]))) for k, v in rem.items()], sep=" ")
    elif rem:
        items = []
        for k, v in rem.items():
            roles = dedupe(r for r, _ in v if r != "get") or ["get"]
            items.append("%s(%s)" % (k, ",".join(roles)))
        out.wrap("remotes: ", items)
    for label, recs in (("attrs", q.sites(path, "attrs")), ("tags", q.sites(path, "tags"))):
        at = {}
        for r in recs:
            if caller is None or norm(r[1]) == caller:
                at.setdefault(r[2], []).append((r[3], r[0]))
        if not at:
            continue
        if lines:
            out.wrap(label + ": ", ["%s(%s)" % (k, lines_fmt(v)) for k, v in at.items()])
        else:
            byop = {}
            for k, v in at.items():
                for op in dedupe(o for o, _ in v):
                    byop.setdefault(op, []).append(k)
            out.wrap(label + " ", ["%s: %s;" % (op, " ".join(ks)) for op, ks in byop.items()])


def dedupe(xs):
    seen = set()
    out = []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def first_line(doc, cap=110):
    for ln in doc.split("\n"):
        ln = ln.strip()
        if ln:
            return ln if len(ln) <= cap else ln[:cap] + "~"
    return ""


def item(q, path, d, a):
    name = q.short(path, d) if d[8] else d[2]
    s = name + q.sig(d, a.types) + ":%d" % d[0]
    if a.docs and d[6]:
        s += "  -- " + first_line(d[6], 100)
    return s


def cmd_doc(q, a, out):
    member = re.sub(r"\.(luau|lua)$", "", a.name)
    dotted = ("." in member or ":" in member) and "/" not in member
    hits = q.find_defs(a.name, fuzzy=False) if dotted else []
    if not hits:
        if q.find_modules(a.name):
            path = pick_module(q, a.name, out)
            out(path)
            for ln in (q.mods[path]["doc"] or "(no module doc)").split("\n"):
                out(ln)
            return
        hits = q.find_defs(a.name, fuzzy=False)
    if not hits:
        out("no def %r" % a.name)
        return
    for path, d in hits[:3]:
        out("%s:%d %s%s" % (path, d[0], d[2], q.sig(d, True)))
        for ln in (d[6] or "(no doc)").split("\n"):
            out(ln)


def cmd_def(q, a, out):
    hits = q.find_defs(a.name)
    if not hits:
        out("no def matches %r" % a.name)
        return
    for path, d in hits:
        extra = ""
        if d[7] > 0:
            extra = " <in %s" % (d[9] or "?")
        elif d[3] not in ("local", "global", "member", "method", "field", "var"):
            extra = " [%s]" % d[3]
        doc = "  -- " + first_line(d[6], 90) if a.docs and d[6] else ""
        out("%s:%d %s%s%s%s" % (path, d[0], d[2], q.sig(d, a.types), extra, doc))


def targets(q, name):
    """Resolve a function/member query to [(path, member_without_root, local_key, exported)]."""
    out = []
    for path, d in q.find_defs(name, fuzzy=False):
        out.append((path, q.short(path, d), norm(d[2]), bool(d[8])))
    return out


def cmd_callers(q, a, out):
    plain = "." not in a.name and ":" not in a.name
    tg = [] if plain and q.find_modules(a.name) else targets(q, a.name)
    if not tg:
        if q.find_modules(a.name):
            return callers_of_module(q, pick_module(q, a.name, out), out)
        out("no def %r" % a.name)
        return
    groups = {}
    for path, mem, key, exported in tg:
        for p, m in q.mods.items():
            for c in q.calls(p):
                if c[2]:  # class methods are reached through objects, so not only exported members
                    if q.resolve(c[2]) == path and (c[3] == mem or c[3].startswith(mem + ".")) and p != path:
                        groups.setdefault(p, []).append((c[1] or "top", c[0]))
                elif p == path and (c[3] == key or c[3] == mem):
                    groups.setdefault(p, []).append(((c[1] or "top") + ("&" if c[4] == "r" else ""), c[0]))
            if exported:
                for r in m["reads"]:
                    if r[2] and r[3] == mem and q.resolve(r[2]) == path:
                        groups.setdefault(p, []).append(((r[1] or "top") + "&", r[0]))
    if len(tg) > 1:
        out("defs: " + " | ".join("%s:%s" % (p, k) for p, _, k, _ in tg))
    if not groups:
        out("no callers found (dynamic or unindexed use? try grep)")
        return
    for p in sorted(groups, key=lambda x: (x not in [t[0] for t in tg], x)):
        out("%s: %s" % (p, lines_fmt(groups[p])))
    if any(lb.endswith("&") for g in groups.values() for lb, _ in g):
        out("(& = passed as a value, not called)")


def callers_of_module(q, path, out):
    groups = {}
    for p, m in q.mods.items():
        if p == path:
            continue
        for c in q.calls(p):
            if c[2] and q.resolve(c[2]) == path:
                groups.setdefault(p, []).append((c[3] or "()", c[0]))
    if not groups:
        out("no calls into %s" % path)
    for p in sorted(groups):
        out("%s: %s" % (p, lines_fmt(groups[p])))


def cmd_calls(q, a, out):
    plain = "." not in a.name and ":" not in a.name
    tg = [] if plain and q.find_modules(a.name) else targets(q, a.name)
    if tg:
        for path, mem, key, exported in tg[:2]:
            m = q.mods[path]
            if len(tg) > 1:
                out("== %s:%s" % (path, key))
            by = {}
            local = []
            for c in q.calls(path):
                if norm(c[1]) != key:
                    continue
                if c[2]:
                    t = q.resolve(c[2]) or "?" + c[2].rsplit(".", 1)[-1]
                    by.setdefault(t, []).append((c[3] or "()", c[0]))
                else:
                    local.append((q_local(m, c[3]) + ("&" if c[4] == "r" else ""), c[0]))
            for t, lst in by.items():
                out("%s: %s" % (q.disp(t) if not t.startswith("?") else t, lines_fmt(lst)))
            if local:
                out("local: " + lines_fmt(local))
            rd = {}
            for r in m["reads"]:
                if norm(r[1]) == key and r[2]:
                    t = q.resolve(r[2]) or "?" + r[2].rsplit(".", 1)[-1]
                    rd.setdefault(t, []).append((r[3] + ("=" if r[4] else ""), r[0]))
            if rd:
                out.wrap("reads: ", ["%s(%s)" % (q.disp(t) if not t.startswith("?") else t, lines_fmt(v)) for t, v in rd.items()])
            wiring(q, path, out, caller=key)
        return
    path = pick_module(q, a.name, out)
    if not path:
        return
    by = {}
    for c in q.calls(path):
        if c[2]:
            t = q.resolve(c[2]) or "?" + c[2]
            by.setdefault(t, {})
            by[t][c[3] or "()"] = by[t].get(c[3] or "()", 0) + 1
    for t, d in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
        out("%s: %s" % (q.disp(t) if not t.startswith("?") else t,
                        " ".join("%s%s" % (k, "(%d)" % v if v > 1 else "") for k, v in sorted(d.items(), key=lambda kv: -kv[1]))))


def q_local(m, key):
    t = m.get("table")
    return key[len(t) + 1:] if t and key.startswith(t + ".") else key


def req_targets(q, path):
    out = []
    for r in q.mods[path]["requires"]:
        t = q.resolve(r[2])
        out.append(t if t else "?" + (r[3] or r[2]))
    return dedupe(out)


def group_paths(paths, q):
    groups = {}
    order = []
    for p in sorted(paths):
        if p.startswith("ext:") or p.startswith("?"):
            d = "ext:" if p.startswith("ext:") else "unresolved:"
        else:
            d = p.rsplit("/", 1)[0] + "/"
        if d not in groups:
            groups[d] = []
            order.append(d)
        groups[d].append(p)
    lines = []
    for d in order:
        names = []
        for p in groups[d]:
            if p.startswith("ext:"):
                names.append(q.disp(p))
            elif p.startswith("?"):
                names.append(p[1:])
            else:
                names.append(os.path.basename(p).split(".")[0] if not os.path.basename(p).startswith("init.") else os.path.basename(p))
        lines.append((d, names))
    return lines


def cmd_requires(q, a, out):
    path = pick_module(q, a.name, out)
    if not path:
        return
    if a.deep:
        seen = set()
        stack = [path]
        while stack:
            p = stack.pop()
            for t in req_targets(q, p):
                if t not in seen and t != path:
                    seen.add(t)
                    if t in q.mods:
                        stack.append(t)
        res = seen
        out("%d modules (transitive)" % len(res))
    else:
        res = req_targets(q, path)
    for d, names in group_paths(res, q):
        out.wrap(d + " ", names)


def cmd_required_by(q, a, out):
    path = pick_module(q, a.name, out)
    if not path:
        return
    rev = {}
    for p, m in q.mods.items():
        for r in m["requires"]:
            t = q.resolve(r[2])
            if t:
                rev.setdefault(t, set()).add(p)
    if a.deep:
        seen = set()
        stack = [path]
        while stack:
            p = stack.pop()
            for s in rev.get(p, ()):
                if s not in seen and s != path:
                    seen.add(s)
                    stack.append(s)
        res = seen
        out("%d modules (transitive)" % len(res))
    else:
        res = rev.get(path, set())
    if not res:
        out("nothing requires %s" % path)
    for d, names in group_paths(res, q):
        out.wrap(d + " ", names)


def site_list(q, recs_by_file, out):
    for p in sorted(recs_by_file, key=lambda p: (q.mods[p]["side"] != "server", p)):
        out("%s [%s]: %s" % (p, q.side(p), lines_fmt(sorted(recs_by_file[p], key=lambda x: x[1]))))


def name_list(counts, out, what):
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out("%d %s names (sites):" % (len(items), what))
    out.wrap("", ["%s:%d" % kv for kv in items])


def suggest(name, names, out, what):
    nl = name.lower()
    sim = [n for n in names if nl in n.lower() or n.lower() in nl][:12]
    out("no %s %r%s" % (what, name, ("; similar: " + " ".join(sim)) if sim else ""))


def cmd_remote(q, a, out):
    simple(q, a, out, "remotes", "remote")


def cmd_attr(q, a, out):
    simple(q, a, out, "attrs", "attribute")


def cmd_tag(q, a, out):
    simple(q, a, out, "tags", "tag")


def simple(q, a, out, field, what):
    allnames = {}
    for p in q.mods:
        for r in q.sites(p, field):
            allnames[r[2]] = allnames.get(r[2], 0) + 1
    if not a.name:
        return name_list(allnames, out, what)
    name = a.name
    if name not in allnames and "*" not in name:
        ci = [n for n in allnames if n.lower() == name.lower()]
        if len(ci) == 1:
            name = ci[0]
    # partly known names are globs (Slot*Tier): Slot1Tier and Slot* both find them
    names = {n for n in allnames if n == name or glob_overlap(n, name)}
    if not names:
        return suggest(name, allnames, out, what)
    by = {}
    for p in q.mods:
        lst = dedupe(((r[3] if r[2] == name else "%s %s" % (r[3], r[2])), r[0]) for r in q.sites(p, field)
                     if r[2] in names)
        if lst:
            by[p] = lst
    site_list(q, by, out)


def glob_overlap(a, b):
    """Could one string match both names? (either may be a glob such as Slot*Tier)"""
    if "*" not in a and "*" not in b:
        return False
    return fnmatch.fnmatchcase(a.replace("*", "x"), b) or fnmatch.fnmatchcase(b.replace("*", "x"), a) \
        or fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)


def cmd_reads(q, a, out):
    qn = norm(a.name)
    parts = qn.split(".")
    alias_mods = None
    field = qn
    if len(parts) > 1:
        for s in range(len(parts) - 1, 0, -1):
            ms = q.find_modules(".".join(parts[:s]))
            if ms:
                alias_mods = set(ms)
                field = ".".join(parts[s:])
                break
        if alias_mods is None:
            alias_mods = {"@" + parts[0]}
            field = ".".join(parts[1:])

    def collect(alias_mods, field):
        def target_ok(target, owner):
            if alias_mods is None:
                return True
            if not target:
                return owner in alias_mods
            if q.resolve(target) in alias_mods:
                return True
            return ("@" + target.rsplit(".", 1)[-1]) in alias_mods

        by, writes = {}, False
        for p, m in q.mods.items():
            for r in m["reads"]:
                if (r[3] == field or r[3].startswith(field + ".")) and target_ok(r[2], p):
                    by.setdefault(p, []).append(((r[1] or "top") + ("=" if r[4] else ""), r[0]))
                    writes = writes or bool(r[4])
            for c in q.calls(p):
                if c[2] and c[3].startswith(field + ".") and target_ok(c[2], p):
                    by.setdefault(p, []).append(((c[1] or "top") + "()", c[0]))
        return by, writes

    by, writes = collect(alias_mods, field)
    if not by and len(parts) > 1:  # Shop.items may mean the field path Config.Shop.items
        alias_mods = None
        by, writes = collect(None, qn)
    if not by:
        out("no reads of %r found" % a.name)
        return
    if alias_mods and len(alias_mods) > 1:
        out("modules: " + " | ".join(sorted(alias_mods)))
    for p in sorted(by):
        out("%s: %s" % (p, lines_fmt(sorted(by[p], key=lambda x: x[1]))))
    if writes:
        out("(name= : assignment)")


def cmd_find(q, a, out):
    t = a.name.lower()
    mods = [p for p in q.mods if t in q.mname(p).lower()]
    if mods:
        out.wrap("modules: ", mods)
    by = {}
    for p, m in q.mods.items():
        for d in m["defs"]:
            hit = t in d[2].lower() or (a.docs and d[6] and t in d[6].lower())
            if hit and (d[7] == 0 or a.all):
                by.setdefault(p, []).append(d)
    for p in sorted(by, key=lambda p: (-sum(d[8] for d in by[p]), p)):
        ds = by[p]
        if a.docs:
            for d in ds:
                out("%s:%d %s%s -- %s" % (p, d[0], d[2], q.sig(d), snippet(d[6], t)))
        else:
            out.wrap(p + ": ", ["%s:%d" % (d[2], d[0]) for d in ds])
    for field, what in (("remotes", "remote"), ("attrs", "attr"), ("tags", "tag")):
        names = {}
        for p in q.mods:
            for r in q.sites(p, field):
                if t in r[2].lower():
                    names[r[2]] = names.get(r[2], 0) + 1
        if names:
            out.wrap(what + ": ", ["%s:%d" % kv for kv in sorted(names.items())])
    if a.docs:
        for p, m in q.mods.items():
            if m["doc"] and t in m["doc"].lower():
                out("%s (module doc) -- %s" % (p, snippet(m["doc"], t)))
    if not out.lines:
        out("nothing matches %r" % a.name)


def snippet(doc, t, w=80):
    if not doc:
        return ""
    flat = " ".join(doc.split())
    i = flat.lower().find(t)
    if i < 0:
        return first_line(doc, w)
    s = max(0, i - w // 3)
    return ("~" if s else "") + flat[s:s + w] + ("~" if s + w < len(flat) else "")


def cmd_stats(q, a, out):
    mods = q.mods
    cls = {}
    for m in mods.values():
        cls[m["cls"]] = cls.get(m["cls"], 0) + 1
    reqs = [r for m in mods.values() for r in m["requires"]]
    res = [q.res.get(r[2], "") for r in reqs]
    defs = sum(len(m["defs"]) for m in mods.values())
    exp = sum(sum(d[8] for d in m["defs"]) for m in mods.values())
    size = os.path.getsize(ix.index_path(q.idx["root"], q.idx["config"]))
    out("files %d (%s) lines %d" % (len(mods), " ".join("%s %d" % kv for kv in sorted(cls.items())),
                                    sum(m["lines"] for m in mods.values())))
    out("defs %d (exported %d) calls %d reads %d" % (defs, exp, sum(len(m["calls"]) for m in mods.values()),
                                                     sum(len(m["reads"]) for m in mods.values())))
    out("requires %d: module %d, external %d, fuzzy %d, unresolved %d" % (
        len(reqs), sum(1 for r in res if r and not r.startswith(("ext:", "~"))), sum(1 for r in res if r.startswith("ext:")),
        sum(1 for r in res if r.startswith("~")), sum(1 for r in res if not r)))
    sites = {f: [r for p in mods for r in q.sites(p, f)] for f in ("remotes", "attrs", "tags")}
    out("remotes %d names %d sites; attrs %d names %d sites; tags %d names %d sites" % (
        len({r[2] for r in sites["remotes"]}), len(sites["remotes"]), len({r[2] for r in sites["attrs"]}),
        len(sites["attrs"]), len({r[2] for r in sites["tags"]}), len(sites["tags"])))
    linked = sum(len(m.get("x", {}).get("c", [])) for m in mods.values())
    out("getters %d, wrappers %d, object method calls linked %d" % (len(q.getters), len(q.idx.get("wrappers", [])), linked))
    out("build %.2fs, index %.1f MB, built %s" % (q.idx["secs"], size / 1e6, time.strftime("%Y-%m-%d %H:%M", time.localtime(q.idx["built"]))))


def blind_names(q):
    """Remote/attr/tag sites whose name is still unknown after linking: {path: ['line op expr']}, counts."""
    wrappers = set(q.idx.get("wrappers", []))
    found, counts = {}, {"remotes": 0, "attrs": 0, "tags": 0, "via": 0, "inferred": 0, "partial": 0}
    for p, m in q.mods.items():
        xs = m.get("x", {})
        for field, xk, expri in (("remotes", "r", 5), ("attrs", "a", 5), ("tags", "t", 4)):
            resolved = {}
            for r in xs.get(xk, []):
                resolved[(r[0], r[3])] = r
            for r in m[field]:
                if r[2]:
                    if "*" in r[2]:
                        counts["partial"] += 1
                    continue
                extra = r[expri + 1] if len(r) > expri + 1 else None
                if extra and "p" in extra and "%s:%s" % (p, extra["p"][0]) in wrappers:
                    counts["via"] += 1
                    continue
                got = resolved.get((r[0], r[3]))
                if got is not None:
                    if got[-1] == "inferred":
                        counts["inferred"] += 1
                    elif "*" in got[2]:
                        counts["partial"] += 1
                    continue
                counts[field] += 1
                found.setdefault(p, []).append("%d %s(%s)" % (r[0], r[3], r[expri] or "?"))
    return found, counts


def cmd_doctor(q, a, out):
    mods = q.mods
    found, counts = blind_names(q)
    out("unresolved names: remotes %d, attrs %d, tags %d; partial (Name*Pattern) %d; inferred from a field name %d;"
        " via wrapper call sites %d" % (counts["remotes"], counts["attrs"], counts["tags"], counts["partial"],
                                         counts["inferred"], counts["via"]))
    for p in sorted(found):
        out.wrap(" %s: " % p, found[p], sep="; ")
    methods = set()
    for m in mods.values():
        for d in m["defs"]:
            if d[3] == "method" or (d[3] in ("member", "field") and d[5]):
                methods.add(norm(d[2]).rsplit(".", 1)[-1])
    uc = [(p, r) for p, m in mods.items() for r in m.get("ucalls", [])]
    proj = [(p, r) for p, r in uc if r[2] in methods]
    strict = [(p, r) for p, r in proj if r[2] not in ENGINE_METHODS]
    out("method calls on untyped receivers: %d; naming a project method: %d; minus engine API names: %d" % (
        len(uc), len(proj), len(strict)))
    top = {}
    for _, r in strict:
        top[r[2]] = top.get(r[2], 0) + 1
    if top:
        out.wrap(" top: ", ["%s(%d)" % kv for kv in sorted(top.items(), key=lambda kv: -kv[1])[:15]])
    sample = {}
    for p, r in strict:
        sample.setdefault(p, []).append("%d %s:%s" % (r[0], r[3], r[2]))
    for p in sorted(sample)[: a.limit_each]:
        out.wrap(" %s: " % p, sample[p][:6], sep="; ")
    zero = [p for p, m in mods.items() if not m["defs"]]
    out("zero-def files: %d" % len(zero))
    for d, names in group_paths(zero, q):
        out.wrap(" " + d + " ", names)
    unres = {}
    fuzzy = []
    for p, m in mods.items():
        bound = {r[1] for r in m["requires"] if r[1] and q.res.get(r[2], "")}
        for r in m["requires"]:
            t = q.res.get(r[2], "")
            if not t and r[1] in bound:
                continue  # the other branch of `if IS_ROBLOX then require(A) else require(B)` resolved it
            if not t:
                why = r[2][1:].split(":")[0] if r[2].startswith("?") else "no such instance in the Rojo tree"
                if r[2].startswith("?") and ":" not in r[2]:
                    why = "dynamic/unknown base"
                unres.setdefault(why, []).append("%s:%d %s" % (p, r[0], r[3] or r[2]))
            elif t.startswith("~"):
                fuzzy.append("%s:%d %s -> %s" % (p, r[0], r[3], t[1:]))
    out("unresolved requires: %d" % sum(len(v) for v in unres.values()))
    for why, lst in unres.items():
        out(" %s (%d):" % (why, len(lst)))
        for s in lst[:a.limit_each]:
            out("  " + s)
    if fuzzy:
        out("fuzzy (suffix-matched) requires: %d" % len(fuzzy))
        for s in fuzzy[:a.limit_each]:
            out("  " + s)
    warn = [(p, m["warn"]) for p, m in mods.items() if m["warn"]]
    out("files with scan warnings: %d" % len(warn))
    for p, w in warn[:a.limit_each * 2]:
        out("  %s: %s" % (p, "; ".join(w)))
    lex = [p for p, m in mods.items() if m.get("lexerr")]
    if lex:
        out("files with lexer errors (unterminated strings etc.): %d %s" % (len(lex), " ".join(lex[:5])))
    noexp = [p for p, m in mods.items() if m["cls"] == "ModuleScript" and not any(d[8] for d in m["defs"])]
    out("modules with no detected exports: %d" % len(noexp))
    for d, names in group_paths(noexp, q):
        out.wrap(" " + d + " ", names)


# ---------------------------------------------------------------- main
COMMANDS = {
    "build": cmd_build, "map": cmd_map, "summary": cmd_summary, "doc": cmd_doc, "def": cmd_def,
    "callers": cmd_callers, "calls": cmd_calls, "requires": cmd_requires, "required-by": cmd_required_by,
    "remote": cmd_remote, "attr": cmd_attr, "tag": cmd_tag, "reads": cmd_reads, "find": cmd_find,
    "stats": cmd_stats, "doctor": cmd_doctor,
}


class Args:
    pass


def parse(argv):
    a = Args()
    a.cmd = None
    a.name = None
    a.budget = None
    a.limit = None
    a.root = None
    a.dir = None
    a.ctx = None
    a.limit_each = 8
    flags = {"--docs": "docs", "--types": "types", "--all": "all", "--deep": "deep", "--changed": "changed",
             "--no-refresh": "no_refresh"}
    for f in flags.values():
        setattr(a, f, False)
    rest = []
    it = iter(argv)
    for x in it:
        if x in flags:
            setattr(a, flags[x], True)
        elif x in ("--budget", "--limit", "--root", "--dir", "--from"):
            val = next(it, None)
            if x in ("--budget", "--limit"):
                setattr(a, x[2:], int(val) if val is not None else None)
            elif x == "--from":
                a.ctx = val
            else:
                setattr(a, x[2:], val)
        elif x in ("-h", "--help", "help"):
            a.cmd = "help"
        else:
            rest.append(x)
    if a.cmd != "help":
        a.cmd = rest[0] if rest else "help"
        a.name = " ".join(rest[1:]) if len(rest) > 1 else None
    return a


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # docs may hold non-ASCII; cp1252 consoles
    except (AttributeError, ValueError):
        pass
    a = parse(sys.argv[1:] if argv is None else argv)
    if a.cmd == "help" or a.cmd not in COMMANDS:
        sys.stdout.write(__doc__.strip() + "\n")
        return 0 if a.cmd == "help" else 2
    needs_name = {"summary", "doc", "def", "callers", "calls", "requires", "required-by", "reads", "find"}
    if a.cmd in needs_name and not a.name:
        print("usage: %s <name>" % a.cmd)
        return 2
    root = os.path.abspath(a.root) if a.root else project.find_root(os.getcwd())
    if a.cmd == "build":
        cfg = project.load_config(root)
        prev = ix.load(root, cfg) if a.changed else None
        idx = ix.build(root, cfg, prev=prev)
    else:
        idx = ix.ensure(root, refresh=not a.no_refresh)
    budget = a.budget if a.budget is not None else DEFAULT_BUDGET.get(a.cmd, 1000)
    out = Out(budget, a.limit)
    q = Q(idx)
    if a.ctx:
        ctx = q.find_modules(a.ctx)
        q.ctx = ctx[0] if len(ctx) == 1 else None
    COMMANDS[a.cmd](q, a, out)
    out.emit()
    return 0
