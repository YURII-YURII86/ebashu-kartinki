#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pick and prepare face reference photos for likeness-preserving generation.

  # 1. contact sheets: numbered thumbnails of every photo + quality metrics
  $PY scripts/faces.py sheet ~/Photos/person --out work/faces

  # 2. prepare the chosen photos as identity references
  $PY scripts/faces.py prep work/faces/sheet.json 3 7 12 15 --name anna --out work/faces/anna
  $PY scripts/faces.py prep photo1.jpg photo2.heic --name anna --out work/faces/anna

  # 3. compare a generated image with the references side by side
  $PY scripts/faces.py compare out/portrait.png work/faces/anna --out work/faces/compare.jpg

Prepared files are named <name>-NN.identity.jpg: the Codex bridge recognises
"identity" in the name and tells the model to keep this person's face.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageStat
except ImportError:  # pragma: no cover
    sys.exit("Нужен Pillow: используйте Python навыка (.venv/bin/python) или pip install pillow")

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
THUMB = 320
COLS, ROWS = 5, 4
REF_MAX_SIDE = 1536


def open_image(path: Path) -> Image.Image:
    """Open any photo, converting HEIC through macOS sips when Pillow cannot read it."""
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        tmp = Path(tempfile.mkdtemp()) / (path.stem + ".jpg")
        subprocess.run(["sips", "-s", "format", "jpeg", str(path), "--out", str(tmp)],
                       capture_output=True, check=True)
        img = Image.open(tmp)
        img.load()
    return ImageOps.exif_transpose(img).convert("RGB")


def collect(inputs: list) -> list:
    files = []
    for item in inputs:
        p = Path(item).expanduser()
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix.lower() in PHOTO_EXT and not f.name.startswith("."))
        elif p.suffix.lower() in PHOTO_EXT:
            files.append(p)
    return files


def metrics(img: Image.Image) -> dict:
    """Cheap quality signals: resolution, sharpness (edge energy), brightness, contrast."""
    gray = img.convert("L")
    small = gray.copy()
    small.thumbnail((800, 800))
    edges = small.filter(ImageFilter.FIND_EDGES)
    st = ImageStat.Stat(gray)
    return {
        "size": f"{img.width}x{img.height}",
        "megapixels": round(img.width * img.height / 1e6, 1),
        "sharpness": round(ImageStat.Stat(edges).var[0], 1),
        "brightness": round(st.mean[0]),
        "contrast": round(st.stddev[0]),
    }


def cmd_sheet(a) -> None:
    files = collect(a.inputs)
    if not files:
        sys.exit("Фото не найдены")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    index, per_sheet = [], COLS * ROWS
    for start in range(0, len(files), per_sheet):
        sheet = Image.new("RGB", (COLS * THUMB, ROWS * (THUMB + 28)), "white")
        draw = ImageDraw.Draw(sheet)
        for k, path in enumerate(files[start:start + per_sheet]):
            n = start + k + 1
            try:
                img = open_image(path)
            except Exception as exc:
                print(f"⚠️  пропускаю {path.name}: {exc}")
                continue
            m = metrics(img)
            index.append({"n": n, "path": str(path.resolve()), **m})
            thumb = ImageOps.contain(img, (THUMB - 8, THUMB - 8))
            x, y = (k % COLS) * THUMB, (k // COLS) * (THUMB + 28)
            sheet.paste(thumb, (x + (THUMB - thumb.width) // 2, y + 4 + (THUMB - 8 - thumb.height) // 2))
            draw.rectangle([x, y + THUMB, x + THUMB, y + THUMB + 28], fill="black")
            draw.text((x + 8, y + THUMB + 7), f"#{n}  {m['size']}  sharp {m['sharpness']:.0f}", fill="white")
        name = out / f"sheet-{start // per_sheet + 1:02d}.jpg"
        sheet.save(name, quality=85)
        print(name)
    (out / "sheet.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(index)} фото. Метрики и пути: {out / 'sheet.json'}")
    print(f"{'#':>4} {'размер':>11} {'Мп':>5} {'резкость':>9} {'яркость':>8} {'контраст':>8}")
    for row in index:
        print(f"{row['n']:>4} {row['size']:>11} {row['megapixels']:>5} {row['sharpness']:>9} {row['brightness']:>8} {row['contrast']:>8}")


def cmd_prep(a) -> None:
    paths = []
    if a.inputs and a.inputs[0].endswith(".json"):
        index = {row["n"]: row["path"] for row in json.loads(Path(a.inputs[0]).read_text(encoding="utf-8"))}
        for n in a.inputs[1:]:
            if int(n) not in index:
                sys.exit(f"Нет фото #{n} в {a.inputs[0]}")
            paths.append(Path(index[int(n)]))
    else:
        paths = collect(a.inputs)
    if not paths:
        sys.exit("Не выбрано ни одного фото")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob(f"{a.name}-*.identity.jpg"):
        old.unlink()
    for i, src in enumerate(paths, start=1):
        img = open_image(src)
        img.thumbnail((REF_MAX_SIDE, REF_MAX_SIDE))
        dst = out / f"{a.name}-{i:02d}.identity.jpg"
        img.save(dst, quality=92)
        print(f"{dst}  ← {src.name}")
    card = out / f"{a.name}.identity.md"
    if not card.exists():
        card.write_text(
            f"# Карточка внешности: {a.name}\n\n"
            "Заполнять только по фото. Описывать количественно всё, чем человек отличается от «среднего».\n\n"
            "Возраст (честно): \nТелосложение (плечи, руки, шея, живот относительно нормы): \n"
            "Волосы — длина в см: \nВолосы — густота (просвечивает ли кожа): \nВолосы — линия роста, лоб: \n"
            "Волосы — цвет, укладка, объём: \nРастительность на лице (длина, густота, контур, цвет, седина): \n"
            "Форма лица: \nБрови: \nГлаза (форма, посадка, цвет, веки): \nНос: \nГубы и улыбка: \n"
            "Скулы, челюсть, подбородок: \nУши: \nКожа (тон, текстура): \n"
            "Особые приметы (сторона человека, не зрителя): \nТипичная мимика: \n",
            encoding="utf-8",
        )
    print(f"\n{len(paths)} референсов готово. Заполни карточку внешности: {card}")


def cmd_compare(a) -> None:
    gen = open_image(Path(a.generated))
    refs = sorted(Path(a.refs).glob("*.identity.jpg")) if Path(a.refs).is_dir() else [Path(a.refs)]
    if not refs:
        sys.exit("В папке нет *.identity.jpg")
    h = 640
    tiles = [ImageOps.contain(gen, (int(h * 1.6), h))] + [ImageOps.contain(open_image(r), (h, h)) for r in refs[:5]]
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + 12 * len(tiles), h + 40), "white")
    draw, x = ImageDraw.Draw(sheet), 0
    for k, t in enumerate(tiles):
        sheet.paste(t, (x, 36))
        draw.text((x + 6, 10), "RESULT" if k == 0 else f"reference {k}", fill="black")
        x += t.width + 12
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    print(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Face references: pick, prepare, compare")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet", help="numbered contact sheets + quality metrics")
    s.add_argument("inputs", nargs="+", help="folders and/or photos")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_sheet)
    r = sub.add_parser("prep", help="prepare chosen photos as identity references")
    r.add_argument("inputs", nargs="+", help="sheet.json + numbers, or photo files")
    r.add_argument("--name", required=True, help="short latin name, e.g. anna")
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_prep)
    c = sub.add_parser("compare", help="generated image next to references")
    c.add_argument("generated")
    c.add_argument("refs", help="folder with *.identity.jpg or one photo")
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_compare)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
