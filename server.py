from __future__ import annotations

import os
# Set before numpy/OpenCV are imported, including in spawned worker processes.
for _thread_setting in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_thread_setting, "1")
import re
import asyncio
import hashlib
import secrets
import shutil
import tempfile
import uuid
import zipfile
import logging
import json
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Error as SQLiteError
from typing import Annotated, Optional
from urllib.parse import quote
from concurrent.futures import CancelledError, ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import asynccontextmanager, contextmanager, suppress
from threading import Lock
import time

from fastapi import APIRouter, BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.routing import APIRoute
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from food_compositor import compose
from food_compositor.account_api import check_origin, create_account_router, current_account
from food_compositor.accounts import AccountStore, accounts_path
from food_compositor.composition_archive import CompositionArchive, OWNER_ID, RECORD_ID, RETENTION, history_time, history_day
from food_compositor.execution import submit_composition, worker_count, estimate_memory, shutdown_executor
from food_compositor.record_cache import RecordCache, read_records
from food_compositor.request_limits import RequestLimits
from food_compositor.cancellation import CompositionCancelled, cancellation_scope
from food_compositor.storage_guard import ensure_space, StorageFull, reserve_bytes
from food_compositor.conversion_api import ConversionJobs
from food_compositor.download_folders import NUMBERED_FOLDER, allocate_download_folder

ROOT = Path(__file__).resolve().parent
WEB_DIST = ROOT / "web" / "dist"
BATCH_ROOT = Path(os.environ.get("COMPOSITION_BATCH_ROOT", ROOT / "runtime" / "batches"))
# Some hosts mount /tmp as a small RAM disk. Multipart spools and ZIPs must
# use application storage, otherwise the disk guard can reject every upload
# or a large request can exhaust RAM despite free space on the main disk.
UPLOAD_TEMP_ROOT = Path(os.environ.get("COMPOSITION_TEMP_ROOT", BATCH_ROOT.parent / "upload-temp")).resolve()
UPLOAD_TEMP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
tempfile.tempdir = str(UPLOAD_TEMP_ROOT)
# Optional machine-specific settings contain no credentials and are never committed.
_local_oss_settings = ROOT / "runtime" / "local-oss.json"
if _local_oss_settings.is_file():
    try:
        _settings = json.loads(_local_oss_settings.read_text(encoding="utf-8"))
        for _name in ("OSS_BUCKET", "OSS_ENDPOINT", "OSS_TEMPLATE_PREFIX", "OSS_COMPOSITION_PREFIX", "OSS_TRANSPORT"):
            if isinstance(_settings.get(_name), str):
                os.environ.setdefault(_name, _settings[_name])
    except (OSError, ValueError, AttributeError):
        logging.getLogger(__name__).warning("本地 OSS 配置无法读取，请检查 runtime/local-oss.json")
# The template library lives in OSS so every user shares one library. Each
# template is a set of self-describing objects (image, thumb, meta json), so
# concurrent uploads never fight over a shared mutable index.
OSS_PREFIX = os.environ.get("OSS_TEMPLATE_PREFIX", "picture-workflow/templates/")
ARCHIVE_PREFIX = os.environ.get("OSS_COMPOSITION_PREFIX", "picture-workflow/compositions/").strip("/") + "/"
WORKSPACE_COOKIE = "composition_workspace"
account_store = AccountStore(accounts_path())
BEIJING_TIME = timezone(timedelta(hours=8))
_batch_locks = [Lock() for _ in range(64)]
_OSS_BUCKET = None
_TEMPLATE_ID_RE = re.compile(r"[0-9a-f]{16}")
_template_cache = RecordCache()


def _template_cache_key(bucket):
    return f"{os.environ.get('OSS_ENDPOINT', '')}:{getattr(bucket, 'bucket_name', getattr(bucket, 'bucket', id(bucket)))}:{OSS_PREFIX}"
DEMO_ASSETS = {
    "template": ROOT / "1.jpg",
    "product": ROOT / "商品1.png",
}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_BATCH_UPLOAD_BYTES = int(os.environ.get("MAX_BATCH_UPLOAD_MB", "8192")) * 1024 * 1024
MAX_BATCH_PRODUCTS = int(os.environ.get("MAX_BATCH_PRODUCTS", "1000"))
MAX_BATCH_TEMPLATES = int(os.environ.get("MAX_BATCH_TEMPLATES", "1000"))
MAX_BATCH_OUTPUTS = int(os.environ.get("MAX_BATCH_OUTPUTS", "10000"))
THUMBNAIL_MAX_EDGE = 360
BATCH_ROOT.mkdir(parents=True, exist_ok=True)
LOGGER = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    _fail_interrupted_batches()
    cleanup = asyncio.create_task(_archive_cleanup_loop())
    conversion_cleanup = asyncio.create_task(conversion_jobs.cleanup_loop())
    try:
        yield
    finally:
        cleanup.cancel()
        conversion_cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup
        with suppress(asyncio.CancelledError):
            await conversion_cleanup
        await run_in_threadpool(shutdown_executor)


app = FastAPI(title="商品海报合成工作台", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestLimits, storage=BATCH_ROOT)
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


def _uploaded_dish_name(filename: str | None) -> str:
    name = (_repair_latin1_utf8(filename) or "").replace("\\", "/").rsplit("/", 1)[-1]
    return Path(name).stem.strip() or "美食"


def _request_username(request: Request) -> str | None:
    account = current_account(account_store, request)
    return account["username"] if account else None


def _output_orientation(path: Path) -> str:
    from PIL import Image
    with Image.open(path) as image:
        width, height = image.size
    if width == height:
        return "方版"
    return "竖版" if width < height else "横板"


def _account_image_name(created_at: datetime, username: str, orientation: str, index: int) -> str:
    day = created_at.astimezone(BEIJING_TIME).strftime("%Y%m%d")
    return f"{day}-{username}-{orientation}-（{index}）.jpg"


def _composition_filename(created_at: datetime, index: int, dish: str, sequence_width: int = 3) -> str:
    date = created_at.astimezone(BEIJING_TIME).strftime("%Y%m%d")
    # Leave room for the date and sequence within a 255-byte UTF-8 filename.
    safe_dish = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', "_", dish).strip(" ._")[:56].rstrip(" ._")
    return f"{date}-{index:0{sequence_width}d}-{safe_dish or '美食'}.jpg"


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


def _validate_output_size(size: str) -> str:
    size = size.strip() or "1080x1920"
    try:
        width, height = (int(value) for value in size.lower().split("x"))
        if width <= 0 or height <= 0 or max(width, height) > 4096 or width * height > 8_000_000:
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=400, detail="输出尺寸无效，最长边须在 4096 像素内，总像素不超过 800 万") from None
    return size


def _oss_sdk():
    """Select the standard SDK or the native-extension-free HTTPS transport."""
    transport = os.environ.get("OSS_TRANSPORT", "sdk").lower()
    if transport == "rest":
        from food_compositor import oss_rest
        return oss_rest
    if transport != "sdk":
        raise HTTPException(status_code=503, detail="OSS_TRANSPORT 配置无效")
    try:
        import oss2
        return oss2
    except ImportError:
        raise HTTPException(status_code=503, detail="模板库依赖加载失败，请检查 oss2 和 pycryptodome 安装及 Windows 应用控制策略") from None
    except OSError:
        LOGGER.exception("模板库依赖的原生组件加载失败")
        raise HTTPException(status_code=503, detail="模板库依赖的原生组件无法加载，请检查 Windows 应用控制策略和 pycryptodome 安装") from None


def _oss_bucket():
    """Lazily build the OSS bucket client from the service environment."""
    global _OSS_BUCKET
    if _OSS_BUCKET is not None:
        return _OSS_BUCKET
    missing = [key for key in ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_ENDPOINT", "OSS_BUCKET") if not os.environ.get(key)]
    if missing:
        raise HTTPException(status_code=503, detail=f"模板库 OSS 未配置，缺少环境变量：{', '.join(missing)}")
    oss2 = _oss_sdk()
    auth = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"])
    _OSS_BUCKET = oss2.Bucket(auth, os.environ["OSS_ENDPOINT"], os.environ["OSS_BUCKET"], connect_timeout=15)
    return _OSS_BUCKET


def _workspace_owner(request: Request, response: Response | None = None) -> str | None:
    token = request.cookies.get(WORKSPACE_COOKIE, "")
    if not OWNER_ID.fullmatch(token):
        if response is None:
            return None
        token = secrets.token_hex(32)
        response.set_cookie(WORKSPACE_COOKIE, token, max_age=365 * 24 * 3600,
                            httponly=True, secure=request.url.scheme == "https", samesite="strict")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@contextmanager
def _composition_archive():
    sdk = _oss_sdk()
    try:
        yield CompositionArchive(_oss_bucket(), sdk, ARCHIVE_PREFIX)
    except sdk.exceptions.OssError as exc:
        LOGGER.warning("合成记录 OSS 操作失败", exc_info=True)
        raise HTTPException(status_code=503, detail="合成记录暂时无法连接 OSS，请稍后重试") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="合成记录读取超时，请稍后重试") from exc


def _purge_composition_storage() -> None:
    now = datetime.now(timezone.utc)
    for path in BATCH_ROOT.glob("*/status.json"):
        try:
            batch_id = path.parent.name
            if not re.fullmatch(r"[0-9a-f]{12}", batch_id):
                continue
            with _batch_locks[hash(batch_id) % len(_batch_locks)]:
                status = _read_batch_status(path.parent)
                if not status:
                    continue
                if status["status"] == "uploading" and not (path.parent / "upload.claimed").exists():
                    if datetime.fromisoformat(status["created_at"]).timestamp() < now.timestamp() - 3600:
                        shutil.rmtree(path.parent)
                    continue
                if status["status"] not in {"done", "failed", "cancelled"}:
                    continue
                timestamp = status.get("finished_at") or status.get("created_at")
                if timestamp and datetime.fromisoformat(timestamp) + RETENTION <= now:
                    shutil.rmtree(path.parent)
                    continue
                for prepared in path.parent.glob("download-*.zip"):
                    if now.timestamp() - prepared.stat().st_mtime > 3600:
                        prepared.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            LOGGER.warning("本地批次清理失败：%s", path.parent.name, exc_info=True)
    if not os.environ.get("OSS_BUCKET") and _OSS_BUCKET is None:
        return
    try:
        with _composition_archive() as archive:
            archive.purge_expired()
    except HTTPException as exc:
        LOGGER.warning("合成空间站未能清理 OSS：%s", exc.detail)


async def _archive_cleanup_loop() -> None:
    while True:
        try:
            await run_in_threadpool(_purge_composition_storage)
        except Exception:
            LOGGER.exception("合成空间站定期清理异常")
        await asyncio.sleep(15 * 60)


def _validated_template_id(template_id: str) -> str:
    if not _TEMPLATE_ID_RE.fullmatch(template_id or ""):
        raise HTTPException(status_code=404, detail="模板不存在")
    return template_id


def _template_record(template_id: str) -> dict[str, object] | None:
    template_id = _validated_template_id(template_id)
    bucket = _oss_bucket()
    oss2 = _oss_sdk()
    cached = _template_cache.peek(_template_cache_key(bucket), template_id)
    if cached is not None:
        return cached
    try:
        result = bucket.get_object(f"{OSS_PREFIX}{template_id}.json")
        try:
            payload = json.loads(result.read())
        finally:
            result.close()
    except oss2.exceptions.NoSuchKey:
        return None
    except oss2.exceptions.OssError as exc:
        LOGGER.warning("模板元数据读取失败：%s", template_id, exc_info=True)
        raise HTTPException(status_code=503, detail="模板库暂时不可用，请稍后重试") from exc
    return payload if isinstance(payload, dict) else None


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
    cancel_marker: Path | None = None,
) -> int:
    """Stream an upload to disk while enforcing per-file and batch limits."""
    max_allowed = MAX_UPLOAD_BYTES
    if total_limit is not None:
        max_allowed = min(max_allowed, total_limit - current_total)
    if max_allowed <= 0:
        raise HTTPException(status_code=413, detail=f"批量上传总大小不能超过 {MAX_BATCH_UPLOAD_BYTES // 1024 // 1024} MB")

    written = 0
    try:
        with destination.open("wb") as handle:
            while True:
                if cancel_marker is not None and cancel_marker.exists():
                    raise CompositionCancelled('任务已取消')
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_allowed:
                    batch_exceeded = total_limit is not None and current_total + written > total_limit
                    raise HTTPException(
                        status_code=413,
                        detail=(f"批量上传总大小不能超过 {MAX_BATCH_UPLOAD_BYTES // 1024 // 1024} MB" if batch_exceeded else f"单个图片不能超过 {MAX_UPLOAD_MB} MB"),
                    )
                try:
                    ensure_space(destination.parent, len(chunk))
                except StorageFull as exc:
                    raise HTTPException(status_code=507, detail=str(exc)) from None
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
        if isinstance(payload, dict) and payload.get("status") in {"queued", "processing", "cancelling", "uploading"}:
            cancelled = (status_path.parent / "cancel.requested").exists()
            payload["status"] = "cancelled" if cancelled else "failed"
            payload["error"] = None if cancelled else "服务重启导致该批次中断，请重新上传合成"
            payload["finished_at"] = datetime.now(timezone.utc).isoformat()
            _write_batch_status(status_path.parent, payload)




@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "food-compositor"}


@app.get("/api/ready")
def readiness() -> JSONResponse:
    checks = {
        "frontend": (WEB_DIST / "index.html").is_file() and (WEB_DIST / "assets").is_dir(),
        "disk": all(shutil.disk_usage(path).free >= reserve_bytes() for path in (BATCH_ROOT, UPLOAD_TEMP_ROOT)),
    }
    return JSONResponse({"status": "ok" if all(checks.values()) else "degraded", "checks": checks},
                        status_code=200 if all(checks.values()) else 503)


@app.get("/api/templates")
def list_templates(response: Response, refresh: bool = False) -> dict[str, object]:
    bucket = _oss_bucket()
    oss2 = _oss_sdk()
    try:
        records, stale = _template_cache.get(
            _template_cache_key(bucket),
            lambda old, versions: read_records(bucket, oss2, OSS_PREFIX, old, versions, _TEMPLATE_ID_RE),
            force=refresh,
            disk=ROOT / "runtime" / "template-catalog-cache.json" if bucket is _OSS_BUCKET else None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="模板库读取失败或不完整，请稍后重试") from exc
    response.headers["Cache-Control"] = "private, no-store"
    records = [{**meta, "url": f"/api/templates/{meta['id']}/file", "thumb_url": f"/api/templates/{meta['id']}/thumb"} for meta in records]
    records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return {"templates": records, "stale": stale}


@app.post("/api/templates")
async def save_template(
    background_tasks: BackgroundTasks,
    template: Annotated[UploadFile, File(description="模板图片")],
    name: Annotated[Optional[str], Form()] = None,
    source: Annotated[Optional[str], Form()] = None,
) -> dict[str, object]:
    suffix = _safe_upload_suffix(template.filename, ".jpg")
    template_id = uuid.uuid4().hex[:16]
    temp_dir = Path(tempfile.mkdtemp(prefix="template-upload-"))
    background_tasks.add_task(shutil.rmtree, temp_dir, ignore_errors=True)
    destination = temp_dir / f"{template_id}{suffix}"
    thumb_path = temp_dir / f"{template_id}_thumb.jpg"
    try:
        size = await _write_upload(template, destination)
        repaired_name = _repair_latin1_utf8(name)
        repaired_filename = _repair_latin1_utf8(template.filename)
        clean_name = (repaired_name or Path(repaired_filename or "未命名模板").stem).strip()[:80] or "未命名模板"
        has_thumb = await run_in_threadpool(_generate_thumbnail, destination, thumb_path)
        object_key = f"{OSS_PREFIX}{template_id}{suffix}"
        thumb_key = f"{OSS_PREFIX}{template_id}_thumb.jpg"
        record: dict[str, object] = {
            "id": template_id,
            "name": clean_name,
            "filename": f"{template_id}{suffix}",
            "object_key": object_key,
            "thumb_key": thumb_key,
            "url": f"/api/templates/{template_id}/file",
            "thumb_url": f"/api/templates/{template_id}/thumb",
            "has_thumb": has_thumb,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": (_repair_latin1_utf8(source) or "upload").strip()[:40],
            "size": size,
        }
        def persist():
            bucket = _oss_bucket()
            bucket.put_object_from_file(object_key, str(destination))
            if has_thumb:
                bucket.put_object_from_file(thumb_key, str(thumb_path))
            # Meta last: a template becomes visible only once its record exists.
            bucket.put_object(f"{OSS_PREFIX}{template_id}.json", json.dumps(record, ensure_ascii=False))
            _template_cache.update(_template_cache_key(bucket), template_id, record)
        await run_in_threadpool(persist)
        return record
    except HTTPException:
        raise
    except Exception:
        LOGGER.exception("模板保存失败")
        raise HTTPException(status_code=500, detail="模板保存失败，请稍后重试") from None


@app.get("/api/templates/{template_id}/file")
def get_template_file(template_id: str) -> StreamingResponse:
    record = _template_record(template_id)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    bucket = _oss_bucket()
    oss2 = _oss_sdk()
    key = str(record.get("object_key") or f"{OSS_PREFIX}{template_id}{Path(str(record.get('filename', '.jpg'))).suffix}")
    try:
        obj = bucket.get_object(key)
    except oss2.exceptions.NoSuchKey:
        raise HTTPException(status_code=404, detail="模板文件不存在") from None
    except oss2.exceptions.OssError as exc:
        LOGGER.warning("模板文件读取失败：%s", key, exc_info=True)
        raise HTTPException(status_code=503, detail="模板库暂时不可用，请稍后重试") from exc
    suffix = Path(key).suffix.lower()
    media_type = {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")
    filename = str(record.get("name", Path(key).name)) + suffix
    return StreamingResponse(
        obj,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}", "Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/templates/{template_id}/thumb")
def get_template_thumbnail(background_tasks: BackgroundTasks, template_id: str) -> Response:
    record = _template_record(template_id)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    bucket = _oss_bucket()
    oss2 = _oss_sdk()
    thumb_key = str(record.get("thumb_key") or f"{OSS_PREFIX}{template_id}_thumb.jpg")
    try:
        return StreamingResponse(bucket.get_object(thumb_key), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    except oss2.exceptions.NoSuchKey:
        pass  # older records predate thumbnails; backfill below
    except oss2.exceptions.OssError as exc:
        LOGGER.warning("模板缩略图读取失败：%s", thumb_key, exc_info=True)
        raise HTTPException(status_code=503, detail="模板库暂时不可用，请稍后重试") from exc
    object_key = str(record.get("object_key") or f"{OSS_PREFIX}{template_id}.jpg")
    temp_dir = Path(tempfile.mkdtemp(prefix="template-thumb-"))
    background_tasks.add_task(shutil.rmtree, temp_dir, ignore_errors=True)
    source_path = temp_dir / Path(object_key).name
    thumb_path = temp_dir / f"{template_id}_thumb.jpg"
    try:
        bucket.get_object_to_file(object_key, str(source_path))
        if _generate_thumbnail(source_path, thumb_path):
            bucket.put_object_from_file(thumb_key, str(thumb_path))
            return FileResponse(thumb_path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    except oss2.exceptions.OssError:
        LOGGER.warning("模板缩略图回填失败：%s", template_id, exc_info=True)
    except Exception:
        LOGGER.warning("模板缩略图生成失败：%s", template_id, exc_info=True)
    return StreamingResponse(bucket.get_object(object_key), media_type="image/jpeg")


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str) -> dict[str, str]:
    record = _template_record(template_id)
    if record is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    bucket = _oss_bucket()
    suffix = Path(str(record.get("object_key") or ".jpg")).suffix or ".jpg"
    object_key = str(record.get("object_key") or f"{OSS_PREFIX}{template_id}{suffix}")
    thumb_key = str(record.get("thumb_key") or f"{OSS_PREFIX}{template_id}_thumb.jpg")
    try:
        bucket.delete_object(object_key)
        bucket.delete_object(thumb_key)
        bucket.delete_object(f"{OSS_PREFIX}{template_id}.json")
        _template_cache.update(_template_cache_key(bucket), template_id, None)
    except Exception as exc:
        LOGGER.exception("模板删除失败：%s", template_id)
        raise HTTPException(status_code=500, detail="模板删除失败，请稍后重试") from exc
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


def _compose_single(product_path, template_path, output_path, **settings):
    return submit_composition(compose, product_path, template_path, output_path,
                              estimated_bytes=estimate_memory(product_path, template_path, settings.get("size")),
                              **settings).result()


@app.post("/api/compose")
async def create_composition(
    request: Request,
    background_tasks: BackgroundTasks,
    template: Annotated[UploadFile, File(description="模板图")],
    product: Annotated[Optional[UploadFile], File(description="商品图；模板替换模式可不上传")] = None,
    mode: Annotated[str, Form()] = "auto",
    size: Annotated[str, Form()] = "1080x1920",
    outline: Annotated[float, Form()] = 0.78,
    preserve_logo_pill: Annotated[bool, Form()] = True,
    quality: Annotated[int, Form()] = 100,
    slot: Annotated[Optional[str], Form()] = None,
) -> FileResponse:
    size = _validate_output_size(size)
    if mode not in {"auto", "overlay", "cutout", "template_replace"}:
        raise HTTPException(status_code=400, detail="合成方式必须是 auto、overlay、cutout 或 template_replace")
    manual_slot = _parse_manual_slot(slot)
    if manual_slot is not None and mode not in {"template_replace", "cutout"}:
        raise HTTPException(status_code=400, detail="手动框选仅在模板替换和主体抠图合成中可用")
    created_at = datetime.now(timezone.utc)
    username = await run_in_threadpool(_request_username, request)
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
        await run_in_threadpool(
            _compose_single,
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
    dish = "空白模板" if mode == "template_replace" else _uploaded_dish_name(product.filename if product else None)
    return FileResponse(
        output_path,
        media_type="image/jpeg",
        filename=(_account_image_name(created_at, username, _output_orientation(output_path), 1)
                  if username else _composition_filename(created_at, 1, dish)),
        headers={"X-Composition-Mode": mode},
        background=background_tasks,
    )


@app.post("/api/batches/reserve")
def reserve_batch(request: Request) -> JSONResponse:
    batch_id = uuid.uuid4().hex[:12]
    response = JSONResponse({"batch_id": batch_id, "total": 0, "status_url": f"/api/batches/{batch_id}/status"})
    owner = _workspace_owner(request, response)
    batch_dir = BATCH_ROOT / batch_id
    batch_dir.mkdir()
    _write_batch_status(batch_dir, {"status": "uploading", "owner": owner, "username": _request_username(request), "total": 0, "done": 0,
                                  "images": [], "skipped": [], "error": None,
                                  "download_url": f"/api/batches/{batch_id}/download",
                                  "created_at": datetime.now(timezone.utc).isoformat()})
    return response


@app.post("/api/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str, request: Request) -> dict:
    batch_dir = _batch_directory(batch_id)
    with _batch_locks[hash(batch_id) % len(_batch_locks)]:
        status = _owned_batch_status(batch_dir, request)
        if not status.get("owner"):
            raise HTTPException(status_code=404, detail="批次不存在")
        if status["status"] not in {"done", "failed", "cancelled"}:
            (batch_dir / "cancel.requested").touch(exist_ok=True)
            status["status"] = "cancelling"
            if not (batch_dir / "upload.claimed").exists() and not (batch_dir / "work" / "manifest.json").exists():
                status["status"] = "cancelled"
                status["finished_at"] = datetime.now(timezone.utc).isoformat()
            _write_batch_status(batch_dir, status)
    return {"status": status["status"]}


class BatchUploadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def with_batch_parser(request: Request):
            # The framework's default 1,000-file cap counts products AND templates.
            # Cache the parsed form so FastAPI reuses it, with normal cleanup.
            await request.form(max_files=MAX_BATCH_PRODUCTS + MAX_BATCH_TEMPLATES + 1)
            return await handler(request)
        return with_batch_parser


batch_router = APIRouter(route_class=BatchUploadRoute)


@batch_router.post("/api/compose-batch")
async def create_batch_composition(
    background_tasks: BackgroundTasks,
    request: Request,
    batch_id: Annotated[Optional[str], Form(description="上传前预留的任务编号")] = None,
    products: Annotated[Optional[list[UploadFile]], File(description="商品图片文件夹或多选图片")] = None,
    template: Annotated[Optional[UploadFile], File(description="兼容单张模板")] = None,
    templates: Annotated[Optional[list[UploadFile]], File(description="按顺序处理的模板图")] = None,
    template_modes: Annotated[Optional[str], Form(description="每张模板的合成方式 JSON 数组")] = None,
    mode: Annotated[str, Form()] = "auto",
    size: Annotated[str, Form()] = "1080x1920",
    outline: Annotated[float, Form()] = 0.78,
    preserve_logo_pill: Annotated[bool, Form()] = True,
    quality: Annotated[int, Form()] = 100,
    slot: Annotated[Optional[str], Form()] = None,
) -> JSONResponse:
    """Accept a batch upload and compose it in the background; poll /status for progress."""
    size = _validate_output_size(size)
    if mode not in {"auto", "overlay", "cutout", "template_replace"}:
        raise HTTPException(status_code=400, detail="合成方式必须是 auto、overlay、cutout 或 template_replace")
    image_products = [upload for upload in (products or []) if upload.filename and Path(upload.filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if mode != "template_replace" and not image_products:
        raise HTTPException(status_code=400, detail="商品文件夹里没有可处理的图片")
    if mode == "template_replace":
        image_products = []

    image_templates = list(templates or []) + ([template] if template is not None else [])
    if not image_templates or any(not upload.filename or Path(upload.filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} for upload in image_templates):
        raise HTTPException(status_code=400, detail="请上传有效的模板图片")
    if len(image_products) > MAX_BATCH_PRODUCTS or len(image_templates) > MAX_BATCH_TEMPLATES:
        raise HTTPException(status_code=413, detail=f"每批最多 {MAX_BATCH_PRODUCTS} 张商品图、{MAX_BATCH_TEMPLATES} 张模板图")
    if max(1, len(image_products)) * len(image_templates) > MAX_BATCH_OUTPUTS:
        raise HTTPException(status_code=413, detail=f"每批最多生成 {MAX_BATCH_OUTPUTS} 张合成图，请分批处理")

    manual_slot = _parse_manual_slot(slot)
    if manual_slot is not None and (mode not in {"template_replace", "cutout"} or len(image_templates) != 1):
        raise HTTPException(status_code=400, detail="手动框选仅支持单张模板的模板替换或主体抠图合成")
    requested_modes = [mode] * len(image_templates)
    if template_modes is not None:
        try:
            parsed_modes = json.loads(template_modes)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="模板合成方式格式不正确") from exc
        if mode != "auto" or not isinstance(parsed_modes, list) or len(parsed_modes) != len(image_templates) or any(item not in ("auto", "cutout", "overlay") for item in parsed_modes):
            raise HTTPException(status_code=400, detail="请为每张模板提供有效的合成方式，且批次方式须为智能判断")
        requested_modes = parsed_modes

    created_at = datetime.now(timezone.utc)
    reserved = batch_id is not None
    username = await run_in_threadpool(_request_username, request)
    if reserved:
        batch_dir = _batch_directory(batch_id)
        with _batch_locks[hash(batch_id) % len(_batch_locks)]:
            previous = _owned_batch_status(batch_dir, request)
            username = previous.get("username", username)
            if not previous.get("owner") or previous["status"] != "uploading" or (batch_dir / "cancel.requested").exists():
                raise HTTPException(status_code=409, detail="任务已取消或已提交，请重新创建任务")
            try:
                (batch_dir / "upload.claimed").touch(exist_ok=False)
            except FileExistsError:
                raise HTTPException(status_code=409, detail="任务已提交，请勿重复上传") from None
    else:
        batch_id = uuid.uuid4().hex[:12]
    response = JSONResponse({
        "batch_id": batch_id,
        "total": max(1, len(image_products)) * len(image_templates),
        "status_url": f"/api/batches/{batch_id}/status",
    })
    owner = _workspace_owner(request, response)
    batch_dir = BATCH_ROOT / batch_id
    work_dir = batch_dir / "work"
    output_dir = batch_dir / "outputs"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_items: list[dict[str, object]] = []
    try:
        total_upload_bytes = 0
        manifest_templates = []
        for index, upload in enumerate(image_templates, start=1):
            template_path = work_dir / f"template-{index:04d}{_safe_upload_suffix(upload.filename, '.jpg')}"
            total_upload_bytes += await _write_upload(upload, template_path, current_total=total_upload_bytes, total_limit=MAX_BATCH_UPLOAD_BYTES, cancel_marker=batch_dir / "cancel.requested")
            name = Path(_repair_latin1_utf8(upload.filename) or f"模板{index}").stem
            manifest_templates.append({"index": index, "work": template_path.name, "name": name, "mode": requested_modes[index - 1], "slot": manual_slot})
        for index, upload in enumerate(image_products, start=1):
            product_path = work_dir / f"product-{index:03d}{_safe_upload_suffix(upload.filename, '.png')}"
            total_upload_bytes += await _write_upload(
                upload,
                product_path,
                current_total=total_upload_bytes,
                total_limit=MAX_BATCH_UPLOAD_BYTES,
                cancel_marker=batch_dir / "cancel.requested",
            )
            dish = _uploaded_dish_name(upload.filename)
            manifest_items.append({"index": index, "work": product_path.name, "name": dish})
        if mode == "template_replace":
            # This mode removes the existing product area; it does not use product uploads.
            manifest_items.append({"index": 1, "work": None, "name": "空白模板"})
        (work_dir / "manifest.json").write_text(
            json.dumps({"templates": manifest_templates, "items": manifest_items}, ensure_ascii=False),
            encoding="utf-8",
        )
        with _batch_locks[hash(batch_id) % len(_batch_locks)]:
            if (batch_dir / "cancel.requested").exists():
                raise CompositionCancelled('任务已取消')
            _write_batch_status(batch_dir, {
            "status": "queued",
            "owner": owner,
            "username": username,
            "total": len(manifest_items) * len(manifest_templates),
            "done": 0,
            "skipped": [],
            "error": None,
            "images": [],
            "download_url": f"/api/batches/{batch_id}/download",
            "created_at": created_at.isoformat(),
            })
    except CompositionCancelled:
        status = _read_batch_status(batch_dir) or {}
        status.update(status="cancelled", error=None, finished_at=datetime.now(timezone.utc).isoformat())
        _write_batch_status(batch_dir, status)
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=409, detail="任务已取消") from None
    except asyncio.CancelledError:
        status = _read_batch_status(batch_dir) or {}
        cancelled = (batch_dir / "cancel.requested").exists()
        status.update(status="cancelled" if cancelled else "failed", error=None if cancelled else "上传连接已中断，请重新上传",
                      finished_at=datetime.now(timezone.utc).isoformat())
        _write_batch_status(batch_dir, status)
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except HTTPException:
        if reserved:
            status = _read_batch_status(batch_dir) or {}
            status.update(status="failed", error="上传未完成，请重新选择图片", finished_at=datetime.now(timezone.utc).isoformat())
            _write_batch_status(batch_dir, status)
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            shutil.rmtree(batch_dir, ignore_errors=True)
        raise
    except Exception:
        if reserved:
            status = _read_batch_status(batch_dir) or {}
            status.update(status="failed", error="上传未完成，请重新选择图片", finished_at=datetime.now(timezone.utc).isoformat())
            _write_batch_status(batch_dir, status)
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
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
    return response


app.include_router(batch_router)


def _compose_one(
    item: dict,
    template_path: str,
    mode: str,
    size: str,
    outline_strength: float,
    preserve_logo_pill: bool,
    quality: int,
    work_dir: str,
    output_dir: str,
) -> dict:
    """Compose a single batch item; runs in a worker process."""
    index = int(item["index"])
    output_path = Path(output_dir) / item["output_name"]
    metadata = {key: item[key] for key in ("template_index", "template_name", "product_index", "product_name")}
    try:
        with cancellation_scope(Path(work_dir).parent / "cancel.requested"):
            ensure_space(output_path.parent, 300_000)
            compose(
                Path(work_dir) / str(item["work"]) if item["work"] is not None else None,
                Path(template_path),
                output_path,
                mode=mode,  # type: ignore[arg-type]
                size=size,
                preserve_logo_pill=preserve_logo_pill,
                outline_strength=outline_strength,
                quality=quality,
                manual_slot=item.get("manual_slot"),
            )
    except CompositionCancelled:
        output_path.unlink(missing_ok=True)
        return {"index": index, "cancelled": True}
    except Exception as exc:
        return {"index": index, **metadata, "error": str(exc)}
    return {"index": index, **metadata, "name": output_path.name}


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
    futures = {}
    username = status.get("username")
    orientation_counts: dict[str, int] = {}
    awaiting_order: dict[int, dict] = {}
    next_index = 1
    cancel_marker = batch_dir / "cancel.requested"
    def cancel_check():
        if cancel_marker.exists():
            raise CompositionCancelled('任务已取消')
    def publish_result(result):
        if result.get("cancelled"):
            return
        metadata = {key: result[key] for key in ("template_index", "template_name", "product_index", "product_name")}
        if username and not result.get("error"):
            original = output_dir / result["name"]
            try:
                orientation = _output_orientation(original)
                number = orientation_counts.get(orientation, 0) + 1
                name = _account_image_name(created_at, username, orientation, number)
                original.rename(output_dir / name)
                orientation_counts[orientation] = number
                result["name"] = name
            except OSError as exc:
                result["error"] = f"成品命名失败：{exc}"
        if result.get("error"):
            LOGGER.warning("批次 %s 模板 %s 商品 %s 合成失败：%s", batch_id, result["template_index"], result["product_index"], result["error"])
            status["skipped"].append({"index": result["index"], **metadata, "error": result["error"]})
        else:
            name = result["name"]
            status["images"].append({"id": f"{result['index']:09d}", **metadata, "name": name, "url": f"/api/batches/{batch_id}/{quote(name)}"})

    def consume(future, entry):
        nonlocal next_index
        try:
            result = future.result()
        except (CancelledError, CompositionCancelled):
            result = {"cancelled": True}
        except Exception as exc:
            result = {**entry, "error": str(exc) or "处理进程异常"}
        if not result.get("cancelled"):
            status["done"] = int(status.get("done", 0)) + 1
        if not username:
            publish_result(result)
            return
        # Workers finish out of order. Assign numbers only when prior inputs have settled.
        awaiting_order[entry["index"]] = result
        while next_index in awaiting_order:
            publish_result(awaiting_order.pop(next_index))
            next_index += 1
    try:
        cancel_check()
        manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
        items = manifest["items"]
        template_items = manifest.get("templates") or [{"index": 1, "work": manifest.get("template"), "name": "模板"}]
        status["total"] = len(items) * len(template_items)
        created_at = datetime.fromisoformat(status["created_at"]) if status.get("created_at") else datetime.now(timezone.utc)
        sequence_width = max(3, len(str(status["total"])))
        status["template_count"] = len(template_items)
        workers = worker_count()
        status["workers"] = workers
        last_status_write = 0.0
        def flush_status(force=False):
            nonlocal last_status_write
            now = time.monotonic()
            if force or now - last_status_write >= 0.25:
                status["images"].sort(key=lambda image: int(image["id"]))
                status["skipped"].sort(key=lambda item: item["index"])
                _write_batch_status(batch_dir, status)
                last_status_write = now
        # One shared pool bounds CPU/memory across batches and single requests.
        # Finish all products for one template before starting the next.
        for template_item in template_items:
            cancel_check()
            template_index = int(template_item["index"])
            status["current_template_index"] = template_index
            status["current_template_name"] = template_item["name"]
            status["current_template_mode"] = template_item.get("mode", mode)
            _write_batch_status(batch_dir, status)
            template_path = work_dir / str(template_item["work"])
            slot = template_item.get("slot")
            manual_slot = tuple(slot) if slot is not None else None
            entries = []
            for item in items:
                product_index = int(item["index"])
                index = (template_index - 1) * len(items) + product_index
                dish = item.get("name") or item.get("stem") or "美食"
                entries.append(dict(item, index=index, template_index=template_index, template_name=template_item["name"],
                                    product_index=product_index, product_name=dish,
                                    output_name=f"result-{index}.jpg" if username else _composition_filename(created_at, index, dish, sequence_width),
                                    manual_slot=manual_slot))
            pending_entries = iter(entries)
            futures = {}
            def submit_next():
                cancel_check()
                entry = next(pending_entries, None)
                if entry is None:
                    return
                ensure_space(output_dir, 300_000 * workers)
                product_path = work_dir / str(entry["work"]) if entry["work"] is not None else None
                future = submit_composition(_compose_one, entry, str(template_path), template_item.get("mode", mode), size,
                                            outline_strength, preserve_logo_pill, quality, str(work_dir), str(output_dir),
                                            estimated_bytes=estimate_memory(product_path, template_path, size), cancel_check=cancel_check)
                futures[future] = entry
            for _ in range(min(workers, len(entries))):
                submit_next()
            while futures:
                cancel_check()
                completed, _ = wait(futures, timeout=.1, return_when=FIRST_COMPLETED)
                if not completed:
                    continue
                future = next(iter(completed))
                entry = futures.pop(future)
                consume(future, entry)
                flush_status()
                submit_next()
            flush_status(force=True)
        if not status["images"]:
            status["status"] = "failed"
            reasons = {item["error"] for item in status["skipped"]}
            status["error"] = status.get("error") or (
                next(iter(reasons)) if len(reasons) == 1 else "没有图片合成成功，请检查模板与商品图"
            )
        else:
            status["status"] = "done"
    except CompositionCancelled:
        status.update(status="cancelling", error=None)
        _write_batch_status(batch_dir, status)
    except Exception as exc:
        LOGGER.exception("批量合成失败")
        status["status"] = "failed"
        status["error"] = str(exc) or "批量合成失败"
    finally:
        # A failed submit/status write must not remove inputs while workers use them.
        if futures:
            if cancel_marker.exists():
                for future in futures:
                    future.cancel()
            wait(futures)
            for future, entry in futures.items():
                consume(future, entry)
        shutil.rmtree(work_dir, ignore_errors=True)
        with _batch_locks[hash(batch_id) % len(_batch_locks)]:
            if cancel_marker.exists():
                status.update(status="cancelled", error=None)
            status["images"].sort(key=lambda image: int(image["id"]))
            status["skipped"].sort(key=lambda item: item["index"])
            status["finished_at"] = datetime.now(timezone.utc).isoformat()
            _write_batch_status(batch_dir, status)


def _batch_directory(batch_id: str) -> Path:
    if len(batch_id) != 12 or any(character not in "0123456789abcdef" for character in batch_id):
        raise HTTPException(status_code=404, detail="批次不存在")
    directory = (BATCH_ROOT / batch_id).resolve()
    if BATCH_ROOT.resolve() not in directory.parents or not directory.is_dir():
        raise HTTPException(status_code=404, detail="批次不存在")
    return directory


def _owned_batch_status(batch_dir: Path, request: Request) -> dict:
    status = _read_batch_status(batch_dir)
    if status is None or (status.get("owner") and status["owner"] != _workspace_owner(request)):
        raise HTTPException(status_code=404, detail="批次不存在")
    if status.get("status") in {"queued", "processing", "uploading"} and (batch_dir / "cancel.requested").exists():
        status["status"] = "cancelling"
    return status


def _batch_result(batch_dir: Path, image: dict) -> Path:
    output_dir = (batch_dir / "outputs").resolve()
    path = (output_dir / Path(image["name"]).name).resolve()
    if path.parent != output_dir or path.suffix.lower() != ".jpg" or not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return path


def _result_image(status: dict, image_id: str) -> dict:
    for image in status.get("images", []):
        if image["id"] == image_id:
            return image
    raise HTTPException(status_code=404, detail="图片不存在")


@app.get("/api/batches/{batch_id}/status")
def get_batch_status(batch_id: str, request: Request, summary: bool = False) -> JSONResponse:
    batch_dir = _batch_directory(batch_id)
    status = _owned_batch_status(batch_dir, request)
    hidden = {'owner'}
    if summary and status.get('status') not in {'done', 'failed', 'cancelled'}:
        hidden.update({'images', 'skipped'})
    return JSONResponse({key: value for key, value in status.items() if key not in hidden},
                        headers={"Cache-Control": "private, no-store"})


def _download_folder(owner: str, batch_id: str, username: str | None) -> str:
    day = datetime.now(timezone.utc).astimezone(BEIJING_TIME).date()
    try:
        return allocate_download_folder(BATCH_ROOT, owner, batch_id, day, username)
    except SQLiteError as exc:
        LOGGER.exception("下载批次编号保存失败")
        raise HTTPException(status_code=503, detail="暂时无法生成下载批次编号，请稍后重试") from exc


def _prepare_batch_download(batch_id: str, request: Request, ids: str | None,
                            ticket: str | None = None) -> tuple[Path, list[dict], str]:
    batch_dir = _batch_directory(batch_id)
    archive_path = None
    records = []
    try:
        with _batch_locks[hash(batch_id) % len(_batch_locks)]:
            status = _owned_batch_status(batch_dir, request)
            wanted = {item.strip() for item in ids.split(",")} if ids is not None else None
            selected = [(image, _batch_result(batch_dir, image)) for image in status.get("images", [])
                        if wanted is None or image["id"] in wanted]
            if not selected:
                raise HTTPException(status_code=404, detail="没有可下载的合成图")
            archive_dir = batch_dir if ticket else Path(tempfile.gettempdir())
            ensure_space(archive_dir, sum(path.stat().st_size + 4096 for _, path in selected))
            owner = status.get("owner")
            folder = _download_folder(owner or _workspace_owner(request) or "", batch_id, status.get("username"))
            filename = f"{folder}.zip"
            if ticket:
                archive_path = batch_dir / f"download-{ticket}-{filename}"
            else:
                with tempfile.NamedTemporaryFile(prefix="food-composites-", suffix=".zip", delete=False) as temporary:
                    archive_path = Path(temporary.name)
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as zipped:
                for _, path in selected:
                    ensure_space(archive_path.parent, path.stat().st_size + 4096)
                    zipped.write(path, arcname=f"{folder}/{path.name}")
            if owner:
                with _composition_archive() as archive:
                    def save_item(item):
                        image, path = item
                        return archive.save(owner, f"{batch_id}-{image['id']}", image, path, "history", download_folder=folder)
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        records = list(pool.map(save_item, selected))
    except Exception as exc:
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)
        if isinstance(exc, StorageFull):
            raise HTTPException(status_code=507, detail=str(exc)) from None
        raise
    return archive_path, records, filename


def _batch_zip_response(archive_path: Path, background_tasks: BackgroundTasks, filename: str) -> FileResponse:
    background_tasks.add_task(archive_path.unlink, missing_ok=True)
    return FileResponse(archive_path, media_type="application/zip", filename=filename,
                        headers={"Cache-Control": "private, no-store"}, background=background_tasks)


@app.get("/api/batches/{batch_id}/download")
def download_batch(batch_id: str, request: Request, background_tasks: BackgroundTasks, ids: Optional[str] = None) -> FileResponse:
    archive_path, _, filename = _prepare_batch_download(batch_id, request, ids)
    return _batch_zip_response(archive_path, background_tasks, filename)


@app.post("/api/batches/{batch_id}/download")
def prepare_batch_download(batch_id: str, request: Request, ids: Optional[str] = None,
                           selection: Annotated[Optional[dict], Body()] = None) -> JSONResponse:
    if selection is not None:
        values = selection.get('ids')
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_BATCH_OUTPUTS or any(not isinstance(value, str) or not re.fullmatch(r'[0-9]{9}', value) for value in values):
            raise HTTPException(status_code=400, detail='请选择有效的合成图')
        ids = ','.join(values)
    ticket = uuid.uuid4().hex
    _, records, filename = _prepare_batch_download(batch_id, request, ids, ticket)
    return JSONResponse({"items": records, "filename": filename, "url": f"/api/batches/{batch_id}/downloads/{ticket}"},
                        headers={"Cache-Control": "private, no-store"})


@app.get("/api/batches/{batch_id}/downloads/{ticket}")
def get_prepared_batch_download(batch_id: str, ticket: str, request: Request, background_tasks: BackgroundTasks) -> FileResponse:
    batch_dir = _batch_directory(batch_id)
    status = _owned_batch_status(batch_dir, request)
    if not status.get("owner") or not re.fullmatch(r"[0-9a-f]{32}", ticket):
        raise HTTPException(status_code=404, detail="下载文件不存在，请重新下载")
    prefix = f"download-{ticket}-"
    for path in batch_dir.glob(f"{prefix}*.zip"):
        folder = path.stem[len(prefix):]
        if NUMBERED_FOLDER.fullmatch(folder) and path.is_file():
            return _batch_zip_response(path, background_tasks, f"{folder}.zip")
    legacy = batch_dir / f"download-{ticket}.zip"
    if legacy.is_file():
        return _batch_zip_response(legacy, background_tasks, "food-composites.zip")
    raise HTTPException(status_code=404, detail="下载文件不存在，请重新下载")


@app.get("/api/batches/{batch_id}/images/{image_id}/download")
def download_batch_image(batch_id: str, image_id: str, request: Request) -> Response:
    batch_dir = _batch_directory(batch_id)
    record = None
    with _batch_locks[hash(batch_id) % len(_batch_locks)]:
        status = _owned_batch_status(batch_dir, request)
        image = _result_image(status, image_id)
        path = _batch_result(batch_dir, image)
        content = path.read_bytes()
        owner = status.get("owner")
        if owner:
            folder = _download_folder(owner, batch_id, status.get("username"))
            with _composition_archive() as archive:
                record = archive.save(owner, f"{batch_id}-{image_id}", image, path, "history", download_folder=folder)
    response = _archived_image_response(path.name, content, download=True)
    if record:
        response.headers["X-Composition-Record"] = quote(json.dumps(record, ensure_ascii=True), safe="")
    return response


@app.delete("/api/batches/{batch_id}/images/{image_id}")
def delete_batch_image(batch_id: str, image_id: str, request: Request) -> dict:
    batch_dir = _batch_directory(batch_id)
    with _batch_locks[hash(batch_id) % len(_batch_locks)]:
        status = _owned_batch_status(batch_dir, request)
        if status["status"] not in {"done", "failed"}:
            raise HTTPException(status_code=409, detail="请等待合成结束后再删除")
        owner = status.get("owner")
        if not owner:
            raise HTTPException(status_code=409, detail="该批旧结果不支持移入回收站")
        image = _result_image(status, image_id)
        path = _batch_result(batch_dir, image)
        folder = _download_folder(owner, batch_id, status.get("username"))
        with _composition_archive() as archive:
            record = archive.save(owner, f"{batch_id}-{image_id}", image, path, "trash", download_folder=folder)
        status["images"] = [item for item in status["images"] if item["id"] != image_id]
        _write_batch_status(batch_dir, status)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("已移入回收站的本地文件暂未清理：%s/%s", batch_id, image_id)
    return {"status": "trashed", "item": record}


def _archive_folder_name(record: dict) -> str:
    name = record.get("download_folder")
    if isinstance(name, str) and NUMBERED_FOLDER.fullmatch(name):
        return name
    return history_day(record)


@app.get("/api/composition-archive")
def list_composition_archive(request: Request, response: Response, view: str = "all", section: str = "history",
                             day: str | None = None, cursor: str | None = None, limit: int = 20, refresh: bool = False,
                             folder: str | None = None, batch: str | None = None, scope: str = "auto") -> dict:
    if view not in {"all", "folders", "batch_folders", "page"} or section not in {"history", "trash"} or not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="目录查询参数不正确")
    if day is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(status_code=400, detail="日期格式不正确")
    if folder is not None and (view != "page" or
                               not (NUMBERED_FOLDER.fullmatch(folder) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", folder))):
        raise HTTPException(status_code=400, detail="文件夹格式不正确")
    if batch is not None and (not folder or not re.fullmatch(r"[0-9a-f]{12}", batch)):
        raise HTTPException(status_code=400, detail="批次格式不正确")
    if scope not in {"auto", "browser", "team"}:
        raise HTTPException(status_code=400, detail="空间范围无效")
    shared = scope != "browser" and current_account(account_store, request) is not None
    if scope == "team" and not shared:
        raise HTTPException(status_code=401, detail="请重新登录后查看共享记录")
    owner = None if shared else _workspace_owner(request, response)
    response.headers["Cache-Control"] = "private, no-store"
    with _composition_archive() as archive:
        records = archive.list(owner, refresh=refresh)
    if view == "all":
        return {"items": records}  # compatibility for existing clients
    if view == "batch_folders":
        groups = {}
        for record in records:
            if record["section"] == section:
                name = _archive_folder_name(record)
                key = (name, record["id"][:12] if shared and NUMBERED_FOLDER.fullmatch(name) else "")
                groups[key] = groups.get(key, 0) + 1

        def folder_order(key):
            stem, _, index = key[0].partition("（")
            return -int(stem[:10].replace("-", "")), stem[10:], int(index[:-1]) if index else 0, key[1]

        return {"folders": [{"folder": name, "day": name[:10], "count": groups[name, batch_id],
                             **({"batch": batch_id} if batch_id else {})}
                            for name, batch_id in sorted(groups, key=folder_order)],
                "trash_count": sum(record["section"] == "trash" for record in records)}
    if view == "folders":
        days = {}
        for record in records:
            if record["section"] == "history":
                key = history_day(record)
                days[key] = days.get(key, 0) + 1
        return {"folders": [{"day": day, "count": days[day]} for day in sorted(days, reverse=True)],
                "trash_count": sum(record["section"] == "trash" for record in records)}
    records = [record for record in records if record["section"] == section
               and (day is None or history_day(record) == day)
               and (folder is None or _archive_folder_name(record) == folder)
               and (batch is None or record["id"][:12] == batch)]
    def sort_key(record):
        reference = record.get("archive_key", record["id"])
        if folder and folder[10:11] == "-":
            # This batch folder follows the original input order, even after a restore.
            return -int(record["id"].rsplit("-", 1)[1]), reference
        stamp = history_time(record) if section == "history" else record.get("trashed_at") or record["saved_at"]
        return stamp, reference
    records.sort(key=sort_key, reverse=True)
    total = len(records)
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if (data["section"] != section or data["day"] != day or data.get("folder") != folder
                    or data.get("batch") != batch or not isinstance(data["id"], str)
                    or not RECORD_ID.fullmatch(data["id"].rsplit("~", 1)[-1])):
                raise ValueError()
            records = [record for record in records if sort_key(record) < (data["time"], data["id"])]
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise HTTPException(status_code=400, detail="分页位置无效，请刷新列表") from None
    page = records[:limit]
    next_cursor = None
    if len(records) > limit:
        stamp, record_id = sort_key(page[-1])
        next_cursor = base64.urlsafe_b64encode(json.dumps({"section": section, "day": day, "folder": folder, "batch": batch, "time": stamp, "id": record_id}).encode()).decode()
    return {"items": page, "next_cursor": next_cursor, "total": total}


def _archive_location(request: Request, reference: str, archive: CompositionArchive) -> tuple[str, str]:
    if request.method != "GET":
        check_origin(request)
    source, separator, entry_id = reference.rpartition("~")
    owner = _workspace_owner(request)
    if not RECORD_ID.fullmatch(entry_id):
        raise HTTPException(status_code=404, detail="合成记录不存在或已过期")
    if separator:
        if OWNER_ID.fullmatch(source) and current_account(account_store, request):
            return source, entry_id
    elif current_account(account_store, request):
        matches = [item for item in archive.list(None) if item["id"] == entry_id]
        if len(matches) == 1:
            return matches[0]["archive_key"].split("~")[0], entry_id
    if not separator and owner:
        return owner, entry_id
    raise HTTPException(status_code=404, detail="合成记录不存在或已过期")


def _archived_image_response(name: str, content: bytes, *, download: bool) -> Response:
    headers = {"Cache-Control": "private, no-store"}
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name, safe='')}"
    return Response(content, media_type="image/jpeg", headers=headers)


@app.get("/api/composition-archive/{entry_id}/image")
def get_archived_image(entry_id: str, request: Request, download: bool = False) -> Response:
    with _composition_archive() as archive:
        owner, entry_id = _archive_location(request, entry_id, archive)
        image = archive.read_image(owner, entry_id, download=download)
    if image is None:
        raise HTTPException(status_code=404, detail="合成图片不存在或已过期")
    return _archived_image_response(*image, download=download)


@app.post("/api/composition-archive/{entry_id}/restore")
def restore_archived_image(entry_id: str, request: Request) -> dict:
    with _composition_archive() as archive:
        owner, entry_id = _archive_location(request, entry_id, archive)
        record = archive.restore(owner, entry_id)
        if record is None:
            raise HTTPException(status_code=404, detail="合成记录不存在或已过期")
    return {"status": "restored", "item": record}


@app.delete("/api/composition-archive/{entry_id}")
def trash_archived_image(entry_id: str, request: Request) -> dict:
    with _composition_archive() as archive:
        owner, entry_id = _archive_location(request, entry_id, archive)
        record = archive.trash(owner, entry_id)
        if record is None:
            raise HTTPException(status_code=404, detail="合成记录不存在或已过期")
    return {"status": "trashed", "item": record}


@app.get("/api/batches/{batch_id}/{filename}")
def get_batch_image(batch_id: str, filename: str, request: Request) -> Response:
    batch_dir = _batch_directory(batch_id)
    with _batch_locks[hash(batch_id) % len(_batch_locks)]:
        status = _owned_batch_status(batch_dir, request)
        image = next((item for item in status.get("images", []) if item["name"] == filename), None)
        if image is None:
            raise HTTPException(status_code=404, detail="图片不存在")
        path = _batch_result(batch_dir, image)
        return _archived_image_response(path.name, path.read_bytes(), download=False)


conversion_jobs = ConversionJobs(os.environ.get('CONVERSION_JOB_ROOT', ROOT / 'runtime' / 'conversions'), _workspace_owner, _write_upload,
                                 max_files=MAX_BATCH_PRODUCTS, max_file_mb=MAX_UPLOAD_MB, max_batch_bytes=MAX_BATCH_UPLOAD_BYTES)
app.include_router(conversion_jobs.router)
app.include_router(create_account_router(account_store))


if WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets", check_dir=False), name="assets")


@app.get("/{path:path}")
def serve_frontend(request: Request, path: str = ""):
    index = WEB_DIST / "index.html"
    if index.is_file():
        requested = (WEB_DIST / path).resolve()
        if requested.is_file() and WEB_DIST.resolve() in requested.parents:
            return FileResponse(requested)
        response = FileResponse(index, headers={"Cache-Control": "private, no-cache"})
        _workspace_owner(request, response)
        return response
    return JSONResponse(
        {"message": "前端尚未构建，请运行 npm install && npm run build，或使用 npm run dev。"},
        status_code=404,
    )
