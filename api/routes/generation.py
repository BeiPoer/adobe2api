import base64
import re
import secrets
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from api.schemas import GenerateRequest
from core.entity_store import entity_store
from core.models.payloads import (
    gpt_image_detail_level_from_quality,
    parse_gpt_image_n,
    parse_gpt_image_size,
)


SEEDANCE_MODEL_ID = "doubao-seedance-2-0-260128"
SEEDANCE_NEGATIVE_PROMPT = "cartoon, vector art, & bad aesthetics & poor aesthetic"


def _parse_seedance_request(data: dict) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    if str(data.get("model") or "").strip() != SEEDANCE_MODEL_ID:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported model; use {SEEDANCE_MODEL_ID}",
        )

    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise HTTPException(status_code=400, detail="content is required")

    prompt_parts: list[str] = []
    frames: dict[str, dict] = {}
    unassigned_frames: list[dict] = []
    for item in content:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="content items must be objects")
        item_type = str(item.get("type") or "").strip()
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                prompt_parts.append(text)
            continue
        if item_type != "image_url":
            raise HTTPException(
                status_code=400,
                detail=f"unsupported content type: {item_type or 'empty'}",
            )

        image_url = item.get("image_url")
        url = (
            str(image_url.get("url") or "").strip()
            if isinstance(image_url, dict)
            else str(image_url or "").strip()
        )
        if not url:
            raise HTTPException(status_code=400, detail="image_url.url is required")
        role = str(item.get("role") or "").strip()
        if role not in {"", "first_frame", "last_frame"}:
            raise HTTPException(status_code=400, detail=f"unsupported image role: {role}")
        normalized = {
            "type": "image_url",
            "image_url": {"url": url},
            "role": role or "first_frame",
        }
        if not role:
            unassigned_frames.append(normalized)
        elif role in frames:
            raise HTTPException(status_code=400, detail=f"duplicate image role: {role}")
        else:
            frames[role] = normalized

    if unassigned_frames:
        if frames or len(unassigned_frames) != 1:
            raise HTTPException(
                status_code=400,
                detail="role is required when using first and last frame images",
            )
        frames["first_frame"] = unassigned_frames[0]
    if "last_frame" in frames and "first_frame" not in frames:
        raise HTTPException(status_code=400, detail="last_frame requires first_frame")
    if len(frames) > 2:
        raise HTTPException(status_code=400, detail="at most two frame images are supported")

    prompt = "\n".join(prompt_parts).strip()
    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="a non-empty text prompt is required by the Adobe upstream",
        )

    resolution = str(data.get("resolution") or "480p").strip().lower()
    ratio = str(data.get("ratio") or "16:9").strip()
    duration = data.get("duration", 5)
    if resolution != "480p":
        raise HTTPException(status_code=400, detail="only resolution=480p is supported")
    if ratio != "16:9":
        raise HTTPException(status_code=400, detail="only ratio=16:9 is supported")
    if isinstance(duration, bool) or duration != 5:
        raise HTTPException(status_code=400, detail="only duration=5 is supported")

    seed = data.get("seed", -1)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise HTTPException(status_code=400, detail="seed must be an integer")
    if not -1 <= seed <= 2147483647:
        raise HTTPException(status_code=400, detail="seed is out of range")
    if seed == -1:
        seed = secrets.randbelow(999999)

    generate_audio = data.get("generate_audio", True)
    if not isinstance(generate_audio, bool):
        raise HTTPException(status_code=400, detail="generate_audio must be boolean")
    watermark = data.get("watermark", False)
    if not isinstance(watermark, bool):
        raise HTTPException(status_code=400, detail="watermark must be boolean")
    if watermark:
        raise HTTPException(status_code=400, detail="watermark=true is not supported")

    unsupported = sorted(
        set(data)
        & {
            "camera_fixed",
            "draft",
            "execution_expires_after",
            "frames",
            "priority",
            "return_last_frame",
            "service_tier",
            "tools",
        }
    )
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported parameter: {unsupported[0]}",
        )

    return {
        "model": SEEDANCE_MODEL_ID,
        "prompt": prompt,
        "images": [
            frames[role]
            for role in ("first_frame", "last_frame")
            if role in frames
        ],
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "seed": seed,
        "generate_audio": generate_audio,
    }


def build_generation_router(
    *,
    store,
    token_manager,
    client,
    generated_dir: Path,
    model_catalog: dict,
    video_model_catalog: dict,
    supported_ratios: set,
    resolve_model: Callable[[str | None], dict],
    resolve_ratio_and_resolution: Callable[[dict, str | None], tuple[str, str, str]],
    require_service_api_key: Callable[[Request], None],
    set_request_task_progress: Callable[..., None],
    run_with_token_retries: Callable[..., Any],
    set_request_error_detail: Callable[..., str],
    set_request_preview: Callable[[Request, str, str], None],
    public_image_url: Callable[[Request, str], str],
    public_generated_url: Callable[[Request, str], str],
    resolve_video_options: Callable[[dict], tuple[bool, str, str]],
    load_input_images: Callable[[Any], list[tuple[bytes, str]]],
    prepare_video_source_image: Callable[[bytes, str, str], tuple[bytes, str]],
    video_ext_from_meta: Callable[[dict], str],
    extract_prompt_from_messages: Callable[[Any], str],
    sse_chat_stream: Callable[[dict], Any],
    on_generated_file_written: Callable[[Path, int, int], None],
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    logger,
) -> APIRouter:
    router = APIRouter()
    entity_ref_re = re.compile(r"@entity:([^\s@]+)")

    def _nanoid(size: int = 21) -> str:
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
        return "".join(secrets.choice(alphabet) for _ in range(size))

    def _entity_name(item: dict) -> str:
        entity_value = item.get("entityValue")
        if isinstance(entity_value, dict):
            name = str(entity_value.get("displayName") or "").strip()
            if name:
                return name
        return str(item.get("name") or item.get("displayName") or "").strip()

    def _entity_urn(item: dict) -> str:
        for key in ("id", "urn", "entityId", "entityUrn"):
            val = str(item.get(key) or "").strip()
            if val:
                return val
        entity = item.get("entity")
        if isinstance(entity, dict):
            return _entity_urn(entity)
        return ""

    def _entity_names_from_prompt(raw_prompt: str) -> list[str]:
        matches = list(entity_ref_re.finditer(raw_prompt or ""))
        names: list[str] = []
        for match in matches:
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
        return names

    def _sync_entity_by_name(name: str) -> list[dict]:
        found: list[dict] = []
        for token_info in token_manager.list_active_account_tokens():
            token = str(token_info.get("token") or "").strip()
            account_id = str(token_info.get("account_id") or "").strip()
            if not token or not account_id:
                continue
            try:
                entities = client.list_entities(token, limit=100)
            except Exception:
                continue
            for item in entities:
                item_name = _entity_name(item)
                if item_name != name:
                    continue
                urn = _entity_urn(item)
                if not urn:
                    continue
                found.append(
                    entity_store.upsert(
                        entity_id=urn,
                        name=item_name,
                        entity_type=str(item.get("entityType") or item.get("type") or ""),
                        account_id=account_id,
                        account_name=str(token_info.get("account_name") or ""),
                        account_email=str(token_info.get("account_email") or ""),
                    )
                )
        return found

    def _resolve_entity_bindings(raw_prompt: str) -> tuple[str, list[dict]]:
        refs: list[dict] = []
        account_id = ""
        for name in _entity_names_from_prompt(raw_prompt):
            matches = entity_store.find_by_name(name)
            if not matches:
                matches = _sync_entity_by_name(name)
            account_ids = {
                str(item.get("account_id") or "").strip()
                for item in matches
                if str(item.get("account_id") or "").strip()
            }
            if not matches:
                raise HTTPException(status_code=400, detail=f"entity not found: {name}")
            if len(account_ids) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"entity name is ambiguous across accounts: {name}",
                )
            if len(matches) > 1 and len({str(item.get("id") or "") for item in matches}) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"entity name is ambiguous: {name}",
                )
            current_account = next(iter(account_ids), "")
            if not current_account:
                raise HTTPException(status_code=400, detail=f"entity has no account: {name}")
            if account_id and account_id != current_account:
                raise HTTPException(
                    status_code=400,
                    detail="entities in one prompt must belong to the same Adobe account",
                )
            account_id = current_account
            refs.append(
                {
                    "name": name,
                    "urn": str(matches[0].get("id") or "").strip(),
                    "account_id": account_id,
                }
            )
        return account_id, refs

    def _resolve_kling_entity_refs(
        token: str,
        raw_prompt: str,
        bound_refs: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        matches = list(entity_ref_re.finditer(raw_prompt or ""))
        if not matches:
            return raw_prompt, []
        if bound_refs is not None:
            by_name = {str(item.get("name") or "").strip(): item for item in bound_refs}
        else:
            entities = client.list_entities(token, limit=100)
            by_name = {_entity_name(item): item for item in entities if _entity_name(item)}
        refs: list[dict] = []
        replacements: dict[str, str] = {}
        for match in matches:
            name = match.group(1).strip()
            if name in replacements:
                continue
            item = by_name.get(name)
            if not item:
                raise HTTPException(status_code=400, detail=f"entity not found: {name}")
            urn = str(item.get("urn") or "").strip() if bound_refs is not None else _entity_urn(item)
            if not urn:
                raise HTTPException(status_code=400, detail=f"entity has no urn: {name}")
            mention_id = _nanoid()
            replacements[name] = mention_id
            refs.append({"name": name, "urn": urn, "mention_id": mention_id})

        def replace_match(match: re.Match) -> str:
            return f"@{replacements[match.group(1).strip()]}"

        return entity_ref_re.sub(replace_match, raw_prompt), refs

    def _invalid_image_request(message: str) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": str(message),
                    "type": "invalid_request_error",
                }
            },
        )

    def _gpt_api_options(
        data: dict, resolved_model_id: str, model_conf: dict
    ) -> tuple[dict | None, int | None, str, int]:
        detail_level = model_conf.get("detail_level")
        if resolved_model_id != "gpt-image-2":
            return None, detail_level, "url", 1

        pixel_size = parse_gpt_image_size(data.get("size"))
        quality = str(data.get("quality") or "medium").strip().lower()
        if quality not in {"auto", "low", "medium", "high"}:
            raise ValueError("quality must be one of: auto, low, medium, high")
        if quality == "auto":
            quality = "medium"
        detail_level = gpt_image_detail_level_from_quality(quality)
        response_format = str(
            data.get("response_format") or "b64_json"
        ).strip().lower()
        if response_format not in {"url", "b64_json"}:
            raise ValueError("response_format must be one of: url, b64_json")
        n = parse_gpt_image_n(data.get("n"))
        return pixel_size, detail_level, response_format, n

    @router.get("/v1/models")
    def list_models(request: Request):
        require_service_api_key(request)
        data = []
        for model_id, conf in model_catalog.items():
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "adobe2api",
                    "description": conf["description"],
                }
            )
        for model_id, conf in video_model_catalog.items():
            if bool(conf.get("hidden", False)):
                continue
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "adobe2api",
                    "description": conf["description"],
                }
            )
        return {"object": "list", "data": data}

    @router.post("/v1/images/generations")
    def openai_generate(data: dict, request: Request):
        require_service_api_key(request)

        prompt = data.get("prompt", "").strip()
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "prompt is required",
                        "type": "invalid_request_error",
                    }
                },
            )

        model_id = data.get("model")
        if str(model_id or "").strip() in video_model_catalog:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Use /v1/chat/completions for video generation",
                        "type": "invalid_request_error",
                    }
                },
            )
        ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
            data, model_id
        )
        model_conf = resolve_model(resolved_model_id)
        try:
            pixel_size, detail_level, response_format, n = _gpt_api_options(
                data, resolved_model_id, model_conf
            )
        except ValueError as exc:
            return _invalid_image_request(str(exc))

        try:
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                def _image_progress_cb(update: dict):
                    set_request_task_progress(
                        request,
                        task_status=str(update.get("task_status") or "IN_PROGRESS"),
                        task_progress=update.get("task_progress"),
                        upstream_job_id=update.get("upstream_job_id"),
                        retry_after=update.get("retry_after"),
                        error=update.get("error"),
                    )

                job_id = uuid.uuid4().hex
                out_path = (
                    generated_dir / f"{job_id}.png"
                    if response_format == "url" and n == 1
                    else None
                )
                old_size = 0
                try:
                    if out_path is not None and out_path.exists():
                        old_size = int(out_path.stat().st_size)
                except Exception:
                    old_size = 0

                image_bytes, _meta = client.generate(
                    token=token,
                    prompt=prompt,
                    aspect_ratio=ratio,
                    output_resolution=output_resolution,
                    upstream_model_id=str(
                        model_conf.get("upstream_model_id") or "gemini-flash"
                    ),
                    upstream_model_version=str(
                        model_conf.get("upstream_model_version") or "nano-banana-2"
                    ),
                    quality_level=(
                        client.gpt_image_quality
                        if str(model_conf.get("upstream_model_id") or "") == "gpt-image"
                        and resolved_model_id != "gpt-image-2"
                        else None
                    ),
                    detail_level=detail_level,
                    pixel_size=pixel_size,
                    n=n,
                    timeout=client.generate_timeout,
                    out_path=out_path,
                    progress_cb=_image_progress_cb,
                )
                if response_format == "b64_json":
                    images = image_bytes if isinstance(image_bytes, list) else [image_bytes]
                    if not images or any(image is None for image in images):
                        raise RuntimeError("image generation returned no image data")
                    return {
                        "created": int(time.time()),
                        "model": resolved_model_id,
                        "data": [
                            {"b64_json": base64.b64encode(image).decode("ascii")}
                            for image in images
                        ],
                    }

                if n > 1:
                    images = image_bytes if isinstance(image_bytes, list) else [image_bytes]
                    if len(images) != n or any(image is None for image in images):
                        raise RuntimeError("image generation returned incomplete image data")
                    image_urls = []
                    for index, image in enumerate(images, start=1):
                        image_id = f"{job_id}-{index}"
                        image_path = generated_dir / f"{image_id}.png"
                        image_path.write_bytes(image)
                        on_generated_file_written(image_path, 0, len(image))
                        image_urls.append(public_image_url(request, image_id))
                    set_request_preview(request, image_urls[0], kind="image")
                    return {
                        "created": int(time.time()),
                        "model": resolved_model_id,
                        "data": [{"url": image_url} for image_url in image_urls],
                    }

                if image_bytes is not None and out_path is not None:
                    out_path.write_bytes(image_bytes)
                if out_path is None:
                    raise RuntimeError("image generation returned no output path")
                new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                on_generated_file_written(out_path, old_size, new_size)
                image_url = public_image_url(request, job_id)
                set_request_preview(request, image_url, kind="image")
                return {
                    "created": int(time.time()),
                    "model": resolved_model_id,
                    "data": [{"url": image_url}],
                }

            return run_with_token_retries(
                request=request,
                operation_name="images.generations",
                run_once=_run_once,
            )

        except quota_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token quota exhausted",
                status_code=429,
                error_type="rate_limit_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token quota exhausted",
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Token quota exhausted",
                        "type": "rate_limit_error",
                        "code": error_code,
                    }
                },
            )
        except auth_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token invalid or expired",
                status_code=401,
                error_type="authentication_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token invalid or expired",
            )
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Token invalid or expired",
                        "type": "authentication_error",
                        "code": error_code,
                    }
                },
            )
        except upstream_temp_error_cls as exc:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=exc,
                status_code=503,
                error_type="server_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )
        except HTTPException as exc:
            err_type = (
                "invalid_request_error"
                if 400 <= int(exc.status_code) < 500
                else "server_error"
            )
            error_code = set_request_error_detail(
                request,
                error=str(exc.detail),
                status_code=exc.status_code,
                error_type=err_type,
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc.detail)
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": err_type,
                        "code": error_code,
                    }
                },
            )
        except Exception as exc:
            error_code = set_request_error_detail(
                request,
                error=exc,
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            logger.exception(
                "Unhandled error in /v1/images/generations log_id=%s model=%s",
                getattr(request.state, "log_id", ""),
                resolved_model_id,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )

    @router.post("/v1/images/edits")
    def openai_edit(
        request: Request,
        prompt: str = Form(""),
        model: str = Form("gpt-image-2"),
        size: str = Form("auto"),
        quality: str = Form("medium"),
        n: int = Form(1),
        image: list[UploadFile] = File(default_factory=list),
        image_array: list[UploadFile] = File(default_factory=list, alias="image[]"),
        mask: UploadFile | None = File(None),
    ):
        require_service_api_key(request)

        prompt = str(prompt or "").strip()
        if not prompt:
            return _invalid_image_request("prompt is required")
        if mask is not None:
            return _invalid_image_request("mask is not supported")

        uploads = [*image, *image_array]
        if not uploads:
            return _invalid_image_request("image is required")
        if len(uploads) > 16:
            return _invalid_image_request("at most 16 input images are supported")

        model_id = str(model or "gpt-image-2").strip() or "gpt-image-2"
        if model_id not in model_catalog:
            return _invalid_image_request(f"Invalid model: {model_id}")
        model_conf = resolve_model(model_id)
        if str(model_conf.get("upstream_model_id") or "") != "gpt-image":
            return _invalid_image_request("/v1/images/edits supports GPT Image models only")

        data = {
            "model": model_id,
            "size": size,
            "quality": quality,
            "n": n,
            "response_format": "b64_json",
        }
        ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
            data, model_id
        )
        try:
            pixel_size, detail_level, _response_format, n = _gpt_api_options(
                data, resolved_model_id, model_conf
            )
        except ValueError as exc:
            return _invalid_image_request(str(exc))

        input_images: list[tuple[bytes, str]] = []
        allowed_mime_types = {"image/jpeg", "image/png", "image/webp"}
        for upload in uploads:
            mime_type = str(upload.content_type or "image/jpeg").split(";", 1)[0].lower()
            if mime_type == "image/jpg":
                mime_type = "image/jpeg"
            if mime_type not in allowed_mime_types:
                return _invalid_image_request(f"unsupported image type: {mime_type}")
            image_bytes = upload.file.read(50 * 1024 * 1024 + 1)
            if not image_bytes:
                return _invalid_image_request("image is empty")
            if len(image_bytes) > 50 * 1024 * 1024:
                return _invalid_image_request("image too large, max 50MB")
            input_images.append((image_bytes, mime_type))

        try:
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                source_image_ids = [
                    client.upload_image(token, image_bytes, mime_type)
                    for image_bytes, mime_type in input_images
                ]

                def _image_progress_cb(update: dict):
                    set_request_task_progress(
                        request,
                        task_status=str(update.get("task_status") or "IN_PROGRESS"),
                        task_progress=update.get("task_progress"),
                        upstream_job_id=update.get("upstream_job_id"),
                        retry_after=update.get("retry_after"),
                        error=update.get("error"),
                    )

                image_bytes, _meta = client.generate(
                    token=token,
                    prompt=prompt,
                    aspect_ratio=ratio,
                    output_resolution=output_resolution,
                    upstream_model_id=str(model_conf.get("upstream_model_id")),
                    upstream_model_version=str(model_conf.get("upstream_model_version")),
                    quality_level=(
                        client.gpt_image_quality
                        if resolved_model_id != "gpt-image-2"
                        else None
                    ),
                    detail_level=detail_level,
                    source_image_ids=source_image_ids,
                    pixel_size=pixel_size,
                    n=n,
                    timeout=client.generate_timeout,
                    progress_cb=_image_progress_cb,
                )
                images = image_bytes if isinstance(image_bytes, list) else [image_bytes]
                if not images or any(image is None for image in images):
                    raise RuntimeError("image edit returned no image data")
                return {
                    "created": int(time.time()),
                    "model": resolved_model_id,
                    "data": [
                        {"b64_json": base64.b64encode(image).decode("ascii")}
                        for image in images
                    ],
                }

            return run_with_token_retries(
                request=request,
                operation_name="images.edits",
                run_once=_run_once,
            )
        except HTTPException as exc:
            error_code = set_request_error_detail(
                request,
                error=str(exc.detail),
                status_code=exc.status_code,
                error_type=(
                    "invalid_request_error"
                    if 400 <= int(exc.status_code) < 500
                    else "server_error"
                ),
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc.detail)
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": (
                            "invalid_request_error"
                            if 400 <= int(exc.status_code) < 500
                            else "server_error"
                        ),
                        "code": error_code,
                    }
                },
            )
        except Exception as exc:
            error_code = set_request_error_detail(
                request,
                error=exc,
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            logger.exception(
                "Unhandled error in /v1/images/edits log_id=%s model=%s",
                getattr(request.state, "log_id", ""),
                resolved_model_id,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )

    @router.post("/api/v3/contents/generations/tasks")
    def create_seedance_task(data: dict, request: Request):
        require_service_api_key(request)
        options = _parse_seedance_request(data)
        input_images = load_input_images(
            [{"role": "user", "content": options["images"]}]
        )
        prepared_images = [
            prepare_video_source_image(image_bytes, options["ratio"], "480p")
            for image_bytes, _mime_type in input_images
        ]

        job = store.create(
            prompt=options["prompt"], aspect_ratio=options["ratio"]
        )
        store.update(
            job.id,
            model=options["model"],
            resolution=options["resolution"],
            duration=options["duration"],
            seed=options["seed"],
            generate_audio=options["generate_audio"],
        )

        def runner(job_id: str):
            store.update(job_id, status="running", progress=0.0)
            max_attempts = client.retry_max_attempts if client.retry_enabled else 1
            max_attempts = max(1, int(max_attempts))
            last_error = "No active tokens available in the pool."

            for attempt in range(1, max_attempts + 1):
                token = token_manager.get_available(
                    strategy=client.token_rotation_strategy
                )
                if not token:
                    break

                try:
                    source_image_ids = [
                        client.upload_image(
                            token,
                            image_bytes,
                            mime_type,
                            firefly=True,
                        )
                        for image_bytes, mime_type in prepared_images
                    ]
                    tmp_path = generated_dir / f"{job_id}.video.tmp"

                    def progress_cb(update: dict):
                        store.update(
                            job_id,
                            progress=float(update.get("task_progress") or 0.0),
                            upstream_job_id=str(
                                update.get("upstream_job_id") or ""
                            ),
                        )

                    video_bytes, video_meta = client.generate_video(
                        token=token,
                        video_conf={"engine": "seedance2", "resolution": "480p"},
                        prompt=options["prompt"],
                        aspect_ratio=options["ratio"],
                        duration=options["duration"],
                        source_image_ids=source_image_ids,
                        timeout=max(int(client.generate_timeout), 600),
                        negative_prompt=SEEDANCE_NEGATIVE_PROMPT,
                        generate_audio=options["generate_audio"],
                        seed=options["seed"],
                        out_path=tmp_path,
                        progress_cb=progress_cb,
                    )
                    filename = f"{job_id}.{video_ext_from_meta(video_meta)}"
                    out_path = generated_dir / filename
                    old_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    if video_bytes is not None:
                        out_path.write_bytes(video_bytes)
                    elif tmp_path.exists():
                        tmp_path.replace(out_path)
                    else:
                        raise RuntimeError(
                            "video generation finished without an output file"
                        )
                    new_size = int(out_path.stat().st_size)
                    on_generated_file_written(out_path, old_size, new_size)
                    token_manager.report_success(token)
                    store.update(
                        job_id,
                        status="succeeded",
                        progress=100.0,
                        image_url=public_generated_url(request, filename),
                    )
                    return
                except quota_error_cls:
                    token_manager.report_exhausted(token)
                    last_error = "Token quota exhausted."
                    retryable = attempt < max_attempts
                except auth_error_cls:
                    token_manager.handle_auth_failure(token)
                    last_error = "Token invalid or expired."
                    retryable = attempt < max_attempts
                except upstream_temp_error_cls as exc:
                    last_error = str(exc)
                    retryable = attempt < max_attempts and (
                        getattr(exc, "status_code", None) == 408
                        or client.should_retry_temporary_error(exc)
                    )
                except Exception as exc:
                    store.update(job_id, status="failed", error=str(exc))
                    return

                if not retryable:
                    break
                delay = client._retry_delay_for_attempt(attempt)
                if delay > 0:
                    time.sleep(delay)

            store.update(job_id, status="failed", error=last_error)

        threading.Thread(target=runner, args=(job.id,), daemon=True).start()
        return {"id": job.id}

    @router.get("/api/v3/contents/generations/tasks/{task_id}")
    def get_seedance_task(task_id: str, request: Request):
        require_service_api_key(request)
        job = store.get(task_id)
        if not job or getattr(job, "model", "") != SEEDANCE_MODEL_ID:
            raise HTTPException(status_code=404, detail="task not found")

        error = None
        if job.error:
            error = {"code": "InternalServiceError", "message": job.error}
        return {
            "id": job.id,
            "model": job.model,
            "status": job.status,
            "error": error,
            "created_at": int(job.created_at),
            "updated_at": int(job.updated_at),
            "content": {"video_url": job.image_url} if job.image_url else None,
            "seed": job.seed,
            "resolution": job.resolution,
            "ratio": job.aspect_ratio,
            "duration": job.duration,
            "frames": job.duration * 24 + 1,
            "framespersecond": 24,
            "generate_audio": job.generate_audio,
        }

    @router.post("/api/v1/generate")
    def create_job(data: GenerateRequest, request: Request):
        require_service_api_key(request)

        prompt = data.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt cannot be empty")

        ratio = data.aspect_ratio.strip() or "16:9"
        if ratio not in supported_ratios:
            raise HTTPException(status_code=400, detail="unsupported aspect ratio")

        output_resolution = (data.output_resolution or "2K").upper()
        if output_resolution not in {"1K", "2K", "4K"}:
            raise HTTPException(status_code=400, detail="unsupported output_resolution")

        model_conf = resolve_model(data.model)
        if data.model:
            output_resolution = model_conf["output_resolution"]

        job = store.create(prompt=prompt, aspect_ratio=ratio)

        def runner(job_id: str):
            store.update(job_id, status="running", progress=5.0)
            max_attempts = client.retry_max_attempts if client.retry_enabled else 1
            max_attempts = max(1, int(max_attempts))
            last_error = "No active tokens available in the pool"

            for attempt in range(1, max_attempts + 1):
                token = token_manager.get_available(
                    strategy=client.token_rotation_strategy
                )
                if not token:
                    break

                try:
                    out_path = generated_dir / f"{job_id}.png"
                    old_size = 0
                    try:
                        if out_path.exists():
                            old_size = int(out_path.stat().st_size)
                    except Exception:
                        old_size = 0

                    image_bytes, meta = client.generate(
                        token=token,
                        prompt=prompt,
                        aspect_ratio=ratio,
                        output_resolution=output_resolution,
                        upstream_model_id=str(
                            model_conf.get("upstream_model_id") or "gemini-flash"
                        ),
                        upstream_model_version=str(
                            model_conf.get("upstream_model_version") or "nano-banana-2"
                        ),
                        quality_level=(
                            client.gpt_image_quality
                            if str(model_conf.get("upstream_model_id") or "") == "gpt-image"
                            else None
                        ),
                        detail_level=model_conf.get("detail_level"),
                        out_path=out_path,
                    )
                    if image_bytes is not None:
                        out_path.write_bytes(image_bytes)
                    new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    on_generated_file_written(out_path, old_size, new_size)
                    progress = float(meta.get("progress") or 100.0)
                    image_url = public_image_url(request, job_id)
                    store.update(
                        job_id,
                        status="succeeded",
                        progress=max(progress, 100.0),
                        image_url=image_url,
                    )
                    return
                except quota_error_cls:
                    token_manager.report_exhausted(token)
                    last_error = "Token quota exhausted."
                    retryable = attempt < max_attempts
                except auth_error_cls:
                    token_manager.report_invalid(token)
                    last_error = "Token invalid or expired."
                    retryable = attempt < max_attempts
                except upstream_temp_error_cls as exc:
                    last_error = str(exc)
                    retryable = (
                        attempt < max_attempts
                        and client.should_retry_temporary_error(exc)
                    )
                except Exception as exc:
                    store.update(job_id, status="failed", error=str(exc))
                    return

                if retryable:
                    delay = client._retry_delay_for_attempt(attempt)
                    if delay > 0:
                        time.sleep(delay)
                    continue
                break

            store.update(job_id, status="failed", error=last_error)

        threading.Thread(target=runner, args=(job.id,), daemon=True).start()

        return {"task_id": job.id, "status": job.status}

    @router.get("/api/v1/generate/{task_id}")
    def get_job(task_id: str, request: Request):
        require_service_api_key(request)

        job = store.get(task_id)
        if not job:
            raise HTTPException(status_code=404, detail="task not found")
        return asdict(job)

    @router.post("/v1/chat/completions")
    def chat_completions(data: dict, request: Request):
        require_service_api_key(request)

        prompt = extract_prompt_from_messages(data.get("messages") or [])
        if not prompt:
            prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "messages or prompt is required",
                        "type": "invalid_request_error",
                    }
                },
            )

        model_id = str(data.get("model") or "").strip()
        if (
            model_id.startswith("firefly-sora2")
            or model_id.startswith("firefly-veo31-fast")
            or model_id.startswith("firefly-veo31-")
            or model_id.startswith("firefly-kling-")
        ) and model_id not in video_model_catalog:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Invalid video model. Use /v1/models to get supported firefly-sora2-*, firefly-veo31-*, firefly-veo31-fast-* or firefly-kling-* models",
                        "type": "invalid_request_error",
                    }
                },
            )
        video_conf = video_model_catalog.get(model_id)
        is_video_model = video_conf is not None
        resolved_model_id = model_id if is_video_model else None
        ratio = "9:16"
        output_resolution = "2K"
        duration = int(video_conf["duration"]) if video_conf else 12
        video_resolution = (
            str(video_conf.get("resolution") or "720p") if video_conf else "720p"
        )
        if video_conf:
            ratio = str(video_conf.get("aspect_ratio") or ratio)
        video_engine = str(video_conf.get("engine") or "sora2") if video_conf else ""
        generate_audio = True
        negative_prompt = ""
        video_reference_mode = (
            str(video_conf.get("reference_mode") or "frame") if video_conf else "frame"
        )
        if is_video_model:
            resolved_video_options = resolve_video_options(data)
            if (
                isinstance(resolved_video_options, tuple)
                and len(resolved_video_options) == 3
            ):
                generate_audio, negative_prompt, requested_reference_mode = (
                    resolved_video_options
                )
                if "reference_mode" not in (video_conf or {}):
                    video_reference_mode = requested_reference_mode
            else:
                generate_audio, negative_prompt = resolved_video_options
            if not any(k in data for k in ("generate_audio", "generateAudio")):
                generate_audio = bool(video_conf.get("generate_audio", generate_audio))
        else:
            ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
                data, model_id or None
            )
        image_model_conf = (
            resolve_model(resolved_model_id) if not is_video_model else {}
        )

        try:
            entity_account_id = ""
            kling_bound_refs: list[dict] | None = None
            if video_engine == "kling-o3":
                entity_account_id, kling_bound_refs = _resolve_entity_bindings(prompt)
            input_images = load_input_images(data.get("messages") or [])
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                source_image_ids: list[str] = []
                image_url = ""
                response_content = ""

                if is_video_model:
                    if (
                        video_engine == "veo31-standard"
                        and video_reference_mode == "image"
                    ):
                        max_video_inputs = 3
                    else:
                        max_video_inputs = (
                            2
                            if video_engine
                            in {"veo31-fast", "veo31-standard", "kling-o3", "kling3"}
                            else 1
                        )
                    if len(input_images) > max_video_inputs:
                        raise HTTPException(
                            status_code=400,
                            detail=f"video model supports at most {max_video_inputs} input image(s)",
                        )
                    for image_bytes, _image_mime in input_images[:max_video_inputs]:
                        prepared_bytes, prepared_mime = prepare_video_source_image(
                            image_bytes,
                            ratio,
                            video_resolution,
                        )
                        source_image_ids.append(
                            client.upload_image(token, prepared_bytes, prepared_mime)
                        )

                    def _video_progress_cb(update: dict):
                        set_request_task_progress(
                            request,
                            task_status=str(update.get("task_status") or "IN_PROGRESS"),
                            task_progress=update.get("task_progress"),
                            upstream_job_id=update.get("upstream_job_id"),
                            retry_after=update.get("retry_after"),
                            error=update.get("error"),
                        )

                    job_id = uuid.uuid4().hex
                    tmp_path = generated_dir / f"{job_id}.video.tmp"
                    old_size = 0
                    try:
                        if tmp_path.exists():
                            old_size = int(tmp_path.stat().st_size)
                    except Exception:
                        old_size = 0

                    video_prompt = prompt
                    entity_refs = None
                    if video_engine == "kling-o3":
                        video_prompt, entity_refs = _resolve_kling_entity_refs(
                            token, prompt, kling_bound_refs
                        )

                    video_bytes, video_meta = client.generate_video(
                        token=token,
                        video_conf=video_conf or {},
                        prompt=video_prompt,
                        aspect_ratio=ratio,
                        duration=duration,
                        source_image_ids=source_image_ids,
                        entity_refs=entity_refs,
                        timeout=max(int(client.generate_timeout), 600),
                        negative_prompt=negative_prompt,
                        generate_audio=generate_audio,
                        reference_mode=video_reference_mode,
                        out_path=tmp_path,
                        progress_cb=_video_progress_cb,
                    )
                    video_ext = video_ext_from_meta(video_meta)
                    filename = f"{job_id}.{video_ext}"
                    out_path = generated_dir / filename
                    if video_bytes is not None:
                        out_path.write_bytes(video_bytes)
                    elif tmp_path.exists():
                        tmp_path.replace(out_path)
                    new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    on_generated_file_written(out_path, old_size, new_size)
                    image_url = public_generated_url(request, filename)
                    set_request_preview(request, image_url, kind="video")
                    response_content = (
                        f"```html\n<video src='{image_url}' controls></video>\n```"
                    )
                else:
                    for image_bytes, image_mime in input_images:
                        source_image_ids.append(
                            client.upload_image(
                                token, image_bytes, image_mime or "image/jpeg"
                            )
                        )

                    def _image_progress_cb(update: dict):
                        set_request_task_progress(
                            request,
                            task_status=str(update.get("task_status") or "IN_PROGRESS"),
                            task_progress=update.get("task_progress"),
                            upstream_job_id=update.get("upstream_job_id"),
                            retry_after=update.get("retry_after"),
                            error=update.get("error"),
                        )

                    job_id = uuid.uuid4().hex
                    out_path = generated_dir / f"{job_id}.png"
                    old_size = 0
                    try:
                        if out_path.exists():
                            old_size = int(out_path.stat().st_size)
                    except Exception:
                        old_size = 0

                    image_bytes, _meta = client.generate(
                        token=token,
                        prompt=prompt,
                        aspect_ratio=ratio,
                        output_resolution=output_resolution,
                        upstream_model_id=str(
                            image_model_conf.get("upstream_model_id") or "gemini-flash"
                        ),
                        upstream_model_version=str(
                            image_model_conf.get("upstream_model_version")
                            or "nano-banana-2"
                        ),
                        quality_level=(
                            client.gpt_image_quality
                            if str(image_model_conf.get("upstream_model_id") or "")
                            == "gpt-image"
                            else None
                        ),
                        detail_level=image_model_conf.get("detail_level"),
                        source_image_ids=source_image_ids,
                        timeout=client.generate_timeout,
                        out_path=out_path,
                        progress_cb=_image_progress_cb,
                    )
                    if image_bytes is not None:
                        out_path.write_bytes(image_bytes)
                    new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    on_generated_file_written(out_path, old_size, new_size)
                    image_url = public_image_url(request, job_id)
                    set_request_preview(request, image_url, kind="image")
                    response_content = f"![Generated Image]({image_url})"

                response_payload = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": resolved_model_id,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_content,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
                if bool(data.get("stream", False)):
                    return StreamingResponse(
                        sse_chat_stream(response_payload),
                        media_type="text/event-stream",
                    )
                return response_payload

            token_selector = None
            if entity_account_id:
                token_selector = lambda: token_manager.get_available_for_account(
                    entity_account_id, strategy=client.token_rotation_strategy
                )
            return run_with_token_retries(
                request=request,
                operation_name="chat.completions",
                run_once=_run_once,
                token_selector=token_selector,
            )
        except quota_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token quota exhausted",
                status_code=429,
                error_type="rate_limit_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token quota exhausted",
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Token quota exhausted",
                        "type": "rate_limit_error",
                        "code": error_code,
                    }
                },
            )
        except auth_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token invalid or expired",
                status_code=401,
                error_type="authentication_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token invalid or expired",
            )
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Token invalid or expired",
                        "type": "authentication_error",
                        "code": error_code,
                    }
                },
            )
        except upstream_temp_error_cls as exc:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=exc,
                status_code=503,
                error_type="server_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )
        except HTTPException as exc:
            err_type = (
                "invalid_request_error"
                if 400 <= int(exc.status_code) < 500
                else "server_error"
            )
            error_code = set_request_error_detail(
                request,
                error=str(exc.detail),
                status_code=exc.status_code,
                error_type=err_type,
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc.detail)
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": err_type,
                        "code": error_code,
                    }
                },
            )
        except Exception as exc:
            error_code = set_request_error_detail(
                request,
                error=exc,
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            logger.exception(
                "Unhandled error in /v1/chat/completions log_id=%s model=%s resolved_model=%s is_video_model=%s",
                getattr(request.state, "log_id", ""),
                model_id,
                resolved_model_id,
                is_video_model,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )

    return router
