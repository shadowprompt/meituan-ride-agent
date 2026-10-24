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
python src/extract_route.py --image local-data/ride.jpg --crop 0 500 1260 1800 --color '#00BFA5' --hue-tolerance 20 --min-saturation 0.35 --min-value 0.2 --min-component 20 --output-dir outputs/ride
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
python src/extract_route.py --image examples/synthetic-ride.png --crop 0 0 640 480 --color '#00BFA5' --output-dir outputs/demo
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

## 路线候选分量选择（本地、可审计）

颜色匹配不等于目标路线。提取后先保存 `candidates-mask.png`（颜色/最小面积与跨度清理后的完整候选），再决定 `mask.png` 的目标分量。`extraction.json` 同时保存 `color_candidate_pixels`、最终 `selected_pixels` 以及 `component_selection`：稳定面积降序编号、像素面积、裁剪局部半开边界框、几何指标、保留/排除结果及原因。`components-overlay.png` 以绿表示保留、橙表示排除，并标注编号；排除的分量不会销毁。

默认不是“只保留最大分量”。只有唯一长带跨度≥裁剪长边45%、框长宽比≥5、占所有候选面积≥85%，并且**其余每个分量**同时满足紧凑（框长宽比≤2、占框面积≥15%）、跨度≤长带10%、框间距≥长带跨度12%且≥估计带宽3倍，才自动排除这些孤立紧凑分量。估计带宽=长带面积/跨度。任一条件不满足即保留全部，并由 Agent 暂停中心线、配准及道路流水线，请用户选择。

这只是保守几何启发式，不是图标语义识别，不能保证任意输入不会误排除：远处紧凑的真实断裂片段可能满足上述条件，长条图标也可能伪装成长带。必须检查叠图；若图形本身无法区分，不能自动推断路线。多路线、大图标、近处碎片及明显断裂路线反例会进入歧义确认，不能通过提高 `min_component` 解决。单分量也仍未经语义验证。

网页“候选路线片段”显示面积、框和编号，用户可勾选多个片段，再勾选“本次使用上面勾选的编号”并继续；通常明显孤立小图标场景无需额外操作。选择可覆盖自动结果，包括重新保留被排除的片段。CLI：

```sh
python3 src/ride_agent.py resume --task-dir outputs/task --local-only --component-ids 1 3
python3 src/extract_route.py --image local-data/image.png --crop 200 1160 1040 1460 --color '#6B798C' --component-ids 1 --output-dir outputs/extraction
```

编号只适用于当前颜色与裁剪；修改颜色/裁剪会清除旧选择，必须先查看新候选。模型不能生成或覆盖用户显式编号。选择不改变裁剪坐标系，不导致同坐标系控制点失效；后续几何、配准、道路结果则重新生成。截图上传授权、地图授权不变，`--local-only` 不调用模型 API。

不恢复 GPS，不保证真实起终点、行进顺序或遮挡部分；不自动连接断裂路线。中心线端点不能当作骑行起终点。
