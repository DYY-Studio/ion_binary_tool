#!/usr/bin/env python3

import os
import sys
import types


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KFXLIB_DIR = os.path.join(BASE_DIR, "kfxlib")

if "kfxlib" not in sys.modules:
    kfxlib = types.ModuleType("kfxlib")
    kfxlib.__file__ = os.path.join(KFXLIB_DIR, "__init__.py")
    kfxlib.__path__ = [KFXLIB_DIR]
    sys.modules["kfxlib"] = kfxlib

from kfxlib.ion_hard_reader import main


if __name__ == "__main__":
    main()
