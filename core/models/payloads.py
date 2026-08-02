from __future__ import annotations

import time
from typing import Optional


def size_from_ratio(ratio: str, output_resolution: str = "2K") -> dict:
    level = (output_resolution or "2K").upper()
    if level == "1K":
        ratio_map = {
            "1:1": {"width": 1024, "height": 1024},
            "1:8": {"width": 384, "height": 3072},
            "1:4": {"width": 512, "height": 2048},
            "16:9": {"width": 1360, "height": 768},
            "9:16": {"width": 768, "height": 1360},
            "4:1": {"width": 2048, "height": 512},
            "4:3": {"width": 1152, "height": 864},
            "3:4": {"width": 864, "height": 1152},
            "8:1": {"width": 3072, "height": 384},
        }
    elif level == "4K":
        ratio_map = {
            "1:1": {"width": 4096, "height": 4096},
            "1:8": {"width": 1536, "height": 12288},
            "1:4": {"width": 2048, "height": 8192},
            "16:9": {"width": 5504, "height": 3072},
            "9:16": {"width": 3072, "height": 5504},
            "4:1": {"width": 8192, "height": 2048},
            "4:3": {"width": 4096, "height": 3072},
            "3:4": {"width": 3072, "height": 4096},
            "8:1": {"width": 12288, "height": 1536},
        }
    else:
        ratio_map = {
            "1:1": {"width": 2048, "height": 2048},
            "1:8": {"width": 768, "height": 6144},
            "1:4": {"width": 1024, "height": 4096},
            "16:9": {"width": 2752, "height": 1536},
            "9:16": {"width": 1536, "height": 2752},
            "4:1": {"width": 4096, "height": 1024},
            "4:3": {"width": 2048, "height": 1536},
            "3:4": {"width": 1536, "height": 2048},
            "8:1": {"width": 6144, "height": 768},
        }
    return ratio_map.get(ratio, ratio_map["16:9"])


def gpt_image_pixels_from_ratio(ratio: str, output_resolution: str = "2K") -> Optional[dict]:
    level = str(output_resolution or "2K").upper()
    if level == "1K":
        ratio_map = {
            "1:1": {"width": 1024, "height": 1024},
            "5:4": {"width": 1120, "height": 896},
            "9:16": {"width": 720, "height": 1280},
            "21:9": {"width": 1456, "height": 624},
            "16:9": {"width": 1280, "height": 720},
            "4:3": {"width": 1152, "height": 864},
            "3:2": {"width": 1248, "height": 832},
            "4:5": {"width": 896, "height": 1120},
            "3:4": {"width": 864, "height": 1152},
            "2:3": {"width": 832, "height": 1248},
        }
    elif level == "4K":
        ratio_map = {
            "1:1": {"width": 2880, "height": 2880},
            "5:4": {"width": 3200, "height": 2560},
            "9:16": {"width": 2160, "height": 3840},
            "21:9": {"width": 3696, "height": 1584},
            "16:9": {"width": 3840, "height": 2160},
            "4:3": {"width": 3264, "height": 2448},
            "3:2": {"width": 3504, "height": 2336},
            "4:5": {"width": 2560, "height": 3200},
            "3:4": {"width": 2448, "height": 3264},
            "2:3": {"width": 2336, "height": 3504},
        }
    else:
        ratio_map = {
            "1:1": {"width": 2048, "height": 2048},
            "5:4": {"width": 2240, "height": 1792},
            "9:16": {"width": 1440, "height": 2560},
            "21:9": {"width": 3024, "height": 1296},
            "16:9": {"width": 2560, "height": 1440},
            "4:3": {"width": 2304, "height": 1728},
            "3:2": {"width": 2496, "height": 1664},
            "4:5": {"width": 1792, "height": 2240},
            "3:4": {"width": 1728, "height": 2304},
            "2:3": {"width": 1664, "height": 2496},
        }
    return ratio_map.get(ratio)


def gpt_image_size_string(size: Optional[dict]) -> str:
    if not isinstance(size, dict):
        raise ValueError("gpt-image size is required")
    width = int(size.get("width") or 0)
    height = int(size.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("gpt-image size must be positive")
    return f"{width}x{height}"


def parse_gpt_image_size(value: Optional[str]) -> dict:
    raw = str(value or "auto").strip().lower()
    if raw == "auto":
        return {"width": 1024, "height": 1024}

    parts = raw.split("x")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("size must be 'WIDTHxHEIGHT' or 'auto'")

    width, height = (int(part) for part in parts)
    long_edge, short_edge = max(width, height), min(width, height)
    pixels = width * height
    if long_edge > 3840:
        raise ValueError("size edge must not exceed 3840px")
    if width % 16 or height % 16:
        raise ValueError("size edges must be multiples of 16px")
    if short_edge <= 0 or long_edge > short_edge * 3:
        raise ValueError("size aspect ratio must not exceed 3:1")
    if not 655_360 <= pixels <= 8_294_400:
        raise ValueError("size total pixels must be between 655360 and 8294400")
    return {"width": width, "height": height}


def parse_gpt_image_n(value: object = None) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("n must be an integer between 1 and 10")
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("n must be an integer between 1 and 10")
    if not 1 <= count <= 10:
        raise ValueError("n must be an integer between 1 and 10")
    return count


def gpt_image_detail_level(output_resolution: str) -> int:
    return 1


def gpt_image_detail_level_from_quality(quality_level: Optional[str]) -> int:
    quality = str(quality_level or "low").strip().lower()
    if quality in {"medium", "high"}:
        return 3
    return 1


def build_image_payload_candidates(
    *,
    prompt: str,
    aspect_ratio: str,
    output_resolution: str,
    upstream_model_id: str,
    upstream_model_version: str,
    quality_level: Optional[str] = None,
    detail_level: Optional[int] = None,
    source_image_ids: Optional[list[str]] = None,
    pixel_size: Optional[dict] = None,
    n: int = 1,
) -> list[dict]:
    normalized_ratio = str(aspect_ratio or "").strip().lower()
    effective_ratio = normalized_ratio or "1:1"
    if str(upstream_model_id or "").strip().lower() == "gpt-image":
        effective_detail_level = detail_level
        if effective_detail_level is None:
            effective_detail_level = gpt_image_detail_level_from_quality(quality_level)
        explicit_pixel_size = pixel_size is not None
        if pixel_size is None:
            pixel_size = gpt_image_pixels_from_ratio(effective_ratio, output_resolution)
        if pixel_size is None:
            raise ValueError(f"unsupported gpt-image ratio: {effective_ratio}")
        seed = int(time.time()) % 999999
        base_payload = {
            "modelId": upstream_model_id,
            "modelVersion": upstream_model_version,
            "n": int(n),
            "prompt": prompt,
            "seeds": [(seed + index) % 999999 for index in range(int(n))],
            "output": {"storeInputs": True},
            "referenceBlobs": [],
            "generationMetadata": {
                "module": "text2image",
                "submodule": "ff-image-generate",
            },
            "modelSpecificPayload": {},
            "generationSettings": {
                "detailLevel": int(effective_detail_level),
            },
        }
        base_payload["size"] = pixel_size
        if not explicit_pixel_size:
            base_payload["outputResolution"] = str(output_resolution or "2K").upper()
        if not source_image_ids:
            return [base_payload]

        subject_reference = dict(base_payload)
        subject_reference["referenceBlobs"] = [
            {"id": img_id, "usage": "subject"} for img_id in source_image_ids
        ]
        subject_reference["modelSpecificPayload"] = {}
        if explicit_pixel_size:
            subject_reference.pop("outputResolution", None)

        reference_image = dict(base_payload)
        reference_image["generationMetadata"] = {
            "module": "image2image",
            "submodule": "ff-image-generate",
        }
        reference_image["referenceBlobs"] = []
        reference_image["referenceImages"] = [
            {"id": img_id} for img_id in source_image_ids
        ]

        local_blob_reference = dict(reference_image)
        local_blob_reference["referenceImages"] = [
            {"localBlobRef": img_id} for img_id in source_image_ids
        ]
        return [subject_reference, reference_image, local_blob_reference]

    base_payload = {
        "modelId": upstream_model_id,
        "modelVersion": upstream_model_version,
        "n": 1,
        "prompt": prompt,
        "size": size_from_ratio(effective_ratio, output_resolution),
        "seeds": [int(time.time()) % 999999],
        "groundSearch": False,
        "skipCai": False,
        "output": {"storeInputs": True},
        "generationMetadata": {
            "module": "text2image",
            "submodule": "ff-image-generate",
        },
        "modelSpecificPayload": {
            "parameters": {"addWatermark": False},
        },
    }
    if normalized_ratio and normalized_ratio != "auto":
        base_payload["modelSpecificPayload"]["aspectRatio"] = normalized_ratio

    if not source_image_ids:
        base_payload["referenceBlobs"] = []
        return [base_payload]

    edited = dict(base_payload)
    edited["generationMetadata"] = {
        "module": "image2image",
        "submodule": "ff-image-generate",
    }
    edited["referenceBlobs"] = [
        {"id": img_id, "usage": "general"} for img_id in source_image_ids
    ]
    return [edited]
