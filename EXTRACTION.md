# 截图轨迹颜色提取（MVP）

本文说明已提供的 `extract_route.py` 命令行接口。此模块只做离线像素颜色筛选，不是语义识别：**截图中的地图裁剪范围和轨迹颜色必须手动选择**。

## 安装与更新

在项目目录中运行（Python 3.10+）：

```bash
git pull --ff-only
python -m pip install -r requirements.txt
```

依赖为 Pillow 和 NumPy，由 `requirements.txt` 管理。Windows 同样使用 `python` 命令；以下命令均为单行，可在 PowerShell 中运行。不需要大模型、API Key 或在线地图服务。

## 提取本地截图

```bash
python extract_route.py --image local-data/ride.jpg --crop 0 500 1260 1800 --color '#00BFA5' --hue-tolerance 20 --min-saturation 0.35 --min-value 0.2 --min-component 20 --output-dir outputs/ride
```

- `--image`：本地截图路径。
- `--crop left top right bottom`：按 EXIF 方向校正后的截图像素坐标，左、上边界包含，右、下边界不包含；示例裁剪宽 1260、高 1300 像素。请按实际地图位置调整，范围须在原图内且宽高为正。
- `--color`：手动选取的轨迹参考色，格式为 `#RRGGBB`；加引号避免 `#` 被终端解释为注释。
- `--hue-tolerance`：HSV 色相容差，单位为度；示例为 20。
- `--min-saturation`、`--min-value`：HSV 饱和度、明度下限，范围为 0–1。
- `--min-component`：保留连通区域的最小像素数；示例为 20。
- `--output-dir`：输出目录。

## 输出与人工复核

输出目录包含：

- `mask.png`：与裁剪区域同尺寸的轨迹颜色掩码。
- `overlay.png`：与裁剪区域同尺寸的检查叠图。
- `extraction.json`：提取结果元数据，提取到像素时状态为 `needs_confirmation`，没有保留像素时为 `no_detection`。

原始颜色筛选和连通区域过滤可能漏掉细线、断线或颜色变化的轨迹，也可能保留同色图标、文字等非轨迹区域。请查看掩码和叠图，检查漏线、误选后调整裁剪、颜色及阈值；不能把输出当作已确认路线。

本模块**不输出 GPS／经纬度，不恢复轨迹点顺序，不做自动地理定位**。

## 合成截图演示

在项目目录依次运行：

```bash
python examples/make_synthetic_screenshot.py
python extract_route.py --image examples/synthetic-ride.png --crop 0 0 640 480 --color '#00BFA5' --output-dir outputs/demo
```

查看 `outputs/demo/mask.png`、`outputs/demo/overlay.png` 和 `outputs/demo/extraction.json`。合成图仅用于检查颜色提取流程，不代表真实截图上的识别效果。

## 灰色 / 偏蓝灰色线路

低饱和度参考色自动使用明度邻域与最大饱和度筛选，不使用灰色的色相。
可设置 `--gray-value-tolerance 0.12 --gray-max-saturation 0.32 --gray-min-span 16`。
同色文字或道路仍可能入选；短区域过滤可能遗漏短路线段。不会自动连接遮挡断点。
颜色检查的 `neutral_candidates` 仅是明度候选，不能按数量排名认定路线。
详见 `GRAY_UPDATE.md`。

## 线路带内部修复

灰色分支在细化之前增加有原图证据约束的封闭内部清理，详见 `BAND_FIX.md`。
新增像素记录在 `mask_cleanup`，不自动封闭开放断点，不声称保留真实骑行拓扑。
