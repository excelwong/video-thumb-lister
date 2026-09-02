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
  /ping                          -> 返回服务代码版本号（纯文本整数，供 GUI 探测
                                     在线服务是否为旧版本，以便自动重启）
  /thumb?path=<封面图片绝对路径>  -> 封面图片（jpg/jpeg/webp 为目录下已有封面，
                                     png 为无封面时 ffmpeg 抽第 10 秒帧生成的）
                                     【兼容旧画廊】path 也可以是视频绝对路径，
                                     服务端会代为解析出封面再返回
  /video?path=<视频绝对路径>      -> 原视频文件（支持 Range，可在弹窗中播放/拖拽）
  /open-explorer?path=<目录绝对路径> -> 调用系统文件浏览器定位到该目录
  /play?path=<视频绝对路径>       -> 调起本机 PotPlayer 播放该视频
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

# 服务代码版本号：/thumb 等端点契约发生变化时 +1。
# GUI 打开画廊前会用 /ping 探测在线服务的版本，旧版本进程会被自动结束并重启，
# 避免「代码更新了、常驻服务还在跑旧逻辑」导致缩略图 404 之类的错配。
SERVER_VERSION = 2


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
# 本地播放器：仅 PotPlayer（点击缩略图时唤起本机 PotPlayer 播放）。
# --------------------------------------------------------------------------
_PLAYER_PORTABLE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local")),
    "Programs", "PotPlayer")
PLAYER_CANDIDATES = [
    os.path.join(_PLAYER_PORTABLE_DIR, "PotPlayerMini64.exe"),
    os.path.join(_PLAYER_PORTABLE_DIR, "PotPlayer64.exe"),
    r"C:/Program Files/PotPlayer/PotPlayerMini64.exe",
    r"C:/Program Files/PotPlayer/PotPlayer64.exe",
    r"C:/Program Files/PotPlayer/PotPlayer.exe",
    r"C:/Program Files (x86)/PotPlayer/PotPlayer.exe",
    r"C:/PotPlayer/PotPlayer.exe",
    "PotPlayerMini64.exe",
    "PotPlayer64.exe",
    "PotPlayer.exe",
]


def find_player():
    """返回本机可用的 PotPlayer 可执行文件路径；找不到返回 None。"""
    for c in PLAYER_CANDIDATES:
        if os.path.isfile(c):
            return c
    for name in ("PotPlayerMini64.exe", "PotPlayer64.exe", "PotPlayer.exe"):
        p = shutil.which(name)
        if p:
            return p
    try:
        import glob as _glob
        base = os.path.dirname(_PLAYER_PORTABLE_DIR)
        for pat in (os.path.join(base, "**", "PotPlayerMini64.exe"),
                    os.path.join(base, "**", "PotPlayer64.exe"),
                    os.path.join(base, "**", "PotPlayer.exe")):
            hits = _glob.glob(pat, recursive=True)
            if hits:
                return hits[0]
    except Exception:  # noqa: BLE001
        pass
    return None


def _cors(handler):
    """给响应加 CORS 头，允许 file://（null 源）页面发起 fetch 到本服务。"""
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


# 图片扩展名：/thumb 用它区分「新契约（封面图片路径）」与「旧契约（视频路径）」
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _resolve_cover_for_video(video_path):
    """【旧画廊兼容】由视频绝对路径解析出封面图片路径；找不到返回 None。

    背景：新版画廊 HTML 里 /thumb 直接传封面图片路径；但用户磁盘上还留有
    旧版生成的画廊 HTML（里面 /thumb 传的是视频路径）。服务端在这里代为
    解析封面，让新旧画廊都能正常显示缩略图。

    延迟导入 video_thumb_lister 以复用封面解析逻辑（模块级导入会循环依赖：
    video_thumb_lister 顶部就 import 本模块）。单独运行 gallery_server.py 时
    动态加载它也没有副作用。
    """
    try:
        import video_thumb_lister as _vtl  # noqa: PLC0415
        cover = _vtl._find_cover(video_path)
        if cover:
            return cover
        png = _vtl.frame_png_for_video(video_path)
        if png and os.path.isfile(png):
            return png
    except Exception:  # noqa: BLE001 - 解析失败就走兜底，不让服务崩
        pass
    # 最后兜底：更早期版本生成的 <视频名>.thumb.jpg（若用户磁盘上还有）
    legacy = video_path + ".thumb.jpg"
    return legacy if os.path.isfile(legacy) else None


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
            # 图片 MIME：/thumb 端点会拿到 jpg / jpeg / webp（目录下已有封面）
            # 或 png（无封面时 ffmpeg 抽帧生成），必须按扩展名正确返回，
            # 否则浏览器无法渲染（webp/png 尤其敏感）。
            images = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp",
                ".bmp": "image/bmp", ".gif": "image/gif",
            }
            if ext in images:
                return images[ext]
            return {
                ".mp4": "video/mp4", ".mkv": "video/x-matroska",
                ".webm": "video/webm", ".mov": "video/quicktime",
                ".avi": "video/x-msvideo", ".m4v": "video/x-m4v",
                ".ts": "video/mp4", ".ogv": "video/ogg",
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
            elif path == "/ping":
                # 返回服务代码版本号（纯文本整数），供 GUI 探测在线服务是否旧版本
                body = str(SERVER_VERSION).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                _cors(self)
                self.end_headers()
                self.wfile.write(body)
            elif path == "/thumb":
                # 兼容两种请求（新旧画廊 HTML 共存）：
                #   新画廊：path = 封面图片绝对路径（jpg/jpeg/webp/png）→ 直接发送；
                #   旧画廊：path = 视频绝对路径（旧版约定"视频路径 + .thumb.jpg"）
                #           → 服务端代为解析出该视频的封面再发送。
                # 绝不能把视频文件直接当图片发出去（浏览器 <img> 渲染不了，
                # 而且大视频会把响应拖死）——这就是「缩略图不见」的根因之一。
                p = qs.get("path", [""])[0]
                if not p:
                    self.send_error(400)
                    return
                ext = os.path.splitext(p)[1].lower()
                if ext in _IMG_EXTS:
                    self._serve_file(p, self._ct_for(p), range_ok=False)
                    return
                cand = _resolve_cover_for_video(p)
                if cand:
                    self._serve_file(cand, self._ct_for(cand), range_ok=False)
                else:
                    self.send_error(404)
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
            elif path == "/play":
                p = qs.get("path", [""])[0]
                if not p or not os.path.isfile(p):
                    self.send_error(404)
                    return
                player = find_player()
                if not player:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", "8")
                    _cors(self)
                    self.end_headers()
                    self.wfile.write(b"noplayer")
                    return
                try:
                    if sys.platform == "win32":
                        subprocess.Popen(
                            [player, p],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    else:
                        subprocess.Popen(
                            [player, p],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
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
