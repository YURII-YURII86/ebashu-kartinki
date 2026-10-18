"""Raster layer extraction and overlap strategy routing."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True)
class LayerMetrics:
    mask_confidence: float
    edge_contamination: float
    occlusion_ratio: float

    def __post_init__(self) -> None:
        for name, value in (
            ("mask_confidence", self.mask_confidence),
            ("edge_contamination", self.edge_contamination),
            ("occlusion_ratio", self.occlusion_ratio),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} должно быть от 0 до 1")


def extract_rgba_layer(source: Image.Image, mask: Image.Image) -> Image.Image:
    if source.size != mask.size:
        raise ValueError("размеры исходника и layer mask должны совпадать")
    layer = source.convert("RGBA")
    layer.putalpha(mask.convert("L"))
    return layer


def paste_layer(background: Image.Image, layer: Image.Image, position: tuple[int, int]) -> Image.Image:
    result = background.convert("RGBA").copy()
    result.alpha_composite(layer.convert("RGBA"), dest=position)
    return result


def choose_layer_strategy(metrics: LayerMetrics, design_mode: bool = False) -> str:
    if design_mode:
        return "ai_regenerate"
    if metrics.mask_confidence < 0.90 or metrics.edge_contamination > 0.08:
        return "ai_regenerate"
    if metrics.occlusion_ratio > 0.20:
        return "occlusion_complete"
    return "direct_extract"


def build_ai_separation_prompt(object_description: str, scene_context: str) -> str:
    return (
        f"Отдели и восстанови один объект с исходного слайда: {object_description}."
        f"Исходное положение и перекрытия: {scene_context}."
        "Сохрани форму, контур, перспективу, материал, цвет, относительный размер и направление света объекта; "
        "дорисуй закрытые части, но не перерисовывай дизайн, не добавляй других объектов, текста, цифр или логотипов."
        "Выведи один объект на фоне, удобном для последующего вырезания."
    )
