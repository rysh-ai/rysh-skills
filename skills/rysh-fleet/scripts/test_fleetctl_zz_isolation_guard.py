#!/usr/bin/env python3
"""Guard (E9 ORDER-7c): no fleetctl test module may leave the process dirty.

Three modules used to plant a stub `ryshfan` in sys.modules and import
`fleetctl` bound to it, and one left the global `subprocess.run` patched. The
next module to import fleetctl, or to run fleetctl as a subprocess, then tested
the stub instead of the code: 18 failures in a full run, none of them alone.

For every other test_fleetctl_*.py this imports it and runs its tests in THIS
process, then checks that the shared state is clean:
  - sys.modules["ryshfan"], if present, is the real ryshfan.py;
  - sys.modules["fleetctl"], if present, is the real fleetctl.py and its `R`
    is the real ryshfan;
  - subprocess.run is still the function the interpreter started with.

Named zz_ so unittest discovery runs it last as well, where a leak from any
earlier module is also visible.

Run: python3 test_fleetctl_zz_isolation_guard.py
"""

import glob
import importlib
import io
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
REAL_RUN = subprocess.run
REAL_RYSHFAN = os.path.realpath(os.path.join(HERE, "..", "..", "rysh-fanout", "scripts", "ryshfan.py"))
REAL_FLEETCTL = os.path.realpath(os.path.join(HERE, "fleetctl.py"))


def _file(mod) -> str:
    return os.path.realpath(getattr(mod, "__file__", "") or "")


def dirty() -> list[str]:
    bad = []
    r = sys.modules.get("ryshfan")
    if r is not None and _file(r) != REAL_RYSHFAN:
        bad.append(f"sys.modules['ryshfan'] is a stub ({r!r})")
    f = sys.modules.get("fleetctl")
    if f is not None:
        if _file(f) != REAL_FLEETCTL:
            bad.append(f"sys.modules['fleetctl'] is not fleetctl.py ({f!r})")
        elif _file(getattr(f, "R", None)) != REAL_RYSHFAN:
            bad.append(f"sys.modules['fleetctl'].R is a stub ({getattr(f, 'R', None)!r})")
    if subprocess.run is not REAL_RUN:
        bad.append(f"subprocess.run is still patched ({subprocess.run!r})")
    return bad


class EveryTestModuleCleansUpAfterItself(unittest.TestCase):

    def test_no_module_leaves_a_stub_or_a_patch_behind(self):
        me = os.path.splitext(os.path.basename(__file__))[0]
        names = sorted(os.path.splitext(os.path.basename(p))[0]
                       for p in glob.glob(os.path.join(HERE, "test_fleetctl_*.py")))
        offenders = {}
        for name in names:
            if name == me:
                continue
            mod = importlib.import_module(name)
            suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
            unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
            bad = dirty()
            if bad:
                offenders[name] = bad
                # Put the process back so one offender is not blamed on the next.
                subprocess.run = REAL_RUN
                r = sys.modules.get("ryshfan")
                if r is not None and _file(r) != REAL_RYSHFAN:
                    del sys.modules["ryshfan"]
                f = sys.modules.get("fleetctl")
                if f is not None and (_file(f) != REAL_FLEETCTL
                                      or _file(getattr(f, "R", None)) != REAL_RYSHFAN):
                    del sys.modules["fleetctl"]
        self.assertEqual(offenders, {}, "test modules left shared state dirty:\n" +
                         "\n".join(f"  {k}: {v}" for k, v in offenders.items()))


if __name__ == "__main__":
    unittest.main()
