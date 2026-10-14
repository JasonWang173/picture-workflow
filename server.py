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
MAX_BATCH_UPLOAD_BYTES = 256 * 1024 * 1024
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
        raise HTTPException(status_code=413, detail="批量上传总大小不能超过 256 MB")

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
                        detail=("批量上传总大小不能超过 256 MB" if batch_exceeded else "单个图片不能超过 24 MB"),
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
        records.append({**record, "url": f"/api/templates/{record['id']}/file"})
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
        clean_name = (name or Path(template.filename or "未命名模板").stem).strip()[:80] or "未命名模板"
        record: dict[str, object] = {
            "id": template_id,
            "name": clean_name,
            "filename": filename,
            "url": f"/api/templates/{template_id}/file",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": (source or "upload").strip()[:40],
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


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str) -> dict[str, str]:
    records = _read_template_records()
    record = next((item for item in records if item.get("id") == template_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    _template_path(record).unlink(missing_ok=True)
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
) -> FileResponse:
    if mode not in {"auto", "overlay", "cutout", "template_replace"}:
        raise HTTPException(status_code=400, detail="合成方式必须是 auto、overlay、cutout 或 template_replace")
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
    products: Annotated[list[UploadFile], File(description="商品图片文件夹或多选图片")],
    template: Annotated[UploadFile, File(description="商家模板图")],
    mode: Annotated[str, Form()] = "auto",
    size: Annotated[str, Form()] = "1080x1920",
    outline: Annotated[float, Form()] = 0.78,
    preserve_logo_pill: Annotated[bool, Form()] = True,
    quality: Annotated[int, Form()] = 100,
) -> JSONResponse:
    """Compose a folder and keep each output available for preview and selection."""
    if mode not in {"auto", "overlay", "cutout", "template_replace"}:
        raise HTTPException(status_code=400, detail="合成方式必须是 auto、overlay、cutout 或 template_replace")
    if mode == "template_replace":
        raise HTTPException(status_code=400, detail="模板替换模式只需要上传一张模板图")
    image_products = [upload for upload in products if upload.filename and Path(upload.filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if not image_products:
        raise HTTPException(status_code=400, detail="商品文件夹里没有可处理的图片")
    if len(image_products) > 100:
        raise HTTPException(status_code=400, detail="一次最多处理 100 张商品图")

    batch_id = uuid.uuid4().hex[:12]
    batch_dir = BATCH_ROOT / batch_id
    work_dir = batch_dir / "work"
    output_dir = batch_dir / "outputs"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        template_path = work_dir / f"template{_safe_upload_suffix(template.filename, '.jpg')}"
        total_upload_bytes = await _write_upload(
            template,
            template_path,
            total_limit=MAX_BATCH_UPLOAD_BYTES,
        )
        output_paths: list[Path] = []
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
            output_path = output_dir / f"{index:03d}_{safe_stem}.jpg"
            compose(
                product_path,
                template_path,
                output_path,
                mode=mode,  # type: ignore[arg-type]
                size=size.strip() or "1080x1920",
                preserve_logo_pill=preserve_logo_pill,
                outline_strength=max(0.0, min(1.0, float(outline))),
                quality=max(1, min(100, int(quality))),
            )
            output_paths.append(output_path)
        shutil.rmtree(work_dir, ignore_errors=True)
    except HTTPException:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise
    except ValueError as exc:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        LOGGER.exception("批量合成失败")
        raise HTTPException(status_code=500, detail="批量合成失败，请稍后重试") from None
    return JSONResponse({
        "batch_id": batch_id,
        "count": len(output_paths),
        "images": [
            {
                "id": output_path.stem.split("_", 1)[0],
                "name": output_path.name,
                "url": f"/api/batches/{batch_id}/{output_path.name}",
            }
            for output_path in output_paths
        ],
        "download_url": f"/api/batches/{batch_id}/download",
    })


def _batch_directory(batch_id: str) -> Path:
    if len(batch_id) != 12 or any(character not in "0123456789abcdef" for character in batch_id):
        raise HTTPException(status_code=404, detail="批次不存在")
    directory = (BATCH_ROOT / batch_id).resolve()
    if BATCH_ROOT.resolve() not in directory.parents or not directory.is_dir():
        raise HTTPException(status_code=404, detail="批次不存在")
    return directory


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
