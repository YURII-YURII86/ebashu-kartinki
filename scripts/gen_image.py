#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate images with gpt-image-2 through the Codex bridge (ChatGPT login, no API key).

One image:
  python3 scripts/gen_image.py --prompt "..." --aspect 16:9 --out outputs/hero.png
  python3 scripts/gen_image.py --prompt-file prompt.txt --ref face.jpg --out outputs/portrait.png

A batch, in parallel:
  python3 scripts/gen_image.py --batch jobs.json --jobs 3
  jobs.json = [{"prompt_file": "a.txt", "brief_file": "a.brief.md", "out": "outputs/a.png", "aspect": "1:1"}, ...]

Each image is saved with <name>.prompt.txt (and <name>.brief.md when a brief is given).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_backend import ASPECT_SIZES, run_codex_image  # noqa: E402


def base_ref(path) -> list:
    """The photo to edit goes first, marked ".base." for the bridge."""
    if not path:
        return []
    src = Path(path).expanduser()
    if ".base." in src.name:
        return [str(src)]
    tmp = Path(tempfile.mkdtemp()) / f"{src.stem}.base{src.suffix}"
    shutil.copy(src, tmp)
    return [str(tmp)]


def face_refs(items: list) -> list:
    """Identity photos: prepared *.identity.jpg files from folders, or single photos marked via a copy."""
    out = []
    for item in items:
        p = Path(item).expanduser()
        if p.is_dir():
            found = sorted(p.glob("*.identity.jpg")) or sorted(
                f for f in p.iterdir() if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
            out += [str(f) for f in found]
        else:
            out.append(str(p))
    tagged = []
    for f in out:
        if ".identity." in Path(f).name:
            tagged.append(f)
        else:  # mark plain photos so the bridge treats them as identity references
            tmp = Path(tempfile.mkdtemp()) / f"{Path(f).stem}.identity{Path(f).suffix}"
            shutil.copy(f, tmp)
            tagged.append(str(tmp))
    return tagged


def generate(job: dict) -> Path:
    prompt = job["prompt"]
    out = Path(job["out"])
    started = time.time()
    saved = run_codex_image(prompt, out, job.get("aspect", "1:1"), job.get("refs", []))
    saved.with_suffix(".prompt.txt").write_text(prompt.strip() + "\n", encoding="utf-8")
    if job.get("brief"):
        saved.with_suffix(".brief.md").write_text(job["brief"].strip() + "\n", encoding="utf-8")
    print(f"✅ {saved}  ({saved.stat().st_size // 1024} KB, {time.time() - started:.0f}s)", flush=True)
    return saved


def load_batch(path: str) -> list:
    base = Path(path).resolve().parent
    jobs = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(jobs, list) or not jobs:
        sys.exit("Batch file must be a non-empty JSON list of jobs.")
    for i, job in enumerate(jobs, start=1):
        if "prompt_file" in job:
            job["prompt"] = (base / job["prompt_file"]).read_text(encoding="utf-8")
        if "brief_file" in job:
            job["brief"] = (base / job["brief_file"]).read_text(encoding="utf-8")
        if not job.get("prompt") or not job.get("out"):
            sys.exit(f"Job {i}: needs 'prompt' (or 'prompt_file') and 'out'.")
        if job.get("aspect", "1:1") not in ASPECT_SIZES:
            sys.exit(f"Job {i}: unknown aspect {job['aspect']!r}.")
        job["out"] = str(base / job["out"])
        job["refs"] = base_ref(str(base / job["base"]) if job.get("base") else None) + face_refs([str(base / f) for f in job.get("faces", [])]) + [str(base / r) for r in job.get("refs", [])]
    return jobs


def main() -> None:
    p = argparse.ArgumentParser(description="gpt-image-2 via the Codex bridge")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", help="prompt text")
    src.add_argument("--prompt-file", help="file with the prompt")
    src.add_argument("--batch", help="JSON list of jobs")
    p.add_argument("--out", help="output PNG (single mode)")
    p.add_argument("--aspect", default="1:1", choices=list(ASPECT_SIZES))
    p.add_argument("--ref", action="append", default=[], help="reference image (style, product…), repeatable")
    p.add_argument("--base", help="photo to edit: keep the person exactly, change only what the prompt says")
    p.add_argument("--face", action="append", default=[], help="photo or folder of the person to depict (identity), repeatable")
    p.add_argument("--brief-file", help="director's brief, saved next to the image as <name>.brief.md")
    p.add_argument("--jobs", type=int, default=3, help="parallel images in batch mode (default 3)")
    a = p.parse_args()

    if a.batch:
        jobs = load_batch(a.batch)
        print(f"→ {len(jobs)} images, {min(a.jobs, len(jobs))} at a time", flush=True)
        failed = 0
        with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
            futures = {pool.submit(generate, job): job for job in jobs}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as exc:  # report every failure, keep the rest going
                    failed += 1
                    print(f"❌ {futures[fut]['out']}: {exc}", file=sys.stderr, flush=True)
        print(f"Done: {len(jobs) - failed} ok, {failed} failed")
        sys.exit(1 if failed else 0)

    if not a.out:
        p.error("--out is required")
    prompt = a.prompt or Path(a.prompt_file).read_text(encoding="utf-8")
    print("→ generating via Codex…", flush=True)
    try:
        brief = Path(a.brief_file).read_text(encoding="utf-8") if a.brief_file else ""
        generate({"prompt": prompt, "out": a.out, "aspect": a.aspect, "refs": base_ref(a.base) + face_refs(a.face) + a.ref, "brief": brief})
    except Exception as exc:
        sys.exit(f"❌ {exc}")


if __name__ == "__main__":
    main()
