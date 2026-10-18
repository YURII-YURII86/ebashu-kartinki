#!/usr/bin/env python3
"""
Template analyzer: turns a .pptx and its rendered pages into a TemplateProfile JSON.

Responsibilities:
1. read_pptx_metadata: extract theme/colors/fonts with python-pptx (optional fallback)
2. load_template_images: load page PNGs from a folder (sorted by name)
3. compute_source_hash: content hash of .pptx + images as the cache key
4. VisionClient: OpenAI-compatible multimodal chat completions client
5. vision_analyze: extract global_style + per-page summary/page_type/json_schema
6. analyze_template: top-level entry point with caching
7. assign_layouts: map slides to layouts by layout_id / page_type, preferring unused layouts
8. coerce_fields: split free-form content into the layout schema fields
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


VISION_TIMEOUT_SECS = int(os.getenv("VISION_TIMEOUT_SECS", "90"))
VISION_MAX_RETRIES = int(os.getenv("VISION_MAX_RETRIES", "2"))
VISION_RETRY_DELAY = int(os.getenv("VISION_RETRY_DELAY", "4"))

CACHE_DIR_NAME = "template_cache"
PROFILE_VERSION = "1"

VALID_PAGE_TYPES = {"cover", "agenda", "section", "content", "data", "quote", "closing", "other"}


# =============================================================================
# helpers
# =============================================================================

def _read_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _data_url_for(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    return f"data:image/{suffix};base64,{_read_image_b64(path)}"


def _strip_json_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        # strip ```lang ... ```
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    return s


def _parse_json_loose(text: str) -> Any:
    """Best-effort JSON extraction from LLM text."""
    s = _strip_json_fence(text)
    try:
        return json.loads(s)
    except Exception:
        pass
    # take everything between the first { and the last }
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except Exception:
            pass
    # take everything between the first [ and the last ]
    start = s.find("[")
    end = s.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except Exception:
            pass
    raise ValueError(f"Не удалось разобрать JSON: {text[:300]}")


# =============================================================================
# .pptx metadata
# =============================================================================

def read_pptx_metadata(pptx_path: str) -> Dict[str, Any]:
    """Extract theme/layouts/fonts/colors with python-pptx. Returns an empty dict on failure."""
    try:
        from pptx import Presentation
    except ImportError:
        print("(!)  python-pptx не установлен, структура .pptx не читается")
        return {}

    try:
        prs = Presentation(pptx_path)
    except Exception as e:
        print(f"(!)  Не удалось открыть .pptx: {e}")
        return {}

    width_emu = prs.slide_width
    height_emu = prs.slide_height
    aspect = "16:9"
    if width_emu and height_emu:
        ratio = width_emu / height_emu
        if abs(ratio - 16 / 9) < 0.05:
            aspect = "16:9"
        elif abs(ratio - 4 / 3) < 0.05:
            aspect = "4:3"
        else:
            aspect = f"{width_emu}:{height_emu}"

    layouts = []
    try:
        for layout in prs.slide_layouts:
            layouts.append({"name": getattr(layout, "name", "") or "", "placeholder_count": len(layout.placeholders)})
    except Exception:
        pass

    page_count = 0
    try:
        page_count = len(prs.slides)
    except Exception:
        pass

    return {
        "aspect": aspect,
        "slide_width_emu": width_emu,
        "slide_height_emu": height_emu,
        "page_count": page_count,
        "master_layouts": layouts,
    }


# =============================================================================
# image loading + hashing
# =============================================================================

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def load_template_images(images_dir: str) -> List[str]:
    """Load all images in images_dir sorted by file name; return absolute paths."""
    if not images_dir:
        return []
    p = Path(images_dir)
    if not p.is_dir():
        raise FileNotFoundError(f"Папка с картинками шаблона не найдена: {images_dir}")
    files = sorted(
        [str(f) for f in p.iterdir() if f.suffix.lower() in IMAGE_EXTS],
        key=lambda s: s.lower(),
    )
    return files


def compute_source_hash(pptx_path: Optional[str], images: List[str]) -> str:
    """sha256 over the .pptx and all images, used as the cache key."""
    h = hashlib.sha256()
    h.update(PROFILE_VERSION.encode())
    if pptx_path and Path(pptx_path).exists():
        h.update(b"PPTX:")
        h.update(Path(pptx_path).name.encode("utf-8"))
        with open(pptx_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    for img in images:
        h.update(b"|IMG:")
        h.update(Path(img).name.encode("utf-8"))
        with open(img, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    return h.hexdigest()[:16]


# =============================================================================
# VisionClient
# =============================================================================

class VisionClient:
    """OpenAI-compatible multimodal chat completions client; reads the VISION_* variables."""

    def __init__(self) -> None:
        self.base_url = os.getenv("VISION_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("VISION_API_KEY", "")
        self.model = os.getenv("VISION_MODEL_NAME", "gemini-3.1-pro-preview")
        if not self.base_url:
            raise ValueError("Не задан VISION_BASE_URL")
        if not self.api_key:
            raise ValueError("Не задан VISION_API_KEY")
        # endpoint URL: do not append /v1 twice
        if "/chat/completions" in self.base_url:
            self.endpoint = self.base_url
        elif self.base_url.rstrip("/").endswith("/v1"):
            self.endpoint = f"{self.base_url}/chat/completions"
        else:
            self.endpoint = f"{self.base_url}/v1/chat/completions"

    def chat_json(
        self,
        system: str,
        user_text: str,
        images: Optional[List[str]] = None,
        temperature: float = 0.2,
    ) -> Any:
        """Non-streaming chat completions call that demands JSON. Returns the parsed JSON."""
        content: List[Dict[str, Any]] = []
        for img in images or []:
            content.append({"type": "image_url", "image_url": {"url": _data_url_for(img)}})
        content.append({"type": "text", "text": user_text})

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": temperature,
            "stream": False,
        }
        # response_format is optional on most compatible gateways; harmless when ignored
        payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        import time as _time
        last_err: Optional[Exception] = None
        for attempt in range(1, VISION_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    self.endpoint, headers=headers, json=payload, timeout=VISION_TIMEOUT_SECS
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"Ошибка vision-запроса status={resp.status_code} body={resp.text[:300]}")
                data = resp.json()
                msg = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                if not msg:
                    raise RuntimeError(f"vision вернул пустой content: {str(data)[:300]}")
                return _parse_json_loose(msg)
            except Exception as e:
                last_err = e
                msg_s = str(e)[:200]
                transient = any(s in msg_s for s in ("timeout", "Read timed out", "Connection aborted",
                                                     "RemoteDisconnected", "502", "503", "504", "524"))
                if attempt < VISION_MAX_RETRIES and transient:
                    print(f"(!)  vision: попытка {attempt} не удалась ({msg_s}), {VISION_RETRY_DELAY} с — повтор")
                    _time.sleep(VISION_RETRY_DELAY)
                    continue
                raise
        raise RuntimeError(f"vision: после {VISION_MAX_RETRIES} попыток всё ещё ошибка: {last_err}")


# =============================================================================
# Prompt templates (ask the vision model for strict JSON)
# =============================================================================

GLOBAL_SYSTEM = (
    "Ты профессиональный аналитик визуального дизайна презентаций. "
    "Тебе дадут миниатюры нескольких страниц шаблона. Нужно:\n"
    "1) описать общий визуальный стиль шаблона (global_style);\n"
    "2) для каждой страницы дать page_type и краткое описание макета (summary);\n"
    "3) для каждой страницы дать строгую JSON Schema по ёмкости контента (properties / required / minLength / maxLength / description).\n"
    "**Верни только JSON — без markdown и без пояснений**."
)

GLOBAL_USER_TPL = """По миниатюрам {n_images} страниц шаблона ниже (в порядке следования) верни JSON:

{{
  "global_style": "...15–40 слов о визуальном характере шаблона: фон/основной/акцентный цвет, характер шрифтов (жирный/антиква/гротеск), декор, воздух и композиция...",
  "theme": {{
    "primary": "#hex",
    "accent": "#hex",
    "background": "#hex",
    "fonts": {{ "title": "название шрифта или unknown", "body": "название шрифта или unknown" }}
  }},
  "layouts": [
    {{
      "id": "уникальный короткий ID (kebab-case, по макету, например cover-large-title / data-three-metrics / agenda-numbered-list)",
      "page_index": 0,
      "page_type": "cover|agenda|section|content|data|quote|closing|other",
      "summary": "15–30 слов о макете страницы: расположение, декор, иерархия текста; без пересказа самого текста",
      "visual_signature": "3–8 слов о самой запоминающейся черте макета: «текст слева, фото справа» / «три карточки» / «таймлайн с номерами»",
      "content_capacity": {{
        "title": "рекомендуемая длина/форма заголовка",
        "body": "ёмкость текста, например 3–5 коротких пунктов / 1 большая цифра + 2 пояснения"
      }},
      "best_for": ["для какого содержимого подходит, например agenda / feature list / comparison / quote"],
      "avoid_for": ["для чего не подходит, например dense table / long legal copy"],
      "variation_tags": ["split", "cards", "timeline", "image-right"],
      "external_image_slots": [
        {
          "id": "hero-image",
          "purpose": "эту зону шаблона удобно заменить реальным фото/скриншотом/графиком",
          "bbox": [0.52, 0.14, 0.40, 0.68],
          "priority": 1
        }
      ],
      "reuse_friendly": true,
      "reuse_reason": "одно предложение: почему страницу можно / нельзя повторять в одной презентации",
      "json_schema": {{
        "type": "object",
        "properties": {{
          "title":    {{ "type": "string", "minLength": 2, "maxLength": 24, "description": "..." }},
          "subtitle": {{ "type": "string", "minLength": 4, "maxLength": 50, "description": "..." }}
        }},
        "required": ["title"],
        "additionalProperties": false
      }}
    }}
  ]
}}

Требования:
- В массиве layouts ровно {n_images} элементов, по одному на страницу, в порядке картинок.
- Каждый layout.json_schema строго по спецификации JSON Schema; ключи properties на английском, description на русском.
- Если на странице есть явная зона под фото, изображение, график, скриншот или главный визуал, дай 1–3 кандидата в external_image_slots.
- external_image_slots[].bbox — нормированные координаты [x, y, w, h], левый верх 0,0, правый низ 1,1; только зоны под реальные изображения, не текстовые.
- Если подходящей зоны нет, external_image_slots = [].
- minLength/maxLength ставь по реальной ёмкости страницы, не шаблонными числами.
- Если на странице график данных — нужно array-поле "metrics"; страница-список — array "items";
  обложка — title/subtitle; оглавление — items; слайд с цифрами — metrics или stats.
- visual_signature / content_capacity / best_for / avoid_for / variation_tags нужны для автоподбора разных макетов:
  * visual_signature — коротко о визуальном отличии, не повтор page_type.
  * content_capacity — сколько информации максимум помещается, чтобы не впихнуть длинный текст в визуальную страницу.
  * variation_tags — короткие английские теги для сравнения макетов.
- Цвета в #RRGGBB; если не уверен — ближайшее приближение.
- Верни строго один JSON-объект и никакого другого текста.

[Правила reuse_friendly]
- true (можно повторять): универсальный макет, декор не привязан к смыслу. Например:
  * чистый текст / сетка карточек / список / типовое сравнение цифр / заголовок раздела
  * декор — только геометрия / абстрактные плашки / типовые иконки
- false (лучше не повторять): у макета сильный смысловой якорь, повтор вызовет «опять эта страница». Например:
  * обложка (cover) — одна на презентацию
  * страница с уникальными персонажами (например, 3 героя разных профессий)
  * страница с уникальной сценой (горы, радиовышка, ретро-радио и т. п.)
  * многошаговый процесс с уникальными иконками (зигзаг из 5 шагов) — повтор намекает на «тот же процесс»
  * необычная визуализация данных (уникальная центральная инфографика)
- reuse_reason — одним предложением, почему так решено.
"""


def vision_analyze(images: List[str], client: VisionClient, pptx_meta: Dict[str, Any]) -> Dict[str, Any]:
    """Send all page thumbnails to vision at once and get a full TemplateProfile (without source/cache fields)."""
    if not images:
        raise ValueError("vision_analyze нужна хотя бы одна картинка шаблона")
    user_text = GLOBAL_USER_TPL.format(n_images=len(images))
    if pptx_meta:
        user_text += f"\n\nСправка: формат исходного .pptx = {pptx_meta.get('aspect', 'unknown')}, всего {pptx_meta.get('page_count', len(images))} стр."

    print(f"🔍 vision анализирует {len(images)} стр. шаблона (model={client.model})...")
    result = client.chat_json(
        system=GLOBAL_SYSTEM,
        user_text=user_text,
        images=images,
        temperature=0.2,
    )
    if not isinstance(result, dict) or "layouts" not in result:
        raise ValueError(f"vision вернул некорректную структуру: {str(result)[:300]}")

    layouts = result.get("layouts", [])
    if len(layouts) > len(images):
        print(f"(!)  vision вернул {len(layouts)} макетов — больше, чем картинок ({len(images)}) — обрезаю")
        layouts = layouts[: len(images)]
    if len(layouts) < len(images):
        # vision often returns fewer layouts (token limits): pad with placeholders that use the page image as reference.
        missing = len(images) - len(layouts)
        print(f"(!)  vision вернул {len(layouts)} макетов — меньше, чем картинок ({len(images)}), добавляю {missing} заглушек")
        for i in range(len(layouts), len(images)):
            layouts.append({
                "id": f"layout-{i + 1:02d}",
                "page_index": i,
                "page_type": "content",
                "summary": "(vision не вернул макет этой страницы, референсом служит сама страница)",
                "reuse_friendly": True,
                "reuse_reason": "(vision не анализировал, по умолчанию можно повторять)",
                "json_schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 2, "maxLength": 40},
                        "body": {"type": "string", "minLength": 0, "maxLength": 600},
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            })
    result["layouts"] = layouts

    # inject reference_image paths and normalize base fields
    for i, layout in enumerate(layouts):
        layout["reference_image"] = str(Path(images[i]).resolve())
        layout.setdefault("page_index", i)
        pt = layout.get("page_type", "")
        if pt not in VALID_PAGE_TYPES:
            layout["page_type"] = "content"
        layout.setdefault("id", f"layout-{i + 1:02d}")
        # reuse: cover is never reusable; others default to True (no noise when vision is silent)
        if layout.get("page_type") == "cover":
            layout["reuse_friendly"] = False
            layout.setdefault("reuse_reason", "Обложка бывает только одна на презентацию")
        else:
            layout.setdefault("reuse_friendly", True)
            layout.setdefault("reuse_reason", "")
        layout["visual_signature"] = str(layout.get("visual_signature") or "").strip()
        capacity = layout.get("content_capacity")
        layout["content_capacity"] = capacity if isinstance(capacity, (dict, list, str)) else {}
        for key in ("best_for", "avoid_for", "variation_tags"):
            val = layout.get(key)
            if isinstance(val, list):
                layout[key] = [str(x).strip() for x in val if str(x).strip()]
            elif isinstance(val, str) and val.strip():
                layout[key] = [val.strip()]
            else:
                layout[key] = []
        slots = layout.get("external_image_slots")
        layout["external_image_slots"] = slots if isinstance(slots, list) else []
    return result


# =============================================================================
# top-level entry point (with cache)
# =============================================================================

def _cache_dir() -> Path:
    """Template analysis cache lives in the caller's cwd, not in the skill folder."""
    return Path.cwd() / CACHE_DIR_NAME


def analyze_template(
    pptx_path: Optional[str],
    images_dir: Optional[str],
    rebuild: bool = False,
    client: Optional[VisionClient] = None,
    cache_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Main entry point: returns a full TemplateProfile.

    - Only pptx_path without images: fallback with basic metadata and no layouts (no cloning possible).
    - With images: run vision for a full profile; use the cache when it hits.
    """
    pptx_meta = read_pptx_metadata(pptx_path) if pptx_path else {}
    images = load_template_images(images_dir) if images_dir else []

    source_label = Path(pptx_path).name if pptx_path else (Path(images_dir).name if images_dir else "unknown")

    if not images:
        print("(!)  --template-images не задан: стиль не анализируется, возвращаю только метаданные .pptx")
        return {
            "version": PROFILE_VERSION,
            "source": source_label,
            "source_hash": compute_source_hash(pptx_path, []),
            "global_style": "",
            "theme": {},
            "layouts": [],
            "pptx_meta": pptx_meta,
        }

    src_hash = compute_source_hash(pptx_path, images)
    cache_root = cache_dir if cache_dir else _cache_dir()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = cache_root / f"{src_hash}.json"

    if cache_path.exists() and not rebuild:
        print(f"📦 Профиль шаблона из кэша {cache_path}")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"(!)  Кэш повреждён ({e}), анализирую заново")

    if client is None:
        client = VisionClient()
    profile = vision_analyze(images, client, pptx_meta)

    profile["version"] = PROFILE_VERSION
    profile["source"] = source_label
    profile["source_hash"] = src_hash
    profile["pptx_meta"] = pptx_meta

    # defensive mkdir: cache_root may have been removed externally after the first mkdir
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    print(f"💾 Профиль шаблона закэширован -> {cache_path}")
    return profile


# =============================================================================
# layout matching + field splitting
# =============================================================================

def match_layout(slide: Dict[str, Any], profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick the best layout for a slide: layout_id -> layout-NN suffix -> page_type -> slide_number fallback."""
    layouts = profile.get("layouts", [])
    if not layouts:
        return None

    layout_id = slide.get("layout_id")
    if layout_id:
        for lay in layouts:
            if lay.get("id") == layout_id:
                return lay
        # unknown layout_id -> for auto IDs like layout-NN fall back to the NN-th layout,
        # keeping the user's intent "slide N -> template page N"
        m = re.search(r"(\d+)\s*$", layout_id)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(layouts):
                print(f"(!)  slide {slide.get('slide_number')} layout_id={layout_id} не найден, беру по номеру layouts[{idx}]")
                return layouts[idx]
        print(f"(!)  slide {slide.get('slide_number')} указан layout_id={layout_id} — такого в шаблоне нет, беру по page_type")

    page_type = slide.get("page_type", "content")
    type_matches = [lay for lay in layouts if lay.get("page_type") == page_type]
    if type_matches:
        return type_matches[0]

    # nothing matched -> align by slide number (slide N -> template page N, clamp to last)
    n = slide.get("slide_number", 1)
    idx = max(0, min(n - 1, len(layouts) - 1))
    return layouts[idx]


def _routing_content_text(slide: Dict[str, Any]) -> str:
    content = slide.get("content", "")
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _routing_features(slide: Dict[str, Any]) -> Dict[str, Any]:
    """Extract conservative content-shape hints for opt-in distilled layouts."""
    text = _routing_content_text(slide)
    lowered = text.lower()
    metric_count = len(re.findall(r"[-+]?\d+(?:\.\d+)?\s*%", text))
    quarter_count = len(set(re.findall(r"\bq[1-4]\b", lowered)))
    year_count = len(set(re.findall(r"\b(?:19|20)\d{2}\b", text)))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullet_count = sum(
        1 for line in lines if re.match(r"^(?:[-*•]|\d+[.)])\s*", line)
    )
    item_count = max(metric_count, bullet_count, max(0, len(lines) - 1))
    shapes: set[str] = set()
    if metric_count:
        shapes.add("metrics")
    if metric_count >= 2 and quarter_count >= 2:
        shapes.update({"metrics-series", "metrics+series"})
    if year_count >= 2 or any(token in lowered for token in ("timeline", "таймлайн", "этап")):
        shapes.add("timeline")
    if any(token in lowered for token in ("before", "after", "versus", " vs ", "сравнени", "до и после", "было", "стало")):
        shapes.add("comparison")
    if any(token in lowered for token in ("step", "phase", "фаз", "шаг", "процесс")):
        shapes.add("process")
    if "|" in text or any(token in lowered for token in ("table", "таблиц", "матриц")):
        shapes.add("table")
    if bullet_count >= 2 or len(lines) >= 4:
        shapes.add("list")
    if not shapes:
        shapes.add("general")
    return {
        "text": lowered,
        "shapes": shapes,
        "metric_count": metric_count,
        "series_count": max(quarter_count, year_count),
        "item_count": item_count,
    }


def _routing_shape_aliases(values: set[str]) -> set[str]:
    """Normalize model-authored shape synonyms into stable routing families."""
    expanded: set[str] = set()
    for value in values:
        token = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
        if not token:
            continue
        expanded.add(token)
        if any(part in token for part in ("metric", "kpi", "chart", "series", "trend")):
            expanded.add("metrics")
        if any(part in token for part in ("table", "tabular", "matrix")):
            expanded.add("table")
        if any(part in token for part in ("timeline", "milestone", "roadmap", "chronolog")):
            expanded.add("timeline")
        if any(part in token for part in ("compar", "versus")):
            expanded.add("comparison")
        if any(part in token for part in ("process", "workflow", "step", "phase")):
            expanded.add("process")
        if any(part in token for part in ("list", "bullet")):
            expanded.add("list")
    return expanded


def layout_routing_score(slide: Dict[str, Any], layout: Dict[str, Any]) -> float:
    """Score explicit routing metadata; layouts without routing remain order-driven."""
    routing = layout.get("routing")
    if not isinstance(routing, dict) or not routing:
        return 0.0
    features = _routing_features(slide)
    shapes = _routing_shape_aliases(set(features["shapes"]))
    score = 1.0
    content_shapes = routing.get("content_shapes")
    if isinstance(content_shapes, str):
        content_shapes = [content_shapes]
    if isinstance(content_shapes, list) and content_shapes:
        wanted = _routing_shape_aliases(
            {str(item).strip().lower() for item in content_shapes if str(item).strip()}
        )
        score += 30.0 if shapes & wanted else -15.0
    for field, delta_present, delta_missing in (
        ("requires", 10.0, -25.0),
        ("excludes", -30.0, 0.0),
    ):
        values = routing.get(field)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            token = str(value).strip().lower()
            if not token:
                continue
            present = bool(_routing_shape_aliases({token}) & shapes) or token in features["text"]
            score += delta_present if present else delta_missing
    for feature_name, min_key, max_key in (
        ("item_count", "min_items", "max_items"),
        ("metric_count", "min_metrics", "max_metrics"),
        ("series_count", "min_series", "max_series"),
    ):
        value = int(features[feature_name])
        minimum = routing.get(min_key)
        maximum = routing.get(max_key)
        if isinstance(minimum, (int, float)):
            score += 4.0 if value >= minimum else -12.0
        if isinstance(maximum, (int, float)):
            score += 4.0 if value <= maximum else -12.0
    for key, delta in (("keywords", 3.0), ("exclude_keywords", -20.0)):
        values = routing.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            for value in values:
                token = str(value).strip().lower()
                if token and token in features["text"]:
                    score += delta
    return score


def assign_layouts(slides: List[Dict[str, Any]], profile: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Assign template layouts to slides while preferring one layout per page.

    Explicit `layout_id` always wins, including intentional reuse. Slides without
    an explicit layout get the best unused layout for their page_type before
    falling back to the older match_layout behavior.
    """
    layouts = profile.get("layouts", [])
    if not layouts:
        return {}

    assigned: Dict[int, Dict[str, Any]] = {}
    used_ids = set()

    def layout_id_of(layout: Dict[str, Any], idx: int) -> str:
        return str(layout.get("id") or f"layout-{idx + 1:02d}")

    def mark(slide: Dict[str, Any], layout: Dict[str, Any]) -> None:
        slide_number = int(slide.get("slide_number", len(assigned) + 1))
        assigned[slide_number] = layout
        try:
            idx = layouts.index(layout)
        except ValueError:
            idx = 0
        used_ids.add(layout_id_of(layout, idx))

    # Pass 1: honor explicit layout choices exactly.
    for slide in slides:
        layout_id = slide.get("layout_id")
        if not layout_id:
            continue
        chosen = None
        for lay in layouts:
            if lay.get("id") == layout_id:
                chosen = lay
                break
        if chosen is None:
            m = re.search(r"(\d+)\s*$", str(layout_id))
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(layouts):
                    print(f"(!)  slide {slide.get('slide_number')} layout_id={layout_id} не найден, беру по номеру layouts[{idx}]")
                    chosen = layouts[idx]
        if chosen is None:
            print(f"(!)  slide {slide.get('slide_number')} указан layout_id={layout_id} — такого в шаблоне нет, назначаю неиспользованный макет")
            continue
        mark(slide, chosen)

    # Pass 2: assign unused layouts, preserving page_type where possible.
    for slide in slides:
        slide_number = int(slide.get("slide_number", len(assigned) + 1))
        if slide_number in assigned:
            continue

        page_type = slide.get("page_type", "content")
        chosen = None

        unused_same_type = [
            lay
            for i, lay in enumerate(layouts)
            if lay.get("page_type") == page_type and layout_id_of(lay, i) not in used_ids
        ]
        if any(isinstance(lay.get("routing"), dict) and lay.get("routing") for lay in unused_same_type):
            chosen = max(
                unused_same_type,
                key=lambda lay: layout_routing_score(slide, lay),
            )

        if chosen is None:
            idx = max(0, min(slide_number - 1, len(layouts) - 1))
            indexed = layouts[idx]
            indexed_id = layout_id_of(indexed, idx)
            if indexed.get("page_type") == page_type and indexed_id not in used_ids:
                chosen = indexed

        if chosen is None:
            for i, lay in enumerate(layouts):
                lid = layout_id_of(lay, i)
                if lay.get("page_type") == page_type and lid not in used_ids:
                    chosen = lay
                    break

        same_type = [lay for lay in layouts if lay.get("page_type") == page_type]
        if chosen is None and same_type:
            reusable = [lay for lay in same_type if lay.get("reuse_friendly", True)]
            if any(isinstance(lay.get("routing"), dict) and lay.get("routing") for lay in reusable):
                chosen = max(reusable, key=lambda lay: layout_routing_score(slide, lay))
            else:
                for lay in reusable:
                    chosen = lay
                    break
            if chosen is None:
                chosen = same_type[0]

        if chosen is None:
            for i, lay in enumerate(layouts):
                lid = layout_id_of(lay, i)
                if lid not in used_ids:
                    chosen = lay
                    break

        if chosen is None:
            # All layouts are already used; reuse the best old-style match.
            tmp = dict(slide)
            tmp.pop("layout_id", None)
            chosen = match_layout(tmp, profile)

        if chosen:
            mark(slide, chosen)

    return assigned


def check_layout_reuse(slides: List[Dict[str, Any]], profile: Dict[str, Any]) -> List[str]:
    """Detect layouts used more than once in the plan and list unused alternatives.

    Returns a list of formatted warning strings for printing.
    Each slide is resolved with match_layout, then grouped by layout.id.
    - repeated + reuse_friendly == False: (!) strong warning, should change;
    - repeated + reuse_friendly == True: (i) soft hint, prefer an unused layout;
    - unused candidates (grouped by page_type) are listed to help decide.
    """
    warnings: List[str] = []
    layouts = profile.get("layouts", [])
    if not layouts:
        return warnings

    # collect which slides use each layout_id
    assigned = assign_layouts(slides, profile)
    usage: Dict[str, List[int]] = {}
    layout_meta: Dict[str, Dict[str, Any]] = {}
    for slide in slides:
        lay = assigned.get(int(slide.get("slide_number", 0)))
        if not lay:
            continue
        lid = lay.get("id", "?")
        usage.setdefault(lid, []).append(slide.get("slide_number", 0))
        layout_meta[lid] = lay

    used_ids = set(usage.keys())
    unused = [lay for lay in layouts if lay.get("id") not in used_ids]

    for lid, slide_nums in usage.items():
        if len(slide_nums) < 2:
            continue
        lay = layout_meta[lid]
        friendly = lay.get("reuse_friendly", True)
        page_type = lay.get("page_type", "?")
        reason = lay.get("reuse_reason") or "Лучше: каждому слайду свой макет"
        # unused layouts with the same page_type as candidates
        candidates = [u.get("id") for u in unused if u.get("page_type") == page_type][:3]
        candidate_hint = (
            f"\n    Запасные макеты (page_type={page_type}), можно переключиться на них: {', '.join(candidates)}"
            if candidates else ""
        )
        if friendly:
            warnings.append(
                f"(i)  layout {lid} ({page_type}) используется слайдами {slide_nums} — {len(slide_nums)} раз, "
                f"помечен reuse_friendly=True, но лучше взять другой макет, чтобы не было визуальных повторов.{candidate_hint}"
            )
        else:
            warnings.append(
                f"(!)  layout {lid} ({page_type}) используется слайдами {slide_nums} — {len(slide_nums)} раз, "
                f"и помечен reuse_friendly=False. Причина: {reason}\n"
                f"    Настоятельно рекомендуется сменить макет.{candidate_hint}"
            )
    return warnings


def summarize_layouts(profile: Dict[str, Any]) -> str:
    """Format all layouts of a profile as a table-like text for picking pages."""
    layouts = profile.get("layouts", [])
    if not layouts:
        return "(в профиле нет макетов)"
    lines = ["id  | page_type | reuse | summary"]
    lines.append("--- | --- | --- | ---")
    for lay in layouts:
        flag = "✓" if lay.get("reuse_friendly", True) else "✗"
        summary = (lay.get("summary", "") or "").replace("\n", " ")[:60]
        lines.append(f"{lay.get('id','?')} | {lay.get('page_type','?')} | {flag} | {summary}")
    return "\n".join(lines)


def coerce_fields(slide: Dict[str, Any], layout: Dict[str, Any]) -> Dict[str, Any]:
    """If the slide already has fields, validate them against the schema; otherwise split content into fields.

    Strategy:
    - first line becomes title
    - remaining non-empty lines become items / bullets / body
    - follow schema.properties names and types strictly
    """
    schema = layout.get("json_schema") or {"type": "object", "properties": {}}
    properties = (schema.get("properties") or {}) if isinstance(schema, dict) else {}

    if "fields" in slide and isinstance(slide["fields"], dict):
        fields = dict(slide["fields"])
    else:
        fields = _content_to_fields(slide.get("content", ""), properties)

    # never cut the user's text: a long field only produces a warning
    for key, prop in properties.items():
        v = fields.get(key)
        max_len = prop.get("maxLength") if isinstance(prop, dict) else None
        if isinstance(v, str) and isinstance(max_len, int) and len(v) > max_len:
            print(f"(i)  {key}: {len(v)} символов при рекомендуемых {max_len} — текст оставлен целиком, лучше сократить")

    # soft jsonschema validation
    try:
        import jsonschema
        try:
            jsonschema.validate(instance=fields, schema=schema)
        except jsonschema.ValidationError as ve:
            print(f"(!)  slide {slide.get('slide_number')} поле не прошло schema: {ve.message[:120]}")
    except ImportError:
        pass
    return fields


def _content_to_fields(content: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    """Split a free-form content string into fields according to schema properties."""
    if not content:
        return {}
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return {}

    fields: Dict[str, Any] = {}
    title = lines[0]
    rest_lines = lines[1:]

    # standard fields: title / subtitle
    if "title" in properties:
        fields["title"] = title
    if "subtitle" in properties and rest_lines:
        fields["subtitle"] = rest_lines[0]
        rest_lines = rest_lines[1:]
    if "tagline" in properties and rest_lines:
        fields["tagline"] = rest_lines[0]
        rest_lines = rest_lines[1:]

    # array fields: items / metrics / bullets / points
    array_field = None
    array_item_props: Dict[str, Any] = {}
    for k, p in properties.items():
        if isinstance(p, dict) and p.get("type") == "array":
            array_field = k
            items_schema = p.get("items") or {}
            if isinstance(items_schema, dict):
                array_item_props = items_schema.get("properties") or {}
            break

    if array_field and rest_lines:
        items: List[Any] = []
        for ln in rest_lines:
            cleaned = re.sub(r"^[\-\*\u2022*•\d\.\)\s]+", "", ln).strip()
            if not cleaned:
                continue
            if not array_item_props:
                items.append(cleaned)
                continue
            obj: Dict[str, Any] = {}
            # split "name: desc" / "name - desc" / "name | desc"
            # hyphens inside words ("ИИ-помощник") must not split, so dashes need spaces around them
            split_match = re.split(r"\s*[:|]\s*|\s+[-–—]\s+", cleaned, maxsplit=1)
            name_val = split_match[0].strip() if split_match else cleaned
            desc_val = split_match[1].strip() if len(split_match) > 1 else ""
            for prop_key in array_item_props.keys():
                if prop_key in ("name", "label", "title", "key"):
                    obj[prop_key] = name_val
                elif prop_key in ("value", "metric", "number"):
                    # extract number + unit
                    # a metric must start with a digit; lines without numbers get no metric
                    # "было → стало": the headline number is the result after the arrow
                    tail = re.split(r"→|->|=>", cleaned)[-1]
                    m = re.search(r"[−-]?\d[\d\s.,]*(?:%|тыс\.?|млн|млрд|x|k|K|M|B|с|\+)?", tail)
                    obj[prop_key] = m.group(0).strip() if m else ""
                else:
                    # a line without "name: desc" is only a name; repeating it as description duplicates text on the slide
                    obj[prop_key] = desc_val
            items.append(obj)
        if items:
            fields[array_field] = items
    elif rest_lines:
        # no array fields: join remaining text into body / description
        for k in ("body", "description", "content", "text"):
            if k in properties:
                fields[k] = "\n".join(rest_lines)
                break

    return fields


# =============================================================================
# field values -> image prompt
# =============================================================================

def render_prompt_from_template(
    profile: Dict[str, Any],
    layout: Dict[str, Any],
    fields: Dict[str, Any],
    aspect_hint: str = "",
    language_rule: str = "",
) -> str:
    """Assemble the final image prompt according to the plan."""
    parts: List[str] = []
    g_style = profile.get("global_style", "").strip()
    if g_style:
        parts.append(f"[Общий стиль]\n{g_style}")
    parts.append(f"[Описание макета]\n{layout.get('summary', '').strip()}")

    variation_lines: List[str] = []
    visual_signature = str(layout.get("visual_signature") or "").strip()
    if visual_signature:
        variation_lines.append(f"- Визуальная подпись: {visual_signature}")
    content_capacity = layout.get("content_capacity")
    if content_capacity:
        if isinstance(content_capacity, str):
            capacity_text = content_capacity
        else:
            capacity_text = json.dumps(content_capacity, ensure_ascii=False)
        variation_lines.append(f"- Ёмкость: {capacity_text}")
    for key, label in (
        ("best_for", "Подходит для"),
        ("avoid_for", "Не подходит для"),
        ("variation_tags", "Теги вариаций"),
    ):
        val = layout.get(key)
        if isinstance(val, list) and val:
            variation_lines.append(f"- {label}: {', '.join(str(x) for x in val if str(x))}")
        elif isinstance(val, str) and val.strip():
            variation_lines.append(f"- {label}: {val.strip()}")
    if variation_lines:
        parts.append("[Вариация макета]\n" + "\n".join(variation_lines))

    parts.append("[Содержимое слайда]")
    schema_props = ((layout.get("json_schema") or {}).get("properties") or {}) if isinstance(layout.get("json_schema"), dict) else {}
    for key, val in fields.items():
        prop = schema_props.get(key, {}) if isinstance(schema_props, dict) else {}
        desc = prop.get("description", "") if isinstance(prop, dict) else ""
        if isinstance(val, list):
            rendered_items = []
            for item in val:
                if isinstance(item, dict):
                    rendered_items.append(" / ".join(f"{k}={v}" for k, v in item.items() if v))
                else:
                    rendered_items.append(str(item))
            joined = "\n  - " + "\n  - ".join(rendered_items)
            parts.append(f"- {key} ({desc}): {joined}")
        else:
            parts.append(f"- {key} ({desc}): {val}")

    if language_rule:
        parts.append(language_rule)
    if aspect_hint:
        parts.append(aspect_hint)

    return "\n\n".join(parts)


# =============================================================================
# CLI self-check (optional)
# =============================================================================

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Template Analyzer CLI (debug)")
    parser.add_argument("--pptx", help="Path to the template .pptx (optional)")
    parser.add_argument("--images", required=True, help="Folder with template page PNGs")
    parser.add_argument("--rebuild", action="store_true", help="Ignore the cache and re-analyze")
    args = parser.parse_args()

    # locate .env
    for cand in [
        Path(__file__).resolve().parent.parent / ".env",
    ]:
        if cand.exists():
            load_dotenv(cand, override=True)
            print(f"загружен .env: {cand}")
            break

    profile = analyze_template(args.pptx, args.images, rebuild=args.rebuild)
    print(json.dumps(profile, ensure_ascii=False, indent=2)[:3000])
