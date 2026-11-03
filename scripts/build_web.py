"""Builds the frontend. Same as `metro web [--demo]`; kept for running from a checkout without installing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from metro.webbuild import ROOT, main  # noqa: E402

for p in main("--demo" in sys.argv):
    print(f"built {p.relative_to(ROOT)} ({p.stat().st_size // 1024} KB)")
