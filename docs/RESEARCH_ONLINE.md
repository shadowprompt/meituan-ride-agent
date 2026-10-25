# 普通网页实时自主调研（非真实原图验收）

网页仍使用 `python3 src/ride_web.py --port 18766`。普通页面与新任务默认
**自主调研 research（WGS84）**，保留默认 API / 道路走廊目标，
选择截图后分别勾选本次模型上传和本次地图查询授权，再创建任务。
不需要 manifest、way/node ID、道路 JSON、行政区层级或控制点。
创建/继续后授权取消；刷新只读，不重放联网。没有授权时工具报告具体缺口。
CLI 同样可 `--map-provider research`，无需 `--research-manifest`；带 manifest 时仍为原离线回放。

## 已实现工具链

- 模型看原图与 `observe_region` 1:1 局部图，提出含不确定性的文字假说。
- `search_map_evidence`：固定 `https://nominatim.openstreetmap.org/search`，
  一个 q、jsonv2、最多5个候选；仅交互单任务，不自动补全、不批量、重复文字查询拒绝再次请求。
  搜索结果只发现ID，不把搜索中心点当作道路节点。relation候选不支持对象核查，须报告此缺口。
- `inspect_map_object`：固定 `https://api.openstreetmap.org/api/0.6/way/{id}/full.json`
  或 `node/{id}/ways.json`。ID只能来自当前搜索结果、真实对象或实际返回的成员节点。
  每一父 way 必须确有请求节点，完整 way 不得夹带不相关对象；不编造坐标/节点。
  搜索 node 可以先查父 ways，再查具体 way/full 获取原始WGS84节点坐标。
- 更新公共证据时 SHA/version 绑定，旧假说、走廊和复核失效。模型记录有序道路假说，
  保留折返重复及 null 未知段，调用现有 `inspect_roads` / `build_corridor`。
- 交付前须成功向模型呈现当前局部原图，记录当前版本 `review_research`。
  needs_revision/ambiguous 不交付，未知段不得跳过拼接。所有结果 verified=false，
  非截图配准、非原始GPS、不可导航；WGS84对象不与高德GCJ-02混合。

## 网络、隐私、预算

地图授权独立于模型上传：只将文字 q 发给 Nominatim、具体返回ID发给官方OSM，
不向地图服务发送截图。公开名称、对象响应和来源会进入之后已授权的模型请求。
文字查询仍可能含截图位置线索，用户须知悉。无任意URL、代理、重定向、自动重试、
Overpass、失败回退或服务端配置变更。固定应用User-Agent含项目联系地址。
每次创建/继续至多3次搜索、8次公共请求，每次25秒/4MB；失败也消耗请求预算。25秒统一包含排队取锁、限频等待、连接、请求头/响应头与正文，慢速响应以deadline主动断开。
跨本应用网页CLI进程以文件锁串行，起始间隔≥1.1秒。它不是其他部署/其他应用的全网限流器；
多实例部署须管理员统一出口限流，不应用公共Nominatim做高流量/批量识别。
局部超时、网络/HTTP错误、响应校验失败或重定向拒绝，返回结构化 unavailable 工具结果，
包含具体查询/ID、安全原因码、已用预算、已成功对象数与发现ID；保留此前成功证据。
模型在剩余动作/请求预算内可核查已返回对象、observe_region 看图核对疑似文字，或提出具体歧义问题，
不因可选第三次搜索失败而否决前两次搜索。疑似错字不得在代码中纠正或硬编码为另一地名。
搜索候选仍仅是发现ID，不是道路几何或截图对应已验证；失败记录与当前可用证据分离。
429、缺少授权、无效请求、响应上限或总请求预算耗尽仍停止本轮；工具循环也有硬上限。
网页问题保留具体失败query/ID；执行 succeeded 只表示本轮执行与状态保存，不代表目标完成。
若候选随后完成，历史局部失败不覆盖当前未验证候选状态，审计仍保留。
仅成功响应的重复q在任务内拒绝；失败仅留审计，不污染成功缓存，用户下一轮显式授权后可以再次查询（不是自动重试）。
同一轮失败query及失败对象ID也在本轮预算中锁定，模型再次提交同词/同对象将本地拒绝并停止，不发第二次网络请求。不会为刷新缓存偷偷请求。

原始搜索JSON仅保存本地 `research/search-<sha>.json`；具体对象JSON保存本地，
manifest记录官方URL和响应hash。`research_requests`记录实际动作、预算索引、成功hash或安全原因码；
不保存凭证或远端错误正文。候选交付 `research-audit.json` 含原图hash、对象源、请求预算与局限。

## 验证边界

本次只运行Mock HTTPS、loopback网页HTTP、脚本模型动作、全量Python与页面JS回归。
没有实际向公网地图、付费模型请求或上传用户原图。真实原图E2E **未执行**，
不能宣称具体道路对应、完整行进顺序或真实定位成功。原图应使用
`local-data/turnback-original-rgb.png`，SHA256
`ac9eafdca8dff978c56c3f462fe20696260ee7d8e6f00d17399f2e456821e5b7`。
后续E2E需要用户分别授权该图/局部图与公开证据上传到已配置模型，
以及本轮公开Nominatim文字检索/OSM对象核查（上述预算），本次没有代用户执行。

历史原始证据只确证具体OSM对象/member响应；没有原始Nominatim搜索响应证明历史搜索成功。当前实时搜索为新实现，只有Mock验证，不能与历史成功混称。

## 局部失败修复的 Mock 验收

针对真实报告中“前两次搜索有候选、第三次搜索超时就整体停止”的代码分支，
本轮新增合成两成功一失败 → 对象 way/full 与 node/ways 核查 → inspect_roads →
build_corridor → 局部图观察 → 模拟版本复核 → 未验证候选交付的主链回归。
同时覆盖全搜索失败后看图提出具体歧义、同轮失败词禁止重试、429/缺授权立即停、
第八次请求失败硬停止并保留旧对象、动作预算耗尽含具体query。
原有 unknown/null 段不得省略拼接的回归保持不变。
这些仅证明 Mock 编排/状态/安全门禁，不代表真实报告用户截图已识路成功；
没有重放用户任务，没有实际地图或模型联网，没有上传原图。

本轮回归结果：`PYTHONPATH=src:test python3 -m unittest discover -s test -q`
共434项，424通过、10跳过；专项 research_online/map_research/web_simple_ui 共41项通过。
Node 执行普通页面完整脚本及新增局部失败摘要断言通过（不是只做语法检查）。
本地日志：`outputs/partial-research-regression/{full-tests-final,targeted-tests,page-js}.log`。

## 默认入口与故障分层回归

普通网页、`POST /api/tasks` 省略 provider/goal 和 CLI 新建默认 research/corridor；
旧任务恢复沿用已有provider，缺provider的旧档仍按旧osm处理，不静默迁移。
像素等高级目标仍可显式选择，不被research存档校验阻塞。中转缺配置只阻止显式中转任务，
不再阻止默认自主调研。模型 api_network 属于模型连接故障，改地图默认不能修复它；
不绕过模型/地图双授权，也不宣称已完成真实图验收。

新增Mock模型HTTP完整主链回归，实际经过APIPlanner解析、原图/局部图请求载荷与成功呈现标记，
搜索→way/full→node/ways→有序重复假说→inspect/build→局部图→review→deliver。
这只验证代码编排与几何来源，不证明模型自主推理质量；原图路线没有硬编码。

### 在线搜索 schema 与有限结构修正

在线 `search_map_evidence` 严格接受 `action`、`names`、`evidence`，以及可选
`region`。`names` 必须为恰好一个公共地名，`region` 只能来自图像/已有证据，
两者各 1–100 字；`evidence` 为必需的非空依据（最多2000字）。示意：

```json
{"action":"search_map_evidence","names":["候选道路"],"region":"有依据的地域","evidence":"可见文字及不确定性"}
```

`region` 不会被忽略或展开多次查询：实际唯一 `q` 为 `region + 空格 + names[0]`；
未传 region 时仅用名称。一次动作仍只消耗一次搜索/一次请求，保持每轮3次搜索、
8次公开请求上限。审计保留 names/region/实际 q，source URL 与响应原始 SHA256
绑定实际请求；搜索存档文件名同时绑定 URL 与正文 hash，避免相同正文覆盖不同查询。
成功查询缓存和本轮失败去重按规范化实际 q 比较，兼容旧 names-only 审计。
禁止 URL、控制/格式字符、查询参数语法及额外键；不能传 endpoint、limit 或其他参数。

缺少必需字段、字段类型/列表形状错误可反馈工具 schema，在原模型动作预算内至多
修正2回合；不追加模型调用预算、不自动修改模型答案、不发送被拒请求。
安全检查优先：未授权、未知对象ID、重复请求、预算耗尽、危险文本/额外参数仍终止，
不会因同时缺 evidence 而放宽。HTTP429等终止边界保持不变。

搜索只得到 discovery IDs，尚未 `inspect_map_object` 获取原始几何时归档对象数为0
是正常状态，不代表无道路或地图故障；网页明确区分两种证据。

本变更仅使用真实报告形状的 Mock HTTP 回归（成功搜索→含 region 且缺 evidence
的动作→schema修正→限定地域搜索→inspect对象→模型继续），没有重放原任务、
调用真实模型或公开地图服务，也没有上传用户图像。

### review_research 前置条件有限恢复（task16ee2db4 同形故障）

根因不是地图预算耗尽：对象归档更新会改变 `research_version`，失效旧假说、
走廊及局部图呈现；模型重建含 `null` 的假说后，未重新 `observe_region` 就
发出 `review_research ambiguous`。原代码把“当前局部图未呈现”直接抛为
`ValueError`，外层统一落入 `task_processing_failed`，把可恢复协议缺步当作整个任务失败。

现在保留原复核门禁，但该缺步返回 `LOCAL_TOOL_RESULT status=rejected`、
`reason_code=observe_region_required`、`missing_tools=[observe_region]` 和明确下一动作。
使用既有持久化 correction 预算（总上限2）及总模型上限28，不重置网络/阶段计数；
到限保存 checkpoint，公开固定前置条件代码及安全说明，不打印异常正文、密钥或路径。
同类白名单仅覆盖缺归档观察、过期版本、缺当前假说、缺局部观察、未知段/顺序不符、
缺本地道路节点观察。没有通用 ValueError 恢复、异常文本匹配或权限放宽；
hash/内部故障、无授权、危险参数和未知网络对象ID仍走原失败边界。

`ambiguous` / `needs_revision` 是正常未决结果：已完成当前图呈现时记录其明确版本、
证据、限制与本地下一步，checkpoint 同时保留该 review；未呈现时仅记录拒绝，不能
伪造复核。只有当前版本 `consistent` 且其他走廊/未知段门禁通过才可能交付。
旧任务从 messages 与累计请求迁移预算；继续错误任务时清除陈旧 error 字段，
新的实际错误仍重新记录固定类别/步骤。网页已有 progress 通道展示具体恢复原因。

新增测试 `test_research_review_recovery.py`：合成对象更新→含 null 假说→未重新看图
的 ambiguous→拒绝→observe→成功 Mock 模型图像传输→ambiguous→checkpoint；
本轮只增加1个预期 Mock 对象请求，网络累计6→7，模型累计12→18，不重试；
新/旧落盘错误任务继续、预算不重置、缺步白名单、未知异常不吞、needs_revision，
以及真实 loopback 网页HTTP→CLI→Agent 的新/旧任务继续均覆盖。
全部图像是合成白图/对象；没有读取或重放真实 task16ee2db4、真实模型/地图外呼，
没有原图上传，未进行真实路线识别验收。没有重启服务；8766 如需重启由父代理先查活动任务。

本补丁验证命令及结果：研究专项70项通过；网页/异步/提示/恢复专项25项通过，
含现有 Node 实际执行的前端逻辑 harness；全部源码内联 JS 3段 `node --check`
通过；修改模块 `py_compile` 与 `git diff --check` 通过。全量最终结果见同次提交
记录下方，不将 Mock 编排通过写作真实原图验收。
最终全量：`PYTHONPATH=src:test python3 -m unittest discover -s test -q`，
467项，457通过、10项既有环境条件跳过，0失败；不是新增跳过。
