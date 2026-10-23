# 截图轨迹 Agent CLI（最小可运行版本）

`ride_agent.py` 的目标是把人工补充信息、颜色提取和中心线处理串成可恢复的对话流程。本文说明已实现接口；**不代表真实模型联调已通过，也不代表整个路线重建目标已完成**。

## 安装与命令

在项目目录使用 Python 3.10+：

```bash
python -m pip install -r requirements.txt
python ride_agent.py start --image local-data/ride.jpg --task-dir outputs/task1 --demo
python ride_agent.py resume --task-dir outputs/task1 --answer '{"crop":[0,0,640,480],"color":"#00BFA5"}'
python ride_agent.py status --task-dir outputs/task1
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
python ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/demo-task --demo
python ride_agent.py resume --task-dir outputs/demo-task --answer '{"crop":[0,0,640,480],"color":"#00BFA5"}'
python ride_agent.py status --task-dir outputs/demo-task
```

固定流程先询问裁剪和颜色，再依次调用 `extract`、`centerline`，交付颜色掩码、提取叠图、骨架、图结构叠图及像素图 JSON，结果保持 `needs_confirmation`（待人工确认）。`crop` 为 `[left,top,right,bottom]`，右、下边界不包含；`color` 为 `#RRGGBB`。

**demo 是硬编码的离线工作流，不会看图理解路线，也不自动选择地图范围或轨迹颜色**。合成图成功仅说明编排链路可运行，不能推断真实截图识别质量。工具细节见 `EXTRACTION.md`、`CENTERLINE.md`。

## 真实模型模式与上传同意

配置环境变量（POSIX shell 示例）：

```bash
export RIDE_API_BASE='https://your-provider.example/v1'
export RIDE_API_KEY='替换为自己的密钥'
export RIDE_MODEL='替换为供应商模型名'
python ride_agent.py start --image local-data/ride.jpg --task-dir outputs/real-task --consent-upload
python ride_agent.py resume --task-dir outputs/real-task --answer '{"crop":[0,0,640,480],"color":"#00BFA5"}' --consent-upload
```

- `RIDE_API_BASE`：OpenAI 兼容 API 基址，必须使用 HTTPS；仅 localhost 本地服务允许 HTTP。
- `RIDE_API_KEY`：API 密钥；不要写进文档、截图或版本库。
- `RIDE_MODEL`：供应商支持的模型标识；截图输入需要供应商及模型支持图像。
- 请求使用基址下的 `/chat/completions`，以结构化 JSON 表达动作，在“模型动作 → 校验 → 本地工具 → 结果反馈”循环中推进；**不使用原生 tool/function-calling API**。

**真实模式每次 `start` 或 `resume` 都必须显式提供 `--consent-upload`，且在任何 API 调用之前检查**；上一次同意不替代本次同意。该参数授权将截图和任务对话发送给配置的供应商，可能产生费用并暴露图片、地名和回答等敏感内容。确认供应商、数据处理政策和截图内容后再使用；离线 demo 无需该参数，不调用模型 API。

未提供真实凭据时，真实模型链路尚未测试，不能声称兼容某家供应商或已经验证真实视觉效果。

## 工具边界、任务状态与限制

- 模型工具白名单为 `extract`、`centerline`、`register`、`fetch_map`、`match`；模型不能执行 shell、任意程序或指定任意文件路径。
- 输出路径由程序固定在当前任务目录内；模型只提供经校验的处理参数，不能决定输入／输出文件位置。
- 任务目录保存 `task.json` 和工具产物；`task.json` 包含用户回答与模型消息，可能含敏感数据。`outputs/` 为 gitignored 本地产物目录，不应提交；使用其他任务路径时需自行确认忽略规则。Git 忽略不等于加密或访问控制。
- 中心线／像素图仍需人工核验；不因流程运行完成而自动认定路线正确、方向正确或已获用户确认。
- 当前编排支持人工控制点配准，可接本地路网或经明确同意获取 OSM 并匹配，但尚无自动地理定位；不会自动把截图像素变成已确认 GPS 轨迹。项目中其他离线模块的存在不意味着本 CLI 已集成它们。

## Windows 推荐用法

用文件传递回答，避免 PowerShell 与 Python 的 JSON 引号差异：

```powershell
python examples/make_synthetic_screenshot.py
python ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/agent-demo --demo
python ride_agent.py resume --task-dir outputs/agent-demo --answer-file examples/demo-answer.json
python ride_agent.py status --task-dir outputs/agent-demo
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
python ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/registration-agent --demo --goal registration
python ride_agent.py resume --task-dir outputs/registration-agent --answer-file examples/demo-answer.json
python ride_agent.py resume --task-dir outputs/registration-agent --controls-file examples/synthetic-controls.json
```

第二步应暂停请求控制点，第三步自动配准并交付额外的 `registration/registration.json` 与 `registration/registered-segments.geojson`。控制点必须由用户通过文件提供；模型动作不能提交或捏造控制点。文件内容仍需人工确认，系统不能验证地标真实性。真实模式每一步仍需 `--consent-upload`；本地报告误差可进入模型上下文。

`--controls-file` 也可把已有像素任务推进到配准目标。它会保留有效的提取、中心线，只使配准失效。改变裁剪框后，旧控制点局部像素坐标可能不再适用，务必提供新文件。

本轮新增模拟 HTTP 请求测试（检查图像载荷和认证头），不发送真实网络请求，不代表供应商联调通过。全量 36 项测试。

## 道路候选阶段

新增 `start --goal roads` 与 `resume --osm-file`，可在人工配准后继续匹配本地道路，生成灰色路网/蓝色参考/红色候选示意图。完整演示见 [ROAD_MATCHING.md](ROAD_MATCHING.md)。
不是自动定位，控制点和地图区域仍需用户指定；配准独立误差门槛不通过时停止匹配并请求修正。每段独立匹配，不冒充确定了整条骑行顺序。


## 按配准范围获取地图（新增）

道路阶段不必再手工导出 OSM：已有配准且独立误差门槛通过后，可运行：

```powershell
python ride_agent.py resume --task-dir outputs/registration-agent --fetch-map --consent-map
```

真实模型模式还需要本次 `--consent-upload`。两种同意相互独立：
- `--consent-map`：允许本次将 **WGS84 地理范围** 发送到固定 Overpass 服务（`https://overpass-api.de/api/interpreter`），泄露位置范围但不发送截图、控制点文件或密钥。地理范围本身仍是敏感信息。
- `--consent-upload`：允许向配置的视觉供应商发送截图与对话，包括工具报告和位置范围。

范围完全取自本地配准分段，并四边增加固定 0.002 度余量；模型不能提供任意坐标、下载地址或文件路径。范围上限为单边 0.1 度且面积不超过 25 km²，响应上限 10 MB，转换后 XML 上限 30 MB；完整 way 可能延伸到范围外。大范围、超大响应、服务错误会停在可恢复的 error，不自动重试，也不会冒充匹配成功。可稍后再次明确同意，或提供 `--osm-file` 离线继续。已有地图不会被下载覆盖；更换控制点导致范围变化时请用新任务或显式提供新的本地地图。

这不是自动地理定位：仍需要用户地标控制点及独立检查点。下载到道路也不代表道路当前允许骑行；OSM 来源需按 OpenStreetMap/ODbL 要求署名（© OpenStreetMap contributors），不得当作导航验证。

网络单元测试使用模拟响应；不代表公网服务可达、真实模型联调或真实截图盲测已通过。
