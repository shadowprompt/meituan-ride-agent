# 本地试用交付说明

> **输出图片怎么看：**见 [输出图片说明与人工核对指南](IMAGE_OUTPUTS.md)，包含候选着色、掩码、中心线、端点/分叉标记的含义、检查目的与验证边界。

## 无需 API：人工有序像素闭环（当前第一阶段）

启动网页后打开 `/pixel-route`，直接上传本地图片，按经过顺序点击折线，预览方向，保存、刷新恢复或按任务编号重开，并导出未验证有序像素 JSON。允许重复经过同一坐标/线段，不推断 GPS、不生成可导航 GPX。完整操作、边界及双截图/真实浏览器证据见 [PIXEL_ROUTE_ACCEPTANCE.md](PIXEL_ROUTE_ACCEPTANCE.md)。这是可用的人工回退闭环，不是全自动路线重建；以下模型/配准说明仍针对原有自动入口。

## 交付口径

这是**待真实模型验收的本地试用版**，不是已完成的自动路线重建产品。

- 已实现：本地网页与 CLI 的截图任务、持久问答、颜色候选、提线、中心线、人工配准、受控路网下载、道路候选叠图及修正重算。
- 离线 demo 为固定流程，不具备看图智能。可验证操作闭环，不能证明识别准确率。
- 调用真实视觉模型需要支持图像的 OpenAI 兼容模型及用户本次明确上传同意；已有证据可仅本地继续配准，不调用模型。
- 仍需人工提供地标控制点及独立检查点；不自动定位、不确定骑行顺序、不提供已验证导航或真实 GPS。
- 未完成真实模型与真实截图盲测前，发布状态保持“待验收”。

## 安装与预检

```powershell
git pull --ff-only
python -m pip install -r requirements.txt
python src/ride_doctor.py
$env:PYTHONPATH = "src"
python -m unittest discover -s test
python src/ride_web.py
```

在浏览器访问终端显示的本地地址。网页仅监听回环地址，不能直接部署到公网；不包含账户系统、TLS、多用户隔离、生产级审计或持久任务队列。关闭进程后任务文件保留，可重启后按任务 ID 恢复。不要把端口转发给其他人。

## 先验证离线交互

1. `python examples/make_synthetic_screenshot.py` 生成合成图。
2. 网页选择离线 demo、像素目标，上传 `examples/synthetic-ride.png`。
3. 在问答框提交 `{"crop":[0,0,640,480],"color":"#00BFA5"}`。
4. 应进入待确认状态，并看到提线叠图和中心线预览。
5. 修改颜色或裁剪后重新执行，核对产物改变及旧证据失效。

道路演示还需提供 `examples/synthetic-controls.json`、本地合成路网（先运行 `python examples/make_synthetic_roads.py`）。控制点必须在提线成功后提供，且局部像素坐标对应当前裁剪框。合成样本有预先已知答案，不可作盲测。

## 配置真实视觉模型

配置在启动服务器的终端环境中，不把密钥粘贴到网页、聊天、文档或版本库：

```powershell
$env:RIDE_API_BASE = "https://your-provider.example/v1"
$env:RIDE_API_KEY = "替换为本机密钥"
$env:RIDE_MODEL = "供应商支持图像的模型名"
python src/ride_doctor.py
python src/ride_web.py
```

预检只验证配置存在和基本格式，**不发送网络请求，不代表供应商兼容性已通过**。真实任务每次调用模型都要同意截图、当前叠图、诊断及对话上传；`--local-only` 或网页“仅本地继续”不调用模型、无需该同意。地图下载另需同意将配准地理范围发给固定 Overpass 服务；两种同意不能互相替代。

## 本轮已验证

- 全量 127 项测试通过，包括本机实际 HTTP 请求与真实 CLI 子进程，不只是模拟函数。
- 合成截图经网页创建、问答、提线、控制点及本地 OSM 上传、道路匹配、叠图读取闭环通过。
- 改裁剪后旧道路图逐项标记失效，不作为当前图片预览；文件仅保留审计链接。
- Host/Origin/CSRF、请求限额、路径与符号链接、上传同意、超时、并发串行测试通过。
- 尚未执行浏览器自动化交互验收、真实视觉模型或公网地图服务测试。

## 发布验收门槛（当前未勾选项不可冒充完成）

- [ ] 经用户授权，真实视觉供应商完成首次截图请求、动作解析及产物反馈。
- [ ] 一张未向模型提前提供颜色答案的真实截图，能提问或选择参数并输出可检查产物。
- [ ] 人工修正后能继续，不重复要求已经有效的信息；失败不宣称成功。
- [ ] 一张有真实控制点、独立检查点及许可路网的样本，完成道路候选闭环。
- [ ] 对缺段、平行道路、复杂折返和模糊截图保留不确定性；人工记录通过/失败原因。
- [ ] 将至少 10 张经授权真实截图与人工标注组成回归集，报告逐样本结果，而不是只报单元测试数。

验收须记录代码提交、模型标识（不记密钥）、截图授权、操作过程、输出和人工结论。原始截图、控制点、地图和任务日志默认放 `outputs/` 或 `local-data/`，不要提交到 Git。

## 故障与恢复

- 缺依赖：运行 requirements 安装与 doctor。
- 真实请求未配置／鉴权失败：在本机终端确认环境变量，不公开密钥；错误不等于已识别。
- error / paused：查看问题与诊断，补充回答后继续；网络调用不自动重试，避免重复费用。
- 控制点失效：改裁剪框后重新提供对应局部坐标控制点；不要绕过误差门槛。
- 地图下载限额／超时：缩小任务范围或上传本地 OSM；不要无限重试公共服务。
- 敏感任务文件：`outputs/web/` 含截图、位置及对话。Git 忽略不代表加密；交付结束后由用户按本机数据政策清理。

## 源码目录调整

业务源码现位于 `src/`，启动命令改为 `python src/ride_web.py --port 8765`。在项目根目录运行，已有 `outputs/web` 任务路径不变。Web 单次执行默认限时与模型 API 单次连接/读取限时均为 300 秒（5 分钟），不是完整多轮任务可无限等待。测试使用 `PYTHONPATH=src python -m unittest discover -s test -v`。

## 无需模型：已有像素任务的本地经纬度交付

适用于已成功提取的任务，包括中心线生成后模型动作或语义复核报错的 API 任务。不要为了导出经纬度反复修改像素小瑕疵：保留当前有效掩码/中心线，提供用户真实地标，走确定性本地配准。

```sh
python src/ride_agent.py status --task-dir outputs/实际任务目录
python src/ride_agent.py resume --task-dir outputs/实际任务目录 --local-only --controls-file local-data/真实控制点.json
```

网页等价步骤：打开已有任务 → 确认当前裁剪、候选与中心线有效 → 在“控制点 JSON”选择文件 → 勾选“仅本地继续”（`local_only`）→ 继续。无需勾选截图上传或地图下载。任务编号必须来自本机现存任务；不能用远端/其他电脑的编号替代本地任务目录。

- `--controls-file` 将像素目标提升到 `registration`；已有 `roads` 目标保持不变。
- 当前中心线和有效控制点齐备时直接 `register`，不调用模型；仅缺中心线时先本地生成。存在未决候选选择/补孔提议时须先由用户处理，不暗中选择或改像素。
- 配准目标交付 `registration/registration.json`（误差与门槛报告）和 `registration/registered-segments.geojson`（WGS84 LineString 集合）；CLI/网页交付清单会包含两项。`verified=false`、`original_gps=false`、`traversal_order_known=false`，不代表有序骑行路线，更不是导航 GPX。
- 有效拟合但独立误差门槛未通过时，仍可交付未验证经纬度候选；错误坐标系、退化/共线控制点、非法数值、空图等安全校验仍会拒绝。道路目标继续受独立误差门槛限制，不会因为候选文件存在就匹配或自动下载。下载必须显式 `--fetch-map --consent-map`，本地 OSM 用 `--osm-file`；不是 `--local-only` 自带的行为。

### 必须提供的真实输入

1. 本机实际任务目录和当前有效 `centerline/pixel-graph.json`，其 `extraction_config.crop` 对应使用的原图。若任务在别处，应先取得它的本地产物与状态，而非重建虚假任务或猜坐标。
2. 至少 **3 个不共线、覆盖路线范围的真实控制点**（像素位置 ↔ WGS84 `[经度,纬度]`）。只导出候选可无检查点；要通过默认门槛，另需至少 **2 个独立检查点**，像素位置不得复用拟合点，全部拟合/检查误差均需 ≤35 米。
3. 用户确认坐标来源和地标对应关系。控制 JSON 必须含 `coordinate_system: "WGS84"`、`pixel_frame: "local_mask"`、`control_points: [{"pixel_xy":[x,y],"wgs84":[lon,lat]}, ...]`，可选 `check_points` 同结构。`examples/synthetic-controls.json` **仅用于合成测试**，绝不可套在真实截图上。

`pixel_xy` 为当前裁剪左上角原点的局部像素：全图 `[x,y]` 应减去 `[left,top]`；确保 EXIF 朝向、缩放和裁剪版本一致。改裁剪后旧控制点失效，须重新核实并提交。颜色修正不改变坐标系，但会重算像素结果。

中国地图截图可能使用 GCJ-02/BD-09；**不能把这类坐标仅改标签当作 WGS84**。系统不自动检测/转换底图坐标系，也无法核实人工地标真值。仿射拟合不能补救错误对应、非线性地图形变或远离控制区的外推；三点拟合误差接近零不等于真实定位准确。

回归测试使用合成图和显式合成地标，经真实 CLI/网页上传流程验证本地交付和零模型调用，不构成真实用户轨迹定位成功。未获得目标任务的本地产物和真实控制点前，不能声称已得到该任务的经纬度。

## 独立近似定位交付

新增 `--approximate-anchors-file` 与网页“近似证据锚点（仅本地）”选择，按 README 的 JSON 格式提供至少三个非共线 WGS84/local_mask 对应点、逐点来源及正数不确定度、整体不确定度说明。无需独立检查点即可输出**明确不可导航的近似候选**，不是放宽严格配准。来源来自用户或公开证据，不由模型编造，服务器不会自动访问来源 URL。

产物独立放在 `approximation/`：`approximation.json`、`approximate-segments.geojson`。经纬度按未排序分段保留，自包含锚点、来源、残差、声明不确定度、凸包外推统计。`approximate=true / verified=false / original_gps=false / navigable=false`，严格准入始终 false；不接受近似候选道路匹配或地图下载。严格模式的3控制点+2独立检查点/35米不变。恢复严格流程需重新上传严格控制点，近似历史产物不再标为当前。

真实交付还需要：原始图片和当前裁剪、颜色/分量确认、可溯源地标与WGS84经纬度、对应 local_mask 像素、每点不确定度和歧义说明。没有这些输入不会凭图名造坐标。近似报告的误差声明不是实际轨迹精度保证，尤其凸包外推可能出现无已知上界偏差。示例与测试中的坐标均为合成，不代表真实任务已定位。

验收命令：`PYTHONPATH=src:test python3 -m unittest test_approximate_location -v`，包含真实 loopback HTTP→CLI 子进程上传、近似模式切换、产物下载与元数据校验，无外部模型/地图调用。全量回归：`PYTHONPATH=src python3 -m unittest discover -s test -v`。

## 道路走廊业务接入（corridor）

已把已有 `road_corridor.py` 接入 Agent JSON 动作白名单、受限执行、CLI续跑及网页本地OSM/道路假设上传，不再只是独立工具。显式 `--goal corridor` 或续跑 `--road-corridor`；`--road-names` 查询，`--road-hypothesis-file` 构建。道路/节点仅来自用户授权本地OSM；模型只能提出带来源依据的假设，其节点引用必须先经当前地图查询。无证据暂停；不下载范围路网，不输出模型自由坐标，不绕过旧严格配准目标门禁。

产物采用UUID隔离，含候选GeoJSON、可审计报告和本地OSM示意图。状态始终未确认；更换地图或重提取失效旧证据，网页明确标记旧文件非当前。使用步骤、上传范围和真实输入缺口见README“独立道路走廊”。

验收范围：`test_corridor_integration.py` 覆盖真实CLI与Web→真实CLI离线查询/构建、mock API实际请求载荷两轮inspect→build、缺地图/缺地名、模型冒充用户/无查询节点/任意路径拒绝、旧配准目标隔离、换地图与重提取失效。测试所有坐标与截图均为明确合成夹具，**没有宣称用户原图到真实轨迹已经通过**。真实数据研究独立进行；本轮业务交付不以历史已知道路演示冒充盲测。

本轮业务接入回归结果：`PYTHONPATH=src python3 -m unittest discover -s test` **312项，全部通过，10项按既有环境条件跳过**；`PYTHONPATH=src:test python3 -m unittest test_corridor_integration test_road_corridor -v` 27项通过。另以 `node --check` 校验网页内联JavaScript语法，`git diff --check` 无错误。测试不调用真实外部模型/地图服务，不能替代真实截图与本地地图证据验收。
