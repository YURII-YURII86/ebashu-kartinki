#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gpt-image-2 image generator (direct OpenAI-compatible API).

Optional backend: the default path is the Codex bridge (codex_backend.py).
Calls /v1/chat/completions with model gpt-image-2, or /v1/images/generations
when GPT_IMAGE_ENDPOINT selects it.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

# Environment loading is handled once by the entry point generate_ppt.py.
# This module never calls load_dotenv() to avoid picking up unrelated .env files.


# 16:9 landscape 1536x864, 9:16 portrait 864x1536, square 1024x1024
ASPECT_TO_SIZE = {
    "16:9": "1536x864",
    "9:16": "864x1536",
    "1:1": "1024x1024",
}

REQUEST_TIMEOUT_SECS = 600  # image generation can take 1-3 minutes
MAX_RETRIES = 3  # retries for transient errors (524, timeouts, dropped connections)
RETRY_DELAY_SECS = 5
MAX_ASPECT_RETRIES = 2  # automatic regenerations when the aspect ratio is off
ASPECT_TOLERANCE = 0.15  # allowed aspect ratio deviation (±15%)
HTTP_USER_AGENT = "gpt-image2/1.0"

# expected aspect ratio (width / height)
ASPECT_RATIO_VALUES = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "1:1": 1.0,
}


class TransientImageGenerationError(RuntimeError):
    """The Images API was reached, but its upstream generator is temporarily unavailable."""


def openai_compatible_endpoint(base_url: str, path: str) -> str:
    """Join an OpenAI-compatible base URL without duplicating the /v1 prefix."""
    base = base_url.rstrip("/")
    suffix = "/" + path.strip("/")
    if suffix.startswith("/v1/"):
        suffix = suffix[3:]
    if base.endswith("/v1"):
        return f"{base}{suffix}"
    return f"{base}/v1{suffix}"


def read_png_dimensions(path: str) -> tuple:
    """Read width/height from the PNG header without PIL. Returns (0, 0) on failure."""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return 0, 0
        # IHDR chunk: width(4) + height(4) big-endian starting at byte 16
        width, height = struct.unpack(">II", head[16:24])
        return width, height
    except Exception:
        return 0, 0


def aspect_acceptable(width: int, height: int, target: str, tolerance: float = ASPECT_TOLERANCE) -> bool:
    """Check that the actual aspect ratio is within tolerance of the target."""
    if not (width and height):
        return True  # unreadable -> let it pass, do not block the flow
    expected = ASPECT_RATIO_VALUES.get(target)
    if expected is None:
        return True
    actual = width / height
    deviation = abs(actual - expected) / expected
    return deviation <= tolerance


class GptImage2Generator:
    """gpt-image-2 image generator"""

    def __init__(self, aspect_ratio: str = "16:9") -> None:
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.model_name = os.getenv("GPT_IMAGE_MODEL_NAME", "gpt-image-2")
        self.quality = os.getenv("GPT_IMAGE_QUALITY", "high")
        # endpoint: chat | images | auto (auto tries images, then chat)
        self.endpoint = os.getenv("GPT_IMAGE_ENDPOINT", "auto").lower()

        if not self.api_key:
            raise ValueError(
                "Не задан OPENAI_API_KEY. Передайте его через окружение "
                "или укажите GPT_IMAGE2_ENV на приватный env-файл."
            )

        self.aspect_ratio = aspect_ratio
        self.default_size = ASPECT_TO_SIZE.get(aspect_ratio, "1536x1024")

        print(
            f"🎨 Генератор gpt-image-2 (API) "
            f"(model={self.model_name}, size={self.default_size}, "
            f"quality={self.quality}, endpoint={self.endpoint})"
        )

    # ---------- helpers ----------

    def _save_b64(self, b64: str, output_path: str) -> None:
        if "," in b64 and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(b64))

    def _download_url(self, url: str, output_path: str) -> None:
        # Safety: only http/https; print the host before downloading so odd domains are visible;
        # 50MB cap against huge files; non image/* Content-Type is warned about but still saved.
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Отказ: URL не http(s): {parsed.scheme}")
        print(f"📥 Скачиваю картинку host={parsed.netloc} path={parsed.path[:80]}")
        resp = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT_SECS)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if ctype and not ctype.startswith("image/"):
            print(f"(!)  Content-Type не image: {ctype} (всё равно сохраняю, проверьте вручную)")
        MAX_BYTES = 50 * 1024 * 1024
        written = 0
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                written += len(chunk)
                if written > MAX_BYTES:
                    f.close()
                    os.remove(output_path)
                    raise ValueError(f"Скачивание превысило {MAX_BYTES} байт, файл отброшен {url[:80]}")
                f.write(chunk)

    def _save_payload(self, payload: str, output_path: str) -> None:
        """Save the payload (b64 / data url / plain url) to disk."""
        if payload.startswith("data:image/") or (len(payload) > 200 and "/" in payload[:200] is False):
            # data:image/...;base64,xxxxx or bare base64
            self._save_b64(payload, output_path)
        elif payload.startswith("http"):
            self._download_url(payload, output_path)
        else:
            # fallback: treat as base64
            self._save_b64(payload, output_path)

    def _normalize_reference_images(self, reference_image_path: Optional[Union[str, List[str]]]) -> List[str]:
        """Return existing local reference images, preserving caller order."""
        if not reference_image_path:
            return []
        if isinstance(reference_image_path, (list, tuple)):
            candidates = [str(p) for p in reference_image_path if p]
        else:
            candidates = [str(reference_image_path)]
        return [p for p in candidates if os.path.exists(p)]

    # ---------- multimodal response parsing (chat completions) ----------

    def _extract_image(self, content: Any) -> str:
        """Extract the image payload (b64 or URL) from chat completions content."""
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    img_url = part.get("image_url", {}).get("url", "")
                    if img_url:
                        return img_url
                if part.get("type") == "text":
                    text = part.get("text", "")
                    found = self._extract_from_text(text)
                    if found:
                        return found
            raise RuntimeError(f"в multimodal parts нет картинки: {str(content)[:300]}")

        if isinstance(content, str):
            found = self._extract_from_text(content)
            if found:
                return found
            raise RuntimeError(f"в text content нет картинки: {content[:300]}")

        raise RuntimeError(f"неизвестный тип content: {type(content)}")

    def _extract_from_text(self, text: str) -> Optional[str]:
        # 1) data:image/xxx;base64,YYY
        m = re.search(r"data:image/[\w]+;base64,[A-Za-z0-9+/=]+", text)
        if m:
            return m.group(0)
        # 2) markdown image ![](url)
        m = re.search(r"!\[.*?\]\((https?://[^\)\s]+)\)", text)
        if m:
            return m.group(1)
        # 3) bare image URL
        m = re.search(r"(https?://[^\s\)]+\.(?:png|jpg|jpeg|webp|gif))", text, re.IGNORECASE)
        if m:
            return m.group(1)
        # 4) any http link (last resort)
        m = re.search(r"(https?://[^\s\)]+)", text)
        if m:
            return m.group(1)
        return None

    # ---------- endpoint 1: /v1/chat/completions ----------

    def _request_via_chat(
        self,
        prompt: str,
        size: str,
        reference_image_path: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """Streaming chat completions request; assembles the image URL / b64 from the text stream.

        Some OpenAI-compatible gateways expose image models through chat completions
        and require stream=True: progress text first, then a markdown image or base64.

        When reference_image_path is set, the reference images are sent as multimodal
        input so gpt-image-2 follows their visual style (template clone mode).
        """
        url = openai_compatible_endpoint(self.base_url, "chat/completions")
        # describe the ratio in words: image models follow "wide 16:9" better
        # than explicit pixel sizes, which they tend to ignore.
        if self.aspect_ratio == "9:16":
            aspect_hint = (
                "\n\n[Формат кадра — обязательно] Строго вертикальный экран телефона 9:16. "
                "(portrait, vertical 9:16, height much taller than width). "
                "Ни в коем случае не квадрат."
            )
        elif self.aspect_ratio == "1:1":
            aspect_hint = "\n\n[Формат кадра — обязательно] Квадрат 1:1."
        else:
            aspect_hint = (
                "\n\n[Формат кадра — обязательно] Горизонтальный широкий кадр 16:9. "
                "(landscape orientation, widescreen 16:9 aspect ratio, "
                "ultrawide horizontal banner format). "
                "Ширина заметно больше высоты, соотношение около 1.78:1. "
                "Никаких квадратных, почти квадратных или вертикальных кадров."
                "Output MUST be 16:9 landscape widescreen, NEVER square or portrait."
            )

        full_prompt = f"{prompt}{aspect_hint}"
        reference_images = self._normalize_reference_images(reference_image_path)
        if reference_images:
            content_parts: List[Dict[str, Any]] = []
            has_asset_skeleton = False
            has_style_reference = False
            for idx, ref_path in enumerate(reference_images, start=1):
                with open(ref_path, "rb") as f:
                    ref_b64 = base64.b64encode(f.read()).decode("ascii")
                ref_data_url = f"data:image/png;base64,{ref_b64}"
                is_asset_skeleton = "asset-skeleton" in os.path.basename(ref_path)
                has_asset_skeleton = has_asset_skeleton or is_asset_skeleton
                has_style_reference = has_style_reference or not is_asset_skeleton
                if is_asset_skeleton:
                    content_parts.append({
                        "type": "text",
                        "text": f"Референс {idx} — карта позиций, а не образец стиля.",
                    })
                else:
                    content_parts.append({
                        "type": "text",
                        "text": f"Референс {idx} — образец стиля/шаблона, не копируй с него текст.",
                    })
                content_parts.append({"type": "image_url", "image_url": {"url": ref_data_url}})

            # edit mode vs template clone mode
            # edit prompts contain phrases like "измени только" / "остальное не трогай"
            is_edit = any(kw in prompt.lower() for kw in [
                "измени только", "за основу возьми", "всё остальное", "не трогай",
                "возьми слайд выше", "поменяй только", "остальное без изменений", "остальное не трогай",
            ])

            if is_edit:
                instruction = (
                    "Возьми слайд-референс выше и создай новую картинку строго по этим правкам.\n"
                    "Меняй только то, что указано; всё остальное (фон, цвета, шрифты, декор, макет, положение) "
                    "должно полностью совпадать с референсом.\n\n"
                )
            elif has_asset_skeleton and has_style_reference:
                instruction = (
                    "Выше два типа референсов: образец шаблона/стиля и карта позиций.\n"
                    "Образец стиля нужен только для палитры, шрифтов, декора, настроения и ритма вёрстки — его текст не копировать.\n"
                    "Тонкие уголки на карте позиций отмечают зоны, куда потом кодом вставят реальные фото; "
                    "это не элементы дизайна, не рамки, не карточки, не маски и не пустые блоки.\n"
                    "Не ставь в эти зоны ничего важного: заголовки, текст, цифры, иконки, людей или главные фото.\n"
                    "Фон под этими зонами должен продолжаться естественно: тот же цвет, фактура бумаги или лёгкий декор.\n"
                    "Запрещены белые/светлые прямоугольники, пустые карточки, плейсхолдеры, рамки, тени, прозрачные дыры и любые видимые заготовки.\n"
                    "Если макет или стиль ниже упоминает главное фото, фото товара, людей, сцены, photo crop, image crop или зону под изображение, "
                    "это заполнят реальные фото кодом — модель не должна сама рисовать или подделывать эти фото.\n"
                    "Не копируй, не обводи и не усиливай уголки с карты позиций; в итоге не должно остаться никаких следов разметки.\n\n"
                )
            elif has_asset_skeleton:
                instruction = (
                    "Картинка выше — не образец стиля, а карта позиций.\n"
                    "Тонкие уголки на ней отмечают зоны, куда потом кодом вставят реальные фото; "
                    "это не элементы дизайна, не рамки, не карточки, не маски и не пустые блоки.\n"
                    "Не ставь в эти зоны ничего важного: заголовки, текст, цифры, иконки, людей или главные фото.\n"
                    "Фон под этими зонами должен продолжаться естественно: тот же цвет, фактура бумаги или лёгкий декор.\n"
                    "Запрещены белые/светлые прямоугольники, пустые карточки, плейсхолдеры, рамки, тени, прозрачные дыры и любые видимые заготовки.\n"
                    "Если макет или стиль ниже упоминает главное фото, фото товара, людей, сцены, photo crop, image crop или зону под изображение, "
                    "это заполнят реальные фото кодом — модель не должна сама рисовать или подделывать эти фото.\n"
                    "На слайде можно оставить текст, тонкие линии, фактуру фона, мягкий свет и немного абстрактного декора выбранного стиля, но без дополнительных фотосюжетов.\n"
                    "Не копируй, не обводи и не усиливай уголки с референса; в итоге не должно остаться никаких следов разметки.\n"
                    "Всё вне уголков оформи по требованиям к слайду ниже.\n\n"
                )
            else:
                instruction = (
                    "Используй референс выше как образец стиля (палитра / шрифты / декор / настроение вёрстки) "
                    "и создай совершенно новый слайд с содержимым ниже, не копируя текст референса.\n\n"
                )

            content_parts.append({"type": "text", "text": instruction + full_prompt})
            user_content: Any = content_parts
        else:
            user_content = full_prompt

        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "user", "content": user_content}
            ],
            "stream": True,
            "temperature": 0.7,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": HTTP_USER_AGENT,
        }
        print(f"🔗 POST {url}  size={size}  stream=True")
        resp = requests.post(
            url, headers=headers, json=payload,
            stream=True, timeout=REQUEST_TIMEOUT_SECS,
        )
        print(f"📥 status={resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ошибка chat-запроса (status={resp.status_code}): {resp.text[:500]}"
            )

        full_text = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                import json as _json
                chunk = _json.loads(data_str)
            except Exception:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                full_text.append(content)
                # print progress live (truncated)
                snippet = content.replace("\n", " ").strip()
                if snippet:
                    print(f"  ↳ {snippet[:80]}")

        merged = "".join(full_text)
        found = self._extract_from_text(merged)
        if not found:
            raise RuntimeError(
                f"В потоковом ответе нет URL/base64 картинки. Полный текст: {merged[:500]}"
            )
        return found

    # ---------- endpoint 2: /v1/images/generations ----------

    def _request_via_images(self, prompt: str, size: str) -> str:
        url = openai_compatible_endpoint(self.base_url, "images/generations")
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": self.quality,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        }
        print(f"🔗 POST {url}  size={size}  quality={self.quality}")
        resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECS)
        print(f"📥 status={resp.status_code}")

        if resp.status_code != 200:
            raise RuntimeError(
                f"Ошибка images-запроса (status={resp.status_code}): {resp.text[:500]}"
            )

        result = resp.json()
        error = result.get("error") if isinstance(result, dict) else None
        if error:
            if isinstance(error, dict):
                error_type = str(error.get("type") or result.get("type") or "unknown")
                error_message = str(error.get("message") or error)
            else:
                error_type = str(result.get("type") or "unknown")
                error_message = str(error)
            detail = (
                f"Ошибка images-запроса (status=200, type={error_type}): "
                f"{error_message[:300]}"
            )
            normalized = f"{error_type} {error_message}".lower()
            if any(marker in normalized for marker in (
                "internal_error",
                "server_error",
                "service unavailable",
                "temporarily unavailable",
                "upstream timeout",
            )):
                raise TransientImageGenerationError(detail)
            raise RuntimeError(detail)

        data = result.get("data") or []
        if not data:
            raise RuntimeError(f"В ответе нет data: {str(result)[:300]}")
        first = data[0]
        b64 = first.get("b64_json")
        url_field = first.get("url")
        if b64:
            return f"data:image/png;base64,{b64}"
        if url_field:
            return url_field
        raise RuntimeError(f"В data[0] нет ни b64_json, ни url: {str(first)[:300]}")

    # ---------- public entry point ----------

    def generate_scene_image(
        self,
        scene_data: Dict[str, Any],
        output_path: str,
        size: str = "auto",
        reference_image_path: Optional[Union[str, List[str]]] = None,
    ) -> str:
        scene_index = scene_data.get("index", 0)
        prompt = scene_data.get("image_prompt", "")
        if not prompt:
            raise ValueError(f"Слайд {scene_index} — нет image_prompt")

        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        target_size = self.default_size if size == "auto" else size
        print(f"📝 prompt[:100]: {prompt[:100].replace(chr(10), ' ')}{'...' if len(prompt) > 100 else ''}")

        import time as _time

        # outer loop: regenerate on wrong aspect ratio (up to MAX_ASPECT_RETRIES)
        for ratio_attempt in range(MAX_ASPECT_RETRIES + 1):
            last_err = None
            # inner loop: retry transient errors (524 / timeouts)
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if self.endpoint == "images":
                        # images endpoint does not take references yet -> fall back to chat
                        if reference_image_path:
                            payload = self._request_via_chat(prompt, target_size, reference_image_path)
                        else:
                            payload = self._request_via_images(prompt, target_size)
                    elif self.endpoint == "chat":
                        payload = self._request_via_chat(prompt, target_size, reference_image_path)
                    else:  # auto
                        try:
                            if reference_image_path:
                                payload = self._request_via_chat(prompt, target_size, reference_image_path)
                            else:
                                payload = self._request_via_images(prompt, target_size)
                        except TransientImageGenerationError as e:
                            # Some OpenAI-compatible relays return HTTP 200 with an
                            # embedded internal_error while their image worker is in
                            # maintenance. Retrying Images first preserves the exact
                            # API contract and avoids an immediate, usually invalid,
                            # switch to chat/completions. On the last attempt, retain
                            # the historical auto-mode chat fallback.
                            if attempt < MAX_RETRIES:
                                raise
                            print(
                                "(!) images падает раз за разом, последняя попытка через chat: "
                                f"{str(e)[:120]}"
                            )
                            payload = self._request_via_chat(
                                prompt, target_size, reference_image_path
                            )
                        except Exception as e:
                            print(f"(!) images не сработал, перехожу на chat: {str(e)[:120]}")
                            payload = self._request_via_chat(prompt, target_size, reference_image_path)

                    self._save_payload(payload, output_path)
                    break  # saved, leave the transient retry loop
                except Exception as e:
                    last_err = e
                    msg = str(e)[:200]
                    transient = isinstance(e, TransientImageGenerationError) or any(
                        s in msg.lower()
                        for s in (
                            "524",
                            "502",
                            "503",
                            "504",
                            "timeout",
                            "read timed out",
                            "connection aborted",
                            "remotedisconnected",
                            "service unavailable",
                            "temporarily unavailable",
                        )
                    )
                    if attempt < MAX_RETRIES and transient:
                        print(f"(!) [scene {scene_index}] попытка {attempt} не удалась ({msg}), {RETRY_DELAY_SECS} с — повтор")
                        _time.sleep(RETRY_DELAY_SECS)
                        continue
                    raise
            else:
                raise RuntimeError(f"После {MAX_RETRIES} попыток всё ещё ошибка: {last_err}")

            # aspect ratio check
            w, h = read_png_dimensions(output_path)
            if aspect_acceptable(w, h, self.aspect_ratio):
                print(f"[OK] Сохранено: {output_path}  ({w}x{h}, соотношение {w/h:.3f}, цель {self.aspect_ratio})")
                return output_path

            # ratio is off
            actual_ratio = w / h if h else 0
            expected = ASPECT_RATIO_VALUES.get(self.aspect_ratio, 16/9)
            dev_pct = abs(actual_ratio - expected) / expected * 100 if expected else 0
            if ratio_attempt < MAX_ASPECT_RETRIES:
                print(f"📐 [scene {scene_index}] размер {w}x{h} (соотношение {actual_ratio:.3f}) "
                      f"отклонение от цели {self.aspect_ratio} {dev_pct:.0f}%, перегенерирую ({ratio_attempt+1}/{MAX_ASPECT_RETRIES})")
                try:
                    os.remove(output_path)
                except OSError:
                    pass
                continue
            else:
                print(f"(!) [scene {scene_index}] размер {w}x{h} всё ещё отклоняется на {dev_pct:.0f}%, лимит повторов исчерпан, оставляю")
                return output_path

        return output_path


if __name__ == "__main__":
    import sys
    gen = GptImage2Generator(aspect_ratio="16:9")
    gen.generate_scene_image(
        {"index": 0, "image_prompt": "A clean blue gradient background with the bold white text 'gpt-image-2 Test'"},
        "test_output.png",
    )
    print(f"Самопроверка пройдена: {Path('test_output.png').resolve()}")
    sys.exit(0)
