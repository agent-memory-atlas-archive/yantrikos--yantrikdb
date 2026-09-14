"""Thin shim: the Memory Atlas exporter now lives in the installed package at
``yantrikdb/atlas/export_atlas.py`` (one canonical copy). This file keeps the
documented ``python examples/memory_atlas/export_atlas.py ...`` invocation
working from a repository checkout without importing the ``yantrikdb``
package (whose import loads the native engine). Prefer ``yantrikdb atlas``.
"""
import runpy
import sys
from pathlib import Path

_PACKAGED = Path(__file__).resolve().parents[2] / "src" / "yantrikdb" / "atlas" / "export_atlas.py"

if __name__ == "__main__":
    if not _PACKAGED.is_file():
        sys.exit(f"packaged exporter not found at {_PACKAGED}; run from a repository checkout "
                 "or use `yantrikdb atlas` from the installed package")
    sys.argv[0] = str(_PACKAGED)
    runpy.run_path(str(_PACKAGED), run_name="__main__")
