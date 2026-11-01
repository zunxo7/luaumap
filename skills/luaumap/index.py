"""Build / load / incrementally refresh the index (.luaumap/index.json), and link modules together.

Linking is a second, whole-project pass (graphify's "call-graph second pass"): each module exports what it
knows (field constants, function return values, class fields, class types, wrapper parameters); the linker
resolves cross-module names with those summaries and writes the derived facts to module["x"].
"""
import json
import os
import time

import project
import scanner

VERSION = 4
_HERE = os.path.dirname(os.path.abspath(__file__))
PARALLEL_MIN = 120
REMOTE_CLASSES = frozenset(("RemoteEvent", "RemoteFunction", "UnreliableRemoteEvent"))  # files: below this a process pool costs more than it saves


def tool_sig():
    """Changes when the scanner itself changes, so an upgraded tool never serves facts from an old one."""
    sig = [VERSION]
    for f in ("lexer.py", "scanner.py", "project.py", "index.py"):
        st = _stamp(os.path.join(_HERE, f))
        sig.append(st[0] if st else 0)
    return sig


def index_path(root, cfg):
    return os.path.join(root, cfg.get("index") or ".luaumap/index.json")


def _stamp(path):
    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def _empty(lines, why):
    return {"lines": lines, "doc": "", "table": None, "module_fn": None, "requires": [], "services": [], "defs": [],
            "calls": [], "reads": [], "remotes": [], "attrs": [], "tags": [], "ucalls": [], "sinks": [], "vals": {},
            "warn": [why], "lexerr": 0, "resyncs": 0}


def _scan_file(root, rel, inst, cls, ctors=None):
    full = os.path.join(root, rel)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as e:
        rec = _empty(0, "unreadable: %s" % e)
    else:
        try:
            rec = scanner.scan(src, inst, cls == "ModuleScript", frozenset(ctors) if ctors else scanner.CONSTRUCTORS, rel)
        except Exception as e:  # last-resort guard: keep the file in the index with what we know
            rec = _empty(src.count("\n") + 1, "scanner crashed: %s: %s" % (type(e).__name__, e))
    rec["inst"] = ".".join(inst)
    rec["cls"] = cls
    rec["side"] = project.side_of(inst, cls)
    rec["stamp"] = _stamp(full)
    return rec


def _scan_job(args):
    return args[1], _scan_file(*args)


def _luaurc_aliases(root):
    try:
        with open(os.path.join(root, ".luaurc"), encoding="utf-8") as f:
            data = json.load(f)
        return {k.lower(): v for k, v in (data.get("aliases") or {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _resolver(mods, others, root):
    inst2mod = {m["inst"]: p for p, m in mods.items()}
    by_suffix = {}
    for p, m in mods.items():
        parts = m["inst"].split(".")
        for k in range(2, len(parts)):
            by_suffix.setdefault(".".join(parts[-k:]), []).append(p)
    aliases = _luaurc_aliases(root)
    by_file = {}
    for p in mods:
        base = p.rsplit(".", 1)[0]
        for suf in (".server", ".client"):
            if base.endswith(suf):
                base = base[: -len(suf)]
        by_file[base[:-5] if base.endswith("/init") else base] = p

    def resolve(target):
        if not target:
            return ""
        if target in inst2mod:
            return inst2mod[target]
        if target.startswith("?alias:"):  # require("@alias/x") via .luaurc (luau-lsp: user aliases are paths)
            spec = target[7:]
            name, _, rest = spec[1:].partition("/")
            base = aliases.get(name.lower())
            if base:
                key = os.path.normpath(os.path.join(base, rest)).replace("\\", "/").lstrip("./")
                return by_file.get(key, "")
            return ""
        if target.startswith("?"):
            return ""
        if target in others:
            return "ext:" + others[target]
        parts = target.split(".")
        for k in range(len(parts) - 1, 0, -1):
            pre = ".".join(parts[:k])
            if pre in others:
                return "ext:" + others[pre]
            if pre in inst2mod and k < len(parts):
                break
        for k in range(len(parts) - 1, 1, -1):
            c = by_suffix.get(".".join(parts[-k:]))
            if c and len(c) == 1:
                return "~" + c[0]
        return ""

    return resolve


class Linker:
    """Cross-module resolution with each module's exported summary ("vals")."""

    def __init__(self, idx):
        self.idx = idx
        self.mods = idx["modules"]
        self.resolve_raw = _resolver(self.mods, idx.get("others", {}), idx.get("root", "."))
        self.res = {}
        self.defnames = {}
        self.defkind = {}
        self.lastname = {}
        for p, m in self.mods.items():
            names = set()
            last = {}
            for d in m["defs"]:
                q = d[2].replace(":", ".")
                names.add(q)
                self.defkind[(p, q)] = d[3]
                last.setdefault(q.rsplit(".", 1)[-1], []).append(q)
            self.defnames[p] = names
            self.lastname[p] = last

    def R(self, target):
        if target not in self.res:
            self.res[target] = self.resolve_raw(target)
        r = self.res[target]
        return r[1:] if r.startswith("~") else r

    def qual(self, path, member):
        """Module member -> normalized def name in that module (Shop.buy for member buy)."""
        m = self.mods.get(path)
        if not m:
            return member
        names = self.defnames[path]
        table = m.get("table")
        if table and table != "<return>" and table + "." + member in names:
            return table + "." + member
        if member in names:
            return member
        return (table + "." + member) if table and table != "<return>" else member

    def short(self, path, q):
        table = self.mods[path].get("table")
        return q[len(table) + 1:] if table and q.startswith(table + ".") else q

    def key(self, path, target, member):
        if not target:
            return (path, member)
        tp = self.R(target)
        if tp not in self.mods:
            return None
        return (tp, self.qual(tp, member))

    def vals(self, path):
        m = self.mods.get(path)
        return m.get("vals", {}) if m else {}

    def lv(self, path, e, depth=0):
        """Resolve an encoded value seen in module `path` as far as the summaries allow."""
        if not e or depth > 6:
            return e
        t = e[0]
        if t == "f":
            tp = self.R(e[1])
            v = self.vals(tp).get("f:" + e[2])
            return self.lv(tp, v, depth + 1) if v else e
        if t == "x":
            tp = self.R(e[1])
            key = e[2]
            if key.startswith("r:"):
                key = "r:" + self.qual(tp, key[2:])
            v = self.vals(tp).get(key)
            return self.lv(tp, v, depth + 1) if v else e
        if t == "n":  # an Instance named by a value: resolve the name
            return ["n", self.lv(path, e[1], depth + 1)] + e[2:]
        if t == "y":  # Mod.fn().Field: follow fn's return value, then the field
            tp = self.R(e[1])
            r = self.vals(tp).get("r:" + self.qual(tp, e[2])) if tp in self.mods else None
            r = self.lv(tp, r, depth + 1) if r else None
            if r and r[0] == "m":
                return self.lv(tp, ["f", r[1], e[3]], depth + 1)
            if r and r[0] == "o":
                return self.lv(tp, ["x", r[1], "c:%s.%s" % (r[2], e[3]) if r[2] else "c:" + e[3]], depth + 1)
            return e
        if t in ("e", "k"):  # elements / keys of another module's table (loops over Other.List)
            tp = self.R(e[1])
            v = self.vals(tp).get(t + ":" + e[2])
            return v or e
        if t == "+":  # concatenation whose parts other modules know; unknown parts stay * (a pattern)
            acc = [""]
            for part in e[1]:
                ss = ["*"] if part == ["*"] else self.strs(self.lv(path, part, depth + 1))
                if not ss:
                    return e
                acc = [x + y for x in acc for y in ss][:48]
            acc = sorted({self.squash(a) for a in acc})
            if any("*" in a for a in acc):
                return ["P", acc[0]] if len(acc) == 1 and len(acc[0].replace("*", "")) >= 3 else e
            return ["s", acc[0]] if len(acc) == 1 else ["S", acc]
        if t == "t":
            tp = self.R(e[1])
            v = self.vals(tp).get("t:" + e[2])
            return ["o", self.mods[tp]["inst"], v[2]] if v and tp in self.mods else e
        if t == "o":
            return ["o", e[1] or self.mods[path]["inst"], e[2]] if path in self.mods else e
        if t == "c":
            target, member, args = e[1], e[2], e[3] or []
            tp = path if not target else self.R(target)
            args = [self.lv(path, a, depth + 1) for a in args]
            if tp in self.mods:
                q = member if not target else self.qual(tp, member)
                r = self.vals(tp).get("r:" + q)
                if r:
                    return self.lv(tp, self.subst(r, q, args), depth + 1)
                return ["c", self.mods[tp]["inst"], q, args]
            return ["c", target, member, args]
        return e

    def subst(self, v, fq, args):
        if not isinstance(v, list) or not v:
            return v
        if v[0] == "p":
            return args[v[2]] if v[1] == fq and v[2] < len(args) else v
        if v[0] == "c":
            return ["c", v[1], v[2], [self.subst(a, fq, args) for a in (v[3] or [])]]
        if v[0] == "n":
            return ["n", self.subst(v[1], fq, args)] + v[2:]
        if v[0] == "+":
            return ["+", [self.subst(a, fq, args) for a in v[1]]]
        return v

    @staticmethod
    def squash(glob):
        while "**" in glob:
            glob = glob.replace("**", "*")
        return glob

    @staticmethod
    def strs(e):
        """Names in a resolved value; a partly known name is one glob (Slot*Tier)."""
        if not e:
            return []
        if e[0] == "s":
            return [e[1]]
        if e[0] == "S":
            return list(e[1])
        if e[0] == "P":
            return [e[1]]
        return []

    def remote_names(self, path, e, getters):
        """Names a resolved remote value can be, plus the getter key it came through."""
        e = self.lv(path, e)
        if not e:
            return [], None
        if e[0] == "i":
            return [e[1].rsplit(".", 1)[-1]], None
        if e[0] == "n":
            return self.strs(e[1]), None
        if e[0] == "c":
            tp = path if not e[1] else self.R(e[1])
            gk = (tp, e[2] if not e[1] or tp not in self.mods else self.qual(tp, e[2]))
            args = e[3] or []
            if gk in getters and getters[gk] < len(args):
                return self.strs(args[getters[gk]]), gk
            for a in args:
                s = self.strs(a)
                if s:
                    return s, gk
        return [], None

    def link(self):
        mods = self.mods
        cfg = self.idx["config"]
        by_file = {}
        for p in mods:
            base = p.rsplit(".", 1)[0]
            for suf in (".server", ".client"):
                if base.endswith(suf):
                    base = base[: -len(suf)]
            by_file[base[:-5] if base.endswith("/init") else base] = p
        for m in mods.values():  # every target the CLI will ask about
            for r in m["requires"]:
                if not self.R(r[2]) and len(r) > 4 and r[4] in by_file:
                    self.res[r[2]] = by_file[r[4]]
            for c in m["calls"]:
                if c[2]:
                    self.R(c[2])
            for r in m["reads"]:
                if r[2]:
                    self.R(r[2])
        named = {}
        for p, m in mods.items():
            named.setdefault(m["inst"].rsplit(".", 1)[-1], []).append(p)
        # ---- remote getters: config, used-with-a-role, wrappers that return a getter call on a parameter
        getters = {}
        for g in cfg.get("remote_getters") or []:
            mod, _, fn = g.rpartition(".")
            for p in named.get(mod, []):
                getters[(p, self.qual(p, fn))] = 0
        for p, m in mods.items():
            for r in m["remotes"]:
                if r[2] and r[4] and "#" in r[4]:
                    via, _, i = r[4].rpartition("#")
                    target, _, member = via.rpartition(".")
                    k = self.key(p, target, member)
                    if k:
                        getters.setdefault(k, int(i))
        for p, m in mods.items():  # factories: return a new RemoteEvent/RemoteFunction named by a parameter
            for key, v in m.get("vals", {}).items():
                if key.startswith("r:") and v and v[0] == "n" and len(v) > 2 and v[2] in REMOTE_CLASSES                         and v[1] and v[1][0] == "p" and v[1][1] == key[2:]:
                    getters.setdefault((p, key[2:]), v[1][2])
        for _ in range(3):
            for p, m in mods.items():
                for key, v in m.get("vals", {}).items():
                    if key.startswith("r:") and v and v[0] == "c":
                        fq = key[2:]
                        k = self.key(p, v[1], v[2])
                        if k in getters and getters[k] < len(v[3] or []):
                            a = v[3][getters[k]]
                            if a and a[0] == "p" and a[1] == fq:
                                getters.setdefault((p, fq), a[2])
        # ---- wrapper sinks, propagated through calls that pass a parameter on (depth 3)
        sinks = {}
        for p, m in mods.items():
            for fq, i, kind, op, via in m.get("sinks", []):
                sinks.setdefault((p, fq), set()).add((i, kind, op, via))
        for _ in range(3):
            grew = False
            for p, m in mods.items():
                for c in m["calls"]:
                    k = self.key(p, c[2], c[3])
                    if k not in sinks or not c[5]:
                        continue
                    for i, kind, op, via in list(sinks[k]):
                        a = self.arg(c, k, i)
                        if a and a[0] == "p":
                            s = sinks.setdefault((p, a[1]), set())
                            if (a[2], kind, op, via) not in s:
                                s.add((a[2], kind, op, via))
                                grew = True
            if not grew:
                break
        # ---- derived facts per module
        used_sinks = set()
        for p, m in mods.items():
            X = {"r": [], "a": [], "t": [], "c": []}
            role_lines = {(r[0], r[2]) for r in m["remotes"] if r[2]}
            for c in m["calls"]:
                k = self.key(p, c[2], c[3])
                if k in sinks:
                    for i, kind, op, via in sinks[k]:
                        a = self.arg(c, k, i)
                        if kind == "rolev":
                            names, _ = self.remote_names(p, a, getters)
                        else:
                            names = self.strs(self.lv(p, a))
                        for nm in names:
                            used_sinks.add(k)
                            if kind == "attr":
                                X["a"].append([c[0], c[1], nm, op, "", "via " + k[1]])
                            elif kind == "tag":
                                X["t"].append([c[0], c[1], nm, op, "via " + k[1]])
                            else:
                                X["r"].append([c[0], c[1], nm, op, "", "via " + k[1]])
                if k in getters and c[5] and getters[k] < len(c[5]):
                    for nm in self.strs(self.lv(p, c[5][getters[k]])):
                        if (c[0], nm) not in role_lines:
                            X["r"].append([c[0], c[1], nm, "get", "", ""])
                if c[4] in ("o", "x"):
                    lk = self.method_link(p, c)
                    if lk:
                        X["c"].append([c[0], c[1], lk[0], lk[1], "m", c[5]])
            for r in m["remotes"]:
                if r[2] or len(r) <= 6:
                    continue
                names = self.remote_names(p, r[6]["x"], getters)[0] if "x" in r[6] else []
                for nm in names:
                    X["r"].append([r[0], r[1], nm, r[3], "", ""])
                if not names and r[6].get("fb"):  # Registry.Field:FireServer(): the field name, marked inferred
                    X["r"].append([r[0], r[1], r[6]["fb"], r[3], "", "inferred"])
            for r in m["attrs"]:
                if not r[2] and len(r) > 6 and "x" in r[6]:
                    for nm in self.strs(self.lv(p, r[6]["x"])):
                        X["a"].append([r[0], r[1], nm, r[3], r[4], ""])
            for r in m["tags"]:
                if not r[2] and len(r) > 5 and "x" in r[5]:
                    for nm in self.strs(self.lv(p, r[5]["x"])):
                        X["t"].append([r[0], r[1], nm, r[3], ""])
            m["x"] = X
        self.idx["getters"] = sorted("%s:%s#%d" % (k[0], k[1], i) for k, i in getters.items())
        self.idx["wrappers"] = sorted("%s:%s" % k for k in used_sinks)
        self.idx["res"] = self.res

    def arg(self, c, k, i):
        """Argument at parameter index i of callee k, allowing for obj:m() into function C.m(self, ...)."""
        args = c[5] or []
        colon_call = c[4] in ("m", "o", "x")
        colon_def = self.defkind.get(k) == "method"
        j = i - 1 if colon_call and not colon_def else i + 1 if colon_def and not colon_call and c[4] != "l" else i
        return args[j] if 0 <= j < len(args) else None

    def method_link(self, p, c):
        """'o'/'x' call records -> (target instance, member) of the class method, or None (engine API)."""
        target, member = c[2], c[3]
        if c[4] == "x":
            tp = self.R(target)
            key = c[6] if len(c) > 6 else ""
            if key.startswith("r:"):
                key = "r:" + self.qual(tp, key[2:])
            v = self.lv(tp, self.vals(tp).get(key)) if tp in self.mods else None
            if not v or v[0] != "o":
                return None
            target, member = v[1], (v[2] + "." if v[2] else "") + c[3]
        tp = self.R(target)
        if tp not in self.mods:
            return None
        if member.startswith("@"):
            typ, _, f = member[1:].partition(".")
            v = self.vals(tp).get("t:" + typ)
            if not v:
                return None
            member = (v[2] + "." if v[2] else "") + f
        names = self.defnames[tp]
        table = self.mods[tp].get("table")
        for q in (member, (table + "." + member) if table and table != "<return>" else None):
            if q and q in names:
                return self.mods[tp]["inst"], self.short(tp, q)
        last = member.rsplit(".", 1)[-1]
        cands = self.lastname[tp].get(last, [])
        if len(cands) == 1:  # closure objects: Thing.new() returns {draw = draw}
            return self.mods[tp]["inst"], self.short(tp, cands[0])
        return None


def link(idx):
    Linker(idx).link()
    return idx


def build(root, cfg=None, prev=None, quiet=False):
    t0 = time.time()
    cfg = cfg or project.load_config(root)
    mp = project.Mapping(root, cfg).build()
    sig = tool_sig()
    old = prev.get("modules", {}) if prev and prev.get("tool") == sig and prev.get("config") == cfg else {}
    mods = {}
    todo = []
    ctors = cfg.get("constructors")
    for rel, (inst, cls) in mp.scripts.items():
        o = old.get(rel)
        full = os.path.join(root, rel)
        if o and o.get("stamp") == _stamp(full) and o.get("inst") == ".".join(inst) and o.get("cls") == cls:
            mods[rel] = o
        else:
            todo.append((root, rel, inst, cls, ctors))
    done = None
    if len(todo) >= PARALLEL_MIN and (os.cpu_count() or 1) > 2:
        try:
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
                done = dict(ex.map(_scan_job, todo, chunksize=8))
        except Exception:
            done = None  # no process pool here (frozen/embedded interpreter): scan serially
    if done is None:
        done = dict(_scan_job(a) for a in todo)
    mods.update(done)
    mods = {rel: mods[rel] for rel in mp.scripts}
    idx = {
        "v": VERSION,
        "tool": sig,
        "root": root,
        "config": cfg,
        "project": mp.project_file,
        "others": mp.others,
        "modules": mods,
        "scanned": len(todo),
        "built": time.time(),
    }
    link(idx)
    idx["secs"] = round(time.time() - t0, 2)
    save(root, cfg, idx)
    return idx


def save(root, cfg, idx):
    path = index_path(root, cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, separators=(",", ":"))
    os.replace(tmp, path)


def load(root, cfg):
    try:
        with open(index_path(root, cfg), encoding="utf-8") as f:
            idx = json.load(f)
        return idx if idx.get("v") == VERSION else None
    except (OSError, ValueError):
        return None


def stale(root, cfg, idx):
    """Cheap check: same config, same file set, same mtimes/sizes."""
    if idx.get("config") != cfg or idx.get("tool") != tool_sig():
        return True
    mp = project.Mapping(root, cfg).build()
    mods = idx["modules"]
    if set(mp.scripts) != set(mods):
        return True
    for rel in mp.scripts:
        if mods[rel].get("stamp") != _stamp(os.path.join(root, rel)):
            return True
    return False


def ensure(root, refresh=True):
    cfg = project.load_config(root)
    idx = load(root, cfg)
    if idx is None:
        return build(root, cfg)
    if refresh and stale(root, cfg, idx):
        return build(root, cfg, prev=idx)
    return idx
