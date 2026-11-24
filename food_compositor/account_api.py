"""Login endpoints for the workbench."""
from __future__ import annotations

import json
import secrets
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from ipaddress import ip_address
from threading import BoundedSemaphore, Lock

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .accounts import AccountStore, DuplicateAccount, REMEMBER_SECONDS

_HTTPS_COOKIE = "__Host-workflow_session"
_LOCAL_COOKIE = "workflow_local_session"
_PRIVATE_HEADERS = {"Cache-Control": "no-store"}
_MAX_AUTH_BODY_BYTES = 1024 * 1024


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


def _session_response(cookie: str, account, remembered, *, session_token: str = "", status: int = 200):
    response = JSONResponse({"account": account, "remembered": remembered}, headers=_PRIVATE_HEADERS, status_code=status)
    options = dict(httponly=True, secure=cookie == _HTTPS_COOKIE, samesite="strict", path="/")
    if session_token:
        response.set_cookie(cookie, session_token, **options)
    else:
        response.delete_cookie(cookie, **options)
        if remembered is None:
            response.delete_cookie(cookie + "_device", **options)
    return response


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


async def _auth_payload(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_AUTH_BODY_BYTES:
            raise HTTPException(status_code=413, detail="账号请求过大")
        body.extend(chunk)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="账号请求格式不正确") from None
    return data


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

    @contextmanager
    def credential_attempt(keys):
        if not hash_slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="账号请求较多，请稍后重试", headers={"Retry-After": "2"})
        try:
            if not limits.allow(keys):
                raise HTTPException(status_code=429, detail="尝试次数过多，请稍后重试", headers={"Retry-After": "300"})
            yield
        finally:
            hash_slots.release()

    @router.get("/session")
    def session(request: Request):
        cookie = _cookie_name(request)
        remembered = store.remembered_account(request.cookies.get(cookie + "_device", "")) if cookie else None
        return JSONResponse({"account": current_account(store, request), "remembered": remembered}, headers=_PRIVATE_HEADERS)

    async def authenticate(request: Request, *, register: bool):
        cookie = _require_auth_post(request)
        payload = await _auth_payload(request)
        try:
            username, password = payload["username"], payload["password"]
            remember = payload.get("remember_device", True)
            if not isinstance(remember, bool) or not isinstance(username, str) or not isinstance(password, str) or len(username) > 64:
                raise ValueError()
            password.encode("utf-8")
        except (ValueError, TypeError, KeyError):
            raise HTTPException(status_code=400, detail="请输入有效的账号和密码") from None
        ip = request.client.host if request.client else "unknown"
        keys = [(("register", ip), 12)] if register else [(("ip", ip), 40), (("account", username.strip().lower()), 12)]
        with credential_attempt(keys):
            previous_token = request.cookies.get(cookie, "")
            device = secrets.token_hex(32) if remember else ""
            device_args = dict(previous_device=request.cookies.get(cookie + "_device", ""), new_device=device)
            if register:
                try:
                    result = await run_in_threadpool(store.create, username, "", password, previous_token=previous_token, **device_args)
                except DuplicateAccount as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from None
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from None
            else:
                result = await run_in_threadpool(store.sign_in, username, password, previous_token, **device_args)
        if result is None:
            raise HTTPException(status_code=401, detail="账号或密码错误")
        account, token = result
        response = _session_response(cookie, account, account if remember else None, session_token=token,
                                     status=201 if register else 200)
        options = dict(httponly=True, secure=cookie == _HTTPS_COOKIE, samesite="strict", path="/")
        if remember:
            response.set_cookie(cookie + "_device", device, max_age=REMEMBER_SECONDS, **options)
        else:
            response.delete_cookie(cookie + "_device", **options)
        return response

    @router.post("/login")
    async def login(request: Request):
        return await authenticate(request, register=False)

    @router.post("/register")
    async def register(request: Request):
        return await authenticate(request, register=True)

    @router.post("/change-password")
    async def change_password(request: Request):
        cookie = _require_auth_post(request)
        token = request.cookies.get(cookie, "")
        account = await run_in_threadpool(store.session, token)
        if not account:
            raise HTTPException(status_code=401, detail="登录已失效，请重新登录", headers=_PRIVATE_HEADERS)
        payload = await _auth_payload(request)
        if payload.get("username") != account["username"]:
            raise HTTPException(status_code=409, detail="当前登录账号已变化，请重新打开修改密码", headers=_PRIVATE_HEADERS)
        current, new = payload.get("current_password"), payload.get("new_password")
        try:
            if not isinstance(current, str) or not current or not isinstance(new, str) or not new:
                raise ValueError()
            current.encode("utf-8")
            new.encode("utf-8")
        except ValueError:
            raise HTTPException(status_code=400, detail="请输入有效的原密码和新密码", headers=_PRIVATE_HEADERS) from None
        ip = request.client.host if request.client else "unknown"
        with credential_attempt([(("change-ip", ip), 40), (("change-account", account["username"]), 12)]):
            try:
                changed = await run_in_threadpool(store.change_password, token, account["username"], current, new)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc), headers=_PRIVATE_HEADERS) from None
        if not changed:
            raise HTTPException(status_code=401, detail="登录已失效，请重新登录", headers=_PRIVATE_HEADERS)
        return _session_response(cookie, None, None)

    @router.post("/logout")
    def logout(request: Request, forget: bool = True):
        cookie = _require_auth_post(request)
        device = request.cookies.get(cookie + "_device", "")
        store.sign_out(request.cookies.get(cookie, ""), device=device, forget=forget)
        return _session_response(cookie, None, None if forget else store.remembered_account(device))

    @router.post("/resume/{username}")
    def resume(request: Request, username: str):
        cookie = _require_auth_post(request)
        device = request.cookies.get(cookie + "_device", "")
        result = store.resume(device, username, request.cookies.get(cookie, ""))
        if not result:
            raise HTTPException(status_code=401, detail="此账号的快捷登录已失效，请使用密码登录", headers=_PRIVATE_HEADERS)
        account, token = result
        return _session_response(cookie, account, account, session_token=token)

    return router
