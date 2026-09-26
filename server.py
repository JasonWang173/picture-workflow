from __future__ import annotations

import shutil
import tempfile
import uuid
import zipfile
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
DEMO_ASSETS = {
    "template": ROOT / "1.jpg",
    "product": ROOT / "商品1.png",
}
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
BATCH_ROOT.mkdir(parents=True, exist_ok=True)

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


async def _write_upload(upload: UploadFile, destination: Path) -> None:
    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"文件为空：{upload.filename or '未命名文件'}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="单个图片不能超过 24 MB")
    destination.write_bytes(content)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "food-compositor"}


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
    product: Annotated[UploadFile, File(description="商品图")],
    template: Annotated[UploadFile, File(description="模板图")],
    mode: Annotated[str, Form()] = "auto",
    size: Annotated[str, Form()] = "1080x1920",
    outline: Annotated[float, Form()] = 0.78,
    preserve_logo_pill: Annotated[bool, Form()] = True,
    quality: Annotated[int, Form()] = 100,
) -> FileResponse:
    if mode not in {"auto", "overlay", "cutout"}:
        raise HTTPException(status_code=400, detail="合成方式必须是 auto、overlay 或 cutout")
    temp_dir = Path(tempfile.mkdtemp(prefix="food-compositor-"))
    background_tasks.add_task(shutil.rmtree, temp_dir, ignore_errors=True)
    try:
        product_path = temp_dir / f"product{_safe_upload_suffix(product.filename, '.png')}"
        template_path = temp_dir / f"template{_safe_upload_suffix(template.filename, '.jpg')}"
        output_path = temp_dir / f"composition-{uuid.uuid4().hex[:8]}.jpg"
        await _write_upload(product, product_path)
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"合成失败：{exc}") from exc
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
    if mode not in {"auto", "overlay", "cutout"}:
        raise HTTPException(status_code=400, detail="合成方式必须是 auto、overlay 或 cutout")
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
        await _write_upload(template, template_path)
        output_paths: list[Path] = []
        for index, upload in enumerate(image_products, start=1):
            product_path = work_dir / f"product-{index:03d}{_safe_upload_suffix(upload.filename, '.png')}"
            await _write_upload(upload, product_path)
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
    except Exception as exc:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"批量合成失败：{exc}") from exc
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
