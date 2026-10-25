# 普通网页实时自主调研（非真实原图验收）

网页仍使用 `python3 src/ride_web.py --port 18766`。在普通页面地图服务中选择
**自主调研：实时文字检索与具体对象核查（WGS84）**，保留默认 API / 道路走廊目标，
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
429、超时、拒绝或预算耗尽即停本轮，网页直接呈现具体查询/ID与原因，不自动重试。
仅成功响应的重复q在任务内拒绝；失败仅留审计，不污染成功缓存，用户下一轮显式授权后可以再次查询（不是自动重试）。不会为刷新缓存偷偷请求。

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
