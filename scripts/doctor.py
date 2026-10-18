#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check that the Codex bridge is ready: Python deps, Codex CLI, ChatGPT login.

  python3 scripts/doctor.py           # quick checks, nothing is generated
  python3 scripts/doctor.py --render  # also generate one small test image
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_backend import codex_command, run_codex_image  # noqa: E402

DEPS = {"pptx": "python-pptx", "PIL": "pillow", "fitz": "pymupdf", "jsonschema": "jsonschema",
        "requests": "requests", "dotenv": "python-dotenv"}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--render", action="store_true", help="generate one test image")
    a = p.parse_args()
    ok = True

    venv_py = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    # venv python is a symlink to the system one, so compare prefixes, not resolved binaries
    if venv_py.exists() and Path(sys.prefix).resolve() != venv_py.parent.parent.resolve():
        # slides run with the skill's own .venv, so check packages there
        probe = "import importlib.util as u; print(' '.join(p for m, p in %r.items() if u.find_spec(m) is None))" % DEPS
        out = subprocess.run([str(venv_py), "-c", probe], capture_output=True, text=True).stdout.split()
        missing = out
    else:
        missing = [pkg for mod, pkg in DEPS.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"⚠️  Slides need: {' '.join(missing)}  →  pip install -r requirements.txt"
              "\n   (single images work without them)")
    else:
        print("✅ Python packages for slides")

    try:
        exe = codex_command()[0]
        version = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.strip()
        print(f"✅ Codex: {version}  ({exe})")
        status = subprocess.run([exe, "login", "status"], capture_output=True, text=True)
        text = (status.stdout + status.stderr).strip()
        if status.returncode == 0 and "logged in" in text.lower():
            print(f"✅ {text.splitlines()[0]}")
        else:
            ok = False
            print(f"❌ Not signed in: run `{exe} login` and sign in with ChatGPT")
    except Exception as exc:
        ok = False
        print(f"❌ {exc}")

    if ok and a.render:
        out = Path(tempfile.mkdtemp()) / "doctor.png"
        print("→ test render…", flush=True)
        try:
            run_codex_image("A single ripe lemon on a plain white background, soft studio light.", out)
            print(f"✅ Test image: {out}")
        except Exception as exc:
            ok = False
            print(f"❌ Test render failed: {exc}")

    print("\nReady 🚀" if ok else "\nFix the items above and run again.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
