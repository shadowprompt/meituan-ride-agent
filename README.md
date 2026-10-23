# 美团骑行轨迹识别 Agent

## 目标

把用户提供的美团骑行记录截图，解析为结构化运动记录，并保留原图与识别证据，供后续运动档案、热量分析、轨迹展示和标准格式导出使用。

## MVP 范围

输入：
- 美团骑行结束页截图（优先）
- 单张或多张截图
- 支持 PNG/JPEG/WebP

识别内容：
- 骑行日期与时间
- 骑行时长
- 距离
- 平均速度
- 起点、终点（截图中有地址时）
- 地图轨迹区域（先保存原图和裁剪区域，MVP 不承诺恢复 GPS 坐标）
- 页面截图中的其他可见指标

输出：
- 标准化 JSON 运动记录
- 每个字段的置信度、来源区域和原始文本
- 缺失字段列表
- 需要用户确认的字段列表
- 识别失败原因与重试建议

## 明确不在第一版

- 不登录或抓取美团账号
- 不绕过验证码、风控或接口权限
- 不从地图图片直接声称生成精确 GPX
- 不把 OCR 猜测结果当作事实写入健康档案
- 不处理非美团页面，除非后续扩展适配器

## 识别链路

1. 接收图片并计算哈希
2. 图片预检：格式、尺寸、清晰度、是否疑似美团骑行页
3. 页面分区：指标区、地图区、地址区、时间区
4. OCR：提取文字和候选数字
5. 规则归一化：单位、时长、日期、速度、距离
6. 交叉校验：速度 ≈ 距离 / 时长；日期格式；数值范围
7. 输出字段级置信度和证据
8. 低置信度或关键字段冲突时，请用户确认
9. 保存原图引用和结构化结果

## 验收标准

- 同一张清晰截图重复识别，核心字段结果一致
- 距离、时长、平均速度能正确识别并统一单位
- OCR 不确定时能够标记，而不是静默猜测
- 缺少日期或距离时，返回明确的补全问题
- 非美团截图返回可解释的“不支持/无法确认”结果
- 输出 JSON 可被后续运动记录模块直接消费

## 下一步

需要收集至少 10 张脱敏样本，覆盖：清晰截图、低清截图、长距离、短距离、深色模式、不同手机分辨率、多截图记录。样本中请遮盖姓名、手机号、订单号和精确住址。

## 可运行代码进展

- `route_eval.py`：统一离线候选评测 CLI（几何评分 + 可选 OSM 边检查），不是自动截图识别器。
- `test_route_eval.py`：9 项自动化测试。
- 使用方式、验证结果与已知限制见 [EVALUATION.md](EVALUATION.md)。
- `training-demo/` 仍是合成数据训练演示，未用于真实路线置信度。

## 本地快速运行（无需大模型）

需要 Python 3.10+。评测器与测试仅使用标准库：

```sh
python -m unittest discover -s . -p "test_*.py" -v
python route_eval.py --reference examples/reference.geojson --candidate examples/reference.geojson --candidate examples/shifted.geojson --output outputs/evaluation.json
```

`examples/` 是不含个人位置的合成几何示例，不是真实骑行数据。
结果写入 `outputs/evaluation.json`；此例不加载 OSM，道路检查为 null。
Linux/macOS 如需要可将 `python` 换为 `python3`。

可选训练演示（仅合成数据）：

```sh
python -m pip install numpy
python training-demo/train.py
```

仓库不发布个人截图、实验轨迹及下载的 OSM 数据；`reconstruction/` 仅在开发机保留。
EVALUATION.md 中该目录的命令属于开发机实验，首次克隆请使用本节合成示例。
当前没有 Web 服务、截图上传入口或大模型 API 接入。

## 截图轨迹颜色提取（新增）

手动选择截图裁剪范围和轨迹颜色，生成掩码与叠图，不恢复经纬度。

```sh
python -m pip install -r requirements.txt
python examples/make_synthetic_screenshot.py
python extract_route.py --image examples/synthetic-ride.png --color "#00BFA5" --output-dir outputs/demo
```

完整参数与真实截图用法见 [EXTRACTION.md](EXTRACTION.md)。安装依赖后全量测试共 15 项。
仅评测模块仍无第三方依赖，可用 `python -m unittest test_route_eval -v` 单独测试。

目录约定：源代码和合成示例纳入 Git；个人输入放 `local-data/`，下载缓存放 `cache/`，生成结果放 `outputs/`，三者均不提交。
旧 `reconstruction/` 暂作为私有历史实验保留，不直接迁移含硬编码位置的脚本；可复用能力逐步提取至正式模块。

## 像素中心线与拓扑（新增）

在颜色提取后运行：

```sh
python centerline.py --mask outputs/demo/mask.png --output-dir outputs/demo-centerline
```

输出骨架图、端点/分叉检查图及 `pixel-graph.json`。仅单一开放无分支链导出几何有序点；
分叉、断线、闭环不擅自拼接。坐标为掩码局部像素，不是 GPS，不推断骑行方向或重叠次数。
详见 [CENTERLINE.md](CENTERLINE.md)。当前全量自动化测试共 23 项。

## 人工控制点配准（新增）

中心线分段可通过人工确认的 WGS84 控制点映射到候选地理坐标：

```sh
python register_pixels.py --graph outputs/demo-centerline/pixel-graph.json --controls examples/synthetic-controls.json --output-dir outputs/demo-registration
```

详见 [REGISTRATION.md](REGISTRATION.md)。至少三个不共线控制点；独立检查门槛要求至少两个未参与拟合的检查点。
输出是未验证的地理分段集合，不自动贴路、不确定骑行顺序，不能直接作为单一有序路线提交给评测器。
当前全量测试共 28 项；合成演示不是独立真实精度评测。

## Agent 入口（新增）

现在可以用 `ride_agent.py` 创建任务、接收追问、回答后继续，并保存工具调用与产物。

```sh
python examples/make_synthetic_screenshot.py
python ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/agent-demo --demo
python ride_agent.py resume --task-dir outputs/agent-demo --answer-file examples/demo-answer.json
```

此 demo 是固定流程编排，不是视觉模型识别。真实模式支持 OpenAI 兼容视觉 API，必须配置模型并逐次明确同意上传截图/对话；真实供应商尚未联调。详见 [AGENT.md](AGENT.md)。
当前 Agent 只集成提色与中心线工具，尚未集成配准、地图搜索或道路匹配，不是完整自动路线识别产品。
全量测试 33 项（本地/离线）；不代表模型识别准确率。
