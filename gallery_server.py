#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""video-thumb-lister 的独立后台画廊服务（可单独运行）。

为什么独立：画廊 HTML 里的缩略图、视频播放、『打开文件浏览器』、『用 PotPlayer 播放』
都依赖一个本地 HTTP 服务。把这份服务从主程序（video_thumb_lister.py）里解耦出来，
做成单独可运行的程序，这样：

  - 即使主程序（GUI / 扫描器）已经关闭，只要本服务在运行，双击生成的画廊 .html
    文件（以 file:// 打开）也能正常显示缩略图、调起 PotPlayer、打开所在文件夹；
  - 用户可手动启动本服务，无需启动主程序：

        python gallery_server.py --dir "D:/你的画廊输出目录"

服务默认绑定 127.0.0.1:8765；可用环境变量 VTL_GALLERY_PORT 或 --port 覆盖
（注意：画廊 HTML 里写死的服务器地址端口必须与本服务一致，二者共用同一默认值/环境变量）。

端点：
  /                              -> 重定向到 /<gallery_name>
  /<gallery_name>.html           -> 画廊页面（同一输出目录内可有多份画廊）
  /thumb?path=<缩略图绝对路径>   -> 该视频的封面缩略图文件（jpg/webp/png，按扩展名返回正确 MIME）
  /video?path=<视频绝对路径>      -> 原视频文件（支持 Range，可在弹窗中播放/拖拽）
  /open-explorer?path=<目录绝对路径> -> 调用系统文件浏览器定位到该目录
  /play?path=<视频绝对路径>       -> 用系统默认视频播放器（如 PotPlayer）打开并播放该视频
"""

import os
import sys
import shutil
import threading
import subprocess
import http.server
import urllib.parse
import argparse

# --------------------------------------------------------------------------
# 端口：画廊 HTML 与服务器都必须用同一个端口，故抽成统一常量。
# 优先级：命令行 --port > 环境变量 VTL_GALLERY_PORT > 默认 8765。
# --------------------------------------------------------------------------
DEFAULT_PORT = 8765

# 1x1 透明 PNG（用于 /ping 健康探测：file:// 页面用 Image() 跨域探测时，
# 只有真正的图片资源才会触发 onload，文本响应会触发 onerror 造成误报）。
PING_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300000002000001e221bc330000000049454e44ae426082"
)


def gallery_server_port(explicit=None):
    """返回服务端口（整数）。explicit 为命令行 --port 覆盖值（可为 None）。"""
    if explicit:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    env = os.environ.get("VTL_GALLERY_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_PORT


def is_server_up(port=None):
    """探测 127.0.0.1:port 是否已有服务在监听（用于复用而非重复启动）。"""
    import socket
    port = gallery_server_port(port)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except Exception:  # noqa: BLE001
        return False


def gallery_server_base(port=None):
    """返回画廊 HTML 里写死的服务器地址前缀，例如 http://127.0.0.1:8765。"""
    return "http://127.0.0.1:%d" % gallery_server_port(port)


# --------------------------------------------------------------------------
# 打开视频：用操作系统【默认关联程序】播放（即「在文件浏览器中双击文件」的效果，
# 由本机默认视频播放器——如 PotPlayer——打开并播放）。无需定位特定播放器可执行文件。
# --------------------------------------------------------------------------
def open_with_default(path):
    """用系统默认程序打开文件（等价于在资源管理器双击，由默认播放器接管）。返回是否成功。"""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # 资源管理器「打开」动词：默认视频播放器（PotPlayer 等）直接播放
        elif sys.platform == "darwin":
            subprocess.run(["open", path], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        else:
            subprocess.run(["xdg-open", path], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        return True
    except Exception:  # noqa: BLE001
        return False


def _cors(handler):
    """给响应加 CORS 头，允许 file://（null 源）页面发起 fetch 到本服务。"""
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def serve_gallery(out_dir, port=0, gallery_name="gallery.html"):
    """启动本地 HTTP 服务（守护线程），返回 (httpd, port)。

    画廊通过 http://127.0.0.1:port/<gallery_name> 访问；并提供：
      /thumb /video /open-explorer /play 端点（详见模块 docstring）。
    """
    out_dir = os.path.abspath(out_dir)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静默访问日志
            pass

        def _ct_for(self, path):
            ext = os.path.splitext(path)[1].lower()
            return {
                ".mp4": "video/mp4", ".mkv": "video/x-matroska",
                ".webm": "video/webm", ".mov": "video/quicktime",
                ".avi": "video/x-msvideo", ".m4v": "video/x-m4v",
                ".ts": "video/mp4", ".ogv": "video/ogg",
            }.get(ext, "application/octet-stream")

        def _ct_for_image(self, path):
            """按扩展名返回图片 MIME（封面缩略图可能是 jpg/webp/png）。"""
            ext = os.path.splitext(path)[1].lower()
            return {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp",
                ".gif": "image/gif", ".bmp": "image/bmp",
            }.get(ext, "application/octet-stream")

        def _serve_file(self, fpath, ctype, range_ok=True):
            if not os.path.isfile(fpath):
                self.send_error(404)
                return
            size = os.path.getsize(fpath)
            rng = self.headers.get("Range") if range_ok else None
            if rng and rng.startswith("bytes="):
                try:
                    spec = rng[len("bytes="):].split(",")[0].strip()
                    s, e = spec.split("-")
                    start = int(s) if s else 0
                    end = int(e) if e else size - 1
                    if end >= size:
                        end = size - 1
                    if start > end:
                        raise ValueError
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Type", ctype)
                    _cors(self)
                    self.end_headers()
                    with open(fpath, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(length))
                    return
                except Exception:  # noqa: BLE001 - 回退到整文件
                    pass
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            _cors(self)
            self.end_headers()
            with open(fpath, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

        def do_OPTIONS(self):
            self.send_response(204)
            _cors(self)
            self.end_headers()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            if path in ("/", ""):
                self.send_response(302)
                self.send_header("Location", "/" + self.server.gallery_name)
                _cors(self)
                self.end_headers()
            elif path.endswith(".html"):
                fname = path[1:]
                if "/" in fname or "\\" in fname:
                    self.send_error(400)
                    return
                self._serve_file(
                    os.path.join(self.server.out_dir, fname),
                    "text/html; charset=utf-8", range_ok=False)
            elif path == "/thumb":
                p = qs.get("path", [""])[0]
                if p:
                    self._serve_file(p, self._ct_for_image(p), range_ok=False)
                else:
                    self.send_error(400)
            elif path == "/video":
                p = qs.get("path", [""])[0]
                if p:
                    self._serve_file(p, self._ct_for(p))
                else:
                    self.send_error(400)
            elif path == "/open-explorer":
                p = qs.get("path", [""])[0]
                ok = False
                if p and os.path.isdir(p):
                    try:
                        if sys.platform == "win32":
                            subprocess.run(
                                ["explorer.exe", "/select,", p],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            )
                        elif sys.platform == "darwin":
                            subprocess.run(["open", p])
                        else:
                            subprocess.run(["xdg-open", p])
                        ok = True
                    except Exception:  # noqa: BLE001
                        ok = False
                self.send_response(200)
                body = b"ok" if ok else b"err"
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                _cors(self)
                self.end_headers()
                self.wfile.write(body)
            elif path == "/ping":
                # 健康探测：返回 1x1 PNG（供 file:// 页面用 Image() 跨域探测服务是否在线）。
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(PING_PNG)))
                _cors(self)
                self.end_headers()
                self.wfile.write(PING_PNG)
            elif path == "/play":
                p = qs.get("path", [""])[0]
                if not p or not os.path.isfile(p):
                    self.send_error(404)
                    return
                # 用系统默认关联程序打开视频（默认视频播放器接管，无需定位具体 exe）。
                ok = open_with_default(p)
                self.send_response(200)
                body = b"ok" if ok else b"err"
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                _cors(self)
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.out_dir = out_dir
    httpd.gallery_name = gallery_name
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def run_server(out_dir, gallery_name, port=0, port_file=None):
    """启动画廊 HTTP 服务并【永久阻塞】（供独立进程 / __main__ 使用）。

    port_file 非空时，把实际绑定端口写入该文件，供调用方回读。
    """
    httpd, bound_port = serve_gallery(out_dir, port=port, gallery_name=gallery_name)
    if port_file:
        try:
            with open(port_file, "w", encoding="utf-8") as f:
                f.write(str(bound_port))
        except Exception:  # noqa: BLE001
            pass
    print(f"gallery_server 正在服务：http://127.0.0.1:{bound_port}/{gallery_name}")
    print(f"  托管的输出目录：{os.path.abspath(out_dir)}")
    print("  按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="video-thumb-lister 的独立后台画廊服务（可单独运行）")
    ap.add_argument("--dir", default=os.getcwd(),
                   help="画廊 HTML 所在输出目录（默认当前目录）")
    ap.add_argument("--name", default="gallery.html",
                   help="画廊文件名（默认 gallery.html）")
    ap.add_argument("--port", type=int, default=None,
                   help="绑定端口（默认 8765，或环境变量 VTL_GALLERY_PORT）")
    ap.add_argument("--port-file", default=None,
                   help="把实际绑定端口写入该文件（供调用方回读）")
    ap.add_argument("--open", action="store_true",
                   help="启动后自动用浏览器打开画廊页面")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.dir):
        print(f"错误：目录不存在或无效：{args.dir}", file=sys.stderr)
        sys.exit(1)

    port = gallery_server_port(args.port)
    httpd, bound_port = serve_gallery(args.dir, port=port, gallery_name=args.name)
    if args.port_file:
        try:
            with open(args.port_file, "w", encoding="utf-8") as f:
                f.write(str(bound_port))
        except Exception:  # noqa: BLE001
            pass
    url = f"http://127.0.0.1:{bound_port}/{args.name}"
    print(f"gallery_server 正在服务：{url}")
    print(f"  托管的输出目录：{os.path.abspath(args.dir)}")
    print("  按 Ctrl+C 停止。")
    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        httpd.server_close()


if __name__ == "__main__":
    main()
