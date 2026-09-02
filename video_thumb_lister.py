#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_thumb_lister.py
====================================================================
递归扫描某个目录（含子目录）中的视频文件，
为每段视频抽取「首帧」缩略图，并生成可浏览的画廊（HTML）。

只依赖：
  - Python 3 标准库（无需 opencv / PIL / 第三方包）
  - 一份 ffmpeg（仅用于抽帧，找不到会自动在常见位置/环境变量里找）

用法：
  python video_thumb_lister.py <目录> [--out 输出目录] [--ffmpeg ffmpeg路径] [--force]
  （不传 <目录> 时，会在交互模式下提示输入；--force 强制重抽全部首帧）

功能（当前实现 (1)+(2)+(3)+(4)）：
  (1) 扫描目录（含子目录），把每个视频文件的首帧截图 + 文件名列出来
      —— 以 HTML 画廊形式呈现（每个卡片 = 首帧缩略图 + 文件名 + 路径 + 大小），
         并在控制台打印进度与汇总。
  (2) 鼠标点击视频首帧截图时，新开一个窗口（弹窗）播放该视频。
  (3) 应用左侧呈现目录树，点击不同子目录可筛选显示对应视频（并附带文件名搜索）。
  (4) 首次生成的「首帧截图」直接保存在【视频所在的目录】里（文件名形如
      <视频名>.thumb.jpg）；下次再扫描同一目录时，若发现该截图已存在且来源未被
      改动（以修改时间判断），则直接复用，不再调用 ffmpeg 重新生成，速度大幅提升。
      可用 --force 强制重新生成全部缩略图。
  (4b) 缩略图来源优先级：若视频同目录存在可作封面的图片，则直接把它另存为缩略图，
      完全跳过抽帧；缓存是否失效也以封面图的修改时间为准（视频改动不再触发重抽，
      封面被替换才重抽）。封面匹配规则：
        * 完全同名优先：a.mp4 -> a.jpg / a.png …；
        * 其次按「核心番号」匹配：剥离视频名末尾的版本后缀（-U/-C/-UC/-WC、末尾 R）
          后再找图，故封面 ABC-123.jpg 可用于 ABC-123.mp4 / ABC-123R.mp4 /
          ABC-123-U.mp4 / ABC-123-C.mp4 / ABC-123-UC.mp4 / ABC-123-WC.mp4。
====================================================================
"""

import argparse
import datetime
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

# 后台画廊服务已解耦到独立的 gallery_server.py（可单独运行）。
# 这里只导入它暴露的端口/地址工具与启动函数，避免在 video_thumb_lister 里
# 再维护一份重复的 HTTP 服务实现。
from gallery_server import (  # noqa: E402
    gallery_server_port,
    gallery_server_base,
    is_server_up,
    run_server,
)

# 支持的视频扩展名（小写）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".m4v",
    ".ts", ".webm", ".mpg", ".mpeg", ".3gp", ".m2ts", ".vob",
    ".ogv", ".mts", ".m2v", ".mp4v", ".f4v", ".rmvb", ".rm",
}

# 同名封面图候选扩展名（按优先级，先找到的用作缩略图）
# 例：视频 a.mp4 对应封面 a.jpg / a.png ...
COVER_EXTS = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"]

# ffmpeg 候选路径（按优先级查找）
FFMPEG_CANDIDATES = [
    os.environ.get("FFMPEG_BIN"),
    r"C:/Users/excel/WorkBuddy/Claw/video-ad-cutter/ffmpeg/bin/ffmpeg.exe",
    "ffmpeg",                       # 在 PATH 中
    r"C:/ffmpeg/bin/ffmpeg.exe",
    r"C:/Program Files/ffmpeg/bin/ffmpeg.exe",
    r"D:/ffmpeg/bin/ffmpeg.exe",
]


def find_ffmpeg(explicit=None):
    """返回可用的 ffmpeg 可执行文件路径；找不到返回 None。"""
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        if shutil.which(explicit):
            return explicit
        raise FileNotFoundError(f"指定的 ffmpeg 不存在或不可执行：{explicit}")
    for c in FFMPEG_CANDIDATES:
        if not c:
            continue
        if c == "ffmpeg":
            p = shutil.which("ffmpeg")
            if p:
                return p
            continue
        if os.path.isfile(c):
            return c
    return None


# 本地播放器：仅使用 PotPlayer（左键点击缩略图时唤起本机 PotPlayer 播放）。
# 优先 PotPlayerMini64.exe（免安装/便携版，无需注册表），其次 PotPlayer64.exe / PotPlayer.exe。
# 含「免管理员」的便携版安装位置（本应用解压到用户目录的 PotPlayer）。
_PLAYER_PORTABLE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local")),
    "Programs", "PotPlayer")
PLAYER_CANDIDATES = [
    # 本应用解压的便携版（优先 mini 64 位）
    os.path.join(_PLAYER_PORTABLE_DIR, "PotPlayerMini64.exe"),
    os.path.join(_PLAYER_PORTABLE_DIR, "PotPlayer64.exe"),
    # 常见安装位置
    r"C:/Program Files/PotPlayer/PotPlayerMini64.exe",
    r"C:/Program Files/PotPlayer/PotPlayer64.exe",
    r"C:/Program Files/PotPlayer/PotPlayer.exe",
    r"C:/Program Files (x86)/PotPlayer/PotPlayer.exe",
    r"C:/PotPlayer/PotPlayer.exe",
    # PATH 中的可执行文件
    "PotPlayerMini64.exe",
    "PotPlayer64.exe",
    "PotPlayer.exe",
]


def find_player():
    """返回本机可用的 PotPlayer 可执行文件路径；找不到返回 None。"""
    for c in PLAYER_CANDIDATES:
        if os.path.isfile(c):
            return c
    # PATH 兜底
    for name in ("PotPlayerMini64.exe", "PotPlayer64.exe", "PotPlayer.exe"):
        p = shutil.which(name)
        if p:
            return p
    # 便携版可能解压出带版本号的子目录，做一次递归兜底
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


def _read_reg_sz(root, subkey, value=None):
    """读取注册表字符串值；任意一步失败返回 None（跨平台安全）。"""
    try:
        import winreg
    except Exception:  # noqa: BLE001 - 非 Windows
        return None
    try:
        with winreg.OpenKey(root, subkey) as k:
            if value is None:
                return winreg.QueryValue(k, None)
            return winreg.QueryValueEx(k, value)[0]
    except OSError:
        return None


# 便携版 HTML Help Workshop（含 hhc.exe / hha.dll / itcc.dll），来自 GitHub 发布：
#   https://github.com/skywind3000/support/releases/download/1.0.0/htmlhelp.zip
_HHW_ZIP_URL = "https://github.com/skywind3000/support/releases/download/1.0.0/htmlhelp.zip"
_HHW_FILES = ("hhc.exe", "hha.dll", "itcc.dll")
_HHW_FETCH_TRIED = False


def _fetch_hhw():
    """尝试从 GitHub 下载便携版 HTML Help Workshop 并解压到本应用 tools/ 下。

    仅在 tools/ 缺少 hhc.exe 或 itcc.dll 时才真正下载；已齐全则直接返回 True。
    网络不可达 / 被拦截 / 解压失败均静默返回 False，由调用方回退到 chmcmd
    并在报错时给出手动下载指引（见 build_chm 的错误信息）。
    返回 True 表示 tools/ 下已至少有 hhc.exe（核心依赖就位）。
    """
    global _HHW_FETCH_TRIED
    here = os.path.dirname(os.path.abspath(__file__))
    tools_dir = os.path.join(here, "tools")
    hhc_local = os.path.join(tools_dir, "hhc.exe")
    itcc_local = os.path.join(tools_dir, "itcc.dll")
    if os.path.isfile(hhc_local) and os.path.isfile(itcc_local):
        return True
    if _HHW_FETCH_TRIED:
        return os.path.isfile(hhc_local)
    _HHW_FETCH_TRIED = True
    try:
        import tempfile
        import urllib.request
        import zipfile
        os.makedirs(tools_dir, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
        os.close(tmp_fd)
        try:
            req = urllib.request.Request(
                _HHW_ZIP_URL,
                headers={"User-Agent": "Mozilla/5.0 (video-thumb-lister)"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp, \
                 open(tmp_path, "wb") as out:
                out.write(resp.read())
            with zipfile.ZipFile(tmp_path) as zf:
                names = set(zf.namelist())
                for needed in _HHW_FILES:
                    # 兼容 zip 顶层有/无子目录两种布局
                    hit = next((n for n in names
                                if n.lower() == needed.lower()
                                or n.lower().endswith("/" + needed.lower())), None)
                    if not hit:
                        continue
                    dest = os.path.join(tools_dir, needed)
                    if os.path.isfile(dest):
                        continue
                    with open(dest, "wb") as f:
                        f.write(zf.read(hit))
        finally:
            try:
                os.remove(tmp_path)
            except Exception:  # noqa: BLE001
                pass
        return os.path.isfile(hhc_local)
    except Exception:  # noqa: BLE001
        return os.path.isfile(hhc_local)


def find_hhc():
    """定位 Microsoft HTML Help Workshop 的 hhc.exe（CHM 编译器）。

    按以下顺序查找，命中即返回绝对路径；都找不到返回 None：
      0) 若本地 tools/ 缺 hhc/itcc，先尝试自动下载便携版（见 _fetch_hhw）；
      1) 环境变量 HHC_PATH（用户手动指定）；
      2) PATH 中的 hhc.exe；
      3) 常见安装目录；
      4) 注册表 App Paths（hhw.exe / hhc.exe）。
    若返回 None，说明本机未取得 HTML Help Workshop —— 调用方应给出
    清晰的安装/配置指引，而不是自行实现 CHM 二进制格式。
    """
    # 本地 tools/ 缺核心文件时，先尝试自动下载并解压（网络不可达则静默跳过）
    _fetch_hhw()
    candidates = []
    env = os.environ.get("HHC_PATH")
    if env:
        candidates.append(env)
    # 本应用目录 / tools 子目录：把 hhc.exe 放应用旁边即可，免去配置 PATH
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        for _c in (os.path.join(_here, "hhc.exe"),
                   os.path.join(_here, "tools", "hhc.exe"),
                   os.path.join(_here, "hhc", "hhc.exe")):
            if os.path.isfile(_c):
                return _c
    except Exception:  # noqa: BLE001
        pass
    candidates += [
        "hhc.exe",
        r"C:/Program Files (x86)/HTML Help Workshop/hhc.exe",
        r"C:/Program Files/HTML Help Workshop/hhc.exe",
        r"C:/HTML Help Workshop/hhc.exe",
    ]
    for c in candidates:
        if not c:
            continue
        if c == "hhc.exe":
            p = shutil.which("hhc.exe")
            if p:
                return p
            continue
        if os.path.isfile(c):
            return c
    # 注册表 App Paths（HTML Help Workshop 注册的是 hhw.exe，其目录含 hhc.exe）
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for app in ("hhw.exe", "hhc.exe"):
                v = _read_reg_sz(hive,
                                 r"Software\Microsoft\Windows\CurrentVersion\App Paths\\" + app)
                if v:
                    exe = v.split('"')[0].strip() if v.strip().startswith('"') else v.strip()
                    exe = exe.strip('"')
                    if os.path.isfile(exe):
                        return exe
                    # App Paths 的默认值是 exe 路径，但有时只给了目录；补 hhc.exe
                    cand = os.path.join(os.path.dirname(exe), "hhc.exe")
                    if os.path.isfile(cand):
                        return cand
    except Exception:  # noqa: BLE001
        pass
    return None


def find_chmcmd():
    """定位 Free Pascal 的 chmcmd.exe（开源 CHM 编译器，不依赖 itircl/注册）。

    查找顺序，命中即返回绝对路径；都找不到返回 None：
      1) 环境变量 CHMCMD_PATH；
      2) 本应用目录 / tools 子目录（把 chmcmd.exe 放旁边即可）；
      3) PATH 中的 chmcmd.exe；
      4) 常见目录。
    """
    candidates = []
    env = os.environ.get("CHMCMD_PATH")
    if env:
        candidates.append(env)
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        for _c in (os.path.join(_here, "chmcmd.exe"),
                   os.path.join(_here, "tools", "chmcmd.exe")):
            if os.path.isfile(_c):
                return _c
    except Exception:  # noqa: BLE001
        pass
    candidates += [
        "chmcmd.exe",
        r"C:/Program Files (x86)/HTML Help Workshop/chmcmd.exe",
        r"C:/Program Files/HTML Help Workshop/chmcmd.exe",
    ]
    for c in candidates:
        if c == "chmcmd.exe":
            p = shutil.which("chmcmd.exe")
            if p:
                return p
            continue
        if os.path.isfile(c):
            return c
    return None


def _regsvr(dll_path):
    """用 32 位 regsvr32 静默注册一个 COM DLL；文件不存在或失败则忽略。"""
    try:
        if not dll_path or not os.path.isfile(dll_path):
            return
        # hhc 是 32 位程序，注册 32 位 DLL 必须用 32 位 regsvr32（SysWOW64）
        rs = r"C:/Windows/SysWOW64/regsvr32.exe"
        if not os.path.isfile(rs):
            rs = "regsvr32.exe"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run([rs, "/s", dll_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=flags)
    except Exception:  # noqa: BLE001
        pass


def _ensure_hhw_registered(hhc_path):
    """尽力注册 HTML Help Workshop 的 COM 组件，避免 hhc 编译失败。

    hhc.exe 是 32 位程序，编译时依赖需要注册的 COM 组件（默认与 hhc.exe 同目录）：
      - itcc.dll : 目录/索引编译器（导出 DllRegisterServer，必须注册）；
                   系统 itircl.dll 运行时会调用它，缺失/未注册 →
                   HHC6003: The file Itircl.dll has not been registered correctly
    说明：hha.dll 并不导出 DllRegisterServer，它由 hhc.exe 直接 LoadLibrary 调用，
          无需也不能用 regsvr32 注册（注册会返回 4）。只要 hha.dll 待在 hhc.exe 同目录即可。
    此外系统自带的 itircl.dll / itss.dll 通常已注册，这里也顺手补注册一次。
    注册需管理员权限；无权限/被拦截则静默返回，由调用方在 hhc 报错时给指引。
    """
    try:
        hhw_dir = os.path.dirname(os.path.abspath(hhc_path))
    except Exception:  # noqa: BLE001
        hhw_dir = None
    if hhw_dir:
        _regsvr(os.path.join(hhw_dir, "itcc.dll"))
    for sys_dll in (r"C:/Windows/SysWOW64/itircl.dll",
                    r"C:/Windows/System32/itircl.dll",
                    r"C:/Windows/SysWOW64/itss.dll",
                    r"C:/Windows/System32/itss.dll"):
        _regsvr(sys_dll)


def scan_error_log_path(out_dir, directory=None, when=None):
    """返回【本次扫描】的错误日志完整路径。

    文件名带上：扫描目录名 + 扫描时间，形如
        scan_errors_<目录名>_<YYYYMMDD_HHMMSS>.log
    便于区分多次扫描、定位来源。目录名与画廊命名规则一致（去盘符冒号 /
    分隔符 / 非法字符），过长则截断并追加短哈希保证唯一且不超文件名长度限制。

    directory / when 省略时（兼容旧调用）退回固定名 scan_errors.log。
    when 必须【一次扫描只算一次】（在扫描开始时传入 err_log 并复用），
    否则多次 record_failure 会各自生成不同时间戳的文件。
    """
    if not directory:
        return os.path.join(out_dir, "scan_errors.log")
    p = os.path.abspath(directory)
    token = p.replace(":", "").replace("\\", "_").replace("/", "_")
    token = re.sub(r'[*?"<>|]', "", token)
    token = token.strip().strip(".")
    if not token:
        token = "root"
    max_readable = 80
    if len(token) > max_readable:
        h = hashlib.md5(p.encode("utf-8")).hexdigest()[:8]
        token = token[:max_readable] + "_" + h
    if when is None:
        when = datetime.datetime.now()
    stamp = when.strftime("%Y%m%d_%H%M%S")
    return os.path.join(out_dir, f"scan_errors_{token}_{stamp}.log")


def record_failure(out_dir, video_path, reason, log_path=None):
    """把单个视频的扫描失败（原因 + 具体文件夹）追加写入扫描错误日志。

    out_dir 为画廊输出目录；video_path 为失败视频的完整路径；
    reason 为异常信息。log_path 为本次扫描的错误日志路径
    （由 scan_error_log_path 在扫描开始时算好并传入，保证同一扫描写入同一文件）；
    省略时退回固定名 scan_errors.log。日志格式便于人工排查。
    """
    try:
        os.makedirs(out_dir, exist_ok=True)
        folder = os.path.dirname(video_path)
        name = os.path.basename(video_path)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{ts}] FOLDER: {folder}\n"
            f"    FILE: {name}\n"
            f"    REASON: {reason}\n"
        )
        target = log_path or scan_error_log_path(out_dir)
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 - 写日志失败不应影响主流程
        pass


def _mktag(a, b, c, d):
    return ord(a) | (ord(b) << 8) | (ord(c) << 16) | (ord(d) << 24)


# FFmpeg 的 AVERROR 退出码（在 Windows 下以无符号 32 位形式暴露给调用方），
# 映射为人类可读的中文解释，便于区分「文件本身有问题」还是「文件名/路径问题」。
_FFMPEG_AVERROR = {
    (-_mktag("I", "N", "D", "A")) & 0xFFFFFFFF:
        "AVERROR_INVALIDDATA：视频数据损坏/无效，无法解码 —— 是【文件内容】问题，非文件名问题",
    (-_mktag("D", "E", "C", " ")) & 0xFFFFFFFF:
        "AVERROR_DECODER_NOT_FOUND：找不到对应解码器（该编码格式不受支持）",
    (-_mktag("D", "E", "M", " ")) & 0xFFFFFFFF:
        "AVERROR_DEMUXER_NOT_FOUND：无法识别的封装/容器格式",
    (-_mktag("E", "O", "F", " ")) & 0xFFFFFFFF:
        "AVERROR_EOF：文件被截断/不完整（提前到达文件尾）",
    (-_mktag("S", "T", "R", " ")) & 0xFFFFFFFF:
        "AVERROR_STREAM_NOT_FOUND：文件里找不到视频流",
    2 & 0xFFFFFFFF: "ENOENT：文件不存在或路径无法访问（可能是文件名/路径问题）",
    (-2) & 0xFFFFFFFF: "ENOENT：文件不存在或路径无法访问（可能是文件名/路径问题）",
    13 & 0xFFFFFFFF: "EACCES：没有读取权限（可能是文件名/路径或权限问题）",
    (-13) & 0xFFFFFFFF: "EACCES：没有读取权限（可能是文件名/路径或权限问题）",
}

# Windows 进程「崩溃类」退出码（NTSTATUS），说明 ffmpeg 进程异常终止而非正常报错。
_WIN_STATUS = {
    0xC0000005: "STATUS_ACCESS_VIOLATION：ffmpeg 进程崩溃（内存访问违规），通常也是文件严重损坏所致",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN：ffmpeg 进程崩溃",
    0xC000013A: "被 Ctrl-C / 关闭控制台中断",
    0xC0000374: "STATUS_HEAP_CORRUPTION：ffmpeg 进程崩溃（堆损坏）",
}


def explain_exit_code(code):
    """把 ffmpeg 的退出码翻译成可读中文；无法识别返回 None。"""
    if code is None:
        return None
    u = code & 0xFFFFFFFF
    if u in _FFMPEG_AVERROR:
        return _FFMPEG_AVERROR[u]
    if u in _WIN_STATUS:
        return _WIN_STATUS[u]
    return None


# Windows 文件名保留字符（除路径分隔符外）
_ILLEGAL_NAME_CHARS = '<>:"|?*'


def suspicious_filename_reason(path):
    """检查文件名/路径是否含可能导致读取失败的可疑字符；无问题返回 None。"""
    name = os.path.basename(path)
    problems = []
    bad = sorted({c for c in name if c in _ILLEGAL_NAME_CHARS})
    if bad:
        problems.append("含非法字符 " + " ".join(bad))
    ctrl = [c for c in name if ord(c) < 32]
    if ctrl:
        problems.append("含控制字符/不可见字符")
    if name != name.rstrip(" ."):
        problems.append("文件名以空格或点结尾（Windows 非法）")
    if len(path) >= 260:
        problems.append(f"路径过长({len(path)}≥260，可能触发 MAX_PATH 限制)")
    return "；".join(problems) if problems else None


def _ffmpeg_stderr_tail(stderr, n=3):
    """取 ffmpeg stderr 的最后 n 行有效内容，用于日志。"""
    if not stderr:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return " | ".join(lines[-n:])


class ThumbExtractError(Exception):
    """抽帧失败异常：携带退出码、可读原因、文件名检查与 ffmpeg 输出摘要。"""

    def __init__(self, video_path, returncode, ff_detail=""):
        self.video_path = video_path
        self.returncode = returncode
        self.ff_detail = ff_detail
        super().__init__(self._build())

    def _build(self):
        parts = []
        if self.returncode is not None:
            parts.append(f"ffmpeg 退出码 {self.returncode & 0xFFFFFFFF}")
            exp = explain_exit_code(self.returncode)
            if exp:
                parts.append(exp)
        fn = suspicious_filename_reason(self.video_path)
        parts.append("文件名可疑：" + fn if fn else "文件名正常（无非法字符）")
        if self.ff_detail:
            parts.append("ffmpeg 输出：" + self.ff_detail)
        return "；".join(parts)


def human_size(n):
    """把字节数转成易读字符串。"""
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(n)
    for u in units:
        if f < 1024 or u == "TB":
            if u == "B":
                return f"{int(f)} {u}"
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} PB"


def file_url(path):
    """把本地绝对路径转成浏览器可打开的 file:// URL（兼容 Windows 盘符路径）。"""
    p = path.replace("\\", "/")
    if p.startswith("/"):
        return "file://" + p
    return "file:///" + p  # 形如 C:/...


def reveal_in_explorer(path):
    """在系统文件浏览器中定位/选中指定文件（而非用浏览器打开它）。

    Windows：explorer /select, "<文件>"  —— 打开资源管理器并高亮该文件；
    macOS：open -R <文件>；Linux：xdg-open <所在目录>。
    失败静默忽略（不影响画廊本身已生成的事实）。
    """
    path = os.path.abspath(path)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["explorer.exe", "/select,", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", path])
        else:
            subprocess.run(["xdg-open", os.path.dirname(path)])
    except Exception:  # noqa: BLE001
        pass


def gallery_filename_for(root):
    """根据扫描目录的【驱动器和路径】生成唯一的画廊文件名。

    形如 gallery_C_Users_excel_Videos.html （保留中文，去掉盘符冒号与分隔符）。
    这样同一个输出目录里，不同扫描目录会各有一份不冲突的画廊文件。
    路径过长时截断可读部分并追加短哈希，保证唯一且不超文件名长度限制。
    """
    p = os.path.abspath(root)
    # 盘符冒号 + 路径分隔符 -> 下划线
    token = p.replace(":", "").replace("\\", "_").replace("/", "_")
    # 去掉 Windows 文件名非法字符（保留中文、字母、数字、. _ - 与空格）
    token = re.sub(r'[*?"<>|]', "", token)
    token = token.strip().strip(".")  # 去掉首尾的点/空格
    if not token:
        token = "root"
    max_readable = 100
    if len(token) > max_readable:
        h = hashlib.md5(p.encode("utf-8")).hexdigest()[:8]
        token = token[:max_readable] + "_" + h
    return "gallery_" + token + ".html"


# --------------------------------------------------------------------------
# 运行日志：记录「每次扫描过的目录 -> 对应的画廊文件」，
# 下次扫描同一目录时直接打开该画廊，无需重新扫描。
# --------------------------------------------------------------------------
def _log_norm_key(directory):
    """目录归一化键（Windows 下忽略大小写、统一分隔符）。"""
    return os.path.normcase(os.path.abspath(directory))


def scan_log_path(out_dir):
    return os.path.join(out_dir, "scan_log.json")


def load_scan_log(out_dir):
    """读取运行日志，返回 {归一化目录: {gallery, at, count}} 字典。"""
    p = scan_log_path(out_dir)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 损坏则视为无日志
        return {}


def save_scan_log_entry(out_dir, directory, gallery_name, count):
    """把一次扫描结果写入运行日志（同目录会覆盖旧记录）。"""
    os.makedirs(out_dir, exist_ok=True)
    data = load_scan_log(out_dir)
    data[_log_norm_key(directory)] = {
        "gallery": gallery_name,
        "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": count,
    }
    with open(scan_log_path(out_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find_cached_gallery(out_dir, directory):
    """若同一目录此前扫描过且画廊文件仍在，返回其完整路径；否则 None。

    调用方在「未强制重扫」时可直接用该路径打开，跳过扫描。
    """
    data = load_scan_log(out_dir)
    entry = data.get(_log_norm_key(directory))
    if not entry:
        return None
    gpath = os.path.join(out_dir, entry["gallery"])
    return gpath if os.path.isfile(gpath) else None



def scan_videos(root):
    """递归收集目录下所有视频文件的完整路径（按路径排序）。"""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in VIDEO_EXTS:
                found.append(os.path.join(dirpath, fn))
    return found


def _thumb_path_for_video(video_path):
    """缩略图就放在【视频所在目录】，文件名形如 <视频名>.thumb.jpg。

    例：C:/Videos/clip.mp4  ->  C:/Videos/clip.mp4.thumb.jpg
    这样下次扫描时直接在视频旁边找到它即可复用，无需再次抽帧。
    """
    return video_path + ".thumb.jpg"


# 视频名相对「核心番号」可能带的版本后缀（不区分大小写）：
#   -U / -C / -UC / -WC （去码 / 中文字幕 等标记，带连字符）
#   末尾单独的 R          （流出/修正版等标记，直接附着，无连字符）
# 例：核心番号 ABC-123 的封面 ABC-123.jpg，可用于
#   ABC-123.mp4 / ABC-123R.mp4 / ABC-123-U.mp4 / ABC-123-C.mp4 / ABC-123-UC.mp4
#   / ABC-123-WC.mp4
# ⚠️ 正则里 UC|WC 必须排在单个 U|C 之前，否则交替匹配会先命中 U 而漏掉 UC。
_COVER_TAIL_TAG = re.compile(r"-(?:UC|WC|U|C)$", re.IGNORECASE)
_COVER_TAIL_R = re.compile(r"R$", re.IGNORECASE)


def _cover_core_candidates(base):
    """由视频名（不含扩展名）推导出可能的「封面核心名」候选列表。

    先返回原名（精确匹配优先），再依次剥离末尾的 -U/-C/-UC/-WC 和 R，
    使 ABC-123R / ABC-123-U / ABC-123-UC / ABC-123-WC 等都能回退到核心名 ABC-123。
    按具体到宽泛排列，去重后返回。
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
    # 去重且保持顺序
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _find_cover(video_path):
    """在视频同目录找可作封面的同名/核心名图片。

    匹配规则（按优先级）：
      1) 与视频完全同名的封面（a.mp4 -> a.jpg），最优先；
      2) 剥离版本后缀（-U/-C/-UC/-WC、末尾 R）后的核心名封面，
         如 ABC-123R.mp4 / ABC-123-U.mp4 / ABC-123-WC.mp4 -> ABC-123.jpg。
    对每个候选核心名再按 COVER_EXTS 顺序找图片，返回首个命中的完整路径；
    都找不到返回 None。
    """
    d = os.path.dirname(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    for core in _cover_core_candidates(base):
        for ext in COVER_EXTS:
            cand = os.path.join(d, core + ext)
            if os.path.isfile(cand):
                return cand
    return None


def _need_regen(video_path, thumb_file):
    """判断缩略图是否需要重新生成。

    缩略图来源优先级：同目录同名封面图 > ffmpeg 抽帧。
    因此缓存是否失效，以「实际来源」为准：
      - 缩略图不存在                    -> 需要
      - 存在同名封面图：以封面 mtime 为准（封面被替换 -> 需要）
      - 否则以视频 mtime 为准（视频被替换/编辑 -> 需要）
    force 由调用方在传入前处理。
    """
    if not os.path.isfile(thumb_file):
        return True
    try:
        t_thumb = os.path.getmtime(thumb_file)
    except OSError:
        return True
    # 缩略图存在但本身不是真正的 JPEG（例如旧版本把 webp 改名当 jpg 留下的伪文件），
    # 重新生成以确保输出是合法的 jpg。
    if not _is_jpeg_file(thumb_file):
        return True
    ref = _find_cover(video_path) or video_path
    try:
        return t_thumb < os.path.getmtime(ref)
    except OSError:
        return True


def _is_jpeg_file(path):
    """按文件头魔数判断 path 是否为真正的 JPEG（而非仅以 .jpg 结尾的伪文件，
    例如把 webp/png 直接改名成 .jpg 的情况）。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(3)
    except OSError:
        return False
    return head[:3] == b"\xff\xd8\xff"


def _convert_image_to_jpg(src, dst, ffmpeg):
    """把图片 src 另存为真正的 JPEG 到 dst。

    - 若 src 已是 JPEG，直接复制（保留原文件、最快）；
    - 若是 webp/png/bmp/gif 等其它格式，用 ffmpeg 转码为真正的 jpg
      （而非只改后缀名），避免浏览器/CHM 把 webp 字节当 jpg 解析导致无法显示。
    返回 True 表示 dst 是真正的 jpg；ffmpeg 不可用或转码失败时回退为
    原样复制（流程不中断，但显示可能异常）。
    """
    if _is_jpeg_file(src):
        shutil.copy2(src, dst)
        return True
    if not ffmpeg:
        shutil.copy2(src, dst)
        return False
    cmd = [ffmpeg, "-y", "-i", src, dst]
    run_kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Windows 下 ffmpeg 是控制台程序，加 CREATE_NO_WINDOW 让它后台运行不闪窗
    if sys.platform == "win32":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(cmd, **run_kwargs, check=True, timeout=60)
    except Exception:  # noqa: BLE001
        # 转码失败：回退为原样复制，保证流程不中断
        try:
            shutil.copy2(src, dst)
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


def extract_first_frame(ffmpeg, video_path, out_jpg, timeout=120):
    """用 ffmpeg 抽取「第 3 秒附近」的那一帧作为缩略图。

    很多视频片头是黑场/淡入/标题卡，抽第 0 秒或第 1 秒的帧仍可能黑屏，
    所以这里默认取约 3 秒处的画面。

    把 -ss 放在 -i 之后（输出定位），可保证取到真正约 3 秒处的帧，
    不会被关键帧对齐带回到 0 秒；缩放为宽度 320。
    若视频不足 3 秒导致取帧失败，则回退到真正的首帧（-ss 0）。
    """
    def _run(ss):
        cmd = [
            ffmpeg, "-y", "-i", video_path,
            "-ss", ss, "-frames:v", "1", "-an",
            "-vf", "scale=320:-1", "-q:v", "3", out_jpg,
        ]
        run_kwargs = dict(
            stdout=subprocess.DEVNULL,
            # 捕获 stderr 而非丢弃，失败时才能把 ffmpeg 的真实报错写进日志
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=True,
        )
        # Windows 下 ffmpeg 是控制台程序，默认每抽一个视频就弹一个黑窗；
        # 加 CREATE_NO_WINDOW 让它完全后台运行，不再闪窗干扰。
        if sys.platform == "win32":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(cmd, **run_kwargs)

    try:
        _run("3")
        return
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # 第一次可能因视频不足 3 秒而失败，回退到真正首帧再试一次
        pass
    try:
        _run("0")
    except subprocess.TimeoutExpired as e:
        raise ThumbExtractError(
            video_path, None, f"ffmpeg 抽帧超时（>{timeout}s）"
        ) from e
    except subprocess.CalledProcessError as e:
        # 两次都失败：抛出带「退出码解读 + 文件名检查 + ffmpeg 输出」的富异常
        raise ThumbExtractError(
            video_path, e.returncode, _ffmpeg_stderr_tail(e.stderr)
        ) from e


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频首帧列表（___COUNT___）</title>
<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #222; height: 100vh; display: flex; flex-direction: column; }
.topbar { padding: 12px 18px; background: #1f2933; color: #fff; }
.topbar h1 { margin: 0; font-size: 17px; }
.topbar p { margin: 4px 0 0; font-size: 12px; opacity: .8; word-break: break-all; }
.layout { display: flex; flex: 1; min-height: 0; }
.tree { width: 260px; flex: 0 0 260px; background: #fff; border-right: 1px solid #e3e6ea; overflow: auto; padding: 10px 8px; }
.tree h2 { font-size: 12px; color: #6b7280; margin: 6px 8px 8px; text-transform: uppercase; letter-spacing: .5px; }
.search { width: calc(100% - 16px); margin: 4px 8px 10px; padding: 7px 9px; border: 1px solid #d0d5dd; border-radius: 7px; font-size: 13px; }
.node { padding: 6px 8px; border-radius: 6px; cursor: pointer; display: flex; justify-content: space-between; gap: 8px; font-size: 13px; align-items: center; }
.node:hover { background: #eef2f7; }
.node.active { background: #2563eb; color: #fff; }
.node .nm { word-break: break-all; }
.node .cnt { font-size: 11px; background: rgba(0,0,0,.08); border-radius: 10px; padding: 1px 7px; flex: 0 0 auto; }
.node.active .cnt { background: rgba(255,255,255,.25); }
.tree ul { list-style: none; margin: 0; padding-left: 16px; }
.tree > ul { padding-left: 4px; }
.tree ul.tree-root { padding-left: 4px; }
.branch > .children { display: block; }
.branch.collapsed > .children { display: none; }
.caret { display: inline-flex; align-items: center; justify-content: center; width: 1.8em; height: 1.8em; margin-right: 1px; margin-left: -3px; font-size: 16px; line-height: 1; color: #6b7280; cursor: pointer; user-select: none; border-radius: 5px; transition: background .12s; }
.caret:hover { background: #e2e8f0; }
.content { flex: 1; min-width: 0; overflow: auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; padding: 18px; }
.card { position: relative; background: #fff; border: 1px solid #e3e6ea; border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; }
.thumb-link { display: block; position: relative; cursor: pointer; }
.card img { width: 100%; height: 180px; object-fit: cover; background: #000; display: block; transition: filter .15s; }
.thumb-link:hover img { filter: brightness(.78); }
.thumb-link .play { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 44px; color: #fff; text-shadow: 0 2px 10px rgba(0,0,0,.6); opacity: 0; transition: opacity .15s; pointer-events: none; }
.thumb-link:hover .play { opacity: .95; }
.meta { padding: 10px 12px; }
.name { font-weight: 600; font-size: 14px; word-break: break-all; }
.path { font-size: 12px; color: #6b7280; margin-top: 4px; word-break: break-all; }
.size { font-size: 12px; color: #2563eb; margin-top: 4px; }
.hint { font-size: 12px; color: #2563eb; margin-top: 4px; }
.empty { padding: 40px; text-align: center; color: #9aa3af; }
.ctx { position: fixed; display: none; z-index: 999; background: #fff; border: 1px solid #d0d5dd; border-radius: 8px; box-shadow: 0 6px 20px rgba(0,0,0,.18); min-width: 240px; padding: 4px; font-size: 13px; }
.ctx-item { padding: 8px 12px; border-radius: 6px; cursor: pointer; white-space: nowrap; }
.ctx-item:hover { background: #eef2f7; }
</style></head>
<body>
<div class="topbar"><h1>视频首帧列表</h1><p>共 ___COUNT___ 个视频 · 生成于 ___GEN___ · 扫描目录：___ROOT___</p></div>
<div id="modewarn" style="display:none; background:#fff4e5; color:#8a4b00; padding:10px 18px; font-size:13px; line-height:1.6; border-bottom:1px solid #f0c36d;">
  ⚠️ 你是以「本地文件（file://）」方式打开本画廊的。浏览器出于安全限制，<b>不允许网页在此页面内唤起 PotPlayer</b>，
  因此 avi / rmvb / flv / ts / vob / wmv / mpg 等格式点击只会打开所在文件夹，无法直接播放。
  如需「点一下就直接播放任意格式」，请通过本应用界面的 <b>「在浏览器中打开画廊」</b> 按钮打开本页（http 模式，会调用本机播放器）。
</div>
<div class="layout">
  <nav class="tree">
    <h2>目录</h2>
    <input class="search" id="q" type="text" placeholder="搜索文件名…" autocomplete="off">
    <div id="treenav">
      <div class="node active" data-filter="">全部视频 <span class="cnt">___COUNT___</span></div>
      ___TREE___
    </div>
  </nav>
  <main class="content">
    <div class="grid" id="grid">
___CARDS___
    </div>
    <div class="empty" id="empty" style="display:none;">没有匹配的视频</div>
  </main>
</div>
<div id="ctx" class="ctx">
  <div class="ctx-item" data-act="play">&#9654; 播放视频</div>
  <div class="ctx-item" data-act="explore">&#128193; 在文件浏览器中打开所在目录</div>
</div>
<script>
function applyFilter(f){
  document.querySelectorAll('.node').forEach(function(n){ n.classList.toggle('active', n.dataset.filter === f); });
  var q = (document.getElementById('q').value || '').toLowerCase();
  var cards = document.querySelectorAll('.card');
  var shown = 0;
  cards.forEach(function(c){
    var d = c.dataset.dir || '';
    var dirOk = (f === '') || (d === f) || (d.indexOf(f + '/') === 0);
    var nm = (c.dataset.name || '').toLowerCase();
    var nameOk = !q || (nm.indexOf(q) !== -1);
    var show = dirOk && nameOk;
    c.style.display = show ? '' : 'none';
    if (show) shown++;
  });
  document.getElementById('empty').style.display = shown ? 'none' : 'block';
}
document.getElementById('treenav').addEventListener('click', function(e){
  var caret = e.target.closest('.caret');
  var n = e.target.closest('.node');
  if (!n) return;
  var li = n.parentElement;
  if (caret && li.classList.contains('branch')) {
    li.classList.toggle('collapsed');
    caret.textContent = li.classList.contains('collapsed') ? '▸' : '▾';
    return;
  }
  applyFilter(n.dataset.filter);
  if (li.classList.contains('branch')) {
    li.classList.remove('collapsed');
    var c2 = li.querySelector(':scope > .node .caret');
    if (c2) c2.textContent = '▾';
  }
});
document.getElementById('q').addEventListener('input', function(){
  var a = document.querySelector('.node.active');
  applyFilter(a ? a.dataset.filter : '');
});
</script>
<script>
function openFallback(url){
  // 本机没装 PotPlayer 时的兜底：浏览器内 <video> 弹窗
  var w = window.open('', 'vplayer', 'width=960,height=600');
  if(!w){ alert('浏览器拦截了弹窗，请允许本页面弹出窗口后重试。'); return; }
  w.document.open();
  w.document.write(
    '<!doctype html><html><head><meta charset="utf-8"><title>播放</title></head>' +
    '<body style="margin:0;background:#000;height:100vh;display:flex;align-items:center;justify-content:center">' +
    '<video controls autoplay src="' + url + '" style="max-width:100%;max-height:100%"></video>' +
    '</body></html>'
  );
  w.document.close();
}
// 浏览器原生可内联播放的格式（无需外部播放器）；其余格式都必须交给本机 PotPlayer
var NATIVE_EXT = {'.mp4':1, '.webm':1, '.ogv':1, '.m4v':1, '.mov':1, '.mkv':1};
function openPlayer(url){
  // 始终通过独立的 gallery_server.py（SERVER_BASE）调起本机 PotPlayer，
  // 支持【所有格式】（含 avi / rmvb / flv / ts / vob / wmv / mpg 等浏览器放不了的），
  // 因此任何后缀都【直接播放、不会变成下载】。
  // url 形如 SERVER_BASE + '/video?path=<encoded 绝对路径>'
  var abs = '';
  try {
    var u = new URL(url, location.href);
    abs = decodeURIComponent(u.searchParams.get('path') || '');
  } catch (e) { abs = ''; }
  if (!abs) { openFallback(url); return; }
  fetch(SERVER_BASE + '/play?path=' + encodeURIComponent(abs))
    .then(function(r){ return r.text(); })
    .then(function(t){
      if (t === 'noplayer') { openFallback(url); }
      else if (t === 'err') { alert('调用播放器失败，请确认本机已安装 PotPlayer。'); }
      // 'ok' 表示已交由 PotPlayer 播放
    })
    .catch(function(){
      var last = Math.max(abs.lastIndexOf('/'), abs.lastIndexOf(String.fromCharCode(92)));
      var dir = last > 0 ? abs.slice(0, last) : abs;
      window.open('file:///' + dir);
      alert('未能连接画廊服务（gallery_server.py）。已为你打开视频所在文件夹，请用本机 PotPlayer 双击播放。');
    });
}
</script>
<script>
var SERVER_BASE = '___SERVER_BASE___';
var ctx = document.getElementById('ctx');
var ctxPath = '', ctxFp = '';
function showCtx(e, path, fpath){
  e.preventDefault();
  ctxPath = path; ctxFp = fpath;
  ctx.style.left = (e.clientX + window.scrollX) + 'px';
  ctx.style.top  = (e.clientY + window.scrollY) + 'px';
  ctx.style.display = 'block';
}
document.addEventListener('click', function(){ ctx.style.display = 'none'; });
document.getElementById('grid').addEventListener('contextmenu', function(e){
  var card = e.target.closest('.card');
  if (!card) return;
  showCtx(e, card.dataset.path, card.dataset.fpath);
});
// 普通左键单击缩略图：拦截默认跳转（直接访问 /video 会让浏览器把
// avi/rmvb 等放不了的格式当成下载），改为走 openPlayer →
// http 模式下调 /play 唤起本机 PotPlayer（任意格式都直接播放）。
document.getElementById('grid').addEventListener('click', function(e){
  var link = e.target.closest('a.thumb-link');
  if (!link) return;
  e.preventDefault();
  openPlayer(link.getAttribute('href'));
});
ctx.addEventListener('click', function(e){
  var it = e.target.closest('.ctx-item'); if (!it) return;
  var act = it.dataset.act;
  ctx.style.display = 'none';
  if (act === 'play') {
    openPlayer(makeVurl(ctxPath, ctxFp));
  } else if (act === 'explore') {
    var last = Math.max(ctxPath.lastIndexOf('/'), ctxPath.lastIndexOf(String.fromCharCode(92)));
    var dir = last > 0 ? ctxPath.slice(0, last) : ctxPath;
    fetch(SERVER_BASE + '/open-explorer?path=' + encodeURIComponent(dir))
      .then(function(r){ if (!r.ok) alert('打开文件浏览器失败：服务返回 ' + r.status); })
      .catch(function(){
        window.open('file:///' + ctxFp.substring(0, ctxFp.lastIndexOf('/')));
        alert('未能连接画廊服务（gallery_server.py），已改为直接打开所在文件夹。');
      });

  }
});

function makeThumbUrl(p, fp){ return SERVER_BASE + '/thumb?path=' + encodeURIComponent(p); }
function makeVurl(p, fp){ return SERVER_BASE + '/video?path=' + encodeURIComponent(p); }

// 初始化：根据打开方式（本地 file:// 双击 / 经本应用服务 http://）补全每张卡片的缩略图与播放链接
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.card').forEach(function(card){
    var p = card.dataset.path, fp = card.dataset.fpath;
    var img = card.querySelector('img');
    var a = card.querySelector('a.thumb-link');
    if (img) {
      img.src = makeThumbUrl(p, fp);
      img.onerror = function(){ this.onerror = null; this.src = 'file:///' + fp + '.thumb.jpg'; };
    }
    if (a) a.href = makeVurl(p, fp);
  });
});
</script>
</body></html>"""


def build_tree(videos, root):
    """根据视频所在子目录，构造嵌套字典树 + 每节点视频计数。"""
    root_abs = os.path.abspath(root)

    def rel_dir(v):
        d = os.path.dirname(v)
        r = os.path.relpath(d, root_abs)
        return "" if r == "." else r.replace("\\", "/")

    rels = [rel_dir(v) for v in videos]
    # 计数：每个节点 = 该目录及其所有子目录下的视频数
    counts = {}
    for r in rels:
        if r == "":
            continue
        parts = r.split("/")
        for i in range(1, len(parts) + 1):
            key = "/".join(parts[:i])
            counts[key] = counts.get(key, 0) + 1
    # 嵌套结构
    tree = {}
    for r in rels:
        if r == "":
            continue
        node = tree
        for p in r.split("/"):
            node = node.setdefault(p, {})

    def render(node, prefix=""):
        items = []
        for name in sorted(node.keys()):
            child = name if prefix == "" else prefix + "/" + name
            sub = render(node[name], child)
            cnt = counts.get(child, 0)
            # 默认全部收拢（collapsed）：caret 用 ▸，点击再逐级展开下一级
            items.append(
                '<li class="branch collapsed"><div class="node" data-filter="' + html.escape(child, quote=True) + '">'
                '<span class="caret">▸</span>'
                '<span class="nm">' + html.escape(name) + '</span>'
                '<span class="cnt">' + str(cnt) + '</span></div>' + sub + '</li>'
            )
        return ("<ul class=\"children\">" + "".join(items) + "</ul>") if items else ""

    inner = render(tree)
    # 把「初始扫描目录」作为目录树的根节点：默认展开（不加 collapsed），
    # 其子节点（第一级子目录）保持收拢 —— 从而初始只展开到根目录内的内容（第一级）。
    root_name = os.path.basename(root_abs.rstrip(os.sep)) or root_abs
    return (
        '<ul class="children tree-root">'
        '<li class="branch"><div class="node" data-filter="" title="' + html.escape(root_abs, quote=True) + '">'
        '<span class="caret">▾</span>'
        '<span class="nm">' + html.escape(root_name) + '</span>'
        '<span class="cnt">' + str(len(videos)) + '</span>'
        '</div>' + inner + '</li>'
        '</ul>'
    )


def build_gallery(videos, out_html, root, serve=False):
    """生成 HTML 画廊（左侧目录树 + 右侧缩略图网格）。

    缩略图位于各视频所在目录（见 _thumb_path_for_video）。

    关键：画廊本身【不再区分 serve / file:// 两种生成方式】。
    卡片只写入视频的绝对路径（data-path / data-fpath），URL 在浏览器端
    按打开方式自动决定：
      - 经本应用本地服务打开（http://）→ 走 /thumb、/video、/open-explorer；
      - 直接双击打开（file://）→ 走本地 file:// 绝对路径。
    因此同一份 HTML 既能由本应用服务访问，也能单独双击打开，
    缩略图与单击播放在两种场景下都正常。
    serve 参数仅保留用于向后兼容（GUI 仍据此决定是否启动本地服务），
    不再影响写入的 URL 形式。
    """
    root_abs = os.path.abspath(root)

    def rel_dir(v):
        d = os.path.dirname(v)
        r = os.path.relpath(d, root_abs)
        return "" if r == "." else r.replace("\\", "/")

    tree_html = build_tree(videos, root)
    rows = []
    for v in videos:
        name = os.path.basename(v)
        thumb_file = _thumb_path_for_video(v)
        if serve:
            # 绝对服务器地址：无论画廊以 http:// 还是 file:// 打开，缩略图/视频
            # 都命中独立的 gallery_server.py（端口与生成时一致，默认 8765）。
            base = gallery_server_base()
            thumb_url = base + "/thumb?path=" + urllib.parse.quote(v)
            vurl = base + "/video?path=" + urllib.parse.quote(v)
        else:
            thumb_url = file_url(thumb_file)
            vurl = file_url(v)
        try:
            size = human_size(os.path.getsize(v))
        except OSError:
            size = "?"
        d = rel_dir(v)
        rows.append(
            '<div class="card" data-dir="' + html.escape(d, quote=True) + '" '
            'data-name="' + html.escape(name, quote=True) + '" '
            'data-path="' + html.escape(v, quote=True) + '" '
            'data-fpath="' + html.escape(v.replace("\\", "/"), quote=True) + '">'
            f'<a class="thumb-link" href="{html.escape(vurl, quote=True)}" '
            f'rel="noopener" title="点击播放：{html.escape(name)}">'
            f'<img loading="lazy" src="{html.escape(thumb_url, quote=True)}" alt="{html.escape(name)}">'
            '<span class="play">&#9654;</span>'
            '</a>'
            '<div class="meta">'
            f'<div class="name" title="{html.escape(name)}">{html.escape(name)}</div>'
            f'<div class="path">{html.escape(v)}</div>'
            f'<div class="size">{html.escape(size)}</div>'
            '<div class="hint">&#9654; 点击截图播放 / 右键更多</div>'
            "</div></div>"
        )
    cards = "\n".join(rows)
    doc = (
        HTML_TEMPLATE
        .replace("___CARDS___", cards)
        .replace("___TREE___", tree_html)
        .replace("___COUNT___", str(len(videos)))
        .replace("___ROOT___", html.escape(root_abs, quote=True))
        .replace("___GEN___", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        .replace("___SERVER_BASE___", gallery_server_base())
    )
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(doc)


# --------------------------------------------------------------------------
# CHM 打包：用微软官方 hhc.exe 编译（业界标准做法，不自行实现 CHM 二进制）。
# CHM 内只含「画廊 HTML + 封面图」，纯展示（不含任何外部播放器调用）。
# --------------------------------------------------------------------------
CHM_GALLERY_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="___CHARSET___">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>电影封面画廊（___COUNT___）</title>
<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #222; }
.topbar { padding: 12px 18px; background: #1f2933; color: #fff; }
.topbar h1 { margin: 0; font-size: 17px; }
.topbar p { margin: 4px 0 0; font-size: 12px; opacity: .8; word-break: break-all; }
.tocnav { padding: 10px 18px; background: #eef1f5; border-bottom: 1px solid #e3e6ea; }
.tocnav-h { font-size: 12px; font-weight: 600; color: #6b7280; margin-bottom: 6px; }
.tocnav ul { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px 10px; }
.tocnav li { font-size: 12px; }
.tocnav a { color: #1d4ed8; text-decoration: none; }
.tocnav a:hover { text-decoration: underline; }
.folder { padding: 14px 18px 4px; font-size: 14px; font-weight: 600; color: #374151; border-bottom: 1px solid #e3e6ea; margin-top: 8px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; padding: 16px 18px; align-items: start; }
.card { background: #fff; border: 1px solid #e3e6ea; border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; }
/* 图片容器：保持原始比例，绝不拉伸变形；用 contain 完整显示，黑底留边 */
.imgwrap { width: 100%; background: #111; overflow: hidden; line-height: 0; }
.card img { width: 100%; height: auto; display: block; }
.card .nocover { width: 100%; min-height: 130px; display: flex; align-items: center; justify-content: center; background: #e9edf2; color: #9aa3af; font-size: 13px; line-height: 1.4; padding: 10px; }
.meta { padding: 9px 11px; }
.name { font-weight: 600; font-size: 13px; word-break: break-all; }
.path { font-size: 11px; color: #6b7280; margin-top: 3px; word-break: break-all; }
</style></head>
<body>
<div class="topbar"><h1>电影封面画廊</h1><p>共 ___COUNT___ 个视频 · 生成于 ___GEN___ · 扫描目录：___ROOT___</p></div>
<nav class="tocnav"><div class="tocnav-h">目录导航（点左侧目录树或下方链接跳转）</div><ul>___NAV___</ul></nav>
___BODY___
</body></html>"""


def _safe_cover_name(video_path):
    """用视频路径的哈希生成唯一、合法的封面文件名（避免中文/特殊字符冲突）。"""
    h = hashlib.md5(os.path.abspath(video_path).encode("utf-8")).hexdigest()[:16]
    return h + ".jpg"


def build_chm_gallery(videos, work_dir, root, charset="utf-8", body_encoding="utf-8"):
    """生成 CHM 专用画廊：把封面拷进 work_dir/covers/，返回 (画廊HTML路径, 目录索引表)。

    每张卡片只展示封面图 + 文件名/路径（不再内置任何外部播放器调用）。
    封面以相对路径 covers/<hash>.jpg 引用，chmcmd/hhc 会一并打进 CHM。

    返回的 目录索引表 形如 { 绝对目录路径: sec序号 }，供 _build_chm_toc 生成
    左侧保留原始目录结构的导航栏（每个目录指向 gallery.html#secN 锚点）。

    charset / body_encoding 由 build_chm 按系统代码页决定（中文系统 GBK/Codepage=936，
    UTF-8 Beta 系统 UTF-8/Codepage=65001）；写出时 errors="ignore" 丢弃无法编码的字符。
    """
    root_abs = os.path.abspath(root)
    covers_dir = os.path.join(work_dir, "covers")
    os.makedirs(covers_dir, exist_ok=True)
    # 封面可能本身是 webp 等格式，转 CHM 时统一转成真正的 jpg（无 ffmpeg 则原样复制）
    ffmpeg = find_ffmpeg()

    # 按所在文件夹分组，便于在 CHM 里分节浏览
    groups = {}
    for v in videos:
        d = os.path.dirname(os.path.abspath(v))
        groups.setdefault(d, []).append(v)

    # 按相对路径排序，并为每个含视频的目录分配锚点序号（与 TOC 对应）
    ordered_dirs = sorted(groups.keys(), key=lambda p: os.path.relpath(p, root_abs))
    folder_index = {d: i for i, d in enumerate(ordered_dirs)}

    sections = []
    for d in ordered_dirs:
        rel = os.path.relpath(d, root_abs)
        rel = "" if rel == "." else rel
        sec_id = folder_index[d]
        rows = []
        for v in groups[d]:
            name = os.path.basename(v)
            thumb_src = _thumb_path_for_video(v)
            cover_name = _safe_cover_name(v)
            cover_dst = os.path.join(covers_dir, cover_name)
            if os.path.isfile(thumb_src):
                try:
                    _convert_image_to_jpg(thumb_src, cover_dst, ffmpeg)
                    img = f'<div class="imgwrap"><img src="covers/{cover_name}" alt="{html.escape(name)}"></div>'
                except Exception:  # noqa: BLE001
                    img = '<div class="imgwrap"><div class="nocover">无封面</div></div>'
            else:
                img = '<div class="imgwrap"><div class="nocover">无封面</div></div>'
            rows.append(
                f'<div class="card">{img}'
                f'<div class="meta"><div class="name" title="{html.escape(v)}">'
                f'{html.escape(name)}</div>'
                f'<div class="path">{html.escape(v)}</div></div>'
                f'</div>'
            )
        heading = "全部视频" if rel == "" else html.escape(rel)
        # 每个目录区块加锚点 id=secN，左侧导航栏据此跳转
        sections.append(
            f'<div class="folder" id="sec{sec_id}">{heading}（{len(groups[d])}）</div>'
            f'<div class="grid">{"".join(rows)}</div>'
        )

    # 页内目录导航（兜底）：即使 CHM 左侧目录树不可用，也能在正文里跳转
    nav_items = []
    for d in ordered_dirs:
        rel = os.path.relpath(d, root_abs)
        rel = "" if rel == "." else rel
        label = "全部视频" if rel == "" else rel
        nav_items.append(
            '<li><a href="#sec%d">%s</a>（%d）</li>'
            % (folder_index[d], html.escape(label), len(groups[d]))
        )
    nav_html = "\n".join(nav_items)

    body = "\n".join(sections)
    doc = (
        CHM_GALLERY_TEMPLATE
        .replace("___CHARSET___", charset)
        .replace("___NAV___", nav_html)
        .replace("___BODY___", body)
        .replace("___COUNT___", str(len(videos)))
        .replace("___ROOT___", html.escape(root_abs, quote=True))
        .replace("___GEN___", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    gallery_html = os.path.join(work_dir, "gallery.html")
    # 正文按 GBK（或 UTF-8）写入，errors="ignore" 丢弃 GBK 无法表示的字符，
    # 避免 'gbk' codec can't encode 崩溃（个别生僻/外文/emoji 字符会丢失，可接受）。
    with open(gallery_html, "w", encoding=body_encoding, errors="ignore") as f:
        f.write(doc)
    return gallery_html, folder_index


def _build_chm_toc(folder_index, root, meta=""):
    """根据 目录索引表 生成 CHM 目录文件(.hhc)内容：镜像原始文件夹结构。

    meta 为写入 <HEAD> 的编码声明（如 '<meta charset="utf-8">'），
    用于告诉编译器 .hhc 里的目录名是什么编码，避免中文在导航栏乱码。

    - 含视频的目录：作为可点击主题，Local 指向 gallery.html#secN（锚点跳转）。
    - 中间目录（仅作为路径容器）：作为书节点（无 Local），可展开/收起。
    - 根目录：指向 gallery.html 顶部。
    """
    root_abs = os.path.abspath(root)
    tree = {}
    for d in folder_index:
        rel = os.path.relpath(d, root_abs)
        parts = [] if rel == "." else rel.split(os.sep)
        node = tree
        for p in parts:
            node = node.setdefault(p, {})

    def rel_to_abs(parts):
        return root_abs if not parts else os.path.join(root_abs, *parts)

    def render(parts):
        node = tree
        for p in parts:
            node = node[p]
        items = []
        for name in sorted(node.keys()):
            child_parts = parts + [name]
            child_abs = rel_to_abs(child_parts)
            sub = render(child_parts)
            if child_abs in folder_index:
                local = "gallery.html#sec%d" % folder_index[child_abs]
                obj = (
                    '<OBJECT type="text/sitemap">'
                    '<param name="Name" value="%s">'
                    '<param name="Local" value="%s"></OBJECT>'
                    % (html.escape(name), local)
                )
            else:
                obj = (
                    '<OBJECT type="text/sitemap">'
                    '<param name="Name" value="%s"></OBJECT>'
                    % html.escape(name)
                )
            items.append("<LI>" + obj + (sub if sub else "") + "</LI>")
        return ("<UL>\n" + "\n".join(items) + "\n</UL>") if items else ""

    root_name = os.path.basename(root_abs.rstrip(os.sep)) or root_abs
    root_obj = (
        '<OBJECT type="text/sitemap">'
        '<param name="Name" value="%s">'
        '<param name="Local" value="gallery.html"></OBJECT>'
        % html.escape(root_name)
    )
    inner = render([])
    return (
        '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML//EN">\n'
        '<HTML><HEAD>' + meta + '</HEAD><BODY>\n<UL>\n'
        '<LI>' + root_obj + (inner if inner else "") + '</LI>\n'
        '</UL>\n</BODY></HTML>'
    )


def chm_filename_for(root):
    """根据扫描目录生成唯一的 CHM 文件名（与 gallery_filename_for 同规则）。"""
    p = os.path.abspath(root)
    token = p.replace(":", "").replace("\\", "_").replace("/", "_")
    token = re.sub(r'[*?"<>|]', "", token).strip().strip(".")
    if not token:
        token = "root"
    if len(token) > 100:
        token = token[:100] + "_" + hashlib.md5(p.encode("utf-8")).hexdigest()[:8]
    return "gallery_" + token + ".chm"


def _get_ansi_codepage():
    """返回系统 ANSI 代码页（GetACP），用于决定 CHM 的编码配对。

    常见值：936=简体中文(GBK)，950=繁体中文，932=日文，65001=Windows“UTF-8 Beta”。
    """
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetACP())
    except Exception:  # noqa: BLE001
        try:
            import locale
            enc = (locale.getpreferredencoding() or "").lower()
            if "65001" in enc or "utf-8" in enc or "utf8" in enc:
                return 65001
        except Exception:  # noqa: BLE001
            pass
        return 936


def build_chm(videos, out_dir, root):
    """把画廊编译成 .chm，返回 .chm 绝对路径。

    编译器：优先 hhc.exe（微软官方，能编译出真正的左侧目录树/导航栏）；
    回退 chmcmd.exe（开源，但本构建不出目录树，仅作兜底、保证正文不乱码）。
    若两者都找不到，抛出 FileNotFoundError 并附带安装/配置指引。
    """
    # 优先 hhc（能出真正的目录树/导航栏）；兜底 chmcmd（本构建不出 TOC，但正文可正确）
    hhc = find_hhc()
    chmcmd = find_chmcmd()
    compiler = hhc or chmcmd
    if not compiler:
        raise FileNotFoundError(
            "未找到 CHM 编译器（hhc.exe 或 chmcmd.exe）。任选其一即可：\n"
            "  1) 用 Microsoft HTML Help Workshop（推荐，左侧目录树/导航栏最稳）：\n"
            "     从 GitHub 下载便携包并解压：\n"
            "       https://github.com/skywind3000/support/releases/download/1.0.0/htmlhelp.zip\n"
            "     把里面的 hhc.exe / hha.dll / itcc.dll 三个文件放到本应用 tools/ 下；\n"
            "     首次用请以管理员运行一次 tools/register_hhw.bat 完成 COM 注册。\n"
            "  2) 或下载开源 chmcmd（免注册、不依赖 itircl/itcc，但本版无目录树，仅兜底）：\n"
            "     https://github.com/skywind3000/support/releases/download/1.0.0/chmcmd.zip\n"
            "     解压后把 chmcmd.exe 放到本应用目录的 tools/ 下即可。\n"
            "  3) 设置环境变量 HHC_PATH / CHMCMD_PATH 指向对应 exe 的绝对路径。\n"
            "完成后重新点击「打包成 CHM」即可。"
        )

    # ---- 导航栏/正文编码（关键）----
    # HTML Help 查看器用【CHM 自身声明的 Codepage】来解码：
    #   (a) 左侧目录树(TOC) 的字符串表(#STRINGS)；
    #   (b) 各 HTML 正文的字节（与 <meta> 冲突时多数版本以 CHM 代码页为准）。
    # 故“写入 .hhc / .html 的字节编码”必须与“声明的 Codepage”严格配对。
    # hhc 4.74 是 ANSI 程序，实测在中文系统上 GBK + Codepage=936 时导航栏中文
    # 最稳；UTF-8 + Codepage=65001 反而会让中文导航栏乱码，故默认走 GBK。
    # 代价：GBK 无法表示中文/ASCII 之外的字符（西里尔字母、emoji 等）。为避免
    # Python 写入时抛 'gbk' codec can't encode，所有写出统一用 errors="ignore"
    # 直接丢弃这些无法编码的字符（文件名/目录名里的个别生僻字符会少掉，可接受）。
    acp = _get_ansi_codepage()
    if acp == 65001:
        hhc_encoding = "utf-8-sig"
        toc_meta = '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
        codepage_line = "Codepage=65001\n"
        body_charset, body_encoding = "utf-8", "utf-8"
    else:
        hhc_encoding = "gbk"
        toc_meta = '<meta http-equiv="Content-Type" content="text/html; charset=gb2312">'
        codepage_line = "Codepage=936\n"
        body_charset, body_encoding = "gb2312", "gbk"

    os.makedirs(out_dir, exist_ok=True)
    # 中间产物放到系统临时目录。hhc 4.74 是 ANSI 程序，读中文路径会乱码（HHC5010），
    # 故先编译到 ASCII 临时名，成功后再重命名；.hhp 本身也用 UTF-8 写（仅含 ASCII 路径）。
    import tempfile
    work_dir = tempfile.mkdtemp(prefix="chm_build_")

    gallery_html, folder_index = build_chm_gallery(
        videos, work_dir, root, charset=body_charset, body_encoding=body_encoding
    )

    # 收集封面文件（相对工作目录，反斜杠，编译器识别）
    cover_files = []
    covers_dir = os.path.join(work_dir, "covers")
    if os.path.isdir(covers_dir):
        for fn in sorted(os.listdir(covers_dir)):
            if fn.lower().endswith(".jpg"):
                cover_files.append("covers\\" + fn)

    chm_name = chm_filename_for(root)
    chm_out = os.path.join(out_dir, chm_name)

    compiled_tmp = os.path.join(work_dir, "compiled.chm")

    # 目录文件（.hhc）：镜像原始视频目录结构的导航栏（与 CHM 代码页同编码写入）。
    # errors="ignore" 同样丢弃 GBK 无法表示的字符，避免导航栏里的外文目录名导致崩溃。
    toc = _build_chm_toc(folder_index, root, meta=toc_meta)
    toc_path = os.path.join(work_dir, "toc.hhc")
    with open(toc_path, "w", encoding=hhc_encoding, errors="ignore") as f:
        f.write(toc)

    # 项目文件（.hhp）：仅含 ASCII 路径与 Codepage 指令，用 UTF-8 写最稳。
    files_block = "gallery.html\n" + "".join(c + "\n" for c in cover_files)
    hhp = (
        "[OPTIONS]\n"
        "Compatibility=1.1\n"
        "Compiled file=" + compiled_tmp + "\n"
        "Contents file=toc.hhc\n"
        "Default topic=gallery.html\n"
        "Display compile progress=No\n"
        + codepage_line +
        "Language=0x804\n\n"
        "[FILES]\n" + files_block
    )
    hhp_path = os.path.join(work_dir, "project.hhp")
    with open(hhp_path, "w", encoding="utf-8") as f:
        f.write(hhp)

    # 依次尝试编译器：优先 hhc（能出真正的目录树/导航栏），失败则回退 chmcmd
    # （免注册、不依赖 itircl/itcc）。若 hhc 因 itcc.dll/itircl 缺失或
    # 未注册(HHC6003 / compiler object)而失败，自动回退到 chmcmd，避免硬失败。
    compilers = []
    if hhc:
        compilers.append(("hhc", hhc))
    if chmcmd:
        compilers.append(("chmcmd", chmcmd))

    last_err = None
    for kind, comp in compilers:
        if kind == "hhc":
            _ensure_hhw_registered(hhc)  # 注册 itcc/itircl（hha 由 hhc 直接加载，无需注册），失败静默（会回退到 chmcmd）
            cmd = [comp, hhp_path]
        else:
            cmd = [comp, "--no-html-scan", hhp_path]
        run_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=work_dir,
        )
        if sys.platform == "win32":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(cmd, **run_kwargs)
        except Exception as e:  # noqa: BLE001
            last_err = "调用 %s 失败：%s" % (kind, e)
            continue

        out_txt = (proc.stdout or b"").decode("utf-8", "replace")
        err_txt = (proc.stderr or b"").decode("utf-8", "replace")
        log = out_txt + "\n" + err_txt

        if not os.path.isfile(compiled_tmp):
            last_err = "【%s】未生成 CHM。\n%s\n%s" % (kind, out_txt[:800], err_txt[:800])
            if kind == "hhc" and "HHC6003" in log:
                last_err += (
                    "\n\n（hhc 报 HHC6003：itcc.dll / itircl.dll 未注册或缺失。"
                    "已自动回退到 chmcmd。请以管理员运行一次 tools/register_hhw.bat，"
                    "或把 htmlhelp.zip 里的 itcc.dll 放到 tools/ 下后再试。）"
                )
            continue

        # hhc 在 HHC6003 时仍写出空壳（有 ITSF 头但内容缺失），须看日志判定后回退
        if kind == "hhc" and ("HHC6003" in log or "were not compiled" in log.lower()):
            last_err = "【hhc】内容未编入（多半 itircl.dll 未注册）。\n%s\n%s" % (
                out_txt[:800], err_txt[:800])
            continue

        break  # 编译成功
    else:
        raise RuntimeError(
            "CHM 编译失败（已尝试：%s）。\n%s\n\n建议修复编译器：\n"
            "  hhc 需要 tools/ 下有 hha.dll（直接加载）+ itcc.dll（需注册）；\n"
            "  管理员运行一次 tools/register_hhw.bat 即可注册 itcc/itircl；\n"
            "  itcc.dll/hha.dll 可从便携包取："
            "https://github.com/skywind3000/support/releases/download/1.0.0/htmlhelp.zip\n"
            "  或改用免注册的 chmcmd："
            "https://github.com/skywind3000/support/releases/download/1.0.0/chmcmd.zip"
            % (", ".join(k for k, _ in compilers) or "无", last_err or "")
        )

    # 编译成功：把 ASCII 临时文件重命名为最终文件名（可含中文），并清理临时目录
    if os.path.abspath(compiled_tmp) != os.path.abspath(chm_out):
        if os.path.isfile(chm_out):
            os.remove(chm_out)
        shutil.move(compiled_tmp, chm_out)
    shutil.rmtree(work_dir, ignore_errors=True)

    # 终检：产物必须是合法 CHM（ITSF 头）
    with open(chm_out, "rb") as _f:
        if _f.read(4) != b"ITSF":
            raise RuntimeError("生成的文件不是合法 CHM（缺少 ITSF 头）。")

    return chm_out


def launch_server_subprocess(out_dir, gallery_name, port_file):
    """启动【脱离父进程】的后台画廊服务（独立的 gallery_server.py），返回 (proc, port)。

    返回值为元组：
      - proc：新启动的子进程 Popen；若同名端口已有服务在跑（上一次脱离父进程
              残留的、或用户手动启动的独立 gallery_server.py），则为 None（复用）。
      - port：服务实际绑定的端口（固定默认 8765，或环境变量 VTL_GALLERY_PORT）。

    仅在 Windows 上追加 DETACHED_PROCESS | CREATE_NO_WINDOW，使其彻底独立于
    GUI 父进程且不可见（父进程退出后子进程继续存活）。
    """
    port = gallery_server_port()
    # 端口已被占用（多半是已存在的服务）：直接复用，不再重复启动，并写回 port_file。
    if is_server_up(port):
        try:
            with open(port_file, "w", encoding="utf-8") as f:
                f.write(str(port))
        except Exception:  # noqa: BLE001
            pass
        return (None, port)

    server_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "gallery_server.py")
    cmd = [sys.executable, server_script, "--dir", out_dir,
           "--name", gallery_name, "--port", str(port), "--port-file", port_file]
    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return (proc, port)


def main():
    ap = argparse.ArgumentParser(description="递归扫描视频并列出首帧截图与文件名")
    ap.add_argument("directory", nargs="?", help="要扫描的目录（含子目录）")
    ap.add_argument("--out", default=None,
                   help="画廊 HTML 输出目录（默认在当前目录建 video_thumb_output）")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件路径（找不到时自动搜索）")
    ap.add_argument("--force", action="store_true",
                   help="强制重新生成所有首帧截图（忽略已存在的缓存）")
    ap.add_argument("--chm", action="store_true",
                   help="扫描完成后额外把画廊打包成 .chm（需本机安装 HTML Help Workshop 的 hhc.exe）")
    ap.add_argument("--serve", action="store_true",
                   help="仅启动本地画廊服务并常驻（等价于直接运行 gallery_server.py）")
    ap.add_argument("--name", default="gallery.html",
                   help="--serve 模式下的画廊文件名（默认 gallery.html）")
    ap.add_argument("--port", type=int, default=None,
                   help="--serve 模式绑定的端口（默认 8765，或环境变量 VTL_GALLERY_PORT）")
    ap.add_argument("--port-file", default=None,
                   help="--serve 模式下把实际绑定端口写入该文件（供调用方回读）")
    args = ap.parse_args()

    if args.serve:
        # 后台常驻服务模式（与直接运行 gallery_server.py 等价）
        if not args.directory or not os.path.isdir(args.directory):
            print("错误：--serve 需要指定一个已存在的输出目录（用法：--serve <out_dir>）",
                  file=sys.stderr)
            sys.exit(1)
        run_server(args.directory, args.name, gallery_server_port(args.port), args.port_file)
        return

    directory = args.directory
    if not directory:
        directory = input("请输入要扫描的目录：").strip().strip('"')
    if not directory or not os.path.isdir(directory):
        print(f"错误：目录不存在或无效：{directory}", file=sys.stderr)
        sys.exit(1)

    try:
        ffmpeg = find_ffmpeg(args.ffmpeg)
    except FileNotFoundError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
    if not ffmpeg:
        print("错误：找不到 ffmpeg。请安装 ffmpeg 或用 --ffmpeg 指定路径。", file=sys.stderr)
        sys.exit(1)
    print(f"使用 ffmpeg：{ffmpeg}")

    out_dir = args.out or os.path.join(os.getcwd(), "video_thumb_output")
    os.makedirs(out_dir, exist_ok=True)
    # 本次扫描的错误日志路径（带目录名 + 扫描时间，一次扫描只算一次）
    err_log = scan_error_log_path(out_dir, directory)

    # 同一目录此前扫描过且画廊仍在 -> 直接打开，无需重扫
    hit = find_cached_gallery(out_dir, directory)
    if hit and not args.force:
        print(f"该目录此前已扫描过，直接打开已有画廊：{hit}")
        reveal_in_explorer(hit)
        return

    print(f"正在扫描：{directory}")
    videos = scan_videos(directory)
    print(f"找到 {len(videos)} 个视频文件。开始抽取首帧（约第 3 秒处）...")

    ok, cached, cover_used, fail = 0, 0, 0, 0
    for i, v in enumerate(videos, 1):
        name = os.path.basename(v)
        out_jpg = _thumb_path_for_video(v)
        # 缓存命中：已存在且来源（封面或视频）未被改动（--force 时跳过此分支）
        if not args.force and not _need_regen(v, out_jpg):
            cached += 1
            print(f"  (缓存) ({i}/{len(videos)}) {name}", flush=True)
            continue
        # 优先使用同名封面图：直接另存为缩略图，跳过抽帧
        cover = _find_cover(v)
        if cover is not None:
            try:
                _convert_image_to_jpg(cover, out_jpg, ffmpeg)
                cover_used += 1
                print(f"  (封面) ({i}/{len(videos)}) {name}  <-  {os.path.basename(cover)}", flush=True)
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"  [跳过] {name}：{e}", file=sys.stderr)
                record_failure(out_dir, v, str(e), err_log)
            continue
        try:
            extract_first_frame(ffmpeg, v, out_jpg)
            ok += 1
        except Exception as e:  # noqa: BLE001 - 单个文件失败不应中断整体
            fail += 1
            print(f"  [跳过] {name}：{e}", file=sys.stderr)
            record_failure(out_dir, v, str(e), err_log)
        print(f"  ({i}/{len(videos)}) {name}", flush=True)

    out_html = os.path.join(out_dir, gallery_filename_for(directory))
    build_gallery(videos, out_html, directory)
    save_scan_log_entry(out_dir, directory, os.path.basename(out_html), len(videos))

    if args.chm:
        try:
            chm_path = build_chm(videos, out_dir, directory)
            print(f"CHM 已生成：{chm_path}")
        except Exception as e:  # noqa: BLE001 - CHM 失败不应影响画廊本身
            print(f"CHM 打包失败：{e}", file=sys.stderr)

    print(f"\n完成：抽帧 {ok}，用封面 {cover_used}，复用缓存 {cached}，失败 {fail}")
    if fail:
        print(f"失败明细已写入日志：{err_log}")
    print(f"画廊已生成（在文件资源管理器中打开）：{out_html}")


if __name__ == "__main__":
    main()
