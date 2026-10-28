"""Minimal OSS V1 transport for the template library.

Uses Python's standard HTTPS and HMAC implementation, so local deployments do
not need native crypto extensions for ordinary object operations.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace


class OssError(Exception):
    pass


class NoSuchKey(OssError):
    pass


exceptions = SimpleNamespace(OssError=OssError, NoSuchKey=NoSuchKey)


class Auth:
    def __init__(self, access_key_id: str, access_key_secret: str):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret


class Bucket:
    def __init__(self, auth: Auth, endpoint: str, bucket: str, connect_timeout: int = 15):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('OSS_ENDPOINT 必须是 HTTPS OSS 服务地址')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
            raise ValueError('OSS_BUCKET 名称无效')
        self.auth = auth
        self.bucket = bucket
        self.base_url = f'https://{bucket}.{parsed.netloc}'
        self.timeout = connect_timeout

    def _request(self, method: str, key: str = '', *, params: dict | None = None, data=None, content_type: str = ''):
        date = formatdate(usegmt=True)
        resource = f'/{self.bucket}/{key}'
        sign_data = f'{method}\n\n{content_type}\n{date}\n{resource}'.encode('utf-8')
        digest = hmac.new(self.auth.access_key_secret.encode('utf-8'), sign_data, hashlib.sha1).digest()
        signature = base64.b64encode(digest).decode('ascii')
        headers = {'Date': date, 'Authorization': f'OSS {self.auth.access_key_id}:{signature}'}
        if content_type:
            headers['Content-Type'] = content_type
        url = self.base_url + '/' + urllib.parse.quote(key, safe='/')
        if params:
            url += '?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        # Reads are safe to retry after transient network failures. Mutating
        # operations are not retried automatically because their outcome may
        # be unknown if the connection drops after the server accepts them.
        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                if code == 404:
                    raise NoSuchKey('OSS 文件不存在') from None
                if code >= 500 and attempt + 1 < attempts:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                raise OssError(f'OSS 请求失败（HTTP {code}）') from None
            except (urllib.error.URLError, OSError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                raise OssError(f'OSS 连接失败（{type(reason).__name__}）') from None

    def get_object(self, key: str):
        return self._request('GET', key)

    def get_object_to_file(self, key: str, filename: str):
        with self.get_object(key) as source, open(filename, 'wb') as target:
            while chunk := source.read(65536):
                target.write(chunk)

    def put_object_from_file(self, key: str, filename: str):
        with open(filename, 'rb') as source:
            self.put_object(key, source.read())

    def put_object(self, key: str, data):
        if isinstance(data, str):
            data = data.encode('utf-8')
        content_type = mimetypes.guess_type(key)[0] or 'application/octet-stream'
        with self._request('PUT', key, data=data, content_type=content_type):
            pass

    def delete_object(self, key: str):
        with self._request('DELETE', key):
            pass


def ObjectIteratorV2(bucket: Bucket, prefix: str = ''):
    token = None
    while True:
        params = {'list-type': '2', 'prefix': prefix, 'encoding-type': 'url'}
        if token:
            params['continuation-token'] = token
        try:
            with bucket._request('GET', params=params) as response:
                root = ET.fromstring(response.read())
        except ET.ParseError as exc:
            raise OssError('OSS 列表响应格式不正确') from exc
        for obj in root.findall('{*}Contents'):
            key = obj.findtext('{*}Key')
            if key is not None:
                yield SimpleNamespace(key=urllib.parse.unquote(key))
        if root.findtext('{*}IsTruncated', '').lower() != 'true':
            break
        next_token = root.findtext('{*}NextContinuationToken')
        if not next_token or next_token == token:
            raise OssError('OSS 列表分页响应不正确')
        token = next_token
