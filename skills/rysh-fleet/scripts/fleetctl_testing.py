"""Test-only helper (E9 ORDER-7c): load fleetctl against a STUB ryshfan without
leaking either module into the rest of the test process.

fleetctl does `import ryshfan as R` at module scope, so a test that wants a
stub has to put it in sys.modules["ryshfan"] before fleetctl is imported. The
old way left both behind: sys.modules["ryshfan"] stayed the stub, and the
cached sys.modules["fleetctl"] stayed bound to it. Every later module that
imported fleetctl then tested the stub — 18 failures in a full run, none alone.

This loads fleetctl under a PRIVATE module name (never "fleetctl"), with the
stub visible in sys.modules only for the duration of that one import, and then
puts sys.modules["ryshfan"], sys.modules["fleetctl"] and $RYSHFAN_DIR back
exactly as they were. The returned module keeps its own `R` = the stub, so the
calling test file behaves as before; nothing else in the process can see it.

Not named test_*.py, so unittest discovery does not load it as tests.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FLEETCTL = os.path.join(HERE, "fleetctl.py")
_MISSING = object()


def load_fleetctl_with_stub(stub, private_name: str):
    """Import fleetctl.py as `private_name`, bound to `stub` as its ryshfan."""
    if private_name in ("fleetctl", "ryshfan"):
        raise ValueError("the private name must not shadow a shared module")
    saved = {k: sys.modules.get(k, _MISSING) for k in ("ryshfan", "fleetctl", private_name)}
    saved_env = os.environ.get("RYSHFAN_DIR", _MISSING)
    os.environ.setdefault("RYSHFAN_DIR", HERE)   # what the old code set, for the import only
    sys.modules["ryshfan"] = stub
    try:
        spec = importlib.util.spec_from_file_location(private_name, FLEETCTL)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[private_name] = mod          # dataclasses & co. look themselves up here
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if k == private_name:
                continue
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if saved_env is _MISSING:
            os.environ.pop("RYSHFAN_DIR", None)
        else:
            os.environ["RYSHFAN_DIR"] = saved_env
    return mod
