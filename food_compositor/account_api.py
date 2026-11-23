"""Login endpoints for the workbench."""
from __future__ import annotations

import json
import time
from collections import OrderedDict, deque
from ipaddress import ip_address
from threading import BoundedSemaphore, Lock

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .accounts import AccountStore, DuplicateAccount, SESSION_SECONDS

_HTTPS_COOKIE = "__Host-workflow_session"
_LOCAL_COOKIE = "workflow_local_session"
_PRIVATE_HEADERS = {"Cache-Control": "no-store"}


def _cookie_name(request: Request) -> str | None:
    if request.url.scheme == "https":
        return _HTTPS_COOKIE
    if request.url.hostname in {"localhost", "127.0.0.1", "::1"} and request.client:
        try:
            if ip_address(request.client.host).is_loopback:
                return _LOCAL_COOKIE
        except ValueError:
            pass
    return None


def current_account(store: AccountStore, request: Request):
    cookie = _cookie_name(request)
    return store.session(request.cookies.get(cookie, "")) if cookie else None


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if (origin and origin != f"{request.url.scheme}://{request.url.netloc}") or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="请求来源不正确")


def _require_auth_post(request: Request) -> str:
    cookie = _cookie_name(request)
    if cookie is None:
        raise HTTPException(status_code=403, detail="请通过 HTTPS 登录")
    check_origin(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="登录请求格式不正确")
    return cookie


class _LoginLimits:
    def __init__(self):
        self.lock = Lock()
        self.attempts = OrderedDict()

    def allow(self, keys) -> bool:
        now = time.monotonic()
        with self.lock:
            counters = []
            for key, limit in keys:
                recent = self.attempts.setdefault(key, deque())
                self.attempts.move_to_end(key)
                while recent and recent[0] <= now - 300:
                    recent.popleft()
                counters.append((recent, limit))
            while len(self.attempts) > 4096:
                self.attempts.popitem(last=False)
            if any(len(recent) >= limit for recent, limit in counters):
                return False
            for recent, _ in counters:
                recent.append(now)
            return True


def create_account_router(store: AccountStore) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["账号"])
    limits = _LoginLimits()
    hash_slots = BoundedSemaphore(2)

    @router.get("/session")
    def session(request: Request):
        account = current_account(store, request)
        return JSONResponse({"account": account}, headers=_PRIVATE_HEADERS)

    async def authenticate(request: Request, *, register: bool):
        cookie = _require_auth_post(request)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                raise HTTPException(status_code=413, detail="登录请求过大")
            body.extend(chunk)
        try:
            payload = json.loads(body)
            username, password = payload["username"], payload["password"]
            if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > 128:
                raise ValueError()
            password.encode("utf-8")
        except (ValueError, TypeError, KeyError):
            raise HTTPException(status_code=400, detail="请输入有效的账号和密码") from None
        if not hash_slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="登录请求较多，请稍后重试", headers={"Retry-After": "2"})
        try:
            ip = request.client.host if request.client else "unknown"
            keys = [(("register", ip), 12)] if register else [(("ip", ip), 40), (("account", username.strip().lower()), 12)]
            if not limits.allow(keys):
                raise HTTPException(status_code=429, detail="尝试次数过多，请稍后重试", headers={"Retry-After": "300"})
            previous_token = request.cookies.get(cookie, "")
            if register:
                try:
                    result = await run_in_threadpool(store.create, username, "", password, previous_token=previous_token)
                except DuplicateAccount as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from None
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from None
            else:
                result = await run_in_threadpool(store.sign_in, username, password, previous_token)
        finally:
            hash_slots.release()
        if result is None:
            raise HTTPException(status_code=401, detail="账号或密码错误")
        account, token = result
        response = JSONResponse({"account": account}, status_code=201 if register else 200, headers=_PRIVATE_HEADERS)
        response.set_cookie(cookie, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=cookie == _HTTPS_COOKIE, samesite="strict", path="/")
        return response

    @router.post("/login")
    async def login(request: Request):
        return await authenticate(request, register=False)

    @router.post("/register")
    async def register(request: Request):
        return await authenticate(request, register=True)

    @router.post("/logout")
    def logout(request: Request):
        cookie = _require_auth_post(request)
        store.sign_out(request.cookies.get(cookie, ""))
        response = JSONResponse({"account": None}, headers=_PRIVATE_HEADERS)
        response.delete_cookie(cookie, httponly=True, secure=cookie == _HTTPS_COOKIE, samesite="strict", path="/")
        return response

    return router
