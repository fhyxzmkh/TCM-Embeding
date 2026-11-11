"""Run an already available SCons package with a local M55 GCC; install nothing."""

import argparse
import collections
import collections.abc
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--gcc-bin", type=Path, required=True)
    parser.add_argument("--scons-package", type=Path, required=True, help="Directory containing the SCons module")
    args = parser.parse_args()
    project, gcc, scons = (path.resolve() for path in (args.project, args.gcc_bin, args.scons_package))
    staged = ("applications/tcm_static_probe/probe_assets.h", "applications/home_font.c")
    if not any((project / marker).is_file() for marker in staged):
        parser.error("Use a separately staged TCM_Static_Probe or TCM_Home project.")
    if not (gcc / "arm-none-eabi-gcc.exe").is_file() or not (scons / "SCons/__init__.py").is_file():
        parser.error("The supplied GCC or SCons path does not contain the expected tool.")

    # Studio's bundled SCons 3.1.2 predates the move to collections.abc.
    for name in ("Mapping", "MutableMapping", "Sequence", "MutableSequence", "Iterable", "Callable"):
        if not hasattr(collections, name):
            setattr(collections, name, getattr(collections.abc, name))
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(scons))
    import SCons.Script

    os.environ["RTT_EXEC_PATH"] = str(gcc)
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    os.chdir(project)
    sys.argv = ["scons", "-j4"]
    SCons.Script.main()


if __name__ == "__main__":
    main()
