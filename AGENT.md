# 截图轨迹 Agent CLI（最小可运行版本）

`ride_agent.py` 的目标是把人工补充信息、颜色提取和中心线处理串成可恢复的对话流程。本文说明已实现接口；**不代表真实模型联调已通过，也不代表整个路线重建目标已完成**。

## 安装与命令

在项目目录使用 Python 3.10+：

```bash
python -m pip install -r requirements.txt
python src/ride_agent.py start --image local-data/ride.jpg --task-dir outputs/task1 --demo
python src/ride_agent.py resume --task-dir outputs/task1 --answer '{"crop":[0,0,640,480],"color":"#00BFA5"}'
python src/ride_agent.py status --task-dir outputs/task1
```

- `start --image … --task-dir … [--demo] [--consent-upload]`：创建任务；`--demo` 选择离线固定流程。
- `resume --task-dir … --answer "…" [--consent-upload]`：载入已有任务并提交回答；模式沿用任务状态，不需要重复传 `--demo`。
- `status --task-dir …`：查看本地状态，不触发模型请求。
- 每个任务使用独立目录；示例裁剪只适用于对应尺寸的合成图，真实截图请人工确定范围。

上面 JSON 使用 POSIX shell 单引号写法；其他终端请确保 `--answer` 收到完整 JSON 字符串。

## 离线 demo：验证编排，不是视觉智能

生成合成截图并创建单独的演示任务：

```bash
python examples/make_synthetic_screenshot.py
python src/ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/demo-task --demo
python src/ride_agent.py resume --task-dir outputs/demo-task --answer '{"crop":[0,0,640,480],"color":"#00BFA5"}'
python src/ride_agent.py status --task-dir outputs/demo-task
```

固定流程先询问裁剪和颜色，再依次调用 `extract`、`centerline`，交付颜色掩码、提取叠图、骨架、图结构叠图及像素图 JSON，结果保持 `needs_confirmation`（待人工确认）。`crop` 为 `[left,top,right,bottom]`，右、下边界不包含；`color` 为 `#RRGGBB`。

**demo 是硬编码的离线工作流，不会看图理解路线，也不自动选择地图范围或轨迹颜色**。合成图成功仅说明编排链路可运行，不能推断真实截图识别质量。工具细节见 `EXTRACTION.md`、`CENTERLINE.md`。

## 真实模型模式与上传同意

配置环境变量（POSIX shell 示例）：

```bash
export RIDE_API_BASE='https://your-provider.example/v1'
export RIDE_API_KEY='替换为自己的密钥'
export RIDE_MODEL='替换为供应商模型名'
python src/ride_agent.py start --image local-data/ride.jpg --task-dir outputs/real-task --consent-upload
python src/ride_agent.py resume --task-dir outputs/real-task --answer '{"crop":[0,0,640,480],"color":"#00BFA5"}' --consent-upload
```

- `RIDE_API_BASE`：OpenAI 兼容 API 基址，必须使用 HTTPS；仅 localhost 本地服务允许 HTTP。
- `RIDE_API_KEY`：API 密钥；不要写进文档、截图或版本库。
- `RIDE_MODEL`：供应商支持的模型标识；截图输入需要供应商及模型支持图像。
- 请求使用基址下的 `/chat/completions`，以结构化 JSON 表达动作，在“模型动作 → 校验 → 本地工具 → 结果反馈”循环中推进；**不使用原生 tool/function-calling API**。

**真实模式每次 `start` 或 `resume` 都必须显式提供 `--consent-upload`，且在任何 API 调用之前检查**；上一次同意不替代本次同意。该参数授权将截图、当前有效的处理叠图、诊断和任务对话发送给配置的供应商，可能产生费用并暴露图片、地名和回答等敏感内容。确认供应商、数据处理政策和截图内容后再使用；离线 demo 无需该参数，不调用模型 API。

未提供真实凭据时，真实模型链路尚未测试，不能声称兼容某家供应商或已经验证真实视觉效果。

## 工具边界、任务状态与限制

- 模型工具白名单为 `inspect_colors`、`extract`、`centerline`、`register`、`fetch_map`、`match`；模型不能执行 shell、任意程序或指定任意文件路径。
- 输出路径由程序固定在当前任务目录内；模型只提供经校验的处理参数，不能决定输入／输出文件位置。
- 任务目录保存 `task.json` 和工具产物；`task.json` 包含用户回答与模型消息，可能含敏感数据。`outputs/` 为 gitignored 本地产物目录，不应提交；使用其他任务路径时需自行确认忽略规则。Git 忽略不等于加密或访问控制。
- 中心线／像素图仍需人工核验；不因流程运行完成而自动认定路线正确、方向正确或已获用户确认。
- 当前编排支持人工控制点配准，可接本地路网或经明确同意获取 OSM 并匹配，但尚无自动地理定位；不会自动把截图像素变成已确认 GPS 轨迹。项目中其他离线模块的存在不意味着本 CLI 已集成它们。

## Windows 推荐用法

用文件传递回答，避免 PowerShell 与 Python 的 JSON 引号差异：

```powershell
python examples/make_synthetic_screenshot.py
python src/ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/agent-demo --demo
python src/ride_agent.py resume --task-dir outputs/agent-demo --answer-file examples/demo-answer.json
python src/ride_agent.py status --task-dir outputs/agent-demo
```

真实模型配置示例（仅配置，不会自动调用）：

```powershell
$env:RIDE_API_BASE = "https://your-provider.example/v1"
$env:RIDE_API_KEY = "你的密钥"
$env:RIDE_MODEL = "支持图像的模型名称"
```

普通补充回答保留已有阶段；修改裁剪或颜色请加 `--reset-extraction`，显式使旧提取、中心线及配准状态失效。模型需输出严格 JSON；错误、超时或格式不兼容时任务保存为 error，不冒充成功。每轮最多六次动作，可用 resume 恢复。现阶段不自动重试远端调用，避免重复费用。

## Agent 内人工配准（新增）

无需手工串联 register_pixels.py：Agent 可先完成像素分析，再请求独立控制点文件。

```powershell
python src/ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/registration-agent --demo --goal registration
python src/ride_agent.py resume --task-dir outputs/registration-agent --answer-file examples/demo-answer.json
python src/ride_agent.py resume --task-dir outputs/registration-agent --controls-file examples/synthetic-controls.json
```

第二步应暂停请求控制点，第三步自动配准并交付额外的 `registration/registration.json` 与 `registration/registered-segments.geojson`。控制点必须由用户通过文件提供；模型动作不能提交或捏造控制点。文件内容仍需人工确认，系统不能验证地标真实性。调用真实模型的步骤仍需 `--consent-upload`；已有像素证据的本地配准可用 `--local-only`，无需模型或上传同意。详见 [本地经纬度交付步骤](DELIVERY.md#无需模型已有像素任务的本地经纬度交付)。本地误差报告可在以后授权的模型请求中进入上下文。

`--controls-file` 也可把已有像素任务推进到配准目标。它会保留有效的提取、中心线，只使配准失效。改变裁剪框后，旧控制点局部像素坐标可能不再适用，务必提供新文件。

本轮新增模拟 HTTP 请求测试（检查图像载荷和认证头），不发送真实网络请求，不代表供应商联调通过。全量 36 项测试。

## 道路候选阶段

新增 `start --goal roads` 与 `resume --osm-file`，可在人工配准后继续匹配本地道路，生成灰色路网/蓝色参考/红色候选示意图。完整演示见 [ROAD_MATCHING.md](ROAD_MATCHING.md)。
不是自动定位，控制点和地图区域仍需用户指定；配准独立误差门槛不通过时停止匹配并请求修正。每段独立匹配，不冒充确定了整条骑行顺序。


## 按配准范围获取地图（新增）

道路阶段不必再手工导出 OSM：已有配准且独立误差门槛通过后，可运行：

```powershell
python src/ride_agent.py resume --task-dir outputs/registration-agent --fetch-map --consent-map
```

真实模型模式还需要本次 `--consent-upload`。两种同意相互独立：
- `--consent-map`：允许本次将 **WGS84 地理范围** 发送到固定 Overpass 服务（`https://overpass-api.de/api/interpreter`），泄露位置范围但不发送截图、控制点文件或密钥。地理范围本身仍是敏感信息。
- `--consent-upload`：允许向配置的视觉供应商发送截图与对话，包括工具报告和位置范围。

范围完全取自本地配准分段，并四边增加固定 0.002 度余量；模型不能提供任意坐标、下载地址或文件路径。范围上限为单边 0.1 度且面积不超过 25 km²，响应上限 10 MB，转换后 XML 上限 30 MB；完整 way 可能延伸到范围外。大范围、超大响应、服务错误会停在可恢复的 error，不自动重试，也不会冒充匹配成功。可稍后再次明确同意，或提供 `--osm-file` 离线继续。已有地图不会被下载覆盖；更换控制点导致范围变化时请用新任务或显式提供新的本地地图。

这不是自动地理定位：仍需要用户地标控制点及独立检查点。下载到道路也不代表道路当前允许骑行；OSM 来源需按 OpenStreetMap/ODbL 要求署名（© OpenStreetMap contributors），不得当作导航验证。

网络单元测试使用模拟响应；不代表公网服务可达、真实模型联调或真实截图盲测已通过。


## 当前产物视觉反馈

真实模式的每次决策现在包含原截图，以及当前有效阶段的固定白名单预览：颜色候选图、提取叠图、中心线图、道路候选图（包括 no_match 的地图参照）。预览最长边为 1024 像素，不能直接当作原截图裁剪坐标；提取与中心线使用裁剪局部坐标，道路图是独立地图示意。

- 预览最多四张，每张源文件 ≤5 MB／400 万像素、编码后 ≤1 MB；组合请求 ≤32 MB。非法图片使任务报错，不继续调用 API。旧任务缺少可选预览时跳过。
- 修改参数或重跑阶段前先使相关证据失效；即使工具失败、旧文件仍存在，也不再上传为当前证据。模型不能指定附件路径，越出任务目录的符号链接会被拒绝。
- 在历史消息之后追加当前状态快照及结构化道路覆盖率、成功／缺失分段编号；旧成功报告不再被当作最新状态。历史内容本身仍保留并发送。
- `needs_confirmation` 可能仅有部分分段匹配；道路报告新增 `coverage_fraction`、`matched_segment_indices` 和 `missing_segment_indices`。覆盖率是分段数量比例，不是长度覆盖率或准确率。

**上传同意包含这些衍生产物。** 道路叠图和诊断可能暴露位置关系；地图下载同意不能代替模型上传同意。离线 demo 不看图、不调用模型；本轮模拟 API 测试只验证请求载荷及防护，不证明模型能看懂或主动修正结果。


## 本地颜色候选检查

真实模型现在可以先调用 `{"action":"inspect_colors"}`，或附加原截图坐标的 `crop`，获取最多五种饱和颜色候选及对比预览。固定工具读取当前任务截图，输出 `inspection/colors.json` 和 `inspection/color-preview.png`；不会联网，也不会自动提线或更改已有效的提线结果。

模型可对比原截图、候选分布和预览后选择 `extract` 参数；如果颜色有歧义，应追问确认。候选依据颜色像素统计，不是语义识别：地图绿地、彩色按钮、文字同样可能入选，排名不是路线概率。灰色／低饱和轨迹可能没有候选；不因此断言无轨迹。候选检测使用有界缩略图，细线可能漏检，坐标边界只作为粗略分布提示，不是自动地图裁剪结论。

检查结果保存到可恢复状态，真实模型的后续请求包含当前候选报告及预览。`--reset-extraction` 同时使旧颜色检查失效；单纯检查颜色不使有效提线失效。已有离线 demo 仍要求明确颜色和裁剪，不会悄悄选排名第一项。这一功能不替代人工配准、自动定位或真实模型验收。


## 结构化修正与坐标系保护

已完成过提线的任务，可直接修正颜色或裁剪，无需让模型重新猜参数：

```powershell
python src/ride_agent.py resume --task-dir outputs/task1 --color "#00BFA5"
python src/ride_agent.py resume --task-dir outputs/task1 --crop 20 80 620 420
```

真实模式仍需本次 `--consent-upload`。
- 改颜色：保留已知裁剪框及相同坐标系的控制点，重做提线及其下游步骤。
- 改裁剪框：旧控制点、颜色候选检查和下游结果失效。新提线完成后，需重新提供 `--controls-file`，确认控制点采用新裁剪局部坐标。不能静默平移旧控制点。
- 用户结构化指定的颜色／裁剪约束优先于模型提线参数，并持续保存；若想交回模型选择，使用已有 `--reset-extraction`（也会使控制点失效），再提供回答。
- 只有一个参数且旧任务缺少另一个参数时，Agent 仍需补齐，不能凭空推断。两个参数已知时直接执行本地提线，随后恢复正常编排。
- 颜色或裁剪无效时，在修改任务状态前拒绝。完整裁剪框的任意变化都按坐标系变化处理；即使尺寸相同、只移动位置，也必须重新确认控制点。

**旧任务兼容性变化：** 控制点现在必须记录与当前 `extraction_config.crop` 完全一致的 `controls_crop`。旧任务只有文件、缺少来源绑定时会请求重新提交控制点，不能因文件存在或旧误差检查通过就继续。第一次提供控制点前必须先成功提线；模型自行改变裁剪也受相同保护。

关键状态失效、坐标系绑定、颜色环绕处理和同意边界处已补充中文注释。注释解释约束原因，不把未验证的语义能力写成已实现。

### 模型发现封闭小孔后的用户批准修正

API 可在当前中心线生成后调用 `observe_holes`，读取稳定孔编号与三栏 1:1 局部证据（未叠加原图 / 精确新增像素 / 当前掩码）。只有这些图片真正进入成功的后续 API 请求，才接受 `propose_holes`；提议绑定当前 review ID、source/mask/centerline 哈希和当前提取配置版本，每孔必须说明 `arrow` 或 `gray_band_omission` 的具体图像理由。

模型**只能提出**，不能直接应用。网页直接展示提议孔、理由和新增像素；默认未勾选、掩码不变。用户填写理由，核对未叠加原图并明确确认后，才可批准整个固定提议并重建中心线；也可拒绝，原掩码不变且保留拒绝理由。同像素版本相同孔集合不能换理由/重新编号反复提议。待处理提议不能通过改变参数或离线继续绕过。

批准沿用人工逐孔审计目录、安全锁、CSRF 和路径白名单。修正撤销旧语义复核，下次 API 继续必须重新观察和复核当前版本。人工离线逐孔流程仍保留；仅允许面积不超过256、最长边不超过32的封闭背景孔，不能删除前景、连接开放断点或填写任意坐标。

这些约束不证明模型能分清箭头与真实回转孔。当前只完成本地 HTTP mock 编排与浏览器交互验收；未调用真实视觉服务，不代表真实折返识别已解决。所有产物仍为 `verified=false`、`original_gps=false`，不恢复通行顺序或 GPS，不可导航。
