"""Project discovery: config, Rojo tree -> {file: instance path}, source files to scan."""
import fnmatch
import json
import os

SCRIPT_EXT = (".luau", ".lua")
SKIP_DIRS = {"Packages", "DevPackages", "ServerPackages", "_Index", "node_modules", ".git", ".luaumap"}
DEFAULT_CONFIG = {
    "project": None,  # Rojo project file; default: default.project.json or the only *.project.json
    "roots": [],  # extra dirs to scan that Rojo does not map (named by folder path)
    "exclude": [],  # fnmatch globs on repo-relative paths, e.g. "src/client/Stories/*"
    "include_packages": False,  # scan Wally packages too
    "remote_getters": [],  # e.g. ["Net.remote"]: Module.fn("Name") returns the remote "Name" (also auto-detected)
    "constructors": None,  # names that build an object of their class; default new/New/create/Create
    "map_exclude": None,  # globs left out of `map`; default stories/storybooks/Stories folders
    "index": ".luaumap/index.json",
}
DEAD_DIRS = {"_Index", "node_modules", ".git", ".luaumap"}
SERVER_ROOTS = {"ServerScriptService", "ServerStorage"}
CLIENT_ROOTS = {"StarterPlayer", "StarterGui", "StarterPack", "ReplicatedFirst"}


def find_root(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, ".luaumap.json")) or os.path.isfile(os.path.join(d, "default.project.json")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return os.path.abspath(start)
        d = up


def load_config(root):
    cfg = dict(DEFAULT_CONFIG)
    p = os.path.join(root, ".luaumap.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    return cfg


def rel(root, p):
    return os.path.relpath(p, root).replace("\\", "/")


def script_name(fname):
    """('Foo', 'ModuleScript') for Foo.luau, ('Foo','Script') for Foo.server.luau; None if not a script."""
    for ext in SCRIPT_EXT:
        if fname.endswith(ext):
            stem = fname[: -len(ext)]
            if stem.endswith(".server"):
                return stem[:-7], "Script"
            if stem.endswith(".client"):
                return stem[:-7], "LocalScript"
            return stem, "ModuleScript"
    return None


def other_name(fname):
    """Rojo maps these to instances too (data modules, models): requires into them are not 'unresolved'."""
    for ext in (".model.json", ".project.json", ".meta.json"):
        if fname.endswith(ext):
            return None if ext == ".meta.json" else fname[: -len(ext)]
    for ext in (".json", ".rbxm", ".rbxmx", ".txt", ".csv", ".toml", ".jsonc", ".yaml", ".yml"):
        if fname.endswith(ext):
            return fname[: -len(ext)]
    return None


class Mapping:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.scripts = {}  # relpath -> (inst tuple, class, scan?)
        self.others = {}  # inst string -> relpath (non-script or excluded script)
        self.dirs = []  # (abs dir, inst tuple) roots walked, for staleness checks
        self.project_file = None
        self.excl = [e.replace("\\", "/") for e in cfg.get("exclude") or []]

    # ---- exclusion
    def skipped_dir(self, name):
        return name in SKIP_DIRS and not (self.cfg.get("include_packages") and name not in DEAD_DIRS)

    def excluded(self, relpath):
        return any(fnmatch.fnmatch(relpath, g) or relpath.startswith(g.rstrip("*/") + "/") for g in self.excl if g)

    # ---- building
    def build(self):
        proj = self.cfg.get("project")
        if not proj:
            if os.path.isfile(os.path.join(self.root, "default.project.json")):
                proj = "default.project.json"
            else:
                cands = [f for f in os.listdir(self.root) if f.endswith(".project.json")]
                proj = cands[0] if len(cands) == 1 else None
        if proj and os.path.isfile(os.path.join(self.root, proj)):
            self.project_file = proj
            self._project(os.path.join(self.root, proj), ())
        else:
            src = os.path.join(self.root, "src")
            base = src if os.path.isdir(src) else self.root
            self._dir(base, (os.path.basename(base),), packaged=False)
        for r in self.cfg.get("roots") or []:
            d = os.path.join(self.root, r)
            if os.path.isdir(d):
                self._dir(d, tuple(p for p in r.replace("\\", "/").split("/") if p), packaged=False)
        return self

    def _project(self, path, inst):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for g in data.get("globIgnorePaths") or []:
            self.excl.append(rel(self.root, os.path.join(os.path.dirname(path), g)))
        self._node(data.get("tree") or {}, inst, os.path.dirname(path), top=True)

    def _node(self, node, inst, base, top=False):
        if not isinstance(node, dict):
            return
        p = node.get("$path")
        if isinstance(p, dict):
            p = p.get("optional")
        if isinstance(p, str):
            full = os.path.normpath(os.path.join(base, p))
            if full.endswith(".project.json"):
                self._project(full, inst)
            elif os.path.isdir(full):
                self._dir(full, inst, packaged=self.skipped_dir(os.path.basename(full)))
            elif os.path.isfile(full):
                self._file(full, inst)
        for k, child in node.items():
            if not k.startswith("$"):
                self._node(child, inst + (k,), base)

    def _dir(self, d, inst, packaged):
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        if not packaged:
            self.dirs.append((d, inst))
        if any(e.name == "default.project.json" for e in entries) and inst:
            self._project(os.path.join(d, "default.project.json"), inst)
            return
        init = None
        for e in entries:
            if e.is_file():
                sn = script_name(e.name)
                if sn and sn[0] == "init":
                    init = (e.path, sn[1])
        if init:
            self._add_script(init[0], inst, init[1], packaged)
        for e in entries:
            if e.is_dir():
                if packaged or e.name in DEAD_DIRS and not self.cfg.get("include_packages"):
                    continue  # package folders: top-level thunks only, never their insides
                self._dir(e.path, inst + (e.name,), self.skipped_dir(e.name))
            elif e.is_file():
                sn = script_name(e.name)
                if sn:
                    if sn[0] != "init":
                        self._add_script(e.path, inst + (sn[0],), sn[1], packaged)
                else:
                    on = other_name(e.name)
                    if on:
                        self.others.setdefault(".".join(inst + (on,)), rel(self.root, e.path))

    def _file(self, f, inst):
        sn = script_name(os.path.basename(f))
        if sn:
            self._add_script(f, inst, sn[1], False)
        else:
            self.others.setdefault(".".join(inst), rel(self.root, f))

    def _add_script(self, f, inst, cls, packaged):
        r = rel(self.root, f)
        scan = not packaged and not self.excluded(r)
        if scan:
            self.scripts[r] = (inst, cls)
        else:
            self.others.setdefault(".".join(inst), r)


def side_of(inst, cls):
    if cls == "Script":
        return "server"
    if cls == "LocalScript":
        return "client"
    top = inst[0] if inst else ""
    if top in SERVER_ROOTS:
        return "server"
    if top in CLIENT_ROOTS:
        return "client"
    return "shared"
