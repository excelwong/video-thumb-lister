#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""video-thumb-lister 独立后台画廊服务（自包含单文件版，可直接打包成 exe）。

=========================== 为什么要有这个文件 ===========================
原项目的后端服务是 gallery_server.py，它被主程序 video_thumb_lister.py
import 使用（提供端口常量、SERVER_VERSION、run_server 等），同时也能
`python gallery_server.py --dir ...` 单独运行。

本文件把这份后端服务【完整剥离】出来：不 import 主程序、不依赖项目里
其它任何模块，仅用 Python 标准库即可运行。因此它既能：
  - 用源码跑：python gallery_server_standalone.py --dir "D:/画廊输出目录"
  - 也能被 PyInstaller 打包成单个 gallery_server.exe（本文件即打包入口），
    双击即可启动，无需在机器上安装 Python。

契约与原 gallery_server.py 完全一致（端口、端点、/ping 版本号都相同），
所以它可以直接顶替 `python gallery_server.py ...` 来使用。

=========================== 端点一览 ===========================
  /                                -> 重定向到 /<gallery_name>
  /<gallery_name>.html             -> 画廊页面（同一输出目录内可有多份画廊）
  /ping                            -> 返回服务代码版本号（纯文本整数，供 GUI
                                       探测在线服务是否为旧版本，以便自动重启）
  /thumb?path=<封面图片绝对路径>    -> 封面图片（jpg/jpeg/webp 为目录下已有封面，
                                       png 为无封面时 ffmpeg 抽第 10 秒帧生成的）
                                       【兼容旧画廊】path 也可以是视频绝对路径，
                                       服务端会代为解析出封面再返回
  /video?path=<视频绝对路径>        -> 原视频文件（支持 Range，可播放/拖拽）
  /open-explorer?path=<目录绝对路径> -> 调用系统文件浏览器定位到该目录
  /play?path=<视频绝对路径>         -> 调起本机 PotPlayer 播放该视频

=========================== 三种运行方式 ===========================
1) 【默认 / 双击】守护模式 —— 退出本程序后服务仍在后台常驻：
       gallery_server.exe
   原理：本程序拉起一个【脱离、无窗口】的同名子进程去监听端口，自己随即
   退出。所以关掉窗口 / 退出 exe 都不影响服务，画廊照样能显示缩略图。
   停止与查询：
       gallery_server.exe --stop      停止后台服务
       gallery_server.exe --status    查看后台服务是否在跑

2) 【前台阻塞】—— 与旧版行为一致，供主程序 GUI 调用：
       gallery_server.exe --foreground --dir "D:/输出目录" --port-file pf.txt
   （不加 --foreground 的话 GUI 拿到的父进程会立刻退出，句柄失效，故必须加）

3) 【源码运行】
       python gallery_server_standalone.py --dir "D:/输出目录"

常用参数：
  --dir / --name / --port / --port-file / --open   同旧版
  --foreground  前台阻塞运行（默认不开）
  --stop        停止后台常驻服务
  --status      查看后台服务状态
  --no-msgbox   不弹提示框（静默）
  --child       内部使用：标记自己是后台服务进程（不要在命令行手输）

守护模式会在本程序目录留下两个文件（删掉无副作用）：
  gallery_server.pid  后台服务进程 PID（--stop 用）
  gallery_server.log  启动/停止记录

端口优先级：命令行 --port > 环境变量 VTL_GALLERY_PORT > 默认 8765。
（画廊 HTML 里写死的地址必须与服务端口一致，二者共用同一套取值规则。）
"""

import os
import re
import sys
import glob
import shutil
import threading
import subprocess
import http.server
import urllib.parse
import argparse

# --------------------------------------------------------------------------
# 端口：画廊 HTML 与服务器必须用同一个端口，故抽成统一常量。
# 优先级：命令行 --port > 环境变量 VTL_GALLERY_PORT > 默认 8765。
# --------------------------------------------------------------------------
DEFAULT_PORT = 8765

# 服务代码版本号：/thumb 等端点契约发生变化时 +1。
# GUI 打开画廊前会用 /ping 探测在线服务的版本，旧版本进程会被自动结束并重启，
# 避免「代码更新了、常驻服务还在跑旧逻辑」导致缩略图 404 之类的错配。
# 【务必与原 gallery_server.py 的 SERVER_VERSION 保持一致】
SERVER_VERSION = 2

# 「用户已有封面」的候选扩展名（按优先级）。注意【不含 .png】——
# .png 是主程序无封面时用 ffmpeg 抽帧生成的产物，不能被当成用户封面。
COVER_EXTS = [".jpg", ".jpeg", ".webp"]

# 视频名相对「核心番号」可能带的版本后缀：
#   -U / -C / -UC / -W / -WC（去码 / 中文字幕 等标记，带连字符）
#   末尾单独的 R            （流出/修正版等标记，直接附着，无连字符）
_COVER_TAIL_TAG = re.compile(r"-(?:[UCW]{1,3})$", re.IGNORECASE)
_COVER_TAIL_R = re.compile(r"R$", re.IGNORECASE)


# ==========================================================================
# 端口 / 地址 工具（与原 gallery_server.py 同名同义）
# ==========================================================================
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


# ==========================================================================
# 封面解析（从主程序 video_thumb_lister.py 内联而来，逻辑保持一致）
# —— 单独抽出来是为了不 import 那个 87KB 的主程序模块，便于打包成 exe。
# ==========================================================================
def _cover_core_candidates(base):
    """由视频名（不含扩展名）推导可能的「封面核心名」候选，按具体到宽泛排列。

    ABC-123R / ABC-123-U / ABC-123-UC 等都能回退到核心名 ABC-123。
    """
    cands = [base]
    b = base
    m = _COVER_TAIL_TAG.search(b)
    if m and b[:m.start()]:
        b = b[:m.start()]
        cands.append(b)
    m = _COVER_TAIL_R.search(b)
    if m and b[:m.start()]:
        cands.append(b[:m.start()])
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _cover_core_name(base):
    """视频名（不含扩展名）-> 剥离得最干净的核心番号。"""
    return _cover_core_candidates(base)[-1]


def _find_cover(video_path):
    """在视频同目录找可作封面的同名/核心名图片；找不到返回 None。"""
    d = os.path.dirname(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    for core in _cover_core_candidates(base):
        for ext in COVER_EXTS:
            cand = os.path.join(d, core + ext)
            if os.path.isfile(cand):
                return cand
    return None


def frame_png_for_video(video_path):
    """主程序抽帧生成的封面路径：<视频所在目录>/<核心番号>.png。"""
    d = os.path.dirname(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    return os.path.join(d, _cover_core_name(base) + ".png")


def _resolve_cover_for_video(video_path):
    """【旧画廊兼容】由视频绝对路径解析出封面图片路径；找不到返回 None。

    新版画廊 HTML 里 /thumb 直接传封面图片路径；但用户磁盘上还留有旧版
    生成的画廊 HTML（里面 /thumb 传的是视频路径）。服务端在这里代为解析
    封面，让新旧画廊都能正常显示缩略图。
    """
    try:
        cover = _find_cover(video_path)
        if cover:
            return cover
        png = frame_png_for_video(video_path)
        if png and os.path.isfile(png):
            return png
    except Exception:  # noqa: BLE001 - 解析失败就走兜底，不让服务崩
        pass
    # 最后兜底：更早期版本生成的 <视频名>.thumb.jpg（若用户磁盘上还有）
    legacy = video_path + ".thumb.jpg"
    return legacy if os.path.isfile(legacy) else None


# ==========================================================================
# 本地播放器：仅 PotPlayer（点击缩略图时唤起本机 PotPlayer 播放）
# ==========================================================================
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
        base = os.path.dirname(_PLAYER_PORTABLE_DIR)
        for pat in (os.path.join(base, "**", "PotPlayerMini64.exe"),
                    os.path.join(base, "**", "PotPlayer64.exe"),
                    os.path.join(base, "**", "PotPlayer.exe")):
            hits = glob.glob(pat, recursive=True)
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


def _fix_text(s):
    """把「被 latin-1 解码过的 UTF-8」还原成真正的中文。

    http.server 用 iso-8859-1 解码请求行，客户端若直接发送 UTF-8 字节
    （未做百分号编码），拿到的就是乱码串；这里还原。已是正常中文
    （由百分号编码正确解码而来）或纯 ASCII 时原样返回。
    """
    if not s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _decode_request_path(s):
    """还原 URL 路径：先按原始字节解百分号编码，再按 UTF-8 解码成中文。

    覆盖两种情况：
      - 浏览器百分号编码：/gallery_X_%E6%9C%89%E7%A0%81.html
      - 客户端直发 UTF-8：/gallery_X_有码.html
    不还原的话，中文画廊文件名一律 404。
    """
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        raw = s.encode("utf-8", "replace")
    return urllib.parse.unquote_to_bytes(raw).decode("utf-8", "replace")


# ==========================================================================
# HTTP 服务
# ==========================================================================
def serve_gallery(out_dir, port=0, gallery_name="gallery.html"):
    """启动本地 HTTP 服务（守护线程），返回 (httpd, port)。

    画廊通过 http://127.0.0.1:port/<gallery_name> 访问；并提供
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

        def _text(self, body):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            _cors(self)
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            _cors(self)
            self.end_headers()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = _decode_request_path(parsed.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if path in ("/", ""):
                self.send_response(302)
                # 中文文件名必须百分号编码：HTTP 头只能放 latin-1 字符
                self.send_header(
                    "Location", "/" + urllib.parse.quote(self.server.gallery_name))
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
                self._text(str(SERVER_VERSION))
            elif path == "/thumb":
                # 兼容两种请求（新旧画廊 HTML 共存）：
                #   新画廊：path = 封面图片绝对路径（jpg/jpeg/webp/png）→ 直接发送；
                #   旧画廊：path = 视频绝对路径 → 服务端代为解析出封面再发送。
                # 绝不能把视频文件直接当图片发出去（浏览器 <img> 渲染不了，
                # 而且大视频会把响应拖死）——这就是「缩略图不见」的根因之一。
                p = _fix_text(qs.get("path", [""])[0])
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
                p = _fix_text(qs.get("path", [""])[0])
                if p:
                    self._serve_file(p, self._ct_for(p))
                else:
                    self.send_error(400)
            elif path == "/open-explorer":
                p = _fix_text(qs.get("path", [""])[0])
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
                self._text(b"ok" if ok else b"err")
            elif path == "/play":
                p = _fix_text(qs.get("path", [""])[0])
                if not p or not os.path.isfile(p):
                    self.send_error(404)
                    return
                player = find_player()
                if not player:
                    self._text(b"noplayer")
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
                self._text(b"ok" if ok else b"err")
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
    _write_port_file(port_file, bound_port)
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


# ==========================================================================
# 便捷默认值（仅对「双击运行 / 不带参数」生效，便于 exe 直接使用）
# 注意：GUI 调用时一定会显式传 --dir 与 --name，不会走到这里的猜测逻辑。
# ==========================================================================
def _app_dir():
    """返回本程序所在目录（打包成 exe 时是 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _has_html(directory):
    """目录里是否有 html/htm（用来判断它像不像「画廊输出目录」）。"""
    try:
        return bool(glob.glob(os.path.join(directory, "*.html"))
                    or glob.glob(os.path.join(directory, "*.htm")))
    except Exception:  # noqa: BLE001
        return False


def _pick_default_dir():
    """双击运行（未指定 --dir）时自动挑一个「像画廊输出目录」的目录。

    典型场景：exe 放在项目根目录，而画廊 html 都在 video_thumb_output/ 子目录里
    ——只认 exe 所在目录会托管到一个空目录，画廊打不开。故依次尝试：
      本程序目录 → 其 video_thumb_output 子目录 → 本程序目录下的一层子目录
      → 当前工作目录
    取第一个含 html 的；都没有则退回本程序目录。
    """
    appd = _app_dir()
    if not os.path.isdir(appd):
        return os.path.abspath(os.getcwd())
    cands = [appd, os.path.join(appd, "video_thumb_output")]
    try:
        for name in sorted(os.listdir(appd)):
            sub = os.path.join(appd, name)
            if os.path.isdir(sub):
                cands.append(sub)
    except OSError:
        pass
    cands.append(os.getcwd())
    for d in cands:
        if os.path.isdir(d) and _has_html(d):
            return os.path.abspath(d)
    return os.path.abspath(appd)


def _guess_gallery_name(directory, fallback="gallery.html"):
    """在 directory 里挑一个最像画廊的 html：优先 gallery*.html，其次任意 html。"""
    try:
        htmls = sorted(
            os.path.basename(p) for p in
            glob.glob(os.path.join(directory, "*.html"))
            + glob.glob(os.path.join(directory, "*.htm")))
    except Exception:  # noqa: BLE001
        return fallback
    if not htmls:
        return fallback
    for h in htmls:
        if h.lower().startswith("gallery"):
            return h
    return htmls[0]


def _write_port_file(port_file, port):
    if not port_file:
        return
    try:
        with open(port_file, "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:  # noqa: BLE001
        pass


def _safe_stdout():
    """中文 Windows 控制台默认 GBK，打印中文路径可能抛 UnicodeEncodeError。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# ==========================================================================
# 守护模式（双击 exe 时：退出本程序后，服务仍在后台常驻）
#
# 做法：本程序（父进程）拉起【一个脱离的、无窗口的同名子进程】去真正监听
# 端口，自己随即退出——于是关掉/退出 exe 都不影响后台服务。
# 仅限标准库实现：子进程用 CREATE_NO_WINDOW 隐藏，停止靠 --stop。
# ==========================================================================
_PID_NAME = "gallery_server.pid"
_LOG_NAME = "gallery_server.log"


def _pid_path():
    return os.path.join(_app_dir(), _PID_NAME)


def _log_path():
    return os.path.join(_app_dir(), _LOG_NAME)


def _write_log(msg):
    """把一行追加到日志文件（写不了就算了，绝不影响服务本身）。"""
    try:
        import time as _t
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_t.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


def _msgbox(title, text, icon=0x40):
    """弹一个系统原生消息框（ctypes 调 user32，不依赖任何第三方库）。

    icon: 0x40=信息(i) 0x30=警告(!) 0x10=错误(x)
    无窗口环境（如被 GUI 静默调用）下自动退回 print。
    """
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(0, str(text), str(title), icon)
    except Exception:  # noqa: BLE001
        print(text)
        return None


def _listening_pids(port):
    """用 netstat 找出正在监听 127.0.0.1:port 的 PID 集合。

    注意：不要用 text=True —— 中文 Windows 的 netstat 输出是 GBK，
    本程序可能跑在 UTF-8 模式，直接文本解码会抛 UnicodeDecodeError。
    """
    pids = set()
    try:
        out_b = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or b""
        out = out_b.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return pids
    for line in out.splitlines():
        parts = line.split()
        # 列：Proto  Local-Address  Foreign-Address  State  PID
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                and parts[1].endswith(":%d" % port):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return pids


def _pid_commandline(pid):
    """返回进程命令行文本（查不到返回空串）。用 PowerShell CIM 接口。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % pid],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (r.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def _our_server_pids(port):
    """在监听该端口的进程里，挑出确实是本画廊服务的 PID（防误杀别的程序）。

    PyInstaller onefile 会派生子进程，真正监听端口的是子进程；
    但父子进程命令行都含 gallery_server(.exe)，故都能命中。
    按 "gallery_server" 整体匹配：源码模式命令行是
    ".../gallery_server_standalone.py"（并不含 "gallery_server.py" 这个子串），
    打包模式是 ".../gallery_server.exe"，只认后缀会把其中一种漏掉。
    """
    out = []
    for pid in _listening_pids(port):
        if "gallery_server" in _pid_commandline(pid).lower():
            out.append(pid)
    return out


def stop_background(port=None, quiet=False):
    """停止后台常驻的画廊服务，返回是否成功结束。

    先按 pid 文件杀（/T 连进程树，确保 PyInstaller 的子进程一起走），
    再用 netstat + 命令行兜底，最后确认端口已释放。
    """
    port = gallery_server_port(port)
    killed = False

    pfile = _pid_path()
    if os.path.isfile(pfile):
        try:
            pid = int(open(pfile, encoding="utf-8").read().strip())
            r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            killed = killed or (r.returncode == 0)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(pfile)
        except OSError:
            pass

    for pid in _our_server_pids(port):  # 兜底：pid 文件缺失/过期的情况
        try:
            r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            killed = killed or (r.returncode == 0)
        except Exception:  # noqa: BLE001
            pass

    # 等端口真正释放
    import time as _t
    deadline = _t.time() + 5
    while _t.time() < deadline:
        if not is_server_up(port):
            break
        _t.sleep(0.2)

    gone = not is_server_up(port)
    _write_log("已停止后台服务（端口 %d）：%s" % (port, "成功" if gone else "未能确认"))
    if gone:
        print("后台画廊服务已停止（端口 %d）。" % port)
    elif killed:
        print("已发出停止指令，但端口 %d 仍在监听。" % port)
    else:
        print("端口 %d 上没有找到正在运行的画廊服务。" % port)
    if not quiet:
        if gone:
            _msgbox("画廊服务", "后台画廊服务已停止（端口 %d）。" % port)
        elif killed:
            _msgbox("画廊服务", "已发出停止指令，但端口 %d 仍在监听，"
                                "请稍候或到任务管理器结束 gallery_server.exe。" % port, 0x30)
        else:
            _msgbox("画廊服务", "端口 %d 上没有找到正在运行的画廊服务。" % port, 0x40)
    return gone


def status_background(port=None, quiet=False):
    """查看后台服务是否在跑，返回 (bool, url_or_None)。"""
    port = gallery_server_port(port)
    pids = _our_server_pids(port)
    if not pids:
        print("后台画廊服务当前【未运行】（端口 %d）。" % port)
        if not quiet:
            _msgbox("画廊服务", "后台画廊服务当前【未运行】（端口 %d）。" % port)
        return (False, None)
    url = "http://127.0.0.1:%d/" % port
    text = ("后台画廊服务正在运行：%s\n（进程 PID：%s）"
            % (url, ", ".join(str(p) for p in sorted(pids))))
    print(text)
    if not quiet:
        _msgbox("画廊服务", text)
    return (True, url)


def _spawn_background(out_dir, gallery_name, port, port_file, timeout=25):
    """拉起【脱离、无窗口】的后台服务子进程，返回其 Popen；失败返回 None。

    子进程是本程序的 --child 模式。父进程（本函数返回后）随即退出，
    子进程继续常驻——这就是「退出 exe 后服务仍在」的原理。
    """
    exe = sys.executable
    if not getattr(sys, "frozen", False):
        # 源码运行时，用当前解释器重新拉起本文件
        cmd = [exe, os.path.abspath(__file__)]
    else:
        cmd = [exe]
    cmd += ["--child", "--dir", out_dir, "--name", gallery_name,
            "--port", str(port)]
    if port_file:
        cmd += ["--port-file", port_file]

    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except Exception as e:  # noqa: BLE001
        print("启动后台服务进程失败：" + str(e), file=sys.stderr)
        return None


def main(argv=None):
    _safe_stdout()
    ap = argparse.ArgumentParser(
        description="video-thumb-lister 的独立后台画廊服务（自包含单文件版 / 可打包 exe）")
    ap.add_argument("--dir", default=None,
                   help="画廊 HTML 所在输出目录（默认：本程序所在目录，其次当前目录）")
    ap.add_argument("--name", default=None,
                   help="画廊文件名（默认 gallery.html；若不存在则自动挑一个 html）")
    ap.add_argument("--port", type=int, default=None,
                   help="绑定端口（默认 8765，或环境变量 VTL_GALLERY_PORT）")
    ap.add_argument("--port-file", default=None,
                   help="把实际绑定端口写入该文件（供调用方回读）")
    ap.add_argument("--open", action="store_true",
                   help="启动后自动用浏览器打开画廊页面")
    ap.add_argument("--foreground", action="store_true",
                   help="前台阻塞运行（GUI 调用用；不用此参数时，退出本程序后服务仍在后台）")
    ap.add_argument("--stop", action="store_true",
                   help="停止后台常驻的画廊服务")
    ap.add_argument("--status", action="store_true",
                   help="查看后台画廊服务是否在运行")
    ap.add_argument("--no-msgbox", action="store_true",
                   help="不弹出提示框（静默运行）")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    port = gallery_server_port(args.port)

    # ---- 停止 / 查状态：不需要目录等参数，直接处理完返回 ----
    if args.stop:
        stop_background(port, quiet=args.no_msgbox)
        return 0
    if args.status:
        status_background(port, quiet=args.no_msgbox)
        return 0

    # ---- 目录：显式指定 > 自动挑选（本程序目录及其子目录里含 html 的那个）----
    out_dir = os.path.abspath(args.dir or _pick_default_dir())

    if not os.path.isdir(out_dir):
        msg = f"错误：目录不存在或无效：{out_dir}"
        print(msg, file=sys.stderr)
        if not args.child and not args.no_msgbox:
            _msgbox("画廊服务", msg, 0x10)
        return 1

    # ---- 画廊文件名 ----
    gallery_name = args.name or "gallery.html"
    if not args.name and not os.path.isfile(os.path.join(out_dir, gallery_name)):
        gallery_name = _guess_gallery_name(out_dir, gallery_name)
        if not os.path.isfile(os.path.join(out_dir, gallery_name)):
            print(f"提示：{out_dir} 里没有找到可托管的画廊 html，"
                  f"可用 --dir / --name 手动指定。")

    qname = urllib.parse.quote(gallery_name)

    # ---- 端口上已有服务：直接复用，不重复启动 ----
    if is_server_up(port):
        url = "http://127.0.0.1:%d/%s" % (port, qname)
        print(f"端口 {port} 上已有画廊服务在运行，直接复用：{url}")
        if args.open:
            _open_browser(url)
        if not args.child and not args.no_msgbox:
            _msgbox("画廊服务", "端口 %d 上已有画廊服务在运行，无需重复启动：\n%s" % (port, url))
        return 0

    # ---- 前台 / 子进程：真正阻塞跑服务 ----
    if args.child or args.foreground:
        return _serve_forever(out_dir, gallery_name, port,
                              args.port_file, args.open)

    # ---- 默认（双击）：守护模式——拉起后台子进程后本程序退出 ----
    return _start_daemon(out_dir, gallery_name, port,
                         args.port_file, args.open, not args.no_msgbox)
    return 0


def _open_browser(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def _serve_forever(out_dir, gallery_name, port, port_file=None, open_browser=False):
    """真正绑定端口并阻塞运行（--child / --foreground 模式）。"""
    try:
        httpd, bound_port = serve_gallery(out_dir, port=port, gallery_name=gallery_name)
    except OSError as e:
        # 端口被占用是最常见的情况，给一句人话而不是 traceback
        msg = f"错误：无法绑定 127.0.0.1:{port}（{e}）。多半是该端口上已有画廊服务" \
              f"在运行（可直接复用），或用 --port 换一个端口（画廊 HTML 里的端口需同步修改）。"
        print(msg, file=sys.stderr)
        _write_log("启动失败：" + msg)
        return 2

    _write_port_file(port_file, bound_port)
    url = "http://127.0.0.1:%d/%s" % (bound_port, urllib.parse.quote(gallery_name))
    print(f"gallery_server 正在服务：{url}")
    print(f"  托管的输出目录：{os.path.abspath(out_dir)}")
    print("  按 Ctrl+C 停止。")
    _write_log("已启动：%s  目录：%s" % (url, os.path.abspath(out_dir)))
    if open_browser:
        _open_browser(url)
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
    return 0


def _start_daemon(out_dir, gallery_name, port, port_file=None,
                  open_browser=False, msgbox=True, timeout=30):
    """守护模式：拉起【脱离、无窗口】的后台子进程，本程序随即退出。

    这是「退出 exe 后服务仍在后台」的关键——真正监听端口的是子进程，
    父进程（本函数返回后）退出并不影响它。
    """
    import tempfile
    import time

    tmp_pf = port_file or os.path.join(
        tempfile.gettempdir(), "vtl_daemon_port_%d.txt" % os.getpid())
    try:
        if os.path.isfile(tmp_pf):
            os.remove(tmp_pf)
    except OSError:
        pass

    proc = _spawn_background(out_dir, gallery_name, port, tmp_pf)
    if proc is None:
        msg = "启动后台画廊服务进程失败。"
        print(msg, file=sys.stderr)
        if msgbox:
            _msgbox("画廊服务", msg, 0x10)
        return 2

    # 等子进程把实际端口写进 port-file（也给 onefile 解压留时间）
    bound = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if os.path.isfile(tmp_pf):
                c = open(tmp_pf, encoding="utf-8").read().strip()
                if c.isdigit():
                    bound = int(c)
                    break
        except Exception:  # noqa: BLE001 - 文件正在写入
            pass
        if proc.poll() is not None:  # 子进程已退出，别空等
            break
        time.sleep(0.2)
    if bound is None and is_server_up(port):  # 兜底：直接探测
        bound = port

    if bound is None:
        msg = "后台画廊服务启动失败（未能确认端口 %d 已监听）。\n" \
              "可加 --foreground 前台运行查看具体报错。" % port
        print(msg, file=sys.stderr)
        _write_log("守护模式启动失败：" + msg)
        if msgbox:
            _msgbox("画廊服务", msg, 0x10)
        return 2

    # 记录子进程 PID，供 --stop 使用
    try:
        with open(_pid_path(), "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except Exception:  # noqa: BLE001
        pass

    url = "http://127.0.0.1:%d/%s" % (bound, urllib.parse.quote(gallery_name))
    print(f"后台画廊服务已启动：{url}")
    print(f"  托管的输出目录：{os.path.abspath(out_dir)}")
    print("  服务已脱离本程序独立运行 —— 现在可以关闭本窗口。")
    print("  停止服务：gallery_server.exe --stop")
    print("  查看状态：gallery_server.exe --status")
    _write_log("后台已启动（守护模式）：%s  子进程 PID=%d" % (url, proc.pid))

    if open_browser:
        _open_browser(url)
    if msgbox:
        _msgbox("画廊服务",
                "后台画廊服务已启动：\n%s\n\n"
                "现在可以关闭本窗口，服务会继续在后台运行。\n\n"
                "停止服务：gallery_server.exe --stop\n"
                "查看状态：gallery_server.exe --status" % url)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
