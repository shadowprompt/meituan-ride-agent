# 美团骑行轨迹识别 Agent

> **输出图片怎么看：**见 [输出图片说明与人工核对指南](IMAGE_OUTPUTS.md)，包含候选着色、掩码、中心线、端点/分叉标记的含义、检查目的与验证边界。

> **无需 API 的第一阶段入口：** `python3 src/ride_web.py` 后访问 `http://127.0.0.1:8765/pixel-route`。可在原图上按经过顺序人工点击、方向预览、保存/重开及导出有序像素 JSON；保留重复经过，明确人工近似、未验证、非 GPS。操作和真实浏览器验收见 [PIXEL_ROUTE_ACCEPTANCE.md](PIXEL_ROUTE_ACCEPTANCE.md)。自动图段排序尚未实现。

> **当前地图入口：默认 `osm_relay` 受控OSM中转。** 模型读图提出明确行政层级/精确路名 → 本次独立授权 → 固定中转服务查询 → WGS84 OSM way/node 证据 → `inspect_roads` / `build_corridor` 未验证走廊。**本机永不直连OSM/Overpass；无自动重试或供应商回退。** 高德保留为显式兼容选项，仅GCJ-02位置/地标线索，不支持道路走廊。严格截图配准仍需真实控制点；走廊不是历史GPS或导航路线。

## 当前地图默认与旧任务迁移

完整配置、授权、服务契约、测试层级与未上线边界见 [OSM中转接入说明](docs/OSM_RELAY.md)。

- 管理员私下配置 `OSM_SERVICE_URL`、`OSM_SERVICE_TOKEN`；仅HTTPS或loopback HTTP（SSH隧道），不接受用户/模型指定地址、路径、凭证。配置预检不发网络请求，不代表真实上游可用。
- 新任务默认 `osm_relay`。API模式每次须明确同意模型上传；地图查询另须本次 `consent_map`。只发行政层级/道路文字或way ID，不把截图发给中转。
- 国家level=2到最后市区level>=6必须明确、递增、有图文依据；不能只凭城市猜国家，不能固定某一城市。名称与模型层级均未验证，由服务逐层精确解析，歧义停止。
- 旧高德任务**不静默改回OSM、不重放、不上传**；用户显式切换并给予双授权：

```sh
python3 src/ride_agent.py resume --task-dir outputs/旧任务 \
  --map-provider osm_relay --consent-map --consent-upload
```

旧OSM任务也须显式选择中转；缺任一授权拒绝，旧地图/对话保留审计、旧道路证据失效。仅本地CLI可用 `--map-provider osm --local-only --osm-file /本地/获授权地图.osm` 只读调试；不能将本地XML冒充中转证据。旧 `fetch_map` 范围下载完全停用。

高德兼容需显式 `--map-provider amap` / 网页选择高德及私下 `AMAP_API_KEY`；不自动回退OSM。POI内容许可门禁保留：仅本地展示/复核，不回传模型或生成配准锚点。历史高德说明见 [高德定位线索](docs/AMAP_PROVIDER.md)，其中“唯一/默认高德”属于已被本节替代的历史策略。

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
- 旧命令 `python ride_web.py` 改为 `python src/ride_web.py`；其他业务脚本同理。已有 `outputs/web` 任务及产物保留；OSM旧任务只读恢复无需重传，但联网继续必须显式选择受控中转或高德兼容并给予本次双授权，不能直接重放。

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

当前OSM中转验收与历史测试迁移见 [docs/OSM_RELAY.md](docs/OSM_RELAY.md)。历史测试数量不代表当前全量；本地OSM直连始终拒绝，在线地图仅通过受控中转或显式高德兼容。


## 异步 Web 协议与资源边界

- `POST /api/tasks`、`POST /api/tasks/<task_id>/resume` 校验授权和输入后返回 **202 Accepted**，包含 `task_id`、`status_url`、`execution_state: queued`、`poll_after_ms: 1000`。这只表示接受执行，**不代表处理成功**；旧的同步 201/200 返回不再是生产 HTTP 协议。
- `GET /api/tasks/<task_id>` 返回 `execution_state` 和独立的 `web_execution`（`state`、更新时间、预算及安全错误提示）。执行状态依次为 `queued` → `running` → `succeeded` / `failed`；历史任务无执行记录时为 `idle`。业务 `status` 仍独立保留：`succeeded` 只表示本轮后台流程结束，可能仍需回答、候选选择或批准，不是真值验证。状态尚未落盘或启动失败也能按编号查询执行错误。
- 同任务排队/运行中再次继续或任何补孔预览/应用/决定返回 **409**，不覆盖输入、不重启计算。GET 不等待后台 CLI 的长锁。原任务目录、候选和审计文件不清理；失败后先看已保存结果再决定是否继续。
- 默认 **2 个后台执行线程 + 最多4个等待任务**（共6个未完成任务）；超过容量返回 **503**，无排队重试。最多16个同时 HTTP 处理线程，慢连接拥塞时关闭新连接；HTTP 请求读限时15秒。每个 Web 根目录最多256个任务目录；达到额度由管理员停服归档，不能通过 HTTP 删除或指定任意输出路径。单图20MB、单请求44MB和原有产物白名单仍有效。内部测试构造器可降低 CLI 预算，生产默认4500秒；这不是无限后台执行。
- 网页约每秒只发起一次 GET 状态轮询；请求20秒超时、失败即停止并提示手动刷新，约77分钟停止自动轮询，任务切换时停止旧轮询。终态停止轮询并恢复操作按钮；不再提示“最长3分钟”。轮询从不重新 POST、不自动调用模型。刷新网页后可粘贴原任务编号点“刷新状态”继续只读跟踪。
- 后台异常只暴露固定错误分类/恢复建议，不输出原始异常、stdout/stderr、密钥或私有路径。失败不会伪造成功，已有候选可以继续查看。
- **重启语义**：仅将 `web-execution.json` 中遗留的 queued/running 置为 failed / interrupted；不保存可重放的命令或授权，不自动重新上传截图或下载地图。没有 `task.json` 的启动中断任务只能查看安全错误并新建；已有任务继续必须按原规则重新明确授权（或选择仅本地继续）。服务应单实例使用一个 Web 根目录，运维重启必须停止整个旧服务进程组（含其 CLI 子进程）后再启动新实例，不能让新旧实例并行操作同一根目录。单纯关浏览器或丢失 HTTP 连接不会取消已明确授权的后台轮次；本轮最多12个 Agent 步骤并受总预算限制。授权仅对本次明确提交有效，不继承上次勾选。

异步专项回归：`PYTHONPATH=src python3 -m unittest discover -s test -p test_web_async.py -v`，使用本地 mock 与真实 loopback HTTP；旧 Web fixture 在202后等待终态以兼容已有业务断言，专项绕开此兼容层，直接验证生产202、非阻塞GET、409、防泄密、容量、重启及前端轮询。

## 近似地理定位与经纬度导出（独立模式）

当已有可信来源的**近似对应点**、但没有独立检查点时，可明确选择近似模式，先交付带不确定度的候选，而不是假装严格配准通过。不会根据图名或语言模型发明坐标，不下载地图、不调用外部模型。**本功能提供有证据锚点到经纬度导出的闭环，不声称仅靠截图自动识别城市。** 缺锚点会停下来索取证据。

1. 在网页上传图片，用离线模式/仅本地继续完成颜色、裁剪及中心线检查；有多个分量时先选择候选。
2. 在继续区域的“定位模式”选择“近似证据锚点（仅本地）”，勾选“仅本地继续”，上传以下格式的 JSON。可把截图中可辨识的地标与用户确认/公开证据中的 WGS84 坐标对应；至少三个不共线点，每点必须有来源和正数 `uncertainty_m`。来源只是保留的证据记录，不自动核验或访问网址。
3. 点击继续，下载 `approximation/approximation.json` 和 `approximation/approximate-segments.geojson`。GeoJSON 自包含全部来源、不确定度说明、拟合残差、凸包外推统计，坐标顺序为 `[经度,纬度]`。

以下数值**全部为合成格式示例，绝非真实截图地标**，不能替代真实输入：

```json
{
  "mode": "approximate",
  "coordinate_system": "WGS84",
  "pixel_frame": "local_mask",
  "synthetic": true,
  "uncertainty_note": "合成示例，仅演示格式；真实使用须写明地标对应歧义、坐标来源和误差假设。",
  "anchors": [
    {"pixel_xy": [0,0], "wgs84": [120,30], "source": "synthetic example, not evidence", "uncertainty_m": 100},
    {"pixel_xy": [700,0], "wgs84": [120.007,30], "source": "synthetic example, not evidence", "uncertainty_m": 100},
    {"pixel_xy": [0,500], "wgs84": [120,29.995], "source": "synthetic example, not evidence", "uncertainty_m": 150}
  ]
}
```

`pixel_xy` 必须相对**当前裁剪左上角的 local_mask**，不是缩略预览坐标或未减去裁剪偏移的原图坐标。原图点 `[x,y]` 对裁剪 `[left,top,right,bottom]` 应填写 `[x-left,y-top]`；不能直接填 GCJ-02/BD-09，系统不猜测也不自动转换。来源请填写能追溯的用户声明/公开资料 URL 与地标名称，误差声明需覆盖像素选点、坐标来源和地标歧义；无真实依据时不要输出虚构锚点。

已有像素任务的 CLI 用法：

```sh
python3 src/ride_agent.py resume --task-dir outputs/my-task --local-only \
  --approximate-anchors-file /path/to/evidence-anchors.json
```

可用 `start --goal approximate --demo` 新建离线任务，再按现有流程提供颜色/裁剪；`resume` 上传近似锚点也会显式切换到近似目标。近似文件、严格控制文件、OSM/下载请求禁止同次组合。修改裁剪会使锚点失效，须重新提供对应点。回到严格模式需明确重新上传 `--controls-file`，仍执行原有独立误差门槛。

### 近似输出的边界

- 顶层报告、GeoJSON 集合和每个分段都标记 `approximate=true`、`verified=false`、`original_gps=false`、`navigable=false`；未排序分段，不推断行进顺序，不输出导航 GPX。
- **近似结果永不置位严格 `registered` 或 `registration_gate_passed`**，独立门槛始终为 false。严格模式仍要求至少3个不共线控制点、2个独立检查点且拟合/独立误差均不大于35米，近似输入不能进入严格配准。
- `uncertainty_m` 是证据提供方声明，不是经验证的轨迹误差上界。三点仿射残差接近0不证明定位正确；地标对应错误、投影/地图形变、凸包外推均可能使实际偏差远超声明。报告提供 `vertices_outside_anchor_hull`、`extrapolation_present` 与残差，**不伪造统计置信区间**。
- 近似候选不能进入道路匹配或地图下载；道路读取器也拒绝带近似标记的 GeoJSON。原有上传同意、地图同意、候选选择、当前裁剪有效性、语义复核要求不被放宽。
- 自动化测试使用明确标注的合成锚点；即使对真实图片测试提线，也不代表真实地理位置或真实轨迹精度已经验收。

## 离线 OSM 只读文件调试（非默认流程，禁止联网和模型上传）

`corridor` 与 `registration`/`roads` 严格配准目标隔离：无需先提线、控制点或像素配准即可查询本地道路。旧目标的配准独立误差门槛和视觉复核门禁不变。本模式**不下载地图、不生成控制点、不接收模型自由坐标**，也不证明截图真实位置或骑行路线。

```sh
# 离线起步，不上传；截图是真实用户输入，而非按文件名猜城市
python3 src/ride_agent.py start --demo --goal corridor --image /path/original.png --task-dir outputs/corridor-demo
# 已获授权的本地 WGS84 OSM XML（不是GCJ-02地图截图）
python3 src/ride_agent.py resume --task-dir outputs/corridor-demo --local-only --map-provider osm --osm-file /path/local.osm --road-names '真实道路名A' '真实道路名B'
# 查看 outputs/corridor-demo/corridor-inspection.json 中的way/node与交叉口
python3 src/ride_agent.py resume --task-dir outputs/corridor-demo --local-only --road-hypothesis-file /path/hypothesis.json
```

`hypothesis.json` 格式如下；**占位ID不可照抄，必须替换为当前本地OSM真实节点**。人工假设标记 `user`，模型假设标记 `model`；模型不能冒充用户，其必经节点必须来自当前地图 `inspect_roads` 返回。每段至少两个 `via`，相邻段边界共用同一节点；折返需显式重复节点/路段，禁止自由坐标、自动吸附或跨断裂补边。

```json
{"schema_version":1,"coordinate_system":"WGS84","source":{"kind":"user","evidence":"实际原图可读地名、道路顺序及节点选择依据；说明不确定部分"},"legs":[{"road":{"name":"真实道路名"},"via":[{"node_id":111},{"node_id":222}]}]}
```

历史实现说明（已停用，不可按此联网或上传OSM证据）：按既有配置设置 `RIDE_API_BASE`/`RIDE_API_KEY`/`RIDE_MODEL`，创建时不用 `--demo` 并明确 `--consent-upload`；继续上传本地OSM时同样须本次 `--consent-upload`。模型读取原图地名，调用 `inspect_roads` 查询证据，再提出 `build_corridor` 假设；地名不可读或证据不足应询问，不编造节点。**每次API调用会上传原图、派生预览、诊断与历史对话，其中包含查询到的本地OSM道路名、节点和坐标；不会上传完整OSM文件。** `--local-only` 不调用模型，不上传。已有API任务也可本地继续并导出候选，缺少用户假设则暂停。

历史网页流程已停用，当前不提供OSM入口。以下仅为旧产物格式说明：已有任务勾选“明确切换独立道路走廊目标”。上传本地OSM、按行输入精确道路名即可先查询；查看查询JSON后上传有依据的道路假设JSON继续。仅本地继续不需要API；需要模型自己读图时每次重新同意上传。不要同时上传控制点/近似锚点或勾选下载。网页产物含 `corridors/<服务端UUID>/road-corridor.geojson`、`road-corridor.json`、`road-corridor.png`。示意PNG是本地OSM底图，不是原图配准叠图；JSON/GeoJSON带地图与假设哈希、节点/边way_id、有序分段、未确认起终点与限制。所有结果 `verified=false`、`original_gps=false`，`status=candidate_needs_review`；任务 `needs_confirmation` 也不代表已验证。重提取/修正颜色裁剪/更换OSM会使旧候选失效，网页保留审计链接而不当作当前预览。

真实交付仍需：可读地名的原图、对应地区获授权且节点引用完整的本地WGS84 OSM XML、有依据的道路顺序/转折点/重复行进假设；若要严格截图定位或误差估计，还需独立真实控制点和检查点，不能拿本模式替代。当前回归为合成夹具及mock API动作链，不代表两张用户真实图已跑通或原始GPS已恢复。

### 行政区父子层级查询修复

`query_map` 支持保守解析中文省/市/区/县拼接名称，以及显式 `area_hierarchy`（2–3级且拼接等于原始名称）。一次QL先验证父对象唯一，再限定父范围与 `subarea` 成员查询子行政relation；不猜选全国同名区县、不扩大范围。此OSM层级联网实现已停用，当前一律返回 osm_network_disabled，不进行请求或回退。失败主结果直接展示原始名称、查询层级及安全候选ID；旧任务不会被自动重跑。归属数据不足会保守停止，不能解释为真实行政区不存在。详见 [层级查询、授权预算与未验证边界](docs/ADMIN_AREA_QUERY.md)。
