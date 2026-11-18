"""Read-only release smoke check, usable from deployment scripts or uptime monitors."""
import argparse
import json
import re
import sys
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen


def verify(base, expected_asset=None):
    base = base.rstrip("/") + "/"
    def read(path):
        with urlopen(urljoin(base, path), timeout=15) as response:
            if response.status != 200:
                raise ValueError(f"{path}: HTTP {response.status}")
            return response.read()
    for path in ("api/health", "api/ready"):
        if json.loads(read(path)).get("status") != "ok":
            raise ValueError(f"{path}: service not ready")
    html = read("").decode("utf-8")
    assets = re.findall(r'(?:src|href)="(/assets/[^"<>]+)"', html)
    if not any(asset.endswith(".js") for asset in assets) or not any(asset.endswith(".css") for asset in assets):
        raise ValueError("Missing frontend JS/CSS")
    if expected_asset and not any(expected_asset == asset.rsplit("/", 1)[-1] for asset in assets):
        raise ValueError("Frontend release does not match expected artifact")
    for path in assets:
        if not read(path).strip():
            raise ValueError(f"Empty asset: {path}")
    return {"status": "ok", "assets": assets}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Base URL, for example http://127.0.0.1:8000")
    parser.add_argument("--expected-asset", help="Exact JS filename produced by the release build")
    args = parser.parse_args()
    if urlsplit(args.url).scheme not in {"http", "https"}:
        parser.error("URL must use HTTP or HTTPS")
    try:
        print(json.dumps(verify(args.url, args.expected_asset)))
    except Exception as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        sys.exit(1)
