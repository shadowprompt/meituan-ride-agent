# 美团骑行轨迹识别 Agent

> **输出图片怎么看：**见 [输出图片说明与人工核对指南](IMAGE_OUTPUTS.md)，包含候选着色、掩码、中心线、端点/分叉标记的含义、检查目的与验证边界。

> **无需 API 的第一阶段入口：** `python3 src/ride_web.py` 后访问 `http://127.0.0.1:8765/pixel-route`。可在原图上按经过顺序人工点击、方向预览、保存/重开及导出有序像素 JSON；保留重复经过，明确人工近似、未验证、非 GPS。操作和真实浏览器验收见 [PIXEL_ROUTE_ACCEPTANCE.md](PIXEL_ROUTE_ACCEPTANCE.md)。自动图段排序尚未实现。

> **当前交付入口：[本地试用与验收说明](DELIVERY.md)。** 运行 `python src/ride_web.py` 使用本地网页；运行 `python src/ride_doctor.py` 检查本机条件。当前是待真实模型验收的试用版，仍需人工配准，未实现自动定位。下面早期 OCR／指标目标属于规划，不能视作当前已实现功能。

## 源码目录与运行方式

请始终在项目根目录执行命令（无需 `cd src`）：

```bash
python -m pip install -r requirements.txt
python src/ride_doctor.py
python src/ride_web.py --port 8765
# 从项目根目录发现 test/ 内的测试，显式指定源码导入路径
PYTHONPATH=src python -m unittest discover -s test -v
```

- `src/`：业务源码、网页与 CLI 入口。
- `test/`：回归测试；`examples/`：合成示例；`training-demo/`：独立训练演示。
- `outputs/`、`local-data/`：本地任务与私有数据，仍在原位置，不会迁入 `src/`。
- Web 创建/继续采用后台异步任务协议：HTTP 快速返回 **202** 和任务编号，网页轮询状态；不再把完整多轮流程压在一个 HTTP 请求内。模型 API 单次超时仍为 **300 秒**，后台一轮 CLI 默认总预算 **4500 秒（75 分钟）**。地图下载、HTTP 请求读取等安全限时不变；不自动重试或重放上传授权。详见下方“异步 Web 协议”。
- 旧命令 `python ride_web.py` 改为 `python src/ride_web.py`；其他业务脚本同理。已有 `outputs/web` 任务可直接继续，无需重传。

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
- `test/test_route_eval.py`：9 项自动化测试。
- 使用方式、验证结果与已知限制见 [EVALUATION.md](EVALUATION.md)。
- `training-demo/` 仍是合成数据训练演示，未用于真实路线置信度。

## 本地快速运行（无需大模型）

需要 Python 3.10+。评测器与测试仅使用标准库：

```sh
PYTHONPATH=src python -m unittest discover -s test -p "test_*.py" -v
python src/route_eval.py --reference examples/reference.geojson --candidate examples/reference.geojson --candidate examples/shifted.geojson --output outputs/evaluation.json
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
python src/extract_route.py --image examples/synthetic-ride.png --color "#00BFA5" --output-dir outputs/demo
```

完整参数与真实截图用法见 [EXTRACTION.md](EXTRACTION.md)。安装依赖后全量测试共 15 项。
仅评测模块仍无第三方依赖，可用 `PYTHONPATH=src:test python -m unittest test_route_eval -v` 单独测试。

目录约定：源代码和合成示例纳入 Git；个人输入放 `local-data/`，下载缓存放 `cache/`，生成结果放 `outputs/`，三者均不提交。
旧 `reconstruction/` 暂作为私有历史实验保留，不直接迁移含硬编码位置的脚本；可复用能力逐步提取至正式模块。

## 像素中心线与拓扑（新增）

在颜色提取后运行：

```sh
python src/centerline.py --mask outputs/demo/mask.png --output-dir outputs/demo-centerline
```

输出骨架图、端点/分叉检查图及 `pixel-graph.json`。仅单一开放无分支链导出几何有序点；
分叉、断线、闭环不擅自拼接。坐标为掩码局部像素，不是 GPS，不推断骑行方向或重叠次数。
详见 [CENTERLINE.md](CENTERLINE.md)。当前全量自动化测试共 23 项。

## 人工控制点配准（新增）

中心线分段可通过人工确认的 WGS84 控制点映射到候选地理坐标：

```sh
python src/register_pixels.py --graph outputs/demo-centerline/pixel-graph.json --controls examples/synthetic-controls.json --output-dir outputs/demo-registration
```

详见 [REGISTRATION.md](REGISTRATION.md)。至少三个不共线控制点；独立检查门槛要求至少两个未参与拟合的检查点。
输出是未验证的地理分段集合，不自动贴路、不确定骑行顺序，不能直接作为单一有序路线提交给评测器。
当前全量测试共 28 项；合成演示不是独立真实精度评测。

## Agent 入口（新增）

现在可以用 `ride_agent.py` 创建任务、接收追问、回答后继续，并保存工具调用与产物。

```sh
python examples/make_synthetic_screenshot.py
python src/ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/agent-demo --demo
python src/ride_agent.py resume --task-dir outputs/agent-demo --answer-file examples/demo-answer.json
```

此 demo 是固定流程编排，不是视觉模型识别。真实模式支持 OpenAI 兼容视觉 API，必须配置模型并逐次明确同意上传截图/对话；真实供应商尚未联调。详见 [AGENT.md](AGENT.md)。
当前 Agent 已集成提色、中心线、用户控制点配准与受控道路候选匹配，但不自动地理定位，不是完整自动路线识别产品。已有像素任务（包括模型复核报错的 API 任务）可通过 `resume --local-only --controls-file ...` 确定性导出未排序 WGS84 分段，无需模型复核；详见 [本地经纬度交付](DELIVERY.md#无需模型已有像素任务的本地经纬度交付)。
全量测试 33 项（本地/离线）；不代表模型识别准确率。

### Agent 人工配准闭环

Agent 已接入 `register` 白名单工具，可用 `start --goal registration` 请求完整人工配准阶段，随后通过 `resume --controls-file ...` 提供地标文件；详见 AGENT.md。不会允许模型捏造控制坐标。全量测试现为 36 项，包含模拟 API 请求；真实模型仍待联调。

### Agent 道路候选闭环

新增本地 `road_match.py` 工具并接入 Agent `--goal roads`。从截图提线到人工配准、路网候选和道路叠图的合成流程已跑通；3/3 分段获得候选。详见 [ROAD_MATCHING.md](ROAD_MATCHING.md)。
全量测试 52 项。仍缺自动定位和真实视觉模型/真实截图独立验收；当前不是全自动成品。

测试发现请显式使用 `-s test`，不要用裸 `unittest discover`，避免未发现测试却显示成功。单跑模块的 `PYTHONPATH=src:test` 是 POSIX 写法；PowerShell 对应 `$env:PYTHONPATH = "src;test"`。


## 异步 Web 协议与资源边界

- `POST /api/tasks`、`POST /api/tasks/<task_id>/resume` 校验授权和输入后返回 **202 Accepted**，包含 `task_id`、`status_url`、`execution_state: queued`、`poll_after_ms: 1000`。这只表示接受执行，**不代表处理成功**；旧的同步 201/200 返回不再是生产 HTTP 协议。
- `GET /api/tasks/<task_id>` 返回 `execution_state` 和独立的 `web_execution`（`state`、更新时间、预算及安全错误提示）。执行状态依次为 `queued` → `running` → `succeeded` / `failed`；历史任务无执行记录时为 `idle`。业务 `status` 仍独立保留：`succeeded` 只表示本轮后台流程结束，可能仍需回答、候选选择或批准，不是真值验证。状态尚未落盘或启动失败也能按编号查询执行错误。
- 同任务排队/运行中再次继续或任何补孔预览/应用/决定返回 **409**，不覆盖输入、不重启计算。GET 不等待后台 CLI 的长锁。原任务目录、候选和审计文件不清理；失败后先看已保存结果再决定是否继续。
- 默认 **2 个后台执行线程 + 最多4个等待任务**（共6个未完成任务）；超过容量返回 **503**，无排队重试。最多16个同时 HTTP 处理线程，慢连接拥塞时关闭新连接；HTTP 请求读限时15秒。每个 Web 根目录最多256个任务目录；达到额度由管理员停服归档，不能通过 HTTP 删除或指定任意输出路径。单图20MB、单请求44MB和原有产物白名单仍有效。内部测试构造器可降低 CLI 预算，生产默认4500秒；这不是无限后台执行。
- 网页约每秒只发起一次 GET 状态轮询；请求20秒超时、失败即停止并提示手动刷新，约77分钟停止自动轮询，任务切换时停止旧轮询。终态停止轮询并恢复操作按钮；不再提示“最长3分钟”。轮询从不重新 POST、不自动调用模型。刷新网页后可粘贴原任务编号点“刷新状态”继续只读跟踪。
- 后台异常只暴露固定错误分类/恢复建议，不输出原始异常、stdout/stderr、密钥或私有路径。失败不会伪造成功，已有候选可以继续查看。
- **重启语义**：仅将 `web-execution.json` 中遗留的 queued/running 置为 failed / interrupted；不保存可重放的命令或授权，不自动重新上传截图或下载地图。没有 `task.json` 的启动中断任务只能查看安全错误并新建；已有任务继续必须按原规则重新明确授权（或选择仅本地继续）。服务应单实例使用一个 Web 根目录，运维重启必须停止整个旧服务进程组（含其 CLI 子进程）后再启动新实例，不能让新旧实例并行操作同一根目录。单纯关浏览器或丢失 HTTP 连接不会取消已明确授权的后台轮次；本轮最多12个 Agent 步骤并受总预算限制。授权仅对本次明确提交有效，不继承上次勾选。

异步专项回归：`PYTHONPATH=src python3 -m unittest discover -s test -p test_web_async.py -v`，使用本地 mock 与真实 loopback HTTP；旧 Web fixture 在202后等待终态以兼容已有业务断言，专项绕开此兼容层，直接验证生产202、非阻塞GET、409、防泄密、容量、重启及前端轮询。
