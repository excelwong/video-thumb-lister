# 视频首帧列表生成器（video-thumb-lister）

递归扫描某个目录（含子目录）下的**所有视频文件**，为每段视频抽取「首帧」缩略图，并生成一个
**可浏览的 HTML 画廊**（左侧目录树 + 右侧缩略图网格）。点击缩略图即可用本机 **PotPlayer**
直接播放对应视频，方便在大量影片里快速「看图挑片」。

---

## 一、核心特性

- **递归扫描**：自动收集目录树里所有常见格式的视频（`.mp4 .mkv .avi .mov .flv .wmv .m4v
  .ts .webm .mpg .mpeg .3gp .m2ts .vob .ogv .mts .m2v .mp4v .f4v .rmvb .rm` 等）。
- **封面图优先（不另存）**：视频同目录已有电影封面图（`jpg` / `webp`，按同名或核心番号匹配，
  如 `a.jpg` 可用于 `a.mp4` / `a.mp4-R` / `a-WC.mp4` 等）→ **直接用它当缩略图**，不改名、不另存。
- **无封面才抽帧**：同目录没有任何封面图时，用 ffmpeg 截取视频**第 10 秒**那一帧（避开片头黑场 /
  标题卡；不足 10 秒自动回退到真正首帧），生成 `<核心番号>.png` 作为封面（文件名即去掉
  `-C`/`-U`/`-UC`/`-W`/`-WC` 及末尾 `R` 后缀的番号）。同目录多个视频共享同一核心番号时只生成一张 png。
- **缓存复用**：封面图（jpg/webp）直接用、不重抽；生成的 `<核心番号>.png` 若已存在且未比视频更旧，
  下次扫描直接复用，不再调用 ffmpeg（想强制重抽用 `--force` 或勾选「强制重新生成」）。
- **HTML 画廊**：左侧可展开 / 收拢的目录树 + 文件名搜索；右侧缩略图网格（卡片含首帧、文件名、
  完整路径、大小）。
- **点击即播**：经本地服务（http 模式）打开画廊时，左键点缩略图会**唤起本机 PotPlayer**播放
  任意格式；右键还有「播放 / 在文件浏览器中打开所在目录」。
- **后台常驻服务**：GUI 启动一个脱离主窗口的本地 HTTP 服务，即使关掉 GUI 窗口，已打开的画廊
  页面里点击播放仍可用。

---

## 二、依赖

- **Python 3 标准库**（含 `tkinter`、`http.server`），**无需**安装 Pillow / opencv。
- **一个 ffmpeg 可执行文件**（仅用于抽首帧）。程序会按以下顺序自动查找：
  1. 环境变量 `FFMPEG_BIN`
  2. `C:/Users/excel/WorkBuddy/Claw/video-ad-cutter/ffmpeg/bin/ffmpeg.exe`
  3. `PATH` 中的 `ffmpeg`
  4. `C:/ffmpeg/bin/ffmpeg.exe`、`C:/Program Files/ffmpeg/bin/ffmpeg.exe`、`D:/ffmpeg/bin/ffmpeg.exe`
  5. 或启动时用 `--ffmpeg <路径>` 显式指定
- **（可选）PotPlayer**：用于点击缩略图直接播放。没装则回退为浏览器内置 `<video>` 播放
  （仅 mp4/webm/ogv/m4v/mov/mkv 等浏览器原生支持的格式），或打开视频所在文件夹让你手动双击。

> 没有 ffmpeg 时程序会报错退出；请确保 ffmpeg 可用，或把路径加入 `FFMPEG_BIN` / `PATH`。

---

## 三、运行

### 方式 A：图形界面（推荐）

双击 **`启动.bat`**（用 `pythonw` 无控制台启动 GUI）。启动后：

1. 点「**浏览…**」选要扫描的视频目录（含子目录）。
2. 点「**开始扫描**」。扫描在后台线程进行，进度条 / 日志实时刷新，完成后自动用默认浏览器打开画廊。
3. 「**强制重新生成**」复选框：勾上后忽略缓存，重新抽全部首帧。
4. 「**在浏览器中打开画廊**」按钮：随时打开（含之前已生成的）画廊，与扫描解耦。
5. 「**停止后台服务**」按钮：关闭常驻的本地 HTTP 服务（默认关 GUI 窗口不会停，以便点击播放持续可用）。

> 命令行若显式传入目录（`video_thumb_lister_gui.py "D:\videos"`），则打开即直接扫描。

### 方式 B：命令行（无界面）

```bat
:: 扫描某目录，画廊默认输出到当前目录的 video_thumb_output/
python video_thumb_lister.py "D:\videos"

:: 指定输出目录
python video_thumb_lister.py "D:\videos" --out "E:\galleries"

:: 显式指定 ffmpeg
python video_thumb_lister.py "D:\videos" --ffmpeg "C:\ffmpeg\bin\ffmpeg.exe"

:: 强制重新生成所有缩略图（忽略缓存）
python video_thumb_lister.py "D:\videos" --force
```

不传目录时会交互提示输入；`--force` 会强制重新抽全部首帧。

---

## 四、画廊使用说明

- **目录树**（左侧）：点击某子目录可筛选显示该目录（及其子目录）下的视频；顶部搜索框可按文件名过滤。
- **缩略图网格**（右侧）：每张卡片 = 首帧缩略图 + 文件名 + 完整路径 + 大小。
- **左键单击缩略图**：
  - 经应用「在浏览器中打开画廊」按钮（**http 模式**）打开时 → 调用 `/play` 端点，**用本机
    PotPlayer 直接播放任意格式**（含 avi / rmvb / flv / ts / vob / wmv / mpg 等浏览器放不了的）。
  - **直接双击 HTML 文件**（**file:// 模式**）打开时 → 浏览器出于安全限制**无法拉起桌面播放器**：
    原生支持的格式可内联播放，其余格式会改为「打开所在文件夹」，请手动用 PotPlayer 双击。
    （页面顶部会有黄色提示条说明这一限制。）
- **右键缩略图**：「播放视频」或「在文件浏览器中打开所在目录」。

> 建议始终通过 GUI 的「在浏览器中打开画廊」打开，以获得「点一下直接播放」的最佳体验。

---

## 五、缓存与输出

- **封面/缩略图位置**：优先直接用视频同目录已有的封面图（`jpg`/`webp`，不改名、不另存）；
  无封面时在该目录生成 `<核心番号>.png`（与视频同目录，便于复用与随视频移动）。
- **画廊文件**：输出目录（默认 `video_thumb_output/`）下生成
  `gallery_<扫描目录路径>.html`，不同扫描目录各一份、互不冲突。
- **扫描运行日志** `video_thumb_output/scan_log.json`：记录「目录 → 画廊文件」映射；
  **同一目录再次扫描会直接打开已有画廊**，无需重扫（除非 `--force`）。
- **失败明细日志** `video_thumb_output/scan_errors_<目录>_<时间>.log`：抽帧失败的视频会记录
  所在文件夹、文件名、失败原因（含 ffmpeg 退出码的中文解读：损坏 / 缺解码器 / 截断 / 权限 / 文件名
  含非法字符等），方便排查坏片。

---

## 六、注意事项

- **ffmpeg 必需**：没有可用 ffmpeg 时无法抽帧，请先准备好（见「二、依赖」）。
- **封面图优先**：视频同目录的 `jpg`/`webp` 封面图（同名或核心番号匹配）会被直接当缩略图、跳过抽帧；
  想强制重新抽帧请改用 `--force` 或临时移走封面图。
- **后台服务常驻**：关闭 GUI 窗口**不会**自动停掉本地 HTTP 服务（设计如此，保证已打开页面可继续播放）；
  彻底关闭请点「停止后台服务」按钮，或在任务管理器结束后台 `python` 进程。
- **file:// 模式限制**：直接双击 HTML 打开时，avi/rmvb/flv/ts/vob/wmv/mpg 等无法在线播放，
  会退化为打开文件夹——这是浏览器安全策略，非程序 bug。

---

## 七、打包成 CHM（离线电子书）

GUI 点「**打包成 CHM**」可把当前画廊编译成单个 `.chm` 离线帮助文件（左侧目录树 + 缩略图网格），方便拷贝分发。

- **编译器**：优先微软 `hhc.exe`（生成真正的目录树/导航栏）；若缺失或注册失败，自动回退 `chmcmd.exe`（开源、免注册，但本版本无目录树，仅兜底）。
- **依赖 `tools/` 目录**（已随仓库提交）：内含 `hhc.exe`、`hha.dll`、`itcc.dll`、`chmcmd.exe`、`register_hhw.bat`。
  - **`itcc.dll` 必须先注册一次**：右键 `tools/register_hhw.bat` → **以管理员身份运行**。脚本会用 32 位 `regsvr32`（`SysWOW64`）注册 `itcc.dll` / `itircl.dll` / `itss.dll`。
    > 注意：`hha.dll` 由 `hhc.exe` 直接加载，**无需也不能**用 `regsvr32` 注册（注册会返回 4）。
  - 缺 `hhc.exe` / `itcc.dll` 时，程序会联网从 GitHub 便携包（`skywind3000/support` 的 `htmlhelp.zip`）自动下载解压到 `tools/`（需联网；网络不可用时请手动下载放入）。
- 若仍报 `HHC6003`：确认 `itcc.dll` 已注册、且 `tools/` 下存在 `hha.dll`。

---

## 八、文件说明

| 文件 | 作用 |
|------|------|
| `video_thumb_lister.py` | 核心：扫描、抽帧、缓存、封面匹配、HTML 画廊生成、本地 HTTP 服务、CHM 打包 |
| `video_thumb_lister_gui.py` | tkinter 图形界面，复用上面的核心逻辑 |
| `gallery_server.py` | 本地画廊 HTTP 服务（供浏览器打开画廊、点缩略图唤起播放器） |
| `启动.bat` | 一键启动 GUI（优先 `pythonw` 无控制台） |
| `tools/` | CHM 编译器依赖：hhc.exe / hha.dll / itcc.dll / chmcmd.exe / register_hhw.bat（已提交，运行时需先注册 itcc.dll） |
| `video_thumb_output/` | 画廊 HTML、扫描日志、失败日志（运行时生成，不提交） |
