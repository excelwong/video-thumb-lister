#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_thumb_lister_gui.py —— 视频首帧列表（带 GUI 的版本）
====================================================================
启动后只显示界面，不自动扫描：用户先点「浏览…」选目录，再点「开始扫描」才执行；
（若在命令行显式传入目录，则按原行为直接扫描。）

  - 递归扫描该目录（含子目录）下的所有视频文件；
  - 为每个视频准备首帧缩略图：
        * 同目录有同名封面图（a.jpg / a.png …）→ 直接另存为缩略图，跳过抽帧；
        * 否则用 ffmpeg 抽首帧；
        * 缩略图已存在且来源未被改动 → 直接复用，不调 ffmpeg（功能 (4)）。
  - 生成 HTML 画廊（左侧目录树 + 右侧缩略图网格，点击缩略图新窗口播放）；
  - 扫描完成后自动用默认浏览器打开画廊。

扫描在后台线程执行，主界面通过队列刷新日志与进度条，不会卡顿。
所有扫描/抽帧/缓存逻辑均复用 video_thumb_lister.py，功能 (1)(2)(3)(4)(4b) 一致。

用法：
  - 双击 run_gui.bat（或 run.bat）启动；
  - 也可命令行：python video_thumb_lister_gui.py [目录]  （传了目录则直接扫描）
====================================================================
"""

import os
import sys
import shutil
import queue
import threading
import subprocess
import tempfile
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# 让本文件能 import 同目录的 video_thumb_lister
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import video_thumb_lister as vtl
from video_thumb_lister import reveal_in_explorer


class ThumbListerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("视频首帧列表生成器")
        self.geometry("780x580")

        self._dir = tk.StringVar(value="")
        self._force = tk.BooleanVar(value=False)
        self.q = queue.Queue()
        self.worker = None
        self.out_html = None
        self.serve_proc = None      # 后台独立服务子进程（脱离 GUI 父进程）
        self.serve_port = None      # 该服务绑定的端口
        self.serve_out_dir = None   # 该服务托管的输出目录

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 首次运行不自动扫描：仅当命令行显式传入了目录时才直接扫描，
        # 否则只显示界面，等用户先「浏览…」选目录、再点「开始扫描」。
        if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
            self._dir.set(sys.argv[1])
            self.after(400, self._start_scan)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="目录：").pack(side="left")
        self.dir_entry = ttk.Entry(top, textvariable=self._dir, width=52)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="浏览…", command=self._pick_directory).pack(side="left", padx=2)
        ttk.Button(top, text="开始扫描", command=self._start_scan).pack(side="left", padx=2)
        ttk.Checkbutton(top, text="强制重新生成", variable=self._force).pack(side="left", padx=6)

        prog_frame = ttk.Frame(self, padding=(10, 4, 10, 2))
        prog_frame.pack(fill="x")
        self.prog = ttk.Progressbar(prog_frame, orient="horizontal",
                                    mode="determinate", maximum=100)
        self.prog.pack(side="left", fill="x", expand=True)
        self.prog_pct = ttk.Label(prog_frame, text="0%", width=14, anchor="e")
        self.prog_pct.pack(side="left", padx=(6, 0))

        self.status = ttk.Label(self, text="就绪", foreground="#374151",
                                padding=(10, 0, 10, 4))
        self.status.pack(fill="x")

        self.log = scrolledtext.ScrolledText(self, wrap="word", state="disabled", height=20)
        self.log.pack(fill="both", expand=True, padx=10, pady=6)

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        self.open_btn = ttk.Button(bottom, text="在浏览器中打开画廊",
                                   command=self._open_gallery)
        self.open_btn.pack(side="left")
        self.chm_btn = ttk.Button(bottom, text="打包成 CHM",
                                  command=self._build_chm)
        self.chm_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(bottom, text="停止后台服务",
                                   command=self._stop_server, state="disabled")
        self.stop_btn.pack(side="left", padx=2)
        ttk.Label(bottom,
                   text="打开画廊与扫描解耦：可随时打开（含已生成的）画廊，左键点 rmvb 即用 PotPlayer 播放",
                   foreground="#6b7280").pack(side="left", padx=8)

    # ---------------------------------------------------------- 选目录
    def _pick_directory(self):
        d = filedialog.askdirectory(title="选择要扫描的视频目录（含子目录）", mustexist=True)
        if d:
            self._dir.set(d)

    # ---------------------------------------------------------- 日志/进度
    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.configure(state="disabled")
        self.log.see("end")

    def _set_progress(self, frac, i=None, total=None):
        pct = max(0, min(100, int(frac * 100)))
        self.prog["value"] = pct
        if i is not None and total is not None:
            self.prog_pct.configure(text=f"{pct}%  ({i}/{total})")
        else:
            self.prog_pct.configure(text=f"{pct}%")

    def _set_status(self, msg):
        self.status.configure(text=msg)

    # ---------------------------------------------------------- 启动扫描
    def _start_scan(self):
        d = self._dir.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showerror("错误", "请先选择一个有效的目录。")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在扫描中，请稍候。")
            return
        self.open_btn.configure(state="disabled")
        self.out_html = None
        self._set_progress(0)
        self._set_status("准备中…")
        self._log(f"开始扫描：{d}")
        force = self._force.get()
        self.worker = threading.Thread(target=self._scan_worker, args=(d, force), daemon=True)
        self.worker.start()
        self.after(100, self._poll)

    # ---------------------------------------------------------- 后台扫描
    def _scan_worker(self, directory, force):
        try:
            ffmpeg = vtl.find_ffmpeg(None)
        except FileNotFoundError as e:
            self.q.put(("error", str(e)))
            return
        if not ffmpeg:
            self.q.put(("error", "找不到 ffmpeg。请安装 ffmpeg，或在 video_thumb_lister.py 中配置 FFMPEG_CANDIDATES 路径。"))
            return
        self.q.put(("log", f"使用 ffmpeg：{ffmpeg}"))

        out_dir = os.path.join(os.getcwd(), "video_thumb_output")
        os.makedirs(out_dir, exist_ok=True)
        # 本次扫描的错误日志路径（带目录名 + 扫描时间，一次扫描只算一次）
        err_log = vtl.scan_error_log_path(out_dir, directory)

        # 同一目录此前扫描过且画廊仍在 -> 直接打开，跳过扫描
        hit = vtl.find_cached_gallery(out_dir, directory)
        if hit and not force:
            self.q.put(("log", f"该目录此前已扫描过，直接打开已有画廊：{hit}"))
            self.q.put(("done_cached", hit))
            return

        videos = vtl.scan_videos(directory)
        self.q.put(("log", f"找到 {len(videos)} 个视频文件。"))
        if not videos:
            out_html = os.path.join(out_dir, vtl.gallery_filename_for(directory))
            vtl.build_gallery(videos, out_html, directory, serve=True, thumb_map={})
            vtl.save_scan_log_entry(out_dir, directory, os.path.basename(out_html), 0)
            self.q.put(("done", (0, 0, 0, 0, out_html, 0)))
            return

        ok = cover = cached = fail = 0
        total = len(videos)
        thumb_map = {}
        for i, v in enumerate(videos, 1):
            name = os.path.basename(v)
            self.q.put(("status", f"正在处理 {i}/{total}：{name}"))
            thumb, action = None, "none"
            exc = None
            try:
                thumb, action = vtl.resolve_thumbnail(v, ffmpeg, force=force)
            except Exception as e:  # noqa: BLE001
                exc = e
                action = "none"
            if thumb:
                thumb_map[v] = thumb
            if action == "cover":
                cover += 1
                self.q.put(("log", f"  (封面) ({i}/{total}) {name}  <-  {os.path.basename(thumb)}"))
            elif action == "frame-cached":
                cached += 1
                self.q.put(("log", f"  (缓存) ({i}/{total}) {name}  <-  {os.path.basename(thumb)}"))
            elif action == "frame-new":
                ok += 1
                self.q.put(("log", f"  ({i}/{total}) {name}  ->  {os.path.basename(thumb)}"))
            else:  # none（含异常）
                fail += 1
                if exc:
                    self.q.put(("log", f"  [跳过] {name}：{exc}"))
                    vtl.record_failure(out_dir, v, str(exc), err_log)
                else:
                    self.q.put(("log", f"  [跳过] {name}：无法生成封面"))
                    vtl.record_failure(out_dir, v, "无可用封面", err_log)
            self.q.put(("progress", (i, total)))

        out_html = os.path.join(out_dir, vtl.gallery_filename_for(directory))
        vtl.build_gallery(videos, out_html, directory, serve=True, thumb_map=thumb_map)
        vtl.save_scan_log_entry(out_dir, directory, os.path.basename(out_html), total)
        if fail:
            self.q.put(("log", f"失败明细已写入：{err_log}"))
        self.q.put(("done", (ok, cover, cached, fail, out_html, total)))

    # ---------------------------------------------------------- 主线程刷新
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    i, total = payload
                    self._set_progress(i / total, i, total)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "error":
                    self._log("错误：" + payload)
                    messagebox.showerror("错误", payload)
                elif kind == "done_cached":
                    out_html = payload
                    self.out_html = out_html
                    self._log(f"画廊：{out_html}")
                    self._set_progress(1.0)
                    self._set_status("已扫描过，直接打开")
                    self.open_btn.configure(state="normal")
                    self._start_server_and_open(out_html)
                elif kind == "done":
                    ok, cover, cached, fail, out_html, total = payload
                    self._log(f"\n完成：抽帧 {ok}，用封面 {cover}，复用缓存 {cached}，失败 {fail}")
                    self.out_html = out_html
                    self._log(f"画廊：{out_html}")
                    self._set_progress(1.0, total, total)
                    self._set_status("扫描完成")
                    self.open_btn.configure(state="normal")
                    self._start_server_and_open(out_html)
                elif kind == "chm_done":
                    chm_path = payload
                    self._set_progress(1.0)
                    self._set_status("CHM 打包完成")
                    self._log(f"CHM：{chm_path}")
                    self.chm_btn.configure(state="normal")
                    try:
                        os.startfile(chm_path)  # 用默认 CHM 查看器(hh.exe)打开
                    except Exception as e:  # noqa: BLE001
                        self._log("打开 CHM 失败：" + str(e))
                    messagebox.showinfo("完成", f"CHM 已生成并用 CHM 查看器打开：\n{chm_path}")
                elif kind == "chm_error":
                    self._set_status("CHM 打包失败")
                    self._log("错误：" + payload)
                    self.chm_btn.configure(state="normal")
                    messagebox.showerror("CHM 打包失败", payload)
        except queue.Empty:
            pass
        if self.worker and self.worker.is_alive():
            self.after(120, self._poll)

    def _gallery_url(self, out_html=None):
        out_html = out_html or self.out_html
        if (self.serve_proc is not None and self.serve_proc.poll() is None
                and self.serve_port and out_html):
            return f"http://127.0.0.1:{self.serve_port}/{os.path.basename(out_html)}"
        return out_html

    def _wait_port(self, port_file, timeout=6):
        """等待后台子进程把实际绑定端口写入 port_file，返回 int 或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if os.path.isfile(port_file):
                    with open(port_file, "r", encoding="utf-8") as f:
                        c = f.read().strip()
                    if c.isdigit():
                        return int(c)
            except Exception:  # noqa: BLE001 - 文件正在写入，稍后重试
                pass
            time.sleep(0.1)
        return None

    def _start_server_and_open(self, out_html):
        gdir = os.path.dirname(out_html)
        gname = os.path.basename(out_html)
        # 画廊现已全部使用 file:// 链接（缩略图 + 视频），不依赖后台服务即可播放。
        # 此处仍会启动独立的 gallery_server.py 后台进程（无害保留，便于兼容与排错），
        # 但实际播放/显示已不再需要它。最后用『文件资源管理器』定位并选中该画廊文件，
        # 由用户双击以 file:// 方式打开。
        same_dir = bool(self.serve_out_dir and self.serve_out_dir == gdir)
        proc_running = (self.serve_proc is not None and self.serve_proc.poll() is None)
        if not (proc_running and same_dir):
            if proc_running and not same_dir:
                # 旧服务托管的是别的目录，先停掉再起新的（避免目录错配）
                try:
                    self.serve_proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                self.serve_proc = None
            port_file = os.path.join(
                tempfile.gettempdir(), f"vtl_port_{os.getpid()}_{int(time.time())}.tmp")
            try:
                self.serve_proc, port = vtl.launch_server_subprocess(gdir, gname, port_file)
            except Exception as e:  # noqa: BLE001
                self._log("启动后台服务失败，将用文件浏览器直接定位文件：" + str(e))
                self.serve_proc = None
                reveal_in_explorer(out_html)
                return
            try:
                if os.path.isfile(port_file):
                    os.remove(port_file)
            except Exception:  # noqa: BLE001
                pass
            self.serve_port = port
            self.serve_out_dir = gdir
            self.stop_btn.configure(state="normal")
            self._log(f"后台服务已就绪（独立进程 gallery_server.py，关闭窗口也会继续）："
                       f"http://127.0.0.1:{port}/{gname}")
        else:
            # 服务仍在运行且托管同一目录：直接复用
            self._log(f"复用已运行的后台服务："
                       f"http://127.0.0.1:{self.serve_port}/{gname}")
        # 生成后用【文件资源管理器】定位/选中该画廊文件，而不是用浏览器自动打开
        reveal_in_explorer(out_html)

    def _open_gallery(self, target_html=None):
        """打开画廊网页——与「扫描生成」功能解耦，可在任意时刻使用。

        - 传入 target_html 则用它；否则优先用本次会话扫描得到的 self.out_html；
        - 若两者都没有（例如刚重开应用、还没扫描），则弹出文件选择框，
          让用户挑选一个【已生成】的画廊 HTML 文件；
        - 画廊缩略图与视频链接均为 file:// 文件浏览器地址，左键点击即相当于在
          资源管理器双击视频文件、由系统默认播放器（PotPlayer 等）播放，无需本地服务。
          此处仍会启动后台服务（无害），但播放已不依赖它。
        """
        target = target_html or self.out_html
        # 清理掉不存在的路径
        if target and not os.path.isfile(target):
            target = None
        if not target:
            target = filedialog.askopenfilename(
                title="选择已生成的画廊 HTML 文件",
                filetypes=[("HTML 画廊", "*.html"), ("所有文件", "*.*")],
            )
        if not target:
            return  # 用户取消
        self.out_html = target  # 记住，便于后续复用/停止服务
        self.open_btn.configure(state="normal")
        self._log(f"打开画廊：{target}")
        self._start_server_and_open(target)

    # ---------------------------------------------------------- 打包 CHM
    def _build_chm(self):
        """把当前目录的画廊打包成 .chm（调用本机 hhc.exe 编译）。"""
        d = self._dir.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showerror("错误", "请先选择一个有效的目录。")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在处理中，请稍候。")
            return
        self.chm_btn.configure(state="disabled")
        self._set_progress(0)
        self._set_status("准备打包 CHM…")
        self._log(f"开始打包 CHM：{d}")
        self.worker = threading.Thread(target=self._chm_worker, args=(d,), daemon=True)
        self.worker.start()
        self.after(100, self._poll)

    def _chm_worker(self, directory):
        try:
            videos = vtl.scan_videos(directory)
            self.q.put(("log", f"找到 {len(videos)} 个视频，准备生成 CHM（封面取自视频同目录的 jpg/webp 封面或生成的 <番号>.png）…"))
            out_dir = os.path.join(os.getcwd(), "video_thumb_output")
            chm_path = vtl.build_chm(videos, out_dir, directory)
            self.q.put(("chm_done", chm_path))
        except FileNotFoundError as e:
            self.q.put(("chm_error", str(e)))
        except Exception as e:  # noqa: BLE001
            self.q.put(("chm_error", f"打包失败：{e}"))

    def _stop_server(self):
        """手动停止后台本地服务（默认关闭窗口不会停止，以便点击播放持续可用）。"""
        if self.serve_proc is not None and self.serve_proc.poll() is None:
            try:
                self.serve_proc.terminate()
                self._log("已停止后台本地服务。")
            except Exception as e:  # noqa: BLE001
                self._log("停止后台服务时出错：" + str(e))
        self.serve_proc = None
        self.serve_port = None
        self.serve_out_dir = None
        self.stop_btn.configure(state="disabled")

    def _on_close(self):
        # 注意：默认【不】在此终止后台本地服务——该服务以独立子进程运行，
        # 关闭 GUI 窗口、主进程退出后仍会继续常驻，从而保证已打开的画廊页面里
        # 左键点击播放（/play 端点）始终可用。若要停止服务，请点「停止后台服务」
        # 按钮，或在任务管理器结束后台 python 进程。
        self.destroy()


if __name__ == "__main__":
    ThumbListerApp().mainloop()
