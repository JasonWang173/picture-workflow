"""Reject oversized requests before multipart parsing and bound task admission."""
from __future__ import annotations

import os
import shutil
import time
from collections import OrderedDict, deque
from threading import Lock

from starlette.responses import JSONResponse
from starlette.formparsers import MultiPartException


class BodyTooLarge(MultiPartException):
    pass


class RequestLimits:
    def __init__(self, app, storage):
        self.app, self.storage = app, storage
        self.max_bytes = int(os.environ.get("MAX_BATCH_UPLOAD_MB", "512")) * 1024 * 1024 + 4 * 1024 * 1024
        self.per_client = int(os.environ.get("MAX_ACTIVE_TASKS_PER_CLIENT", "2"))
        self.max_active = int(os.environ.get("MAX_ACTIVE_TASKS", "4"))
        self.per_minute = int(os.environ.get("MAX_TASKS_PER_MINUTE", "10"))
        self.disk_reserve = int(os.environ.get("MIN_FREE_DISK_MB", "1024")) * 1024 * 1024
        self.lock = Lock()
        self.active = {}
        self.recent = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        composing = scope["method"] == "POST" and scope["path"] in {"/api/compose", "/api/compose-batch"}
        uploading = composing or (scope["method"] == "POST" and scope["path"] == "/api/templates")
        if not uploading:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "上传长度无效"}, status_code=400)(scope, receive, send)
        if length > self.max_bytes:
            return await JSONResponse({"detail": "上传总大小超过限制，请分批上传"}, status_code=413)(scope, receive, send)
        if shutil.disk_usage(self.storage).free < self.disk_reserve + max(length, 24 * 1024 * 1024):
            return await JSONResponse({"detail": "服务器磁盘空间不足，请稍后重试"}, status_code=507)(scope, receive, send)
        # Use the ASGI peer; forwarded headers must be resolved only by a trusted proxy.
        client = (scope.get("client") or ("unknown",))[0]
        if composing:
            with self.lock:
                now = time.monotonic()
                recent = self.recent.setdefault(client, deque())
                while recent and recent[0] <= now - 60:
                    recent.popleft()
                rejected = (self.active.get(client, 0) >= self.per_client or sum(self.active.values()) >= self.max_active or len(recent) >= self.per_minute)
                if not rejected:
                    recent.append(now)
                    self.active[client] = self.active.get(client, 0) + 1
                self.recent.move_to_end(client)
                while len(self.recent) > 4096:
                    self.recent.popitem(last=False)
            if rejected:
                return await JSONResponse({"detail": "当前任务较多，请等待任务完成后再提交"}, status_code=429,
                                          headers={"Retry-After": "10"})(scope, receive, send)
        transferred = 0
        too_large = False
        async def limited_receive():
            nonlocal transferred, too_large
            message = await receive()
            transferred += len(message.get("body", b""))
            if transferred > self.max_bytes:
                too_large = True
                raise BodyTooLarge("上传总大小超过限制，请分批上传")
            return message
        async def limited_send(message):
            if too_large and message["type"] == "http.response.start":
                message = {**message, "status": 413}
            await send(message)
        try:
            await self.app(scope, limited_receive, limited_send)
        except BodyTooLarge:
            await JSONResponse({"detail": "上传总大小超过限制，请分批上传"}, status_code=413)(scope, receive, send)
        finally:
            if composing:
                with self.lock:
                    self.active[client] -= 1
                    if not self.active[client]:
                        del self.active[client]
