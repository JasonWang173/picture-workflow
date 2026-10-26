from __future__ import annotations

import shutil
import tempfile
import uuid
import zipfile
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
import time

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from food_compositor import compose

ROOT = Path(__file__).resolve().parent
WEB_DIST = ROOT / "web" / "dist"
BATCH_ROOT = ROOT / "runtime" / "batches"
TEMPLATE_ROOT = ROOT / "runtime" / "templates"
TEMPLATE_INDEX = TEMPLATE_ROOT / "index.json"
DEMO_ASSETS = {
    "template": ROOT / "1.jpg",
    "product": ROOT / "商品1.png",
}
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
# Disk guard for one batch upload only; there is no per-batch image count limit.
MAX_BATCH_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
THUMBNAIL_MAX_EDGE = 360
BATCH_ROOT.mkdir(parents=True, exist_ok=True)
TEMPLATE_ROOT.mkdir(parents=True, exist_ok=True)
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="商品海报合成工作台", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://127.0.0.1:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _safe_upload_suffix(filename: str | None, default: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else default


def _repair_latin1_utf8(value: str | None) -> str | None:
    """Undo latin-1 mojibake for UTF-8 form text and header filenames.

    Some multipart parsers decode text fields as latin-1; re-encode and
    decode as UTF-8 when that round-trip is lossless, otherwise keep as-is.
    """
    if not value:
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _parse_manual_slot(raw: str | None) -> tuple[float, float, float, float] | None:
    """Parse a normalized ``x,y,w,h`` rectangle drawn on the template preview."""
    if raw is None or not raw.strip():
        return None
    try:
        x, y, w, h = (float(part) for part in raw.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="框选区域格式不正确") from None
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0.01 < w <= 1 and 0.01 < h <= 1 and x + w <= 1.001 and y + h <= 1.001):
        raise HTTPException(status_code=400, detail="框选区域超出图片范围")
    return (x, y, min(w, 1 - x), min(h, 1 - y))


def _read_template_records() -> list[dict[str, object]]:
    if not TEMPLATE_INDEX.is_file():
        return []
    try:
        payload = json.loads(TEMPLATE_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("模板库索引读取失败，将使用空列表", exc_info=True)
        return []
    if not isinstance(payload, list):
        return []
    return [record for record in payload if isinstance(record, dict) and isinstance(record.get("id"), str)]


def _write_template_records(records: list[dict[str, object]]) -> None:
    temporary = TEMPLATE_INDEX.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(TEMPLATE_INDEX)


def _template_record(template_id: str) -> dict[str, object] | None:
    return next((record for record in _read_template_records() if record.get("id") == template_id), None)


def _template_path(record: dict[str, object]) -> Path:
    filename = Path(str(record.get("filename", ""))).name
    path = (TEMPLATE_ROOT / filename).resolve()
    if TEMPLATE_ROOT.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="模板不存在")
    return path


def _thumbnail_path(template_id: str) -> Path:
    return TEMPLATE_ROOT / f"{template_id}_thumb.jpg"


def _generate_thumbnail(source: Path, destination: Path) -> bool:
    """Write a small JPEG preview for the library grid; failures are non-fatal."""
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE))
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(destination, "JPEG", quality=80)
        return True
    except Exception:
        LOGGER.warning("模板缩略图生成失败：%s", source.name, exc_info=True)
        destination.unlink(missing_ok=True)
        return False


async def _write_upload(
    upload: UploadFile,
    destination: Path,
    *,
    current_total: int = 0,
    total_limit: int | None = None,
) -> int:
    """Stream an upload to disk while enforcing per-file and batch limits."""
    max_allowed = MAX_UPLOAD_BYTES
    if total_limit is not None:
        max_allowed = min(max_allowed, total_limit - current_total)
    if max_allowed <= 0:
        raise HTTPException(status_code=413, detail="批量上传总大小不能超过 8 GB")

    written = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_allowed:
                    batch_exceeded = total_limit is not None and current_total + written > total_limit
                    raise HTTPException(
                        status_code=413,
                        detail=("批量上传总大小不能超过 8 GB" if batch_exceeded else "单个图片不能超过 24 MB"),
                    )
                handle.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if written == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"文件为空：{upload.filename or '未命名文件'}")
    return written


def _batch_status_path(batch_dir: Path) -> Path:
    return batch_dir / "status.json"


def _read_batch_status(batch_dir: Path) -> dict[str, object] | None:
    path = _batch_status_path(batch_dir)
    if not path.is_file():
        return None
    for attempt in range(6):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, json.JSONDecodeError):
            # Windows can refuse opens while another thread replaces the file.
            if attempt == 5:
                LOGGER.warning("批次状态读取失败：%s", path, exc_info=True)
                return None
            time.sleep(0.05)
    return None


def _write_batch_status(batch_dir: Path, status: dict[str, object]) -> None:
    path = _batch_status_path(batch_dir)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except OSError:
            if attempt == 9:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def _fail_interrupted_batches() -> None:
    """Mark batches that were still running when the service last stopped."""
    if not BATCH_ROOT.is_dir():
        return
    for status_path in BATCH_ROOT.glob("*/status.json"):
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("status") in {"queued", "processing"}:
            payload["status"] = "failed"
            payload["error"] = "服务重启导致该批次中断，请重新上传合成"
            _write_batch_status(status_path.parent, payload)


_fail_interrupted_batches()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "food-compositor"}


@app.get("/api/templates")
def list_templates() -> dict[str, object]:
    records = []
    for record in sorted(_read_template_records(), key=lambda item: str(item.get("created_at", "")), reverse=True):
        path = _template_path(record)
        if not path.is_file():
            continue
        records.append({
            **record,
            "url": f"/api/templates/{record['id']}/file",
            "thumb_url": f"/api/templates/{record['id']}/thumb",
        })
    return {"templates": records}


@app.post("/api/templates")
async def save_template(
    template: Annotated[UploadFile, File(description="模板图片")],
    name: Annotated[str | None, Form()] = None,
    source: Annotated[str | None, Form()] = None,
) -> dict[str, object]:
    suffix = _safe_upload_suffix(template.filename, ".jpg")
    template_id = uuid.uuid4().hex[:16]
    filename = f"{template_id}{suffix}"
    destination = TEMPLATE_ROOT / filename
    try:
        size = await _write_upload(template, destination)
        repaired_name = _repair_latin1_utf8(name)
        repaired_filename = _repair_latin1_utf8(template.filename)
        clean_name = (repaired_name or Path(repaired_filename or "未命名模板").stem).strip()[:80] or "未命名模板"
        thumb_generated = _generate_thumbnail(destination, _thumbnail_path(template_id))
        record: dict[str, object] = {
            "id": template_id,
            "name": clean_name,
            "filename": filename,
            "url": f"/api/templates/{template_id}/file",
            "thumb_url": f"/api/templates/{template_id}/thumb",
            "has_thumb": thumb_generated,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": (_repair_latin1_utf8(source) or "upload").strip()[:40],
            "size": size,
        }
        records = _read_template_records()
        records.append(record)
        _write_template_records(records)
        return record
    except HTTPException:
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        LOGGER.exception("模板保存失败")
        raise HTTPException(status_code=500, detail="模板保存失败，请稍后重试") from None


@app.get("/api/templates/{template_id}/file")
def get_template_file(template_id: str) -> FileResponse:
    record = _template_record(template_id)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    path = _template_path(record)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="模板文件不存在")
    media_type = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(path, media_type=media_type, filename=str(record.get("name", path.name)) + path.suffix)


@app.get("/api/templates/{template_id}/thumb")
def get_template_thumbnail(template_id: str) -> FileResponse:
    record = _template_record(template_id)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    path = _template_path(record)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="模板文件不存在")
    thumb_path = _thumbnail_path(template_id)
    if not thumb_path.is_file():
        # Older records predate thumbnails; backfill on first request.
        _generate_thumbnail(path, thumb_path)
    if thumb_path.is_file():
        return FileResponse(thumb_path, media_type="image/jpeg")
    media_type = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(path, media_type=media_type)


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str) -> dict[str, str]:
    records = _read_template_records()
    record = next((item for item in records if item.get("id") == template_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    _template_path(record).unlink(missing_ok=True)
    _thumbnail_path(template_id).unlink(missing_ok=True)
    _write_template_records([item for item in records if item.get("id") != template_id])
    return {"status": "deleted", "id": template_id}


@app.get("/api/demo-assets/{kind}")
def demo_asset(kind: str) -> FileResponse:
    path = DEMO_ASSETS.get(kind)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="示例素材不存在")
    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.get("/api/demo-available")
def demo_available() -> dict[str, bool]:
    return {"available": all(path.is_file() for path in DEMO_ASSETS.values())}


@app.post("/api/compose")
async def create_composition(
    background_tasks: BackgroundTasks,
    template: Annotated[UploadFile, File(description="模板图")],
    product: Annotated[UploadFile | None, File(description="商品图；模板替换模式可不上传")] = None,
    mode: Annotated[str, Form()] = "auto",
    size: Annotated[str, Form()] = "1080x1920",
    outline: Annotated[float, Form()] = 0.78,
    preserve_logo_pill: Annotated[bool, Form()] = True,
    quality: Annotated[int, Form()] = 100,
    slot: Annotated[str | None, Form()] = None,
) -> FileResponse:
    if mode not in {"auto", "overlay", "cutout", "template_replace"}:
        raise HTTPException(status_code=400, detail="合成方式必须是 auto、overlay、cutout 或 template_replace")
    manual_slot = _parse_manual_slot(slot)
    if manual_slot is not None and mode != "template_replace":
        raise HTTPException(status_code=400, detail="手动框选区域仅模板替换模式可用")
    temp_dir = Path(tempfile.mkdtemp(prefix="food-compositor-"))
    background_tasks.add_task(shutil.rmtree, temp_dir, ignore_errors=True)
    try:
        product_path = None
        if product is not None:
            product_path = temp_dir / f"product{_safe_upload_suffix(product.filename, '.png')}"
        template_path = temp_dir / f"template{_safe_upload_suffix(template.filename, '.jpg')}"
        output_path = temp_dir / f"composition-{uuid.uuid4().hex[:8]}.jpg"
        if product_path is not None and product is not None:
            await _write_upload(product, product_path)
        elif mode != "template_replace":
            raise HTTPException(status_code=400, detail="当前合成方式需要上传商品图")
        await _write_upload(template, template_path)
        if size.strip() == "":
            size = "1080x1920"
        compose(
            product_path,
            template_path,
            output_path,
            mode=mode,  # type: ignore[arg-type]
            size=size.strip(),
            preserve_logo_pill=preserve_logo_pill,
            outline_strength=max(0.0, min(1.0, float(outline))),
            quality=max(1, min(100, int(quality))),
            manual_slot=manual_slot,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        LOGGER.exception("单张合成失败")
        raise HTTPException(status_code=500, detail="合成失败，请稍后重试") from None
    return FileResponse(
        output_path,
        media_type="image/jpeg",
        filename="composition.jpg",
        headers={"X-Composition-Mode": mode},
        background=background_tasks,
    )


@app.post("/api/compose-batch")
async def create_batch_composition(
    background_tasks: BackgroundTasks,
    products: Annotated[list[UploadFile], File(description="商品图片文件夹或多选图片")],
    template: Annotated[UploadFile, File(description="商家模板图")],
    mode: Annotated[str, Form()] = "auto",
    size: Annotated[str, Form()] = "1080x1920",
    outline: Annotated[float, Form()] = 0.78,
    preserve_logo_pill: Annotated[bool, Form()] = True,
    quality: Annotated[int, Form()] = 100,
) -> JSONResponse:
    """Accept a batch upload and compose it in the background; poll /status for progress."""
    if mode not in {"auto", "overlay", "cutout", "template_replace"}:
        raise HTTPException(status_code=400, detail="合成方式必须是 auto、overlay、cutout 或 template_replace")
    if mode == "template_replace":
        raise HTTPException(status_code=400, detail="模板替换模式只需要上传一张模板图")
    image_products = [upload for upload in products if upload.filename and Path(upload.filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if not image_products:
        raise HTTPException(status_code=400, detail="商品文件夹里没有可处理的图片")

    batch_id = uuid.uuid4().hex[:12]
    batch_dir = BATCH_ROOT / batch_id
    work_dir = batch_dir / "work"
    output_dir = batch_dir / "outputs"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_items: list[dict[str, object]] = []
    try:
        template_path = work_dir / f"template{_safe_upload_suffix(template.filename, '.jpg')}"
        total_upload_bytes = await _write_upload(
            template,
            template_path,
            total_limit=MAX_BATCH_UPLOAD_BYTES,
        )
        for index, upload in enumerate(image_products, start=1):
            product_path = work_dir / f"product-{index:03d}{_safe_upload_suffix(upload.filename, '.png')}"
            total_upload_bytes += await _write_upload(
                upload,
                product_path,
                current_total=total_upload_bytes,
                total_limit=MAX_BATCH_UPLOAD_BYTES,
            )
            stem = Path(upload.filename or f"product-{index:03d}").stem
            safe_stem = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in stem).strip("_") or f"product-{index:03d}"
            manifest_items.append({"index": index, "work": product_path.name, "stem": safe_stem})
        (work_dir / "manifest.json").write_text(
            json.dumps({"template": template_path.name, "items": manifest_items}, ensure_ascii=False),
            encoding="utf-8",
        )
        _write_batch_status(batch_dir, {
            "status": "queued",
            "total": len(manifest_items),
            "done": 0,
            "skipped": [],
            "error": None,
            "images": [],
            "download_url": f"/api/batches/{batch_id}/download",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except HTTPException:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        LOGGER.exception("批量上传失败")
        raise HTTPException(status_code=500, detail="批量上传失败，请稍后重试") from None
    background_tasks.add_task(
        _run_batch_composition,
        batch_id,
        mode,
        size.strip() or "1080x1920",
        max(0.0, min(1.0, float(outline))),
        preserve_logo_pill,
        max(1, min(100, int(quality))),
    )
    return JSONResponse({
        "batch_id": batch_id,
        "total": len(manifest_items),
        "status_url": f"/api/batches/{batch_id}/status",
    })


def _run_batch_composition(
    batch_id: str,
    mode: str,
    size: str,
    outline_strength: float,
    preserve_logo_pill: bool,
    quality: int,
) -> None:
    batch_dir = BATCH_ROOT / batch_id
    work_dir = batch_dir / "work"
    output_dir = batch_dir / "outputs"
    status = _read_batch_status(batch_dir) or {
        "status": "queued",
        "total": 0,
        "done": 0,
        "skipped": [],
        "error": None,
        "images": [],
        "download_url": f"/api/batches/{batch_id}/download",
    }
    status["status"] = "processing"
    _write_batch_status(batch_dir, status)
    try:
        manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
        template_path = work_dir / str(manifest["template"])
        items = manifest["items"]
        status["total"] = len(items)
        for entry in items:
            index = int(entry["index"])
            output_path = output_dir / f"{index:03d}_{entry['stem']}.jpg"
            try:
                compose(
                    work_dir / str(entry["work"]),
                    template_path,
                    output_path,
                    mode=mode,  # type: ignore[arg-type]
                    size=size,
                    preserve_logo_pill=preserve_logo_pill,
                    outline_strength=outline_strength,
                    quality=quality,
                )
            except Exception as exc:
                # One unusable image must not abort a large batch.
                LOGGER.warning("批次 %s 第 %s 张合成失败，已跳过：%s", batch_id, index, exc)
                status["skipped"].append({"index": index, "error": str(exc)})  # type: ignore[union-attr]
                _write_batch_status(batch_dir, status)
                continue
            status["done"] = int(status.get("done", 0)) + 1
            status["images"].append({  # type: ignore[union-attr]
                "id": f"{index:03d}",
                "name": output_path.name,
                "url": f"/api/batches/{batch_id}/{output_path.name}",
            })
            _write_batch_status(batch_dir, status)
        if not status["images"]:
            status["status"] = "failed"
            status["error"] = status.get("error") or "没有图片合成成功，请检查模板与商品图"
        else:
            status["status"] = "done"
    except Exception as exc:
        LOGGER.exception("批量合成失败")
        status["status"] = "failed"
        status["error"] = str(exc) or "批量合成失败"
    finally:
        _write_batch_status(batch_dir, status)
        shutil.rmtree(work_dir, ignore_errors=True)


def _batch_directory(batch_id: str) -> Path:
    if len(batch_id) != 12 or any(character not in "0123456789abcdef" for character in batch_id):
        raise HTTPException(status_code=404, detail="批次不存在")
    directory = (BATCH_ROOT / batch_id).resolve()
    if BATCH_ROOT.resolve() not in directory.parents or not directory.is_dir():
        raise HTTPException(status_code=404, detail="批次不存在")
    return directory


@app.get("/api/batches/{batch_id}/status")
def get_batch_status(batch_id: str) -> JSONResponse:
    batch_dir = _batch_directory(batch_id)
    status = _read_batch_status(batch_dir)
    if status is None:
        raise HTTPException(status_code=404, detail="批次状态不存在")
    return JSONResponse(status)


@app.get("/api/batches/{batch_id}/download")
def download_batch(batch_id: str, background_tasks: BackgroundTasks, ids: str | None = None) -> FileResponse:
    batch_dir = _batch_directory(batch_id)
    output_dir = (batch_dir / "outputs").resolve()
    available = sorted(output_dir.glob("*.jpg"))
    if ids:
        wanted = {item.strip() for item in ids.split(",") if item.strip()}
        selected = [path for path in available if path.stem.split("_", 1)[0] in wanted]
    else:
        selected = available
    if not selected:
        raise HTTPException(status_code=404, detail="没有可下载的合成图")
    archive_path = batch_dir / f"selected-{uuid.uuid4().hex[:8]}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for image_path in selected:
            archive.write(image_path, arcname=image_path.name)
    background_tasks.add_task(archive_path.unlink, missing_ok=True)
    return FileResponse(archive_path, media_type="application/zip", filename="food-composites.zip", background=background_tasks)


@app.get("/api/batches/{batch_id}/{filename}")
def get_batch_image(batch_id: str, filename: str) -> FileResponse:
    batch_dir = _batch_directory(batch_id)
    output_dir = (batch_dir / "outputs").resolve()
    image_path = (output_dir / Path(filename).name).resolve()
    if output_dir not in image_path.parents or image_path.suffix.lower() not in {".jpg", ".jpeg"} or not image_path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(image_path, media_type="image/jpeg", filename=image_path.name)


if WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/{path:path}")
def serve_frontend(path: str = ""):
    index = WEB_DIST / "index.html"
    if index.is_file():
        requested = (WEB_DIST / path).resolve()
        if requested.is_file() and WEB_DIST.resolve() in requested.parents:
            return FileResponse(requested)
        return FileResponse(index)
    return JSONResponse(
        {"message": "前端尚未构建，请运行 npm install && npm run build，或使用 npm run dev。"},
        status_code=404,
    )
