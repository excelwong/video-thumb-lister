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
        * 其次按「核心番号」匹配：剥离视频名末尾的版本后缀（-U/-C/-UC/-CU、末尾 R）
          后再找图，故封面 ABC-123.jpg 可用于 ABC-123.mp4 / ABC-123R.mp4 /
          ABC-123-U.mp4 / ABC-123-C.mp4 / ABC-123-UC.mp4。
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
#   -U / -C / -UC / -CU （去码 / 中文字幕 等标记，带连字符）
#   末尾单独的 R          （流出/修正版等标记，直接附着，无连字符）
# 例：核心番号 ABC-123 的封面 ABC-123.jpg，可用于
#   ABC-123.mp4 / ABC-123R.mp4 / ABC-123-U.mp4 / ABC-123-C.mp4 / ABC-123-UC.mp4
_COVER_TAIL_TAG = re.compile(r"-(?:U|C|UC|CU)$", re.IGNORECASE)
_COVER_TAIL_R = re.compile(r"R$", re.IGNORECASE)


def _cover_core_candidates(base):
    """由视频名（不含扩展名）推导出可能的「封面核心名」候选列表。

    先返回原名（精确匹配优先），再依次剥离末尾的 -U/-C/-UC/-CU 和 R，
    使 ABC-123R / ABC-123-U / ABC-123-UC 等都能回退到核心名 ABC-123。
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
      2) 剥离版本后缀（-U/-C/-UC/-CU、末尾 R）后的核心名封面，
         如 ABC-123R.mp4 / ABC-123-U.mp4 -> ABC-123.jpg。
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
    ref = _find_cover(video_path) or video_path
    try:
        return t_thumb < os.path.getmtime(ref)
    except OSError:
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
  // 经本应用服务（http://）时：调用 /play 端点，由本机 PotPlayer 播放。
  // 支持【所有格式】（含 avi / rmvb / flv / ts / vob / wmv / mpg 等浏览器放不了的），
  // 因此任何后缀都【直接播放、不会变成下载】。
  if (SERVE) {
    var abs = '';
    try {
      var u = new URL(url, location.href);
      abs = decodeURIComponent(u.searchParams.get('path') || '');
    } catch (e) { abs = ''; }
    if (!abs) { openFallback(url); return; }
    fetch('/play?path=' + encodeURIComponent(abs))
      .then(function(r){ if (!r.ok) openFallback(url); })
      .catch(function(){ openFallback(url); });
    return;
  }
  // 本地（file:// 双击打开）场景：file:// 网页无法拉起桌面播放器，只能交给浏览器/系统默认处理。
  // 浏览器原生支持的格式可内联播放；avi / rmvb / flv / ts / vob / wmv / mpg 等浏览器放不了的，
  // 直接 window.open 会变成“下载”，故改为打开【所在文件夹】，由用户用本机 PotPlayer 双击播放。
  var fp = '';
  try {
    fp = decodeURIComponent(String(url));
    fp = fp.replace(/^file:/i, '').replace(/^[\\/]+/, '');
  } catch (e) { fp = String(url); }
  var dot = fp.lastIndexOf('.');
  var slash = Math.max(fp.lastIndexOf('/'), fp.lastIndexOf('\\\\'));
  var ext = (dot > 0 && dot > slash) ? fp.slice(dot).toLowerCase() : '';
  if (NATIVE_EXT[ext]) { window.open(url, '_blank'); return; }
  var didx = Math.max(fp.lastIndexOf('/'), fp.lastIndexOf('\\\\'));
  var dir = didx > 0 ? fp.slice(0, didx) : fp;
  window.open('file:///' + dir.replace(/\\\\/g, '/'));
  alert('该格式（' + (ext || '未知') + '）浏览器无法在线播放，已为你打开所在文件夹，请用本机 PotPlayer 双击打开它。\\n\\n提示：通过本应用界面「在浏览器中打开画廊」按钮（http 模式）打开本页，可“点一下直接播放”任意格式。');
}
</script>
<script>
var SERVE = (location.protocol !== 'file:');
if (!SERVE) { var __mw = document.getElementById('modewarn'); if (__mw) __mw.style.display = 'block'; }
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
    var dir = ctxPath.replace(/[\\\\/][^\\\\/]*$/, '');
    if (SERVE) {
      fetch('/open-explorer?path=' + encodeURIComponent(dir))
        .then(function(r){ if (!r.ok) alert('无法打开文件浏览器：服务返回 ' + r.status); })
        .catch(function(){ alert('无法打开文件浏览器，请确认本应用仍在运行。'); });
    } else {
      window.open('file:///' + ctxFp.substring(0, ctxFp.lastIndexOf('/')));
    }
  }
});

function makeThumbUrl(p, fp){ return SERVE ? ('/thumb?path=' + encodeURIComponent(p)) : ('file:///' + fp + '.thumb.jpg'); }
function makeVurl(p, fp){ return SERVE ? ('/video?path=' + encodeURIComponent(p)) : ('file:///' + fp); }

// 初始化：根据打开方式（本地 file:// 双击 / 经本应用服务 http://）补全每张卡片的缩略图与播放链接
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.card').forEach(function(card){
    var p = card.dataset.path, fp = card.dataset.fpath;
    var img = card.querySelector('img');
    var a = card.querySelector('a.thumb-link');
    if (img) img.src = makeThumbUrl(p, fp);
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
            thumb_url = "/thumb?path=" + urllib.parse.quote(v)
            vurl = "/video?path=" + urllib.parse.quote(v)
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
    )
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(doc)


def serve_gallery(out_dir, port=0, gallery_name="gallery.html"):
    """启动一个本地 HTTP 服务（守护线程），支撑画廊的『打开文件浏览器』等功能。

    返回 (httpd, port)。画廊通过 http://127.0.0.1:port/<gallery_name> 访问
    （gallery_name 默认 gallery.html，GUI 模式会传入带目录信息的实际文件名）。
    提供端点：
      /thumb?path=<视频绝对路径>         -> 返回该视频的 .thumb.jpg 缩略图
      /video?path=<视频绝对路径>         -> 返回原视频（支持 Range，可在弹窗中播放/拖拽）
      /open-explorer?path=<目录绝对路径> -> 调用系统文件浏览器打开该目录
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
            self.end_headers()
            with open(fpath, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            if path == "/":
                self._serve_file(
                    os.path.join(self.server.out_dir, self.server.gallery_name),
                    "text/html; charset=utf-8", range_ok=False)
            elif path.endswith(".html"):
                # 同一输出目录内可能有多份画廊（不同扫描目录），按文件名直接服务；
                # 仅允许文件名本身，禁止路径穿越。
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
                    self._serve_file(p + ".thumb.jpg", "image/jpeg", range_ok=False)
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
                self.end_headers()
                self.wfile.write(body)
            elif path == "/play":
                # 唤起本机 PotPlayer 播放指定视频文件（传入完整路径）
                p = qs.get("path", [""])[0]
                if not p or not os.path.isfile(p):
                    self.send_error(404)
                    return
                player = find_player()
                if not player:
                    # 本机没装播放器：返回 noplayer，由前端兜底用浏览器 <video>
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", "8")
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
    """（供 GUI 后台独立进程使用）启动画廊 HTTP 服务并【永久阻塞】。

    与 serve_gallery 不同，本函数不返回，直接 serve_forever，便于被一个
    「脱离父进程」的子进程运行——即使 GUI 窗口关闭、主进程退出，服务也能
    在后台继续运行（支撑已打开画廊页面里的左键点击播放 /play 等端点）。
    port_file 非空时，把实际绑定的端口写进该文件，供父进程（GUI）回读。
    """
    httpd, bound_port = serve_gallery(out_dir, port=port, gallery_name=gallery_name)
    if port_file:
        try:
            with open(port_file, "w", encoding="utf-8") as f:
                f.write(str(bound_port))
        except Exception:  # noqa: BLE001 - 写端口失败不影响服务本身
            pass
    print(f"serving gallery at http://127.0.0.1:{bound_port}/{gallery_name}")
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


def launch_server_subprocess(out_dir, gallery_name, port_file):
    """启动一个【脱离父进程】的后台画廊服务子进程，返回 Popen 对象。

    out_dir / gallery_name 会传给子进程；子进程自己选空闲端口（port=0），
    并把端口写入 port_file 供调用方回读。
    仅在 Windows 上追加 DETACHED_PROCESS | CREATE_NO_WINDOW，使其彻底独立于
    GUI 父进程且不可见（父进程退出后子进程继续存活）。
    """
    script = os.path.abspath(__file__)
    cmd = [sys.executable, script, "--serve", out_dir,
           "--name", gallery_name, "--port", "0", "--port-file", port_file]
    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def main():
    ap = argparse.ArgumentParser(description="递归扫描视频并列出首帧截图与文件名")
    ap.add_argument("directory", nargs="?", help="要扫描的目录（含子目录）")
    ap.add_argument("--out", default=None,
                   help="画廊 HTML 输出目录（默认在当前目录建 video_thumb_output）")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件路径（找不到时自动搜索）")
    ap.add_argument("--force", action="store_true",
                   help="强制重新生成所有首帧截图（忽略已存在的缓存）")
    ap.add_argument("--serve", action="store_true",
                   help="仅启动本地画廊服务并常驻（供 GUI 后台独立运行，不扫描）")
    ap.add_argument("--name", default="gallery.html",
                   help="--serve 模式下的画廊文件名（默认 gallery.html）")
    ap.add_argument("--port", type=int, default=0,
                   help="--serve 模式绑定的端口（0=系统自动选空闲端口）")
    ap.add_argument("--port-file", default=None,
                   help="--serve 模式下把实际绑定端口写入该文件（供调用方回读）")
    args = ap.parse_args()

    if args.serve:
        # 后台常驻服务模式：不扫描，仅起 HTTP 服务并阻塞
        if not args.directory or not os.path.isdir(args.directory):
            print("错误：--serve 需要指定一个已存在的输出目录（用法：--serve <out_dir>）",
                  file=sys.stderr)
            sys.exit(1)
        run_server(args.directory, args.name, args.port, args.port_file)
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
        webbrowser.open(file_url(hit))
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
                shutil.copy2(cover, out_jpg)
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

    print(f"\n完成：抽帧 {ok}，用封面 {cover_used}，复用缓存 {cached}，失败 {fail}")
    if fail:
        print(f"失败明细已写入日志：{err_log}")
    print(f"画廊（用浏览器打开）：{out_html}")


if __name__ == "__main__":
    main()
