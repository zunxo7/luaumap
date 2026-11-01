"""End-to-end tests on a tiny Rojo project (tests/fixture). Run: python -m unittest discover -s <luaumap>/tests"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)

import scanner  # noqa: E402


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="luaumap-")
        cls.root = os.path.join(cls.tmp, "fixture")
        shutil.copytree(os.path.join(HERE, "fixture"), cls.root)
        cls.run_cli("build")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def run_cli(cls, *args):
        out = subprocess.run([sys.executable, TOOL, *args, "--root", cls.root], capture_output=True, text=True,
                             encoding="utf-8")
        assert out.returncode == 0, out.stderr
        return out.stdout

    def has(self, cmd, *needles):
        text = self.run_cli(*cmd.split())
        for n in needles:
            self.assertIn(n, text, "%r missing from `%s`:\n%s" % (n, cmd, text))
        return text

    # 1. names: constants, wrappers, loops, concatenation, if-expressions, other modules' constants
    def test_constants_and_aliases(self):
        self.has("remote Ping", "FireServer")
        self.has("remote Alias", "FireServer")
        self.has("remote Fire2", "FireServer")
        self.has("attr Ka", "set")

    def test_wrappers(self):
        self.has("remote Gift", "FireServer~")
        self.has("attr Flag", "set~")
        self.has("remote Sell", "FireServer")

    def test_loops_concat_ifexpr(self):
        text = self.run_cli("attr")
        for name in ("L1", "L2", "M1", "M2", "X1", "X2", "RiftLevel", "ShopOpen", "IfA", "IfB", "Slot1", "Slot2"):
            self.assertIn(name + ":", text)

    def test_cross_module_names(self):
        self.has("attr InRift", "get")
        self.has("attr RiftFloor", "get")
        self.has("attr SourceLegs", "set")
        self.has("tag Rift", "add")

    # 2. values across functions / modules
    def test_stored_and_returned_remotes(self):
        self.has("remote Buy", "client/Main.client.luau [C]: FireServer", "server/Boot.server.luau [S]: OnServerEvent")
        self.has("remote Kb", "FireServer")
        self.has("remote Ret", "FireServer")
        self.has("remote Shoot", "shared/Gun.luau", "FireServer:19,24")

    # 3/6. objects: constructor, annotation, late module-level assignment, self.field
    def test_object_methods(self):
        self.has("callers Gun.fire", "top:63", "typed:67", "use:72")
        self.has("callers Gun.reload", "top:64")
        self.has("callers Sight.aim", "shared/Gun.luau")

    # 4. ambiguous module names
    def test_ambiguity(self):
        self.has("summary Util", "picked shared/Util.luau (also: server/Util.luau)")
        self.has("summary Util --from server/Boot.server.luau", "picked server/Util.luau")

    # 7. doc comment one blank line above
    def test_doc_gap(self):
        self.has("doc Gun.new", "Builds a gun.")

    # 8. stories out of the map by default
    def test_map_excludes_stories(self):
        self.assertNotIn("Stories", self.run_cli("map"))
        self.assertIn("Stories", self.run_cli("map", "--all"))

    def test_doctor_lists_only_dynamic(self):
        self.has("doctor", "unresolved names: remotes 0, attrs 1, tags 0", "set(dyn)")

    # remote registry: Registry.init() fills Registry.X = event(folder, "X") and returns the table
    def test_registry(self):
        self.has("remote Score", "shared/Registry.luau [sh]: get:", "server/Scores.server.luau [S]: FireAllClients:2 OnServerEvent:3")

    def test_inferred_field_name(self):
        self.has("remote Ghost", "FireAllClients?:6")
        self.has("doctor", "inferred from a field name 1")

    # partly known names stay as a glob pattern
    def test_partial_names(self):
        self.has("attr Slot1Tier", "set Slot*Tier:10")
        self.has("attr Slot*", "Slot*Tier")
        self.has("doctor", "partial (Name*Pattern) 1")

    # if/else requires: Rojo path in one branch, filesystem string require in the other
    def test_two_path_requires(self):
        self.has("requires server/Dual", "Util", "Net")
        text = self.has("doctor", "unresolved requires: 1", "script.Parent.Missing")
        self.assertNotIn("../lib/Net", text)
        self.assertNotIn("../shared/Util", text)


class Robustness(unittest.TestCase):
    SRC = """--!strict
--[==[ block ]] comment end function ]==]
local s = `a {b.c("}")} {`nested {x}`}` -- if then
type T<X> = { f: (number) -> X?, g: { [string]: number } | nil }
@native
function M.f<T>(x: T, ...: number): (T, number)
    local y = if x then 1 elseif x then 2 else 3
    y += 1
    for i = 1, 3 do if i == 2 then continue end end
    local z = { a = if y > 1 then function() return 1 end else nil }
    local broken = "unterminated
    local junk = $$$ ?? !!
    return x, y
end
function M.after() return `{M.f(1)}` end
return M
"""

    def test_never_gives_up(self):
        r = scanner.scan(self.SRC, ("X", "M"))
        names = [d[2] for d in r["defs"]]
        self.assertIn("M.f", names)
        self.assertIn("M.after", names)
        self.assertEqual(r["resyncs"], 0)

    def test_fuzz(self):
        import random
        rnd = random.Random(7)
        for _ in range(200):
            s = list(self.SRC)
            for _ in range(4):
                p = rnd.randrange(len(s))
                if rnd.random() < 0.5:
                    del s[p:p + rnd.randint(1, 20)]
                else:
                    s.insert(p, rnd.choice(['"', "{", " end ", "`", "(", "]]"]))
            scanner.scan("".join(s), ("X", "M"))  # must not raise


if __name__ == "__main__":
    unittest.main()
