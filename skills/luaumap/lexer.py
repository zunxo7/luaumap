"""Luau tokenizer. Never raises: anything it cannot read becomes a 'X' token and scanning goes on.

Token kinds: N name, K keyword, S string (value = text), I interpolated string, D number, O operator, X junk.
Comments are not tokens; they are kept in a side list for doc extraction.
Interpolated strings emit an 'I' token followed by each `{expr}` as '(' tokens... ')' so code inside
them (calls, field reads) is still seen.
"""
import re
from bisect import bisect_left

KEYWORDS = frozenset(
    "and break do else elseif end false for function if in local nil not or repeat return then true until while".split()
)

_RX = re.compile(
    r"""
 (?P<ws>[ \t\r\n\f\v]+)
|(?P<lc>--\[(?P<lq>=*)\[[\s\S]*?\](?P=lq)\])
|(?P<lcu>--\[=*\[[\s\S]*)
|(?P<c>--[^\n]*)
|(?P<n>[A-Za-z_][A-Za-z0-9_]*)
|(?P<d>0[xX][0-9a-fA-F_]*|0[bB][01_]*|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?[\d_]+)?)
|(?P<s>"(?:[^"\\\n]|\\z\s*|\\[\s\S])*"|'(?:[^'\\\n]|\\z\s*|\\[\s\S])*')
|(?P<ls>\[(?P<sq>=*)\[[\s\S]*?\](?P=sq)\])
|(?P<lsu>\[=*\[[\s\S]*)
|(?P<su>["'][^\n]*)
|(?P<o>\.\.\.|\.\.=|//=|::|->|==|~=|<=|>=|\.\.|//|\+=|-=|\*=|/=|%=|\^=|[-+*/%^\#&|~<>=(){}\[\];:,.@?!])
|(?P<bt>`)
|(?P<x>.)
""",
    re.X,
)
_ESC = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'"}
_ESC_RX = re.compile(r"\\(.)")
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")": "(", "]": "[", "}": "{"}


def _unq(s):
    body = s[1:-1] if len(s) >= 2 and s[-1] == s[0] else s[1:]
    if "\\" in body:
        body = _ESC_RX.sub(lambda m: _ESC.get(m.group(1), m.group(1)), body)
    return body


def _clean_comment(text):
    if text.startswith("--["):
        m = re.match(r"--\[(=*)\[", text)
        eq = m.group(1) if m else ""
        body = text[len(eq) + 4 :]
        close = "]" + eq + "]"
        if body.endswith(close):
            body = body[: -len(close)]
        return body.strip("\n")
    return re.sub(r"^-+ ?", "", text).rstrip()


class Tokens:
    __slots__ = ("src", "kinds", "vals", "offs", "comments", "nl", "match", "parent", "n", "errors", "interp")

    def line(self, off):
        return bisect_left(self.nl, off) + 1

    def col(self, off):
        i = bisect_left(self.nl, off)
        return off if i == 0 else off - self.nl[i - 1] - 1


def tokenize(src):
    t = Tokens()
    t.src = src
    kinds, vals, offs, comments = [], [], [], []
    t.errors = 0
    t.interp = {}  # I-token index -> ([static parts], [hole '(' indices])
    t.nl = [m.start() for m in re.finditer("\n", src)]
    state = {"last_end": 0}
    _lex(src, 0, len(src), kinds, vals, offs, comments, t, state, False)
    t.kinds, t.vals, t.offs, t.comments = kinds, vals, offs, comments
    t.n = len(kinds)
    _brackets(t)
    return t


def _lex(src, pos, end, kinds, vals, offs, comments, t, state, in_interp):
    """Tokenize src[pos:end]. In interpolation mode stop at the unmatched '}' and return its index."""
    rx = _RX.match
    depth = 0
    ka, va, oa = kinds.append, vals.append, offs.append
    kw = KEYWORDS
    while pos < end:
        m = rx(src, pos)
        g = m.lastgroup
        e = m.end()
        if g == "ws":
            pos = e
            continue
        if g == "n":
            w = m.group()
            ka("K" if w in kw else "N")
            va(w)
            oa(pos)
        elif g == "o":
            w = m.group()
            if in_interp:
                if w == "{":
                    depth += 1
                elif w == "}":
                    if depth == 0:
                        return pos
                    depth -= 1
            ka("O")
            va(w)
            oa(pos)
        elif g in ("c", "lc", "lcu"):
            text = m.group()
            nlpos = src.rfind("\n", 0, pos)
            standalone = nlpos >= state["last_end"] or not offs
            comments.append((pos, e, _clean_comment(text), standalone))
            pos = e
            continue
        elif g == "s":
            ka("S")
            va(_unq(m.group()))
            oa(pos)
        elif g == "d":
            ka("D")
            va(m.group())
            oa(pos)
        elif g == "ls":
            ka("S")
            va(m.group()[len(m.group("sq")) + 2 : -(len(m.group("sq")) + 2)])
            oa(pos)
        elif g in ("su", "lsu"):
            t.errors += 1
            ka("S")
            va(m.group()[1:])
            oa(pos)
        elif g == "bt":
            pos = _interp(src, pos, end, kinds, vals, offs, comments, t, state)
            state["last_end"] = pos
            continue
        else:
            t.errors += 1
            ka("X")
            va(m.group())
            oa(pos)
        state["last_end"] = e
        pos = e
    return pos


def _interp(src, start, end, kinds, vals, offs, comments, t, state):
    """Backtick string at `start`. Emits I token (static text with {} holes), then ( expr ) per hole."""
    idx = len(kinds)
    kinds.append("I")
    vals.append(None)
    offs.append(start)
    static = []
    parts = []  # static text between holes
    opens = []  # token index of each hole's synthetic '('
    pos = start + 1
    while pos < end:
        ch = src[pos]
        if ch == "\\":
            static.append(_ESC.get(src[pos + 1 : pos + 2], src[pos + 1 : pos + 2]))
            pos += 2
        elif ch == "`":
            pos += 1
            break
        elif ch == "\n":
            t.errors += 1
            break
        elif ch == "{":
            parts.append("".join(static))
            static = []
            opens.append(len(kinds))
            kinds.append("O")
            vals.append("(")
            offs.append(pos)
            close = _lex(src, pos + 1, end, kinds, vals, offs, comments, t, state, True)
            kinds.append("O")
            vals.append(")")
            offs.append(min(close, end - 1) if end else 0)
            pos = close + 1
        else:
            static.append(ch)
            pos += 1
    parts.append("".join(static))
    vals[idx] = parts[0] if not opens else None
    if opens:
        t.interp[idx] = (parts, opens)
    return pos


def _brackets(t):
    """match[i] = index of the partner bracket (-1 if none); parent[i] = innermost open bracket around i."""
    n = t.n
    vals, kinds = t.vals, t.kinds
    match = [-1] * n
    parent = [-1] * n
    stack = []
    for i in range(n):
        parent[i] = stack[-1] if stack else -1
        if kinds[i] != "O":
            continue
        v = vals[i]
        if v in _OPEN:
            stack.append(i)
        elif v in _CLOSE:
            want = _CLOSE[v]
            if stack and vals[stack[-1]] == want:
                o = stack.pop()
                match[o], match[i] = i, o
                parent[i] = stack[-1] if stack else -1
            else:
                # Unbalanced: close the nearest matching opener if any, abandon the ones above it.
                for k in range(len(stack) - 1, -1, -1):
                    if vals[stack[k]] == want:
                        o = stack[k]
                        del stack[k:]
                        match[o], match[i] = i, o
                        break
    t.match, t.parent = match, parent
