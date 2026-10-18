#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex bridge: generate images with gpt-image-2 through the user's ChatGPT login.

Claude has no image generator of its own, and we do not use the OpenAI API.
Instead we start `codex exec` (the Codex CLI that ships inside ChatGPT.app or is
installed separately) and ask that Codex session to call its built-in
image_generation tool and save the PNG to an exact path.

    Claude -> python script -> codex exec -> image_generation (gpt-image-2) -> PNG on disk

`run_codex_image()` is the single entry point used by gen_image.py (single
images) and by `CodexImageBackend` (slides in generate_ppt.py).

Env (all optional):
  CODEX_CMD           full codex invocation, e.g. "/path/to/codex exec -s workspace-write"
  CODEX_IMAGE_MODEL   image model Codex is asked to use (default: gpt-image-2)
  CODEX_TIMEOUT_SECS  per-image timeout in seconds (default: 900)
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from shutil import which
from typing import Any, Dict, List, Optional, Sequence, Union

CODEX_APP_PATHS = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    os.path.expanduser("~/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
)
CODEX_EXEC_ARGS = "exec -s workspace-write --skip-git-repo-check"
DEFAULT_TIMEOUT_SECS = 900
MIN_IMAGE_BYTES = 10 * 1024

# gpt-image-2 renders 1024x1024, 1536x1024 and 1024x1536; other ratios are
# requested with the nearest size and described in words.
ASPECT_SIZES = {
    "1:1": ("1024x1024", "square"),
    "16:9": ("1536x864", "landscape, wide 16:9"),
    "3:2": ("1536x1024", "landscape"),
    "4:3": ("1536x1024", "landscape 4:3 composition"),
    "9:16": ("864x1536", "portrait, tall 9:16"),
    "2:3": ("1024x1536", "portrait"),
    "3:4": ("1024x1536", "portrait 3:4 composition"),
}


def png_size(path: Path) -> Optional[tuple]:
    """Width/height from a PNG header (stdlib only); None for other formats."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def orientation_ok(path: Path, aspect: str) -> bool:
    """True when the saved image has the requested orientation (square / landscape / portrait)."""
    size = png_size(path)
    if not size or not size[1]:
        return True
    want_w, want_h = (int(v) for v in ASPECT_SIZES.get(aspect, ASPECT_SIZES["1:1"])[0].split("x"))
    ratio, want = size[0] / size[1], want_w / want_h
    return abs(ratio - want) / want < 0.2


IDENTITY_RULES = (
    "IDENTITY: reference images marked 'identity' show a real person; whoever appears in the result must be "
    "recognizable as them. Carry over who they are — face structure, eyes, brows, nose, mouth, hairline and hair, "
    "facial hair, skin tone, distinctive marks — not the photo itself: pose, expression, outfit, setting, framing "
    "and art style come from the prompt. Anything the prompt changes wins (costume, cartoon or any style, "
    "older/younger, heavier/thinner, new hairstyle, wild action); apply it while keeping the person recognizable. "
    "Where the prompt says nothing, stay true to the references instead of drifting to a generic face, "
    "thicker hair or an idealized body."
)


BASE_RULES = (
    "BASE PHOTO EDIT: the image marked 'base' is the original photo to edit. Change exactly what the prompt asks "
    "(background, outfit, light, pose, body, age, style — anything) and keep everything the prompt does not mention "
    "as in the photo, especially the person's face and identity. Blend edits naturally."
)


def is_base_ref(path: str) -> bool:
    return ".base." in os.path.basename(path)


def is_identity_ref(path: str) -> bool:
    return ".identity." in os.path.basename(path) or os.path.basename(os.path.dirname(path)).endswith(".identity")


def find_codex() -> Optional[str]:
    """Return the codex executable: PATH first, then the copy bundled with ChatGPT.app."""
    on_path = which("codex")
    if on_path:
        return on_path
    for candidate in CODEX_APP_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def codex_command() -> List[str]:
    """Codex argv prefix, honoring CODEX_CMD."""
    custom = os.getenv("CODEX_CMD")
    if custom:
        return shlex.split(custom)
    exe = find_codex()
    if not exe:
        raise RuntimeError(
            "Codex не найден. Установите приложение ChatGPT для компьютера (Codex внутри) "
            "или Codex CLI (npm i -g @openai/codex), войдите через `codex login` либо укажите путь в CODEX_CMD."
        )
    return [exe] + shlex.split(CODEX_EXEC_ARGS)


def build_instruction(
    prompt: str,
    out_path: Path,
    aspect: str = "1:1",
    references: Sequence[str] = (),
    reference_notes: Sequence[str] = (),
    model: str = "gpt-image-2",
) -> str:
    size, orientation = ASPECT_SIZES.get(aspect, ASPECT_SIZES["1:1"])
    lines = [
        "Generate ONE image with your built-in image generation tool and save it to disk.",
        "",
        "Rules:",
        f"- Use the built-in image_generation tool with model {model}. Do NOT write code that calls any API.",
        f"- Size: {size} ({orientation}). Quality: high.",
        f"- Save the final PNG to exactly this absolute path: {out_path}",
        "- The artwork must fill the whole canvas: never pad, letterbox or add white/blank borders.",
        "- Text: render only the words the prompt explicitly asks for, spelled exactly, in the requested language. "
        "If the prompt does not ask for any text, the image must contain no letters at all — no captions, signs, labels, "
        "screen text, packaging text, watermarks or signatures. Brand names and logos appear only when the prompt names them.",
        "- Use the prompt below exactly as written. Do not rewrite, translate or comment on it.",
        "- After saving, check the file exists and is larger than 10 KB; if not, exit with an error.",
    ]
    for idx, ref in enumerate(references, start=1):
        if is_base_ref(ref):
            note = "base: the original photo to edit"
        elif is_identity_ref(ref):
            note = "identity: the person to depict"
        else:
            note = reference_notes[idx - 1] if idx - 1 < len(reference_notes) else "visual reference"
        lines.append(f"- Reference image {idx} (attached; {note}): {ref}")
    plain = [r for r in references if not is_base_ref(r) and not is_identity_ref(r)]
    if plain:
        lines.append(
            "- REFERENCES: take from each reference only what its role says (product, style, composition, palette, "
            "lighting, typography, pose, location…) and follow the prompt for everything else. Do not copy a "
            "reference's text, people, logos or objects unless its role asks for them. A style reference gives the visual "
            "language (palette, texture, medium, type of shapes), never its exact layout or drawing: compose new forms "
            "unless a composition role is given. A product reference must "
            "keep the exact shape, proportions, materials, colors and label of the product."
        )
    if any(is_base_ref(r) for r in references):
        lines.append("- " + BASE_RULES)
    elif any(is_identity_ref(r) for r in references):
        lines.append("- " + IDENTITY_RULES)
    lines += ["", "-----BEGIN PROMPT-----", prompt.strip(), "-----END PROMPT-----", ""]
    return "\n".join(lines)


def _run_codex(instruction: str, out: Path, refs: Sequence[str], timeout: int) -> None:
    argv = codex_command() + ["-C", str(out.parent), "--add-dir", str(out.parent)]
    for ref in refs:
        argv += ["-i", ref]
    # `-i` takes several values, so the instruction goes after an explicit separator.
    argv += ["--", instruction]
    if out.exists():
        out.unlink()
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,  # codex reads piped stdin as extra input and would hang
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Codex не уложился в {exc.timeout} с: {out.name}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-800:]
        raise RuntimeError(f"Codex завершился с ошибкой (код {result.returncode}):\n{tail}")
    if not out.exists():
        tail = (result.stdout or "").strip()[-400:]
        raise RuntimeError(f"Codex закончил, но не сохранил {out}\n{tail}")
    if out.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(f"Сохранённый файл слишком маленький ({out.stat().st_size} байт): {out}")


def run_codex_image(
    prompt: str,
    out_path: Union[str, Path],
    aspect: str = "1:1",
    references: Sequence[str] = (),
    reference_notes: Sequence[str] = (),
    timeout: Optional[int] = None,
) -> Path:
    """Generate one image through the Codex bridge. Returns the saved path or raises."""
    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    refs = [str(Path(r).expanduser().resolve()) for r in references]
    for ref in refs:
        if not os.path.isfile(ref):
            raise FileNotFoundError(f"Референс не найден: {ref}")

    model = os.getenv("CODEX_IMAGE_MODEL", "gpt-image-2")
    limit = timeout or int(os.getenv("CODEX_TIMEOUT_SECS", str(DEFAULT_TIMEOUT_SECS)))
    for attempt in (1, 2):
        instruction = build_instruction(prompt, out, aspect, refs, reference_notes, model=model)
        if attempt == 2:
            size = ASPECT_SIZES.get(aspect, ASPECT_SIZES["1:1"])[0]
            instruction += (
                f"\nIMPORTANT: the previous attempt had the wrong shape. The image MUST be exactly {size} "
                "pixels; pass this size explicitly to the image tool.\n"
            )
        _run_codex(instruction, out, refs, limit)
        if orientation_ok(out, aspect):
            break
        if attempt == 2:
            got = png_size(out)
            print(f"⚠️  {out.name}: просили {aspect}, получилось {got[0]}x{got[1]}; оставляю как есть", flush=True)
    return out


class CodexImageBackend:
    """Slide backend for generate_ppt.py (same interface as GptImage2Generator)."""

    def __init__(self, aspect_ratio: str = "16:9") -> None:
        self.aspect_ratio = aspect_ratio if aspect_ratio in ASPECT_SIZES else "16:9"
        codex_command()  # fail early with a clear message if Codex is missing
        print(f"🎨 Мост к Codex готов (GPT Image 2, {self.aspect_ratio})")

    @staticmethod
    def _normalize_refs(reference_image_path: Optional[Union[str, List[str]]]) -> List[str]:
        if not reference_image_path:
            return []
        if isinstance(reference_image_path, (list, tuple)):
            return [str(p) for p in reference_image_path if p]
        return [str(reference_image_path)]

    def generate_scene_image(
        self,
        scene_data: Dict[str, Any],
        output_path: str,
        size: str = "auto",
        reference_image_path: Optional[Union[str, List[str]]] = None,
    ) -> str:
        index = scene_data.get("index", 0)
        prompt = scene_data.get("image_prompt", "")
        if not prompt:
            raise ValueError(f"У слайда {index} нет промпта")

        refs = self._normalize_refs(reference_image_path)
        notes: List[str] = []
        has_skeleton = False
        for ref in refs:
            if is_identity_ref(ref):
                notes.append("identity: the person to depict")
            elif "asset-skeleton" in os.path.basename(ref):
                has_skeleton = True
                notes.append("position map only, not a style reference")
            else:
                notes.append("style/template reference: copy palette, type and layout mood, never its text")
        if has_skeleton:
            prompt += (
                "\n\nThe position map marks areas where real photos will be pasted later. "
                "Keep titles, text, numbers, icons and people out of those areas and let the "
                "background continue naturally there. Never draw placeholder boxes, empty cards, "
                "frames, white rectangles or the corner marks themselves."
            )

        print(f"🔗 [слайд {index}] отправляю в Codex…")
        saved = run_codex_image(prompt, output_path, self.aspect_ratio, refs, notes)
        print(f"[OK] сохранено: {saved} ({saved.stat().st_size // 1024} КБ)")
        return str(saved)
