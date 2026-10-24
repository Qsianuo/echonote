#!/usr/bin/env python
"""Start EchoNote so a phone on the same Wi-Fi can use it.

Why this exists: the normal launcher binds to 127.0.0.1 (audio must not be
reachable from the network by default). Opening it up requires three
coordinated settings — bind address, access token, allowed origins — plus a
TLS certificate, because **a phone browser will not grant microphone access
over plain http** (getUserMedia needs a secure context, and http://<LAN-IP>
is not one). This script does all of it:

* picks the LAN IPv4 and binds 0.0.0.0 (ECHONOTE_ALLOW_NONLOCAL + a sticky
  access token under models/lan-token.txt);
* generates a self-signed certificate once (models/lan-cert.pem, valid 10
  years, re-made when the LAN IP changes) and serves HTTPS;
* exposes the certificate at /lan-cert.pem so the phone can download and
  trust it in one visit;
* prints the URL **with the token** and a QR code to scan.

Usage:
    python tools/serve_lan.py            # https (recommended, mic works)
    python tools/serve_lan.py --http     # page only; mic blocked on phones
    python tools/serve_lan.py --port 8443
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL_DIR = ROOT / "models"
TOKEN_FILE = MODEL_DIR / "lan-token.txt"
CERT_FILE = MODEL_DIR / "lan-cert.pem"
KEY_FILE = MODEL_DIR / "lan-key.pem"
CERT_IP_FILE = MODEL_DIR / "lan-cert.ip.txt"


def lan_ip() -> str:
    """The IPv4 this machine answers to on the LAN (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("223.5.5.5", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def load_or_create_token() -> str:
    """A stable random token: regenerating per launch would break the saved
    bookmark / home-screen shortcut on the phone."""
    try:
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(12)
    TOKEN_FILE.write_text(value, encoding="utf-8")
    return value


def ensure_cert(ip: str) -> tuple[Path, Path] | None:
    """A 10-year self-signed cert for this LAN IP, regenerated if the IP
    changed (DHCP). Returns None when no openssl is on PATH."""
    openssl = shutil.which("openssl")
    if not openssl:
        print("  !! 未找到 openssl，无法启用 HTTPS；手机麦克风将不可用")
        return None

    fresh = CERT_FILE.is_file() and KEY_FILE.is_file()
    if fresh:
        try:
            fresh = CERT_IP_FILE.read_text(encoding="utf-8").strip() == ip
        except OSError:
            fresh = False
    if fresh:
        return CERT_FILE, KEY_FILE

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(KEY_FILE),
            "-out",
            str(CERT_FILE),
            "-days",
            "3650",
            "-nodes",
            "-subj",
            "/CN=EchoNote-LAN",
            # Chrome ignores CN and only reads subjectAltName, so the LAN IP
            # must be listed here or the phone will warn even after trusting.
            "-addext",
            f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{ip}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  !! 证书生成失败：{result.stderr.strip()[:200]}")
        return None
    CERT_IP_FILE.write_text(ip, encoding="utf-8")
    return CERT_FILE, KEY_FILE


def print_qr(url: str) -> None:
    """Two QRs, because one of them always works: the console one only
    renders when the console font has the half-block characters (the default
    Windows console font often does not — which is exactly why the QR was
    reported "missing"), the PNG one is popped in the default image viewer
    and is font-proof."""
    try:
        import qrcode
    except ImportError:
        return
    code = qrcode.QRCode(border=1)
    code.add_data(url)
    code.make(fit=True)
    try:
        code.print_ascii(invert=True)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    try:
        import os

        png_path = MODEL_DIR / "lan-qr.png"
        qrcode.make(url).save(str(png_path))
        os.startfile(str(png_path))  # type: ignore[attr-defined] - Windows only
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EchoNote LAN/手机访问启动器")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument(
        "--http",
        action="store_true",
        help="不启用 HTTPS（手机上能用界面，但麦克风被浏览器禁止）",
    )
    args = parser.parse_args(argv)

    ip = lan_ip()
    token = load_or_create_token()
    cert_pair = None if args.http else ensure_cert(ip)
    scheme = "https" if cert_pair else "http"

    # All of these are read at import time by app.config, so they must be in
    # the environment *before* the app module is imported below.
    os.environ.update(
        {
            "ECHONOTE_HOST": "0.0.0.0",
            "ECHONOTE_PORT": str(args.port),
            "ECHONOTE_ALLOW_NONLOCAL": "1",
            "ECHONOTE_ACCESS_TOKEN": token,
            "ECHONOTE_ALLOWED_ORIGINS": ",".join(
                [f"http://{ip}:{args.port}", f"https://{ip}:{args.port}"]
            ),
            "ECHONOTE_NO_BROWSER": "1",
        }
    )

    from app import main as app_main  # noqa: E402 - env must be set first
    import uvicorn  # noqa: E402

    if cert_pair:
        # Let the phone fetch the certificate itself; no USB stick needed.
        # The route has to go in *before* the StaticFiles mount at "/" — that
        # mount matches every path, so a route appended after it would never
        # be reached (found the hard way: /lan-cert.pem answered 404).
        from fastapi.responses import FileResponse

        def _cert() -> FileResponse:
            return FileResponse(str(CERT_FILE), filename="echonote-lan-cert.pem")

        routes = app_main.app.router.routes
        # Newer Starlette leaves ``Mount.path`` normalised/empty, so find the
        # static mount by type — it is the only one in this app.
        from starlette.routing import Mount as _Mount

        mount_index = next(i for i, r in enumerate(routes) if isinstance(r, _Mount))
        mount = routes.pop(mount_index)
        app_main.app.get("/lan-cert.pem", include_in_schema=False)(_cert)
        routes.append(mount)

    url = f"{scheme}://{ip}:{args.port}/?token={token}"
    print()
    print("═" * 62)
    print("  EchoNote · 手机访问模式")
    print("═" * 62)
    print(f"  手机与电脑连同一个 Wi-Fi，扫码或输入：")
    print()
    print(f"      {url}")
    print()
    print_qr(url)
    print("  首次使用（HTTPS 自签证书，每台手机做一次）：")
    print(f"    1. 打开上面的地址 → 浏览器提示证书风险 → 选「继续访问」")
    print(f"    2. 再访问 {scheme}://{ip}:{args.port}/lan-cert.pem 下载证书")
    print(f"    3. 安卓：设置→安全→加密与凭据→安装证书→CA 证书")
    print(f"       iOS：设置→已下载的描述文件→安装，再到")
    print(f"             设置→通用→关于本机→证书信任设置→打开完全信任")
    print(f"    4. 回到页面刷新即可用麦克风录音")
    print()
    print("  安卓免装证书的替代（需改用 --http 启动本程序）：USB 连电脑执行")
    print(f"       adb reverse tcp:{args.port} tcp:{args.port}，")
    print(f"       手机开 http://localhost:{args.port}/?token={token}")
    print()
    print("  · 首次启动若 Windows 防火墙弹窗，选择「允许」")
    print(f"  · 访问令牌（已固定，重开不变）：{token}")
    print(f"  · 本机浏览器用：{scheme}://127.0.0.1:{args.port}/?token={token}")
    print("  · 二维码图片也已弹出（找不到就看 models/lan-qr.png）")
    print("═" * 62)
    print()

    uvicorn.run(
        app_main.app,
        host="0.0.0.0",
        port=args.port,
        log_level="info",
        ssl_certfile=str(CERT_FILE) if cert_pair else None,
        ssl_keyfile=str(KEY_FILE) if cert_pair else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
