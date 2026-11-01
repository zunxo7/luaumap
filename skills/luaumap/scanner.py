"""Fact extraction from one Luau file. Two forward passes over the tokens; no full parser.

Pass 1 only gathers knowledge (table/class fields, function return values, assignments to module-level
variables, class type aliases, where each function ends). Pass 2 walks again with that knowledge and records
the facts, so a use that comes before its assignment in the file still resolves.

Values are symbolic tuples:
  ('str', s) / ('strs', (s, ...))      known string(s): constants, concatenation, literal/constant loops
  ('inst', path)                       Instance path (game, script, GetService, .Child, WaitForChild)
  ('module', target)                   require() result; target = instance string or '?<expr>'
  ('field', target, path)              a field of another module (resolved at link time)
  ('call', target, member, args)       a call's result (target '' = local function); args are values
  ('table', name) / ('tab', tid)       module-level local table / any other table constructor
  ('ownfield'|'tabfield', key, path)   slot in such a table; ('objfield', target, cls, path) on an object
  ('obj', target, cls)                 instance of a class (constructor / setmetatable / annotation typing)
  ('tobj', target, Type)               annotated with another module's exported type
  ('func', qual)                       local function;  ('rolefn', value, role) e.g. `remote.FireServer`
  ('param', fq, idx, type)             parameter idx of function fq (wrappers); type when annotated
Assignments that disagree make a slot unknown (graphify's policy: no inference on conflicts or shadowing).
Any surprise inside one statement is caught and counted; the pass always continues.
"""
from lexer import tokenize

ASSIGN = frozenset(("=", "+=", "-=", "*=", "/=", "//=", "%=", "^=", "..="))
EXPR_PREV = frozenset(
    ("=", "(", "[", "{", ",", "return", "and", "or", "not", "==", "~=", "<", ">", "<=", ">=", "..", "+", "-", "*",
     "/", "//", "%", "^", "#", "in", "+=", "-=", "*=", "/=", "//=", "%=", "^=", "..=")
)
CALL_OPEN = frozenset(("(", "{"))
ARITH = frozenset(("+", "-", "*", "/", "//", "%", "^", "==", "~=", "<", ">", "<=", ">="))
ROLE_METHODS = frozenset(("FireServer", "FireClient", "FireAllClients", "InvokeServer", "InvokeClient"))
ROLE_FIELDS = frozenset(("OnServerEvent", "OnClientEvent", "OnServerInvoke", "OnClientInvoke"))
ATTR_METHODS = {"GetAttribute": "get", "SetAttribute": "set", "GetAttributeChangedSignal": "changed"}
TAG_METHODS = {"AddTag": "add", "RemoveTag": "remove", "HasTag": "has", "GetTagged": "tagged",
               "GetInstanceAddedSignal": "added", "GetInstanceRemovedSignal": "removed"}
CONSTRUCTORS = frozenset(("new", "New", "create", "Create"))
# luau-lsp's mapContext: player-local containers are their Starter* sources in a Rojo tree
INST_REMAP = {"PlayerScripts": "StarterPlayerScripts", "PlayerGui": "StarterGui", "StarterGear": "StarterPack"}
SLOTS = frozenset(("ownfield", "tabfield", "objfield"))
DEREF = SLOTS | {"param", "call"}  # values that may stand for something already known (slot, typed param, local call)
XREF = frozenset(("field", "elem", "keys", "cat", "cfield"))
ANY = ("any",)  # an unknown piece of a partly known string: becomes * in a name pattern  # strings known only to another module (link time)
_MISSING = object()
UNK = ("?",)
CONFLICT = ("conflict",)
MAXS = 24


def _norm(q):
    return q.replace(":", ".")


def strs_of(v):
    if not v:
        return ()
    if v[0] == "str":
        return (v[1],)
    if v[0] == "strs":
        return v[1]
    return ()


def mkstrs(seq):
    s = sorted(set(seq))
    if not s:
        return None
    return ("str", s[0]) if len(s) == 1 else ("strs", tuple(s[:MAXS]))


def merge(old, new):
    """Join two facts about one slot: strings union; an unknown kills strings; other kinds must agree."""
    if old is None or old is _MISSING:
        return new if new is not None else UNK
    if new is None:
        new = UNK
    if old == new:
        return old
    if old is CONFLICT or new is CONFLICT:
        return CONFLICT
    so, sn = strs_of(old), strs_of(new)
    if so and sn:
        return mkstrs(so + sn)
    if old == UNK:
        return CONFLICT if sn else new
    if new == UNK:
        return CONFLICT if so else old
    return CONFLICT


def known(v):
    return None if v is None or v is CONFLICT or v == UNK else v


def enc(v, depth=0):
    """Portable (JSON) form of a value, for the index; None when it has no meaning outside this file."""
    if v is None or depth > 4:
        return None
    k = v[0]
    if k == "str":
        return ["s", v[1]]
    if k == "strs":
        return ["S", list(v[1])]
    if k == "inst":
        return ["i", ".".join(v[1])]
    if k == "module":
        return ["m", v[1]]
    if k == "field":
        return ["f", v[1], ".".join(v[2])]
    if k == "call":
        return ["c", v[1], v[2], [enc(a, depth + 1) for a in v[3]]]
    if k == "obj":
        return ["o", v[1], v[2]]
    if k == "tobj":
        return ["t", v[1], v[2]]
    if k == "param":
        return ["p", v[1], v[2]] if v[1] else None
    if k == "objfield" and v[1]:
        return ["x", v[1], "c:%s.%s" % (v[2], ".".join(v[3])) if v[2] else "c:" + ".".join(v[3])]
    if k == "elem":
        return ["e", v[1], ".".join(v[2])]
    if k == "keys":
        return ["k", v[1], ".".join(v[2])]
    if k == "cat":
        parts = [enc(x, depth + 1) for x in v[1]]
        return ["+", parts] if all(x is not None for x in parts) else None
    if k == "any":
        return ["*"]
    if k == "pat":
        return ["P", v[1]]
    if k == "child":  # an Instance whose Name is v[1]: folder:FindFirstChild(name), x.Name = name
        inner = enc(v[1], depth + 1)
        return ["n", inner, v[2] if len(v) > 2 else ""] if inner is not None else None
    if k == "cfield":  # Mod.fn().Field: a field of what another module's function returns
        return ["y", v[1], v[2], ".".join(v[3])]
    return None


def is_xref(v):
    return bool(v) and (v[0] in XREF or (v[0] == "call" and bool(v[1])))


def concat(parts):
    """Join string parts. Known strings fold now; parts another module knows become ('cat', parts) for the
    linker; unknown parts become * in a ('pat', glob) when enough of the name is literal (Slot*Tier) -
    the idea of TypeScript's template literal types."""
    flat = []
    for x in parts:
        if x is not None and x[0] == "cat":
            flat.extend(x[1])
        elif x is not None and x[0] == "pat":
            flat.extend(("str", piece) if piece else ANY for piece in _split_glob(x[1]))
        else:
            flat.append(x if (strs_of(x) or is_xref(x)) else ANY)
    literal = sum(len(min(strs_of(x), key=len)) for x in flat if strs_of(x))
    if ANY in flat and literal < 3:
        return None
    if all(strs_of(x) for x in flat):
        acc = [""]
        for x in flat:
            acc = [a + b for a in acc for b in strs_of(x)][: MAXS * 2]
        return mkstrs(acc)
    if any(is_xref(x) for x in flat):
        return ("cat", tuple(flat)) if len(flat) <= 8 else None
    acc = [""]
    for x in flat:
        acc = [a + b for a in acc for b in (strs_of(x) or ("*",))][:MAXS]
    acc = sorted({_squash(a) for a in acc})
    return ("pat", acc[0]) if len(acc) == 1 else None


def _squash(glob):
    while "**" in glob:
        glob = glob.replace("**", "*")
    return glob


def _split_glob(glob):
    out = []
    for i, piece in enumerate(glob.split("*")):
        if i:
            out.append("")
        if piece:
            out.append(piece)
    return out


def names_in(v):
    """Concrete names, or a single glob for a partly known name."""
    s = strs_of(v)
    if s:
        return s
    if v and v[0] == "pat":
        return (v[1],)
    return ()


class Frame:
    __slots__ = ("kind", "saved", "exprifs", "fdef", "line", "start")

    def __init__(self, kind, line=0, fdef=None, start=0):
        self.kind = kind
        self.saved = []
        self.exprifs = []
        self.fdef = fdef
        self.line = line
        self.start = start


class Know:
    __slots__ = ("tf", "cf", "ret", "root", "types", "fkind", "setmeta", "argtypes")

    def __init__(self):
        self.tf = {}  # table key (own table name or constructor token) -> {field: value, '#': items}
        self.cf = {}  # (target, cls) -> {field: value}
        self.ret = {}  # function qual -> merged return value
        self.root = {}  # module-level local -> merged later assignments
        self.types = {}  # type alias -> (target, cls)
        self.fkind = {}  # function qual -> def kind
        self.setmeta = {}  # (function start token, local) -> object type from a later setmetatable(local, Class)
        self.argtypes = {}  # (local function, param idx) -> merged object type passed at its call sites


class Scanner:
    def __init__(self, src, inst, is_module=True, ctors=CONSTRUCTORS, rel=None):
        self.t = tokenize(src)
        self.inst = tuple(inst)
        self.rel = rel
        self.is_module = is_module
        self.ctors = ctors
        self.req_cache = {}
        self.requires = []
        self.fn_end = {}
        self.KP = Know()
        self.K = Know()
        self.out = False
        lines = self.t.src.split("\n")
        self.blank = {i + 1 for i, ln in enumerate(lines) if not ln.strip()}
        code = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("--")]
        ind = sum(1 for ln in code if ln[:1] in (" ", "\t"))
        self.resync_ok = bool(code) and ind / len(code) > 0.2
        self.reset()

    def reset(self):
        self.sym = {}
        self.frames = [Frame("root")]
        self.fn_names = []  # qual of each open function frame (None = anonymous)
        self.no_chain_until = 0
        self.pending = []  # (apply_at, frame, name, value, local)
        self.expr_kw = set()
        self.brace_owner = {}
        self.fn_hint = {}
        self.loop_vars = []
        self.returned = None
        self.rootdecl = set()
        self.tabs_done = set()
        self.warn = []
        self.errors = 0
        self.resyncs = 0
        self.chain_start = 0
        self.last_field = None
        self.defs = []
        self.calls = []  # [line, caller, target, member, kind, args(, receiver key)]
        self.reads = []  # [line, caller, target, path, w]
        self.remotes = []  # [line, caller, name, role, via, expr(, extra)]
        self.attrs = []  # [line, caller, name, op, receiver, expr(, extra)]
        self.tags = []  # [line, caller, name, op, expr(, extra)]
        self.ucalls = []  # [line, caller, method, receiver] receivers we could not type
        self.sinks = []  # [fq, param idx, kind, op, via] parameters that become remote/attr/tag names
        self.services = set()
        self.ret_fields = []

    # ------------------------------------------------------------------ small helpers
    def line(self, i):
        t = self.t
        return t.line(t.offs[i]) if 0 <= i < t.n else (t.line(len(t.src)) if t.n else 1)

    def v(self, i):
        return self.t.vals[i] if 0 <= i < self.t.n else None

    def k(self, i):
        return self.t.kinds[i] if 0 <= i < self.t.n else None

    def caller(self):
        for nm in self.fn_names:
            if nm:
                return nm
        return ""

    def fdepth(self):
        return len(self.fn_names)

    def bind(self, name, value, frame=None):
        f = frame or self.frames[-1]
        if f.kind != "root":
            f.saved.append((name, self.sym.get(name, _MISSING)))
        self.sym[name] = value

    def fn_start(self):
        for f in reversed(self.frames):
            if f.kind == "function":
                return f.start
        return -1

    def shadowed(self, name):
        for f in self.frames[1:]:
            for n, _ in f.saved:
                if n == name:
                    return True
        return False

    def is_call_open(self, j):
        if j >= self.t.n:
            return False
        k = self.t.kinds[j]
        return k == "S" or k == "I" or (k == "O" and self.t.vals[j] in CALL_OPEN)

    # knowledge: this pass first (sequential truth), then the previous pass (forward references)
    def tf_dict(self, key):
        a, b = self.KP.tf.get(key), self.K.tf.get(key)
        if a and b:
            d = dict(a)
            d.update(b)
            return d
        return b or a or {}

    def tf_get(self, key, f):
        d = self.K.tf.get(key)
        v = d.get(f, _MISSING) if d else _MISSING
        if v is _MISSING:
            d = self.KP.tf.get(key)
            v = d.get(f, _MISSING) if d else _MISSING
        return None if v is _MISSING else known(v)

    def cf_get(self, key, f):
        d = self.K.cf.get(key)
        v = d.get(f, _MISSING) if d else _MISSING
        if v is _MISSING:
            d = self.KP.cf.get(key)
            v = d.get(f, _MISSING) if d else _MISSING
        return None if v is _MISSING else known(v)

    def kget(self, store, key):
        v = getattr(self.K, store).get(key, _MISSING)
        if v is _MISSING:
            v = getattr(self.KP, store).get(key, _MISSING)
        return None if v is _MISSING else v

    def tf_put(self, key, f, value):
        d = self.K.tf.setdefault(key, {})
        d[f] = merge(d.get(f, _MISSING), value)

    def cf_put(self, key, f, value):
        d = self.K.cf.setdefault(key, {})
        d[f] = merge(d.get(f, _MISSING), value)

    def text(self, a, b, cap=90, sig=False):
        """Rebuild source-ish text for tokens a..b-1."""
        out = []
        t = self.t
        size = 0
        for i in range(max(a, 0), min(b, t.n)):
            k, val = t.kinds[i], t.vals[i]
            if k == "S":
                piece = '"%s"' % val
            elif k == "I":
                piece = "`%s`" % (val if val is not None else "...")
            elif k in ("N", "K", "D") and out and out[-1][-1:].isalnum():
                piece = " " + val
            elif val in ("=", "->", "..", "==", "~=", "and", "or", "::"):
                piece = " %s " % val
            elif val == ":" and sig:
                piece = ": "
            elif val == ",":
                piece = ", "
            else:
                piece = val
            out.append(piece)
            size += len(piece)
            if size > cap:
                return "".join(out)[:cap] + "~"
        return "".join(out)

    def arg_end(self, a):
        t = self.t
        close = t.match[t.parent[a]] if t.parent[a] >= 0 and t.match[t.parent[a]] > a else t.n
        e = a
        while e < t.n and e < close and not (t.vals[e] == "," and t.parent[e] == t.parent[a]):
            e = t.match[e] + 1 if t.vals[e] in ("(", "{", "[") and t.match[e] > e else e + 1
        return e

    def argtext(self, starts, n):
        if len(starts) <= n:
            return "?"
        a = starts[n]
        return self.text(a, self.arg_end(a), 40)

    def args(self, j):
        """Call args starting at token j ('(' / string / '{'). Returns (arg start indices, end index)."""
        t = self.t
        if t.kinds[j] in ("S", "I"):
            if t.kinds[j] == "I" and j in t.interp:
                last = t.interp[j][1][-1]
                return [j], (t.match[last] + 1 if t.match[last] > last else j + 1)
            return [j], j + 1
        m = t.match[j]
        if t.vals[j] == "{":
            return [j], (m + 1 if m > 0 else j + 1)
        if m < 0:
            return [], j + 1
        starts = [j + 1] if j + 1 < m else []
        p = j + 1
        while p < m and len(starts) < 5:
            vp = t.vals[p]
            if t.kinds[p] == "O" and vp in ("(", "[", "{") and t.match[p] > p:
                p = t.match[p] + 1
                continue
            if vp == "," and t.kinds[p] == "O":
                starts.append(p + 1)
            p += 1
        return starts, m + 1

    def argvals(self, starts):
        out = []
        for a in starts[:4]:
            val, e = self.eval_expr(a)
            sugar = self.v(a - 1) not in ("(", ",")  # f "x" / f {..}
            if not sugar and self.v(e) not in (",", ")"):
                val = None
            if val and val[0] in DEREF:
                val = self.deref(val)
            out.append(val)
        return out

    def skip_type(self, j):
        """Index just past a type annotation starting at j."""
        t = self.t
        n = t.n
        guard = 0
        while j < n and guard < 10000:
            guard += 1
            k, val = t.kinds[j], t.vals[j]
            if k == "O" and val == "<":
                j = self.skip_angles(j)
            if j >= n:
                return j
            k, val = t.kinds[j], t.vals[j]
            if k == "O" and val in ("(", "{", "["):
                m = t.match[j]
                if m < 0:
                    return j + 1
                j = m + 1
            elif k == "N" or k == "S" or (k == "K" and val in ("nil", "true", "false")):
                j += 1
                if val == "typeof" and self.v(j) == "(" and t.match[j] > 0:
                    j = t.match[j] + 1
                while self.v(j) == "." and self.k(j + 1) == "N":
                    j += 2
                if self.v(j) == "<":
                    j = self.skip_angles(j)
            elif val == "...":
                j += 1
                if self.k(j) == "N":
                    continue
            elif k == "O" and val in ("?", "|", "&"):
                j += 1
                continue
            else:
                return j
            while self.v(j) == "?":
                j += 1
            if self.k(j) == "O" and self.v(j) in ("|", "&", "->"):
                j += 1
                continue
            return j
        return j

    def skip_angles(self, j):
        t = self.t
        depth = 0
        while j < t.n:
            val = t.vals[j]
            if t.kinds[j] == "O":
                if val == "<":
                    depth += 1
                elif val == ">":
                    depth -= 1
                    if depth == 0:
                        return j + 1
                elif val in ("(", "{", "[") and t.match[j] > j:
                    j = t.match[j]
                elif val in (")", "}", "]", "=", ";"):
                    return j
            elif t.kinds[j] == "K" and val not in ("nil", "true", "false"):
                return j
            j += 1
        return j

    def type_value(self, a, b):
        """A type annotation naming a class: `Class`, `Alias` (typeof(setmetatable(...))), `Module.Type`."""
        toks = [p for p in range(a, b) if self.v(p) != "?"]
        if len(toks) == 1 and self.k(toks[0]) == "N":
            nm = self.v(toks[0])
            cls = self.kget("types", nm)
            if cls:
                return ("obj",) + tuple(cls)
            sv = self.sym.get(nm)
            if sv and sv[0] == "table":
                return ("obj", "", sv[1])
            return None
        if len(toks) == 3 and self.k(toks[0]) == "N" and self.v(toks[1]) == "." and self.k(toks[2]) == "N":
            sv = self.sym.get(self.v(toks[0]))
            if sv and sv[0] == "module":
                return ("tobj", sv[1], self.v(toks[2]))
        return None

    def class_in_type(self, a, b):
        """Class named by `typeof(setmetatable({} :: Data, Class))`, `setmetatable<D, typeof(Class)>`,
        or `typeof(Class.new(...))` - the Luau-documented class type patterns."""
        t = self.t
        for p in range(a, min(b, t.n)):
            if t.vals[p] == "setmetatable":
                if self.v(p + 1) == "(" and t.match[p + 1] > p:
                    close = t.match[p + 1]
                    q = p + 2
                    while q < close:
                        if t.vals[q] == "," and t.parent[q] == p + 1 and self.k(q + 1) == "N" and q + 2 == close:
                            return t.vals[q + 1]
                        q += 1
                if self.v(p + 1) == "<":
                    q = p + 2
                    while q < min(b, t.n) and t.vals[q] != ">":
                        if t.vals[q] == "typeof" and self.v(q + 1) == "(" and self.k(q + 2) == "N" and self.v(q + 3) == ")":
                            return t.vals[q + 2]
                        q += 1
            if t.vals[p] == "typeof" and self.v(p + 1) == "(" and self.k(p + 2) == "N" and self.v(p + 3) == "." \
                    and self.v(p + 4) in self.ctors:
                return t.vals[p + 2]
        return None

    # ------------------------------------------------------------------ passes
    def run(self):
        self.loop()
        self.KP, self.K = self.K, Know()
        self.reset()
        self.out = True
        self.loop()
        return self.finish()

    def loop(self):
        t = self.t
        kinds, vals = t.kinds, t.vals
        n = t.n
        i = 0
        while i < n:
            if self.pending and self.pending[0][0] <= i:
                self.apply_pending(i)
            try:
                k = kinds[i]
                if k == "K":
                    self.keyword(i)
                elif k == "N":
                    self.name(i)
                elif k == "O" and vals[i] == "::":
                    self.no_chain_until = max(self.no_chain_until, self.skip_type(i + 1))
            except Exception as e:  # never give up on a file
                self.errors += 1
                if len(self.warn) < 5 and self.out:
                    self.warn.append("L%d %s: %s" % (self.line(i), type(e).__name__, e))
            i += 1
        self.apply_pending(n + 1)
        if len(self.frames) > 1:
            if self.out:
                self.warn.append("unclosed blocks at EOF: %d (from L%d)" % (len(self.frames) - 1, self.frames[1].line))
            while len(self.frames) > 1:
                self.pop_frame(n - 1)

    def apply_pending(self, i):
        keep = []
        for item in self.pending:
            if item[0] <= i:
                at, frame, name, value, local = item
                if local:
                    self.bind(name, value, frame)
                else:
                    self.sym[name] = value
            else:
                keep.append(item)
        self.pending = keep

    def push_frame(self, kind, i, fdef=None):
        f = Frame(kind, self.line(i), fdef, i)
        self.frames.append(f)
        return f

    def pop_frame(self, i):
        if len(self.frames) <= 1:
            if self.out and len(self.warn) < 8:
                self.warn.append("L%d: stray end" % self.line(i))
            return
        f = self.frames.pop()
        for name, old in reversed(f.saved):
            if old is _MISSING:
                self.sym.pop(name, None)
            else:
                self.sym[name] = old
        if f.kind == "function":
            self.fn_names.pop()
            self.fn_end[f.start] = i
            if f.fdef is not None:
                f.fdef["end"] = self.line(i)

    def first_on_line(self, i):
        t = self.t
        if i == 0:
            return True
        return t.src.rfind("\n", t.offs[i - 1], t.offs[i]) >= 0

    def at_col0(self, i):
        return self.t.col(self.t.offs[i]) == 0 and self.first_on_line(i)

    def resync(self, i, why):
        if not self.resync_ok or len(self.frames) <= 1 or not self.at_col0(i):
            return
        self.resyncs += 1
        if self.out and len(self.warn) < 8:
            self.warn.append("L%d: block depth resynced at column-0 %s (was %d deep)" % (self.line(i), why, len(self.frames) - 1))
        while len(self.frames) > 1:
            self.pop_frame(i - 1)

    # ------------------------------------------------------------------ keywords
    def keyword(self, i):
        val = self.t.vals[i]
        if val == "function":
            if self.v(i - 1) != "local":
                self.resync(i, "function")
            self.function(i)
        elif val == "local":
            self.resync(i, "local")
            self.local(i)
        elif val == "end":
            if self.resync_ok and self.at_col0(i) and len(self.frames) > 2:
                self.resyncs += 1
                if self.out and len(self.warn) < 8:
                    self.warn.append("L%d: column-0 end closed %d blocks" % (self.line(i), len(self.frames) - 1))
                while len(self.frames) > 2:
                    self.pop_frame(i - 1)
            self.pop_frame(i)
        elif val == "do":
            f = self.push_frame("do", i)
            for nm, value in self.loop_vars:
                self.bind(nm, value, f)
            self.loop_vars = []
        elif val == "repeat":
            self.push_frame("repeat", i)
        elif val == "until":
            self.pop_frame(i)
        elif val == "if":
            prev = self.v(i - 1)
            if (prev in EXPR_PREV and self.k(i - 1) in ("O", "K")) or (i - 1) in self.expr_kw:
                self.frames[-1].exprifs.append("cond")
            else:
                self.push_frame("if", i)
        elif val == "then":
            ex = self.frames[-1].exprifs
            if ex and ex[-1] == "cond":
                ex[-1] = "then"
                self.expr_kw.add(i)
        elif val == "elseif":
            ex = self.frames[-1].exprifs
            if ex and ex[-1] == "then":
                ex[-1] = "cond"
        elif val == "else":
            ex = self.frames[-1].exprifs
            if ex and ex[-1] == "then":
                ex.pop()
                self.expr_kw.add(i)
        elif val == "for":
            names = []
            j = i + 1
            while self.k(j) == "N":
                names.append(self.t.vals[j])
                j += 1
                if self.v(j) == ":":
                    j = self.skip_type(j + 1)
                if self.v(j) == ",":
                    j += 1
                    continue
                break
            values = [None] * len(names)
            if self.v(j) == "in" and names:
                values = self.loop_values(j + 1, len(names))
            elif self.v(j) == "=" and len(names) == 1 and self.k(j + 1) == "D" and self.v(j + 2) == "," \
                    and self.k(j + 3) == "D" and self.v(j + 4) in ("do", ","):
                try:  # for i = 1, 4 do ... `Slot{i}`
                    lo, hi = int(self.t.vals[j + 1]), int(self.t.vals[j + 3])
                    step = int(self.t.vals[j + 5]) if self.v(j + 4) == "," and self.k(j + 5) == "D" else 1
                    if step and 0 < (hi - lo) // step + 1 <= MAXS:
                        values = [mkstrs(str(x) for x in range(lo, hi + (1 if step > 0 else -1), step))]
                except ValueError:
                    pass
            self.loop_vars = list(zip(names, values))
            self.no_chain_until = max(self.no_chain_until, j)
        elif val == "return":
            self.resync(i, "return")
            if len(self.frames) == 1:
                self.ret(i)
            elif self.fn_names:
                rv, _ = self.eval_expr(i + 1)
                if rv is not None and rv[0] in DEREF:
                    rv = self.deref(rv)
                if rv is not None and rv[0] not in ("other", "num", "tab", "func"):
                    # anonymous functions are keyed by their token: pcall(function() return X end)
                    fq = _norm(self.fn_names[-1]) if self.fn_names[-1] else "@%d" % self.fn_start()
                    self.K.ret[fq] = merge(self.K.ret[fq], rv) if fq in self.K.ret else rv

    def loop_values(self, j, n):
        """`for k, v in {..} / CONST / pairs(CONST)`: the string keys and string values it can produce."""
        if self.v(j) in ("ipairs", "pairs") and self.v(j + 1) == "(":
            j += 2
        val, e = self.eval_expr(j)
        if val and val[0] in DEREF:
            val = self.deref(val)
        if val and val[0] == "field":  # another module's table: its keys / elements, known at link time
            return ([("keys", val[1], val[2]), ("elem", val[1], val[2])] + [None] * n)[:n]
        if not val or val[0] not in ("tab", "table"):
            return [None] * n
        d = self.tf_dict(val[1])
        keys = [f for f in d if f not in ("#", "[]")]
        found = []
        for x in list(d.get("#", ())) + [d[f] for f in keys]:
            found.extend(strs_of(known(x)))
        elem = known(d.get("[]")) if not found else None
        res = [mkstrs(keys) if keys else None, mkstrs(found) if found else elem]
        return (res + [None] * n)[:n]

    def ret(self, i):
        j = i + 1
        t = self.t
        if self.k(j) == "N" and self.v(j + 1) not in (".", ":", "(", "[") and not self.is_call_open(j + 1):
            self.returned = t.vals[j]
        elif self.v(j) == "{":
            self.returned = "<return>"
            self.brace_owner[j] = "<return>"
            self.K.tf["<return>"] = self.K.tf.setdefault(j, {})
            self.eval_table(j)
        elif self.v(j) == "function" and self.inst:
            self.returned = self.inst[-1]
            self.fn_hint[j] = (self.inst[-1], "local")
        elif self.k(j) == "N":
            p = j
            while p < t.n and p < j + 8 and t.vals[p] != "(":
                p += 1
            if p < t.n and t.vals[p] == "(" and self.k(p + 1) == "N" and self.v(p + 2) in (")", ","):
                self.returned = t.vals[p + 1]

    # ------------------------------------------------------------------ function headers
    def function(self, i):
        t = self.t
        prev = self.v(i - 1)
        j = i + 1
        qual = None
        kind = None
        if prev == "local" and self.k(j) == "N":
            qual = t.vals[j]
            kind = "local"
            self.bind(qual, ("func", qual))
            j += 1
        elif self.k(j) == "N":
            parts = [t.vals[j]]
            j += 1
            sep = "."
            while self.v(j) in (".", ":") and self.k(j + 1) == "N":
                sep = t.vals[j]
                parts.append(t.vals[j + 1])
                j += 2
                if sep == ":":
                    break
            if len(parts) == 1:
                qual = parts[0]
                kind = "local" if parts[0] in self.sym else "global"
                if parts[0] in self.sym:
                    self.sym[parts[0]] = ("func", qual)
                else:
                    self.bind(qual, ("func", qual), self.frames[0])
            else:
                qual = ".".join(parts[:-1]) + sep + parts[-1]
                kind = "method" if sep == ":" else "member"
        else:
            qual, kind = self.fn_hint.pop(i, (None, None))
            if qual is None and prev == "=":
                qual, kind = self.lookback_name(i - 2)
        if self.v(j) == "<":
            j = self.skip_angles(j)
        params, sig, body, names = "", "", j, []
        if self.v(j) == "(" and t.match[j] > j:
            close = t.match[j]
            p = j + 1
            while p < close:
                if t.parent[p] == j and self.v(p - 1) in ("(", ","):
                    if t.kinds[p] == "N":
                        typ = None
                        if self.v(p + 1) == ":":
                            te = self.skip_type(p + 2)
                            typ = self.type_value(p + 2, te)
                        names.append((t.vals[p], typ))
                    elif t.vals[p] == "...":
                        names.append(("...", None))
                p += 1
            body = close + 1
            if self.v(body) == ":":
                body = self.skip_type(body + 1)
            params = ", ".join(nm for nm, _ in names)
            if self.out:
                sig = self.text(j, body, 160, sig=True)
        fdef = None
        fq = _norm(qual) if qual else None
        if fq:
            self.K.fkind[fq] = kind
        if qual and self.out:
            di = i - 1 if prev == "local" else i
            while self.v(di - 2) == "@" and self.k(di - 1) == "N":  # @native / @checked above the function
                di -= 2
            fdef = {"line": self.line(i), "end": None, "name": qual, "kind": kind, "params": params, "sig": sig,
                    "doc": self.doc_above(di), "depth": self.fdepth(), "parent": self.caller() or None}
            self.defs.append(fdef)
        f = self.push_frame("function", i, fdef)
        self.fn_names.append(qual)
        root = qual.replace(":", ".").rsplit(".", 1)[0] if qual and kind in ("method", "member", "field") else None
        rootv = self.sym.get(root.split(".")[0]) if root else None
        for idx, (nm, typ) in enumerate(names):
            if nm == "...":
                continue
            if nm == "self" and typ is None and idx == 0 and rootv and rootv[0] == "table":
                typ = ("obj", "", root)  # Luau's documented style: function Class.method(self, ...)
            if typ is None and fq:  # every call site in this file passes the same object type
                typ = known(self.KP.argtypes.get((fq, idx)))
            self.bind(nm, ("param", fq, idx, typ), f)
        if kind == "method":
            self.bind("self", ("obj", "", qual.split(":")[0]), f)
        self.no_chain_until = max(self.no_chain_until, body)

    def lookback_name(self, j):
        """For `x.y = function`, `local x = function`, `{ x = function`: name and kind (or (None, None))."""
        t = self.t
        parts = []
        while self.k(j) == "N":
            parts.insert(0, t.vals[j])
            if self.v(j - 1) == ".":
                j -= 2
            else:
                break
        if not parts:
            return None, None
        before = self.v(j - 1)
        if before == "local":
            return parts[0], "local"
        if before in ("{", ",", ";") and len(parts) == 1:
            p = t.parent[j]
            if p >= 0 and t.vals[p] == "{":
                owner = self.brace_owner.get(p)
                if owner:
                    return (parts[0] if owner == "<return>" else owner + "." + parts[0]), "field"
                return None, None
        if len(parts) == 1:
            return parts[0], "global" if parts[0] not in self.sym else "local"
        return ".".join(parts), "member"

    # ------------------------------------------------------------------ local statements
    def root_merge(self, nm, val):
        later = self.KP.root.get(nm, _MISSING)
        if later is _MISSING:
            return val
        return known(merge(val if val is not None else UNK, later))

    def local(self, i):
        t = self.t
        j = i + 1
        if self.v(j) == "function":
            return
        names = []
        while self.k(j) == "N":
            nm = t.vals[j]
            typ = None
            j += 1
            if self.v(j) == "<":  # <const> / <close>
                j = self.skip_angles(j)
            if self.v(j) == ":":
                ts = j + 1
                j = self.skip_type(ts)
                typ = self.type_value(ts, j)
            names.append((nm, typ))
            if self.v(j) == ",":
                j += 1
                continue
            break
        self.no_chain_until = max(self.no_chain_until, j)
        frame = self.frames[-1]
        root = len(self.frames) == 1
        if self.v(j) != "=":
            for nm, typ in names:
                value = typ
                if value is None and not root:  # local self; self = {..}; setmetatable(self, Class)
                    value = self.KP.setmeta.get((self.fn_start(), nm))
                if root:
                    self.rootdecl.add(nm)
                    value = self.root_merge(nm, value) if self.out else value
                self.bind(nm, value, frame)
            return
        e = j + 1
        if len(names) >= 2 and self.v(e) in ("pcall", "xpcall") and self.v(e + 1) == "(":
            # local ok, value = pcall(function() return X end) / pcall(fn, args): value is fn's return value
            starts, pe = self.args(e + 1)
            got = None
            if starts and self.v(starts[0]) == "function":
                got = ("call", "", "@%d" % starts[0], ())  # resolved lazily: the body comes after this line
            elif starts and self.k(starts[0]) == "N":
                fv = self.sym.get(self.v(starts[0]))
                if fv and fv[0] == "func" and self.v(starts[0] + 1) in (",", ")"):
                    av = self.argvals(starts[1:])
                    got = self.ret_value(fv[1], av) or ("call", "", _norm(fv[1]), tuple(av))
            if got is not None:
                self.pending.append((pe, frame, names[1][0], got, True))
                for nm, _ in [names[0]] + names[2:]:
                    self.pending.append((pe, frame, nm, None, True))
                self.pending.sort(key=lambda x: x[0])
                return
        for idx, (nm, typ) in enumerate(names):
            start = e
            if self.v(start) == "function":
                self.fn_hint[start] = (nm, "local")
            val, e = self.eval_expr(start)
            if val and val[0] in ("other", "num"):
                val = None
            if val is None and typ:
                val = typ
            if not root and (val is None or val[0] == "tab"):  # local self = {..} ... setmetatable(self, Class)
                later = self.KP.setmeta.get((self.fn_start(), nm))
                if later:
                    val = later
            if root and val and val[0] == "tab":
                self.brace_owner[val[1]] = nm
                self.K.tf[nm] = self.K.tf.setdefault(val[1], {})
                val = ("table", nm)
            elif root and self.v(start) == "setmetatable":
                val = ("table", nm)
            if root and self.out and self.v(start) != "function" and not (val and val[0] in ("module", "inst")):
                kind = "table" if val and val[0] == "table" else "const" if val and val[0] in ("str", "strs") else "value"
                self.defs.append({"line": self.line(i), "end": None, "name": nm, "kind": kind, "params": "",
                                  "sig": "", "doc": self.doc_above(i), "depth": 0, "parent": None})
            if val is not None and val[0] == "module":
                rec = self.req_cache.get(val[2]) if len(val) > 2 else None
                if rec is not None and rec[0][1] is None:
                    rec[0][1] = nm
                val = val[:2]
            if root:
                self.rootdecl.add(nm)
                if self.out:
                    val = self.root_merge(nm, val)
            self.pending.append((e, frame, nm, val, True))
            if self.v(e) == ",":
                e += 1
            else:
                for rest, _ in names[idx + 1:]:
                    self.pending.append((e, frame, rest, None, True))
                break
        self.pending.sort(key=lambda x: x[0])

    # ------------------------------------------------------------------ expressions
    def eval_expr(self, j):
        """Symbolic value of the expression at j (no facts recorded) and the index after it."""
        val, e = self.eval_operand(j)
        if self.v(e) == ".." and self.k(e) == "O":
            parts = [val]
            while self.v(e) == ".." and self.k(e) == "O":
                v2, e = self.eval_operand(e + 1)
                parts.append(v2)
            val = concat(parts)
        if self.v(e) in ("and", "or") and self.k(e) == "K":
            op = self.t.vals[e]
            val2, e2 = self.eval_expr(e + 1)
            if op == "and":
                val = val2
            elif val is None or val[0] == "tab":
                val = val2
            else:
                a, b = strs_of(val), strs_of(val2)
                if a and b:
                    val = mkstrs(a + b)
            e = e2
        elif self.k(e) == "O" and self.t.vals[e] in ARITH:
            val = ("other",)
        return val, e

    def eval_operand(self, j):
        t = self.t
        opened = 0
        while self.v(j) == "(" and self.k(j) == "O":
            opened += 1
            j += 1
        k = self.k(j)
        val = None
        if k == "N":
            if t.vals[j] == "setmetatable" and self.v(j + 1) == "(":
                starts, e = self.args(j + 1)
                val = self.setmeta_value(starts)
            else:
                val, e = self.walk(j, False)
                if val and val[0] in DEREF:
                    d = self.deref(val)
                    if d is not None:
                        val = d
        elif k == "S":
            val, e = ("str", t.vals[j]), j + 1
        elif k == "I":
            val, e = self.eval_interp(j)
        elif k == "D":
            val, e = ("num",), j + 1
        elif self.v(j) == "{":
            self.eval_table(j)
            val, e = ("tab", j), (t.match[j] + 1 if t.match[j] > j else j + 1)
        elif self.v(j) == "function":
            end = self.fn_end.get(j)
            return ("other",), (end + 1 if end else j + 1)
        elif self.v(j) in ("not", "-", "#"):
            _, e = self.eval_operand(j + 1)
            return ("other",), e
        elif self.v(j) == "if" and self.k(j) == "K":
            return self.eval_ifexpr(j)
        else:
            return None, j + 1
        if self.v(e) == "::":
            e = self.skip_type(e + 1)
        while opened and self.v(e) == ")":
            opened -= 1
            e += 1
            if self.v(e) == "::":
                e = self.skip_type(e + 1)
        if opened:
            return None, e
        return val, e

    def find_then(self, j):
        t = self.t
        p = j
        while p < t.n and p < j + 300:
            vp = t.vals[p]
            if t.kinds[p] == "K":
                if vp == "then":
                    return p
                if vp in ("end", "local", "function", "do", "return", "if", "else", "elseif"):
                    return None
            if t.kinds[p] == "O" and vp in ("(", "{", "[") and t.match[p] > p:
                p = t.match[p] + 1
                continue
            p += 1
        return None

    def eval_ifexpr(self, j):
        """`if c then A elseif d then B else C`: any of the branch values (strings union)."""
        branches = []
        p = j
        while True:
            th = self.find_then(p + 1)
            if th is None:
                return None, j + 1
            v1, e = self.eval_expr(th + 1)
            branches.append(v1)
            if self.v(e) == "elseif":
                p = e
                continue
            if self.v(e) == "else":
                v2, e = self.eval_expr(e + 1)
                branches.append(v2)
                break
            return None, e
        found = []
        for b in branches:
            sb = strs_of(b)
            if not sb:
                return None, e
            found.extend(sb)
        return mkstrs(found), e

    def eval_interp(self, j):
        t = self.t
        info = t.interp.get(j)
        if not info:
            return ("str", t.vals[j] or ""), j + 1
        parts, opens = info
        pieces = [("str", parts[0])]
        e = j + 1
        ok = True
        for hi, h in enumerate(opens):
            hv, he = self.eval_expr(h + 1)
            m = t.match[h]
            e = m + 1 if m > h else he + 1
            if self.v(he) != ")":
                ok = False
            pieces += [hv, ("str", parts[hi + 1])]
        return (concat(pieces) if ok else None), e

    def setmeta_value(self, starts):
        """setmetatable(t, Class) / setmetatable(t, {__index = Class}) -> object of Class."""
        if len(starts) < 2:
            return None
        b = starts[1]
        if self.k(b) == "N" and self.v(b + 1) == ")":
            return ("obj", "", self.t.vals[b])
        if self.v(b) == "{":
            close = self.t.match[b]
            p = b + 1
            while 0 < p < close:
                if self.v(p) == "__index" and self.v(p + 1) == "=" and self.k(p + 2) == "N" and self.v(p + 3) in (",", "}"):
                    return ("obj", "", self.t.vals[p + 2])
                p += 1
        return None

    def eval_table(self, open_):
        """Fields and list items of a table constructor, kept as knowledge (memoised per pass)."""
        if open_ in self.tabs_done:
            return
        self.tabs_done.add(open_)
        t = self.t
        close = t.match[open_]
        if close <= open_:
            return
        d = self.K.tf.setdefault(open_, {})
        items = []
        p = open_ + 1
        guard = 0
        while p < close and guard < 5000:
            guard += 1
            vp = t.vals[p]
            if vp in (",", ";") and t.kinds[p] == "O":
                p += 1
                continue
            key = None
            vs = p
            if t.kinds[p] == "N" and self.v(p + 1) == "=" and self.k(p + 1) == "O":
                key, vs = vp, p + 2
            elif vp == "[" and self.k(p + 1) == "S" and self.v(p + 2) == "]" and self.v(p + 3) == "=":
                key, vs = t.vals[p + 1], p + 4
            elif vp == "[" and t.match[p] > p and self.v(t.match[p] + 1) == "=":
                kv, _ = self.eval_expr(p + 1)
                key, vs = (strs_of(kv) or False), t.match[p] + 2
            val, e = self.eval_expr(vs)
            if val and val[0] in ("other", "num"):
                val = None
            if isinstance(key, tuple):
                for kk in key:
                    d[kk] = merge(d.get(kk, _MISSING), val)
            elif key:
                d[key] = merge(d.get(key, _MISSING), val)
            elif key is None:
                items.append(val if val is not None else UNK)
            p = max(e, p + 1)
            while p < close and not (t.vals[p] in (",", ";") and t.parent[p] == open_):
                if t.vals[p] == "function" and t.kinds[p] == "K":
                    end = self.fn_end.get(p)
                    if end is None:  # first pass: body extent unknown yet, keep what we have
                        d["#"] = tuple(items)
                        return
                    p = end + 1
                elif t.vals[p] in ("(", "{", "[") and t.match[p] > p:
                    p = t.match[p] + 1
                else:
                    p += 1
        d["#"] = tuple(items)

    # ------------------------------------------------------------------ names: chains, fields, assignments
    def name(self, i):
        t = self.t
        val = t.vals[i]
        prev = self.v(i - 1)
        if prev in (".", ":", "::") and self.k(i - 1) == "O":
            return
        if i < self.no_chain_until:
            return
        if val in ("type", "export") and self.typedecl(i):
            return
        nxt = self.v(i + 1)
        if nxt == "=" and prev in ("{", ",", ";"):
            p = t.parent[i]
            if p >= 0 and t.vals[p] == "{":
                self.field(i, p)
                return
        if nxt in ASSIGN and self.k(i + 1) == "O" and val != "self":
            rootvar = val in self.rootdecl and not self.shadowed(val)
            if nxt == "=":
                v2, e = self.eval_expr(i + 2)
                if v2 is not None and v2[0] == "module" and len(v2) > 2:
                    rec = self.req_cache.get(v2[2])
                    if rec is not None and rec[0][1] is None:
                        rec[0][1] = val
                    v2 = v2[:2]
                if v2 is not None and v2[0] in ("other", "num"):
                    v2 = None
                if v2 is not None and v2[0] in SLOTS:
                    v2 = self.deref(v2)
                    if v2 is not None and v2[0] in SLOTS and rootvar:
                        v2 = None  # another module's field: unknown here, must not conflict with a known type
                if v2 is not None and v2[0] == "tab" and rootvar:
                    v2 = None
            else:
                v2, e = None, i + 2
            if rootvar:
                self.K.root[val] = merge(self.K.root.get(val, _MISSING), v2)
                if self.out:
                    return  # the declaration already carries the merged value of every assignment
            cur = self.sym.get(val)
            if v2 is None and cur is not None and not strs_of(cur):
                return  # `x = nil` in some branch: x keeps its type (strings, below, become unknown)
            if nxt == "=" and (v2 is not None or val in self.sym):
                self.pending.append((e, None, val, v2, False))
                self.pending.sort(key=lambda x: x[0])
            elif nxt != "=" and val in self.sym and strs_of(cur):
                self.sym[val] = None  # name ..= x: no longer a constant
            return
        self.walk(i, True)

    def field(self, i, brace):
        owner = self.brace_owner.get(brace)
        if not owner or self.fdepth() > 0:
            return
        t = self.t
        nm = t.vals[i]
        vstart = i + 2
        if owner == "<return>":
            vname = t.vals[vstart] if self.k(vstart) == "N" and self.v(vstart + 1) in (",", "}", ";") else None
            if self.out:
                self.ret_fields.append((nm, self.line(i), vname, self.doc_above(i)))
            if self.v(vstart) == "function":
                self.fn_hint[vstart] = (nm, "field")
            return
        qual = owner + "." + nm
        if self.v(vstart) == "function":
            self.fn_hint[vstart] = (qual, "field")
            return
        if self.v(vstart) == "{" and owner.count(".") < 1:
            self.brace_owner[vstart] = qual
        if self.out:
            self.defs.append({"line": self.line(i), "end": None, "name": qual, "kind": "field", "params": "",
                              "sig": "", "doc": self.doc_above(i), "depth": 0, "parent": None})

    def typedecl(self, i):
        t = self.t
        j = i
        exported = False
        if t.vals[j] == "export":
            if self.v(j + 1) != "type":
                return False
            exported = True
            j += 1
        if self.k(j + 1) != "N" or self.v(j + 2) not in ("=", "<"):
            return False
        prev = self.v(i - 1)
        if prev in EXPR_PREV or prev in (".", ":"):
            return False
        nm = t.vals[j + 1]
        e = j + 2
        if self.v(e) == "<":
            e = self.skip_angles(e)
        if self.v(e) != "=":
            return False
        end = self.skip_type(e + 1)
        cls = self.class_in_type(e + 1, end)
        if cls:
            self.K.types[nm] = ("", cls)
        if self.out:
            self.defs.append({"line": self.line(i), "end": self.line(max(end - 1, i)), "name": nm,
                              "kind": "export type" if exported else "type", "params": "", "sig": "",
                              "doc": self.doc_above(i), "depth": self.fdepth(), "parent": None})
        self.no_chain_until = max(self.no_chain_until, end)
        return True

    # ------------------------------------------------------------------ require
    def require_at(self, i):
        if i in self.req_cache:
            rec, val = self.req_cache[i]
            return val, rec[4]
        t = self.t
        j = i + 1
        target = None
        expr = ""
        end = j + 1
        if self.v(j) == "(" and t.match[j] > j:
            close = t.match[j]
            end = close + 1
            a = j + 1
            expr = self.text(a, close)
            k = self.k(a)
            if k == "D":
                target = "?asset:" + t.vals[a]
            elif k == "S":
                target = self.string_require(t.vals[a])
            elif k == "N":
                v, e = self.walk(a, False)
                if v and v[0] == "inst":
                    target = ".".join(v[1]) if v[1] else "?game"
                elif v and v[0] == "module":
                    target = "?module-value"
            if target is None:
                target = "?" + (expr or "dynamic")
        elif self.k(j) == "S":
            expr = '"%s"' % t.vals[j]
            target = self.string_require(t.vals[j])
            end = j + 1
        else:
            target = "?dynamic"
        alt = ""
        if self.rel and expr.startswith('"') and expr.strip('"')[:2] in ("./", ".."):
            alt = self.fs_require(expr.strip('"'))
        rec = [self.line(i), None, target, expr, end, alt]
        self.requires.append(rec)
        val = ("module", target, i)
        self.req_cache[i] = (rec, val)
        return val, end

    def fs_require(self, s):
        """require("../x/Y") relative to the requiring file (Luau's require-by-string RFC; luau-lsp falls back
        to the filesystem the same way when the Rojo tree does not have the path)."""
        parts = self.rel.split("/")[:-1]
        for piece in s.split("/"):
            if piece in ("", "."):
                continue
            if piece == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(piece)
        out = "/".join(parts)
        for ext in (".luau", ".lua"):
            if out.endswith(ext):
                out = out[: -len(ext)]
        return out

    def string_require(self, s):
        parts = [p for p in s.split("/") if p]
        if not parts:
            return "?" + s
        if parts[0] == "@self":
            base = list(self.inst)
            parts = parts[1:]
        elif parts[0] == "@game":
            base = []
            parts = parts[1:]
        elif parts[0] in (".", ".."):
            base = list(self.inst[:-1])
        elif parts[0].startswith("@"):
            return "?alias:" + s  # .luaurc aliases are mapped to files by the indexer
        else:
            base = list(self.inst[:-1])
        for p in parts:
            if p == ".":
                continue
            if p == "..":
                base = base[:-1]
            else:
                base.append(p.rsplit(".luau", 1)[0].rsplit(".lua", 1)[0])
        return ".".join(base)

    # ------------------------------------------------------------------ chain walk
    def start_value(self, i):
        nm = self.t.vals[i]
        if nm == "game":
            return ("inst", ())
        if nm == "script":
            return ("inst", self.inst)
        if nm == "workspace":
            return ("inst", ("Workspace",))
        if nm == "Instance" and nm not in self.sym:
            return ("instlib",)
        return self.sym.get(nm)

    def table_insert(self, j):
        """table.insert(list, [pos,] value): the list's element type. Returns False: walk normally."""
        starts, _ = self.args(j)
        if len(starts) not in (2, 3):
            return False
        base = self.deref(self.eval_expr(starts[0])[0])
        v = self.eval_expr(starts[-1])[0]
        if v is not None and v[0] in DEREF:
            v = self.deref(v)
        if base and base[0] in ("tab", "table") and (v is None or v[0] not in ("other", "num", "tab")):
            self.tf_put(base[1], "[]", v)
        return False

    def spawned(self, i):
        """task.spawn(fn, ...) / task.defer / task.delay(t, fn, ...) / pcall(fn, ...) / xpcall(fn, h, ...):
        fn gets these arguments, so its parameters learn their object types. Returns False: walk normally."""
        nm = self.t.vals[i]
        j = i + 1
        if nm == "task":
            if self.v(j) != "." or self.v(j + 1) not in ("spawn", "defer", "delay") or self.v(j + 2) != "(":
                return False
            skip = 1 if self.v(j + 1) == "delay" else 0
            j += 2
        elif self.v(j) == "(":
            skip = 1 if nm == "xpcall" else 0
        else:
            return False
        starts, _ = self.args(j)
        fpos = 1 if nm == "task" and skip else 0
        if len(starts) <= fpos or self.k(starts[fpos]) != "N" or self.v(starts[fpos] + 1) not in (",", ")"):
            return False
        fv = self.sym.get(self.v(starts[fpos]))
        if fv and fv[0] == "func":
            rest = starts[fpos + 1 + (skip if nm == "xpcall" else 0):]
            for idx, a in enumerate(self.argvals(rest)):
                if a is not None and a[0] == "param":
                    a = a[3]
                if a is not None and a[0] in ("obj", "tobj"):
                    key = (_norm(fv[1]), idx)
                    self.K.argtypes[key] = merge(self.K.argtypes.get(key, _MISSING), a)
        return False

    def deref(self, val, depth=0):
        """Slot -> the value stored in it (None if unknown). Typed params -> their type."""
        if val is None or depth > 6:
            return val
        k = val[0]
        if k == "param":
            return val[3] if val[3] is not None else val
        if k == "call":  # a local function's result: its (substituted) return value once known
            if val[1]:
                return val
            r = self.ret_value(val[2], list(val[3]))
            return r if r is not None else val
        if k == "ownfield":
            cur, path = ("table", val[1]), val[2]
        elif k == "tabfield":
            cur, path = ("tab", val[1]), val[2]
        elif k == "objfield":
            if val[1]:
                return val  # another module's class: resolved at link time
            cur, path = ("obj", "", val[2]), val[3]
        else:
            return val
        for f in path:
            ck = cur[0]
            if ck in ("table", "tab"):
                cur = self.tf_get(cur[1], f)
            elif ck == "obj":
                cur = self.cf_get((cur[1], cur[2]), f) if not cur[1] else ("objfield", cur[1], cur[2], (f,))
            elif ck == "inst":
                cur = ("inst", cur[1] + (f,))
            elif ck == "module":
                cur = ("field", cur[1], (f,))
            elif ck == "field":
                cur = ("field", cur[1], cur[2] + (f,))
            else:
                return None
            if cur is None:
                return None
            if cur[0] in SLOTS or cur[0] == "param":
                cur = self.deref(cur, depth + 1)
                if cur is None:
                    return None
        return cur

    def walk(self, i, rec):
        """Evaluate the prefix chain starting at NAME token i. Records facts when rec is True (pass 2)."""
        t = self.t
        kinds, vals, match = t.kinds, t.vals, t.match
        n = t.n
        nm = vals[i]
        j = i + 1
        if rec:
            self.chain_start = i
            self.last_field = None
        if nm == "require" and (self.v(j) == "(" or self.k(j) == "S"):
            val, j = self.require_at(i)
        elif rec and nm == "table" and nm not in self.sym and self.v(j) == "." and self.v(j + 1) == "insert"                 and self.v(j + 2) == "(" and self.table_insert(j + 2):
            val, j = None, i + 1
        elif rec and nm in ("task", "pcall", "xpcall") and nm not in self.sym and self.spawned(i):
            val, j = None, i + 1
        elif nm == "setmetatable" and self.v(j) == "(":
            starts, j = self.args(j)
            val = self.setmeta_value(starts)
            # statement `setmetatable(self, Class)`: self is now an object of Class
            if rec and val and starts and self.k(starts[0]) == "N" and self.v(starts[0] + 1) == "," \
                    and self.v(i - 1) not in EXPR_PREV:
                self.sym[vals[starts[0]]] = val
                self.K.setmeta[(self.fn_start(), vals[starts[0]])] = val
        else:
            val = self.start_value(i)
            if val and val[0] == "func" and rec and self.out and not (j < n and self.is_call_open(j)):
                self.calls.append([self.line(i), self.caller(), "", val[1], "r", None])  # pcall(step, ...)
        guard = 0
        while j < n and guard < 500:
            guard += 1
            tv = vals[j]
            tk = kinds[j]
            if tk != "O" and tk != "S" and tk != "I":
                break
            if tv == "." and tk == "O" and j + 1 < n and kinds[j + 1] == "N":
                f = vals[j + 1]
                fl = j + 1
                j += 2
                if rec and f in ROLE_FIELDS:
                    self.role_site(fl, val, f)
                if rec:
                    self.last_field = f
                if j < n and self.is_call_open(j):
                    starts, j2 = self.args(j)
                    argv = self.argvals(starts)
                    val = self.call_member(val, f, fl, argv, rec, False)
                    j = j2
                else:
                    val = self.field_of(val, f)
            elif tv == ":" and tk == "O" and j + 1 < n and kinds[j + 1] == "N" and j + 2 < n and self.is_call_open(j + 2):
                m = vals[j + 1]
                ml = j + 1
                starts, j2 = self.args(j + 2)
                argv = self.argvals(starts)
                if rec and self.out:
                    self.method_facts(val, m, ml, starts, argv, i)
                val = self.call_member(val, m, ml, argv, rec, True)
                j = j2
            elif tv == "[" and tk == "O":
                c = match[j]
                if c < 0:
                    break
                if c == j + 2 and kinds[j + 1] == "S":
                    val = self.field_of(val, vals[j + 1])
                else:
                    base = self.deref(val) if val and val[0] in DEREF else val
                    if val and val[0] == "field" and rec and self.out:
                        self.flush_field(val, j, False)
                    val = None
                    if base and base[0] in ("tab", "table"):
                        if rec and self.v(c + 1) == "=" and self.k(c + 1) == "O":  # t[k] = v: element type
                            rhs = self.eval_expr(c + 2)[0]
                            if rhs is not None and rhs[0] in DEREF:
                                rhs = self.deref(rhs)
                            if rhs is None or rhs[0] not in ("other", "num", "tab"):
                                self.tf_put(base[1], "[]", rhs)
                        d = self.tf_dict(base[1])
                        found = []
                        for x in list(d.get("#", ())) + [d[f] for f in d if f not in ("#", "[]")]:
                            found.extend(strs_of(known(x)))
                        val = mkstrs(found) if found else known(d.get("[]"))  # LIST[i]: its strings / element type
                j = c + 1
            elif tk == "S" or tk == "I" or (tk == "O" and tv in CALL_OPEN):
                starts, j2 = self.args(j)
                argv = self.argvals(starts)
                val = self.call_value(val, j, argv, rec)
                j = j2
            else:
                break
        if rec and val:
            k = val[0]
            if self.out and k == "field":
                self.flush_field(val, j, True)
            elif self.out and k == "ownfield":
                self.flush_own(val, j, i)
            if k in SLOTS and self.k(j) == "O" and self.v(j) in ASSIGN:
                rhs = self.eval_expr(j + 1)[0] if self.v(j) == "=" else None
                self.store(val, rhs)
        if rec and j == i + 3 and vals[i + 2] == "Name" and vals[i + 1] == "." and self.v(j) == "=" and self.k(j) == "O":
            cur = self.sym.get(nm)
            if cur and cur[0] in ("new", "child"):
                cls = cur[1] if cur[0] == "new" else (cur[2] if len(cur) > 2 else "")
                self.sym[nm] = ("child", self.eval_expr(j + 1)[0], cls)
        return val, j

    def field_of(self, val, f):
        if val is None:
            return None
        k = val[0]
        if k == "param":
            val = val[3]
            if val is None:
                return None
            k = val[0]
        if k == "inst":
            p = val[1]
            return ("inst", p[:-1]) if f == "Parent" else ("inst", p + (INST_REMAP.get(f, f),))
        if k == "module":
            return ("field", val[1], (f,))
        if k == "field":
            return ("field", val[1], val[2] + (f,)) if len(val[2]) < 4 else val
        if k == "table":
            return ("ownfield", val[1], (f,))
        if k == "tab":
            return ("tabfield", val[1], (f,))
        if k == "obj":
            return ("objfield", val[1], val[2], (f,))
        if k in ("ownfield", "tabfield"):
            return (k, val[1], val[2] + (f,)) if len(val[2]) < 4 else val
        if k == "objfield":
            return (k, val[1], val[2], val[3] + (f,)) if len(val[3]) < 4 else val
        if f in ROLE_METHODS:
            return ("rolefn", val, f)
        if k == "call" and val[1]:
            return ("cfield", val[1], val[2], (f,))
        if k == "cfield":
            return (k, val[1], val[2], val[3] + (f,)) if len(val[3]) < 4 else val
        return None

    def store(self, slot, value):
        k = slot[0]
        if k == "ownfield":
            cur, path = ("table", slot[1]), slot[2]
        elif k == "tabfield":
            cur, path = ("tab", slot[1]), slot[2]
        else:
            if slot[1]:
                return
            cur, path = ("obj", "", slot[2]), slot[3]
        for f in path[:-1]:
            cur = self.deref(self.field_of(cur, f))
            if cur is None or cur[0] not in ("table", "tab", "obj") or (cur[0] == "obj" and cur[1]):
                return
        if value is not None and value[0] in ("other", "num"):
            value = None
        if value is not None and value[0] in SLOTS:
            value = self.deref(value)
        f = path[-1]
        if cur[0] == "obj":
            self.cf_put((cur[1], cur[2]), f, value)
        else:
            self.tf_put(cur[1], f, value)

    def flush_field(self, val, j, at_end):
        w = 1 if at_end and self.k(j) == "O" and self.v(j) in ASSIGN else 0
        self.reads.append([self.line(j - 1), self.caller(), val[1], ".".join(val[2]), w])

    def flush_own(self, val, j, i):
        """Own-table chain end: member definition (T.x = value) or own read."""
        path = ".".join(val[2])
        if self.k(j) == "O" and self.v(j) in ASSIGN:
            if self.v(j) == "=" and self.v(j + 1) != "function":
                d = {"line": self.line(i), "end": None, "name": val[1] + "." + path, "kind": "value",
                     "params": "", "sig": "", "doc": self.doc_above(i), "depth": self.fdepth(),
                     "parent": self.caller() or None}
                rv = self.sym.get(self.v(j + 1)) if self.k(j + 1) == "N" and self.v(j + 2) not in (".", ":", "(", "[") else None
                if rv and rv[0] == "func":
                    d["alias"] = rv[1]  # M.x = localFn: same signature
                self.defs.append(d)
                if self.v(j + 1) == "{" and self.fdepth() == 0 and len(val[2]) == 1:
                    self.brace_owner[j + 1] = val[1] + "." + path
            else:
                self.reads.append([self.line(i), self.caller(), "", val[1] + "." + path, 1])
        else:
            self.reads.append([self.line(i), self.caller(), "", val[1] + "." + path, 0])

    def encargs(self, argv):
        out = [enc(a) for a in argv]
        while out and out[-1] is None:
            out.pop()
        return out or None

    def add_call(self, tok, target, member, kind, argv, extra=None):
        rec = [self.line(tok), self.caller(), target, member, kind, self.encargs(argv)]
        if extra is not None:
            rec.append(extra)
        self.calls.append(rec)

    def ret_value(self, qual, argv, method_call=False):
        fq = _norm(qual)
        r = known(self.kget("ret", fq))
        if r is None:
            return None
        if method_call and self.kget("fkind", fq) not in ("method", None):
            argv = [None] + list(argv)  # obj:m(a) into function Class.m(self, a)
        return self.subst(r, fq, argv)

    def subst(self, v, fq, argv, depth=0):
        if v is None or depth > 4:
            return v
        k = v[0]
        if k == "param":
            if v[1] == fq:
                return argv[v[2]] if v[2] < len(argv) else None
            return v
        if k == "call":
            return ("call", v[1], v[2], tuple(self.subst(a, fq, argv, depth + 1) for a in v[3]))
        if k == "child":
            return ("child", self.subst(v[1], fq, argv, depth + 1), v[2] if len(v) > 2 else "")
        if k == "cat":
            return concat([self.subst(a, fq, argv, depth + 1) for a in v[1]])
        return v

    def local_call(self, tok, qual, argv, rec, method=False):
        if rec and self.out:
            self.add_call(tok, "", qual, "l", argv)
        if rec:
            fq = _norm(qual)
            shifted = [None] + list(argv) if method and self.kget("fkind", fq) not in ("method", None) else argv
            for idx, a in enumerate(shifted):
                if a is not None and a[0] == "param":
                    a = a[3]
                if a is not None and a[0] in ("obj", "tobj"):
                    key = (fq, idx)
                    self.K.argtypes[key] = merge(self.K.argtypes.get(key, _MISSING), a)
        r = self.ret_value(qual, argv, method)
        if r is not None:
            return r
        if "." in qual and qual.rsplit(".", 1)[-1] in self.ctors:
            return ("obj", "", qual.rsplit(".", 1)[0])
        return ("call", "", _norm(qual), tuple(argv))

    def call_value(self, val, j, argv, rec):
        """`x(args)` where x is the chain value so far."""
        if val is None:
            return None
        k0 = val[0]
        if k0 == "ownfield":
            return self.local_call(j - 1, val[1] + "." + ".".join(val[2]), argv, rec)
        base = self.deref(val) if k0 in DEREF else val
        if base is None:
            return None
        k = base[0]
        if k == "func":
            return self.local_call(j - 1, base[1], argv, rec)
        if k == "field":
            member = ".".join(base[2])
            if rec and self.out:
                self.add_call(j - 1, base[1], member, "c", argv)
            if base[2][-1] in self.ctors:
                return ("obj", base[1], ".".join(base[2][:-1]))
            return ("call", base[1], member, tuple(argv))
        if k == "rolefn":
            if rec:
                self.role_site(j - 1, base[1], base[2])
            return None
        if k == "module":
            if rec and self.out:
                self.add_call(j - 1, base[1], "", "c", argv)
            return ("call", base[1], "", tuple(argv))
        return None

    def call_member(self, val, f, fl, argv, rec, method):
        """val.f(args) or val:f(args). Returns the call's value."""
        if val is None:
            return None
        k0 = val[0]
        out = rec and self.out
        if k0 == "ownfield" and not method:
            return self.local_call(fl, val[1] + "." + ".".join(val[2] + (f,)), argv, rec)
        base = self.deref(val) if k0 in DEREF or k0 == "param" else val
        if method and f in ("WaitForChild", "FindFirstChild") and argv and argv[0] is not None \
                and (base is None or base[0] in ("child", "new", "param", "cfield")):
            return ("child", argv[0], "")  # some folder's child named by the argument
        if base is None:
            return None
        k = base[0]
        if k == "instlib":
            cls = strs_of(argv[0]) if argv else ()
            return ("new", cls[0] if len(cls) == 1 else "")
        if k == "module":
            if out:
                self.add_call(fl, base[1], f, "m" if method else "c", argv)
            return ("obj", base[1], "") if f in self.ctors else ("call", base[1], f, tuple(argv))
        if k == "field":
            member = ".".join(base[2] + (f,))
            if out:
                self.add_call(fl, base[1], member, "m" if method else "c", argv)
            return ("obj", base[1], ".".join(base[2])) if f in self.ctors else ("call", base[1], member, tuple(argv))
        if k == "table":
            return self.local_call(fl, base[1] + "." + f, argv, rec, method)
        if k == "tab":
            fv = self.tf_get(base[1], f)
            if fv and fv[0] == "func":
                return self.local_call(fl, fv[1], argv, rec)
            if fv and fv[0] == "field":
                return self.call_value(fv, fl + 1, argv, rec)
            return None
        if k == "obj":
            target, cls = base[1], base[2]
            member = (cls + "." if cls else "") + f
            if not target:
                return self.local_call(fl, member, argv, rec, method)
            if out:
                self.add_call(fl, target, member, "o", argv)
            return ("obj", target, cls) if f in self.ctors else ("call", target, member, tuple(argv))
        if k == "tobj":
            if out:
                self.add_call(fl, base[1], "@%s.%s" % (base[2], f), "o", argv)
            return None
        if k == "objfield" and base[1]:
            if out:
                key = "c:%s.%s" % (base[2], ".".join(base[3])) if base[2] else "c:" + ".".join(base[3])
                self.add_call(fl, base[1], f, "x", argv, key)
            return None
        if k == "call" and base[1]:
            if out:  # method on another module's function result: typed at link time from its return value
                self.add_call(fl, base[1], f, "x", argv, "r:" + base[2])
            return None
        if k == "inst" and method:
            p = base[1]
            a0 = strs_of(argv[0]) if argv else ()
            name = a0[0] if len(a0) == 1 else None
            if f == "GetService" and name:
                if out:
                    self.services.add(name)
                return ("inst", (name,))
            if f in ("WaitForChild", "FindFirstChild") and name and (f == "WaitForChild" or len(argv) == 1):
                return ("inst", p + (INST_REMAP.get(name, name),))
            if f in ("WaitForChild", "FindFirstChild") and argv and argv[0] is not None:
                return ("child", argv[0], "")
            if f == "FindFirstAncestor" and name and name in p:
                return ("inst", p[: len(p) - p[::-1].index(name)])
            return None
        return None

    def method_facts(self, val, m, ml, starts, argv, i):
        if m in ROLE_METHODS:
            self.role_site(ml, val, m)
        elif m in ATTR_METHODS:
            self.name_site(self.attrs, argv[0] if argv else None, ATTR_METHODS[m], ml, starts, 0, "attr",
                           self.text(i, ml - 1, 30))
        elif m in TAG_METHODS:
            op = TAG_METHODS[m]
            base = self.deref(val)
            cs = (base and base[0] == "inst" and base[1] == ("CollectionService",)) or self.t.vals[i] == "CollectionService"
            idx = 1 if cs and op in ("add", "remove", "has") else 0
            self.name_site(self.tags, argv[idx] if len(argv) > idx else None, op, ml, starts, idx, "tag", None)
        base = self.deref(val) if val and (val[0] in DEREF or val[0] == "param") else val
        k = base[0] if base else None
        if not (k in ("module", "field", "table", "tab", "obj", "tobj", "inst", "ownfield", "child", "new", "cfield")
                or (k == "call" and base[1]) or (k == "objfield" and base[1])):
            self.ucalls.append([self.line(ml), self.caller(), m, self.text(i, ml - 1, 30)])

    def name_site(self, lst, val, op, tok, starts, idx, kind, recv):
        """An attribute/tag name argument: literal(s), wrapper parameter, other module's constant, or unknown."""
        line, caller = self.line(tok), self.caller()
        head = [line, caller]
        tail = [recv] if kind == "attr" else []
        names = names_in(val)
        if names:
            for nm in names:
                lst.append(head + [nm, op] + tail + [""])
            return
        expr = self.argtext(starts, idx)
        extra = None
        if val and val[0] == "param" and val[1]:
            self.sinks.append([val[1], val[2], kind, op, ""])
            extra = {"p": [val[1], val[2]]}
        elif is_xref(val) and enc(val) is not None:
            extra = {"x": enc(val)}
        lst.append(head + ["", op] + tail + [expr] + ([extra] if extra else []))

    def names_of(self, val):
        """Remote names a value can be: (names, via, extra) where extra explains a non-literal name."""
        if val is None:
            return (), "", None
        k = val[0]
        if k == "child":
            inner = val[1]
            if names_in(inner):
                return names_in(inner), "", None
            if inner and inner[0] == "param" and inner[1]:
                return (), "", {"p": [inner[1], inner[2]], "k": "role"}
            if is_xref(inner) and enc(inner) is not None:
                return (), "", {"x": enc(inner)}
            return (), "", None
        if k == "cfield":
            return (), "", {"x": enc(val)}
        if k == "call":
            target, member, args = val[1], val[2], val[3]
            via = "%s.%s" % (target, member)
            for idx, a in enumerate(args):
                s = names_in(a)
                if s:
                    return s, "%s#%d" % (via, idx), None
            for idx, a in enumerate(args):
                if a and a[0] == "param" and a[1]:
                    return (), "%s#%d" % (via, idx), {"p": [a[1], a[2]], "k": "role"}
                if is_xref(a) and enc(a) is not None:
                    return (), "%s#%d" % (via, idx), {"x": enc(a)}
            if target:
                return (), "", {"x": enc(val)}
            return (), "", None
        if k == "inst" and val[1]:
            return (val[1][-1],), "", None
        if k in ("field", "objfield") and val[1]:
            return (), "", {"x": enc(val)}
        if k == "param" and val[1]:
            return (), "", {"p": [val[1], val[2]], "k": "rolev"}
        return (), "", None

    def role_site(self, tok, val, role):
        if not self.out:
            return
        base = self.deref(val) if val and val[0] in DEREF else val
        names, via, extra = self.names_of(base)
        line, caller = self.line(tok), self.caller()
        if names:
            for nm in names:
                self.remotes.append([line, caller, nm, role, via, ""])
            return
        expr = self.text(self.chain_start, tok - 1, 40)
        if extra and "p" in extra:
            self.sinks.append([extra["p"][0], extra["p"][1], extra["k"], role, via])
            extra = {"p": extra["p"]}
        elif self.last_field and self.last_field not in ROLE_FIELDS:
            extra = dict(extra or {}, fb=self.last_field)  # Remotes.X:FireServer(): X, if nothing better
        self.remotes.append([line, caller, "", role, via, expr] + ([extra] if extra else []))

    # ------------------------------------------------------------------ docs
    def doc_above(self, i):
        """Comment block ending right above token i; one blank line in between is allowed."""
        t = self.t
        if not t.comments:
            return ""
        idx = self._comment_index()
        ln = self.line(i) - 1
        if ln not in idx and ln in self.blank and (ln - 1) in idx:
            ln -= 1
        out = []
        guard = 0
        while ln in idx and guard < 400:
            guard += 1
            c = t.comments[idx[ln]]
            out.append(c[2])
            ln = t.line(c[0]) - 1
        out.reverse()
        return "\n".join(x for x in out if not x.startswith("!"))

    def _comment_index(self):
        if not hasattr(self, "_cidx"):
            t = self.t
            self._cidx = {}
            for ci, c in enumerate(t.comments):
                if c[3]:
                    self._cidx[t.line(max(c[1] - 1, c[0]))] = ci
        return self._cidx

    def module_doc(self):
        t = self.t
        first = t.offs[0] if t.n else len(t.src)
        out = []
        for c in t.comments:
            if c[0] >= first:
                break
            if c[2].startswith("!"):
                continue
            out.append(c[2])
        return "\n".join(out).strip("\n")

    # ------------------------------------------------------------------ result
    def exported_values(self, table):
        """What other modules can learn from this one: field constants, function returns, class fields, types."""
        vals = {}

        def put(key, v):
            e = enc(known(v))
            if e is not None:
                vals[key] = e

        own = self.K.tf.get(table) if table else None
        for f, v in (own or {}).items():
            if f == "#":
                continue
            v = known(v)
            put("f:" + f, v)
            if v and v[0] in ("tab", "table"):
                d = self.K.tf.get(v[1], {})
                keys = [g for g in d if g not in ("#", "[]")]
                for g in keys:
                    put("f:%s.%s" % (f, g), d[g])
                found = []
                for x in list(d.get("#", ())) + [d[g] for g in keys]:
                    found.extend(strs_of(known(x)))
                put("e:" + f, mkstrs(found) if found else None)
                put("k:" + f, mkstrs(keys) if keys else None)
        for fq, v in self.K.ret.items():
            v = known(v)
            if v and v[0] == "table":  # Registry.init() returns the module table itself
                if v[1] == table:
                    vals["r:" + fq] = ["m", ".".join(self.inst)]
                continue
            put("r:" + fq, v)
        for (tg, cls), d in self.K.cf.items():
            if not tg:
                for f, v in d.items():
                    put("c:%s.%s" % (cls, f), v)
        for nm, (tg, cls) in self.K.types.items():
            if not tg:
                vals["t:" + nm] = ["o", "", cls]
        return vals

    def finish(self):
        table = self.returned if self.is_module else None
        if not table and self.is_module:
            roots = {}
            for d in self.defs:
                if d["kind"] in ("member", "method"):
                    r = d["name"].replace(":", ".").split(".")[0]
                    roots[r] = roots.get(r, 0) + 1
            if roots:
                table = max(roots, key=roots.get)
                self.warn.append("no top-level return; module table guessed as " + table)
        names = {_norm(d["name"]) for d in self.defs}
        localfns = {d["name"] for d in self.defs if d["kind"] in ("local", "global")}
        calls = []
        for c in self.calls:
            if c[4] in ("l", "r"):
                mem = _norm(c[3])
                if mem in names or c[3] in localfns:
                    c[3] = mem
                    calls.append(c)
            else:
                calls.append(c)
        reads = [r for r in self.reads if r[2] or (table and r[3].split(".")[0] == table)]
        for r in reads:
            if not r[2]:
                r[3] = r[3].split(".", 1)[1] if "." in r[3] else r[3]
        module_fn = None
        byname = {d["name"]: d for d in self.defs if d["kind"] in ("local", "global")}
        for d in self.defs:
            src = byname.get(d.get("alias"))
            if src:
                d.update(kind="member", params=src["params"], sig=src["sig"], doc=d["doc"] or src["doc"])
        for d in self.defs:
            nm = d["name"]
            root = nm.replace(":", ".").split(".")[0]
            d["exported"] = (bool(table) and root == table and nm != table and nm.replace(":", ".").count(".") == 1) \
                or d["kind"] == "export type"
            if table and nm == table and d["kind"] in ("local", "global") and d["depth"] == 0:
                module_fn = nm
                d["exported"] = True
        if table == "<return>":
            bylocal = {d["name"]: d for d in self.defs if d["depth"] == 0}
            for nm, line, vname, doc in self.ret_fields:
                src = bylocal.get(vname) if vname else None
                d = {"line": line, "end": None, "name": nm, "kind": "field", "params": "", "sig": "", "doc": doc,
                     "depth": 0, "parent": None, "exported": True}
                if src:
                    d.update(kind=src["kind"] if src["kind"] in ("local", "global") else "field", params=src["params"],
                             sig=src["sig"], doc=src["doc"] or doc, line=src["line"], end=src["end"], alias=vname)
                    src["exported"] = True
                self.defs.append(d)
            retnames = {f[0] for f in self.ret_fields}
            for d in self.defs:
                if d["kind"] == "field" and d["name"] in retnames:
                    d["exported"] = True
        tt = self.t
        return {
            "lines": tt.line(len(tt.src)) if tt.src else 0,
            "doc": self.module_doc(),
            "table": table,
            "module_fn": module_fn,
            "requires": [r[:4] + ([r[5]] if r[5] else []) for r in self.requires],
            "services": sorted(self.services),
            "defs": [[d["line"], d["end"], d["name"], d["kind"], d["params"], d["sig"], d["doc"], d["depth"],
                      1 if d.get("exported") else 0, d.get("parent")] for d in self.defs],
            "calls": calls,
            "reads": reads,
            "remotes": self.remotes,
            "attrs": self.attrs,
            "tags": self.tags,
            "ucalls": self.ucalls,
            "sinks": self.sinks,
            "vals": self.exported_values(table),
            "warn": self.warn + (["%d statement errors" % self.errors] if self.errors else []),
            "lexerr": tt.errors,
            "resyncs": self.resyncs,
        }


def scan(src, inst, is_module=True, ctors=CONSTRUCTORS, rel=None):
    return Scanner(src, inst, is_module, ctors, rel).run()
