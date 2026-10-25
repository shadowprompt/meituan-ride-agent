# 默认且唯一联网地图服务：高德定位线索

主界面默认且仅提供高德地图服务，分别勾选**本次模型上传授权**与**本次地图查询授权**，再开始识别或继续查询。普通用户不需要填写道路 JSON、道路名或 OSM。模型从原图读取有依据的行政区和道路/地点文字；文字或地域不清应提问，不猜城市。

## 实际交付边界

- **OSM / Overpass**：所有联网入口已停用（`osm_network_disabled`），包括旧 `fetch_map` 和精确道路对象查询。只保留CLI `--local-only --map-provider osm` 只读本地文件调试；不调用模型，不作失败回退。
- **高德 / amap**：本次实现的是可执行的 **Web服务地理编码适配**（GET `https://restapi.amap.com/v3/geocode/geo`）。以行政区文字 + 道路/地点文字构成结构化地址。返回的只是 **GCJ-02 位置线索**，不是完整道路几何、道路连通节点或 WGS84 轨迹；**不支持 corridor 构建**，不声称完全替代 OSM。
- 地理编码可能多解、无结果、地域不符或返回粗略中心点；即使仅一个点也未验证道路精确匹配/行政区归属。不会把候选点连成骑行轨迹，不进行坐标转换或静默吸附。
- 不调用骑行/驾车/步行规划，不把规划路线当作历史路线；高德授权不允许访问 OSM，失败也不回退到 OSM。不会自动重试或扩大地域/权限。
- 切换 provider 时失效旧地图查询、道路检查、走廊、匹配状态及旧对话中的地图证据（旧对话另存本地审计，不作为当前证据；用户锁定的提取约束仍保留）；原 OSM 文件改名保留在任务目录作本地审计，不会在切回 OSM 时自动恢复使用。须重新授权查询或重新上传获授权地图。

## 旧任务迁移（不自动重放）

只读恢复/刷新保留旧产物，不联网。旧provider=osm或无provider字段的任务，普通CLI/API继续拒绝并提示显式迁移。用户选择amap并同时给予本次 `consent_map` 与 `consent_upload`（CLI两项对应旗标）后才允许迁移；任一缺失不修改任务、不启动模型、不外呼。网页唯一高德选项配合双授权明确迁移。迁移使旧道路证据失效、地图与旧对话留审计文件；不会删除原产物。旧fetch_map即使有授权仍拒绝，不能将范围自动改成高德上传请求。

## 管理员配置（勿向网页或群聊提供 Key）

**任何已在群聊、截图或公开日志出现的 Key 应立即撤销并轮换。不要使用泄露的 Key 做测试。**

申请高德 **Web服务**类型 Key，并通过服务器进程的私密环境配置 `AMAP_API_KEY`。示例仅使用占位符：

```sh
# 仅表示环境变量名称；部署时通过秘密管理或私密进程环境注入，不在共享终端记录真实值。
export AMAP_API_KEY='<新的高德Web服务Key占位符>'
# 既有模型环境仍单独配置，不要复用高德 Key。
PYTHONPATH=src python3 src/ride_web.py --port 8765
```

网页不提供 Key 输入框；HTTP 创建/继续接口拒绝 `key`、`amap_key`、`AMAP_API_KEY` 等未知参数。Key 只在服务端适配器读取，不放入 CLI 参数、任务状态、模型上下文或诊断。按高德官方 GET 协议，凭证通过 TLS 请求查询参数发往固定高德端点；应用不打印/保存该请求 URL，也不回显 HTTP 异常或远端正文。缺 Key 时返回 `amap_key_missing` 明确提示，**不会发高德请求**，无需盲目重新授权。网页 `/api/config` 仅返回配置存在/格式检查状态（不返回 Key，不发测试请求）；创建及非本地继续均在调用模型、保存输入或修改任务之前检查高德配置，缺失即拒绝，避免为不可执行的高德查询浪费模型费。CLI 同样在模型启动之前预检。配置存在不等于权限、配额或网络已验证。

CLI 同样支持显式选择，真实模型模式仍须每次上传授权：

```sh
PYTHONPATH=src python3 src/ride_agent.py start --image examples/input.png \
  --task-dir outputs/amap-demo --goal corridor --map-provider amap \
  --consent-upload --consent-map
PYTHONPATH=src python3 src/ride_agent.py resume --task-dir outputs/amap-demo \
  --map-provider amap --consent-upload --consent-map
```

以上图片路径仅为使用形式示例，请替换成本地获授权的截图。高德模式下 `--fetch-map` 或 OSM 上传被拒绝，不允许切换至OSM联网；本地文件调试仅限CLI --local-only --map-provider osm；provider 由用户明确选择，模型动作无 provider/key/URL 参数。

## 限额、结果与诊断

- 每轮最多一次 `query_map`；接受一个 1–80 字行政区及 1–3 个不重复道路/地点名，有界可见依据；仅发送这些文字，不发送截图。
- 高德最多 3 次地理编码（每名称一次），共同 30 秒总预算，每响应最多 512,000 字节、最多 10 个候选，无翻页、无搜索扩展、无自动重试。
- 固定 HTTPS 主机与路径；不使用环境代理，不跟随重定向；校验状态、响应大小及有限经纬度。响应任意文本不进入产物，仅保留请求名称、坐标数值和固定标记。
- 页面主结果直接显示本次行政区、名称、候选点及 `GCJ-02 / 未验证`，不要求用户翻 JSON。失败显示安全原因码/建议；含 `amap_key_missing`、`amap_credentials_rejected`、`amap_quota`、`amap_service_error`、超时、HTTP 429、响应超限等。
- 主结果下方提供**纯本地点预览**：无底图、不联网、不连线，所有候选逐项列出，重复/重叠点保留；编号仅为列表索引，不表示经过顺序。经纬度分轴缩放只是相对位置示意，不能据此测距。切换服务并执行后旧点失效，恢复/刷新只读，不重查或自动上传。
- `amap-location-clues.json` 保存未验证定位线索；`map-query-diagnostic.json` 保存本次有界请求/可见依据/安全摘要。无地图也有安全诊断；不保存凭证、远端响应正文或带凭证 URL。导出不是 WGS84 GeoJSON 路线。
- 查询结果会在用户明确模型上传授权下进入当前任务模型上下文；没有为此额外上传截图或自动继续模型调用。高德查询结束即停在定位线索状态，不继续构建走廊。

## 官方文档核对

本实现核对以下高德官方 Web 服务文档：

- [地理/逆地理编码](https://lbs.amap.com/api/webservice/guide/api/georegeo)：地理编码返回经纬度位置与匹配级别；逆编码的道路/路口返回位置，并不等同于完整可遍历道路几何。
- [POI 搜索 2.0](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch)：关键词接口 `https://restapi.amap.com/v5/place/text` 返回 POI 位置，支持 region/city_limit，但不是道路 way/node 连通网络。**本轮不调用 POI 接口**，不叠加回退查询预算。
- [高德官方坐标系说明](https://lbs.amap.com/api/javascript-api/guide/transform/convertfrom)：明确高德坐标属于 GCJ-02，区别于 WGS84。
- [坐标转换](https://lbs.amap.com/api/webservice/guide/api/convert)：区分 GPS 与高德坐标，不能把高德输出直接贴 WGS84 标签；本轮不调用转换接口。

自动化回归只用合成图、回环 HTTP 和 mock 外部模型/高德传输；覆盖创建→独立授权→继续→恢复→结果下载、服务切换失效、缺 Key 调用前阻断、页面 JS 语法及点预览执行：

```sh
PYTHONPATH=src:test python3 -m unittest test_amap_provider -v
PYTHONPATH=src python3 -m unittest discover -s test -v
```

### 独立连通性实测（与应用回归分开）

管理员已独立使用轮换后的服务器凭证，对公开地点“上海长风公园”执行一次官方地理编码请求：`status=1`、`infocode=10000`，约 0.68 秒，返回 GCJ-02 坐标。本轮开发不重复该请求，不保存凭证、不硬编码坐标。该结果只证明当时该凭证的独立地理编码调用成功，**不等于应用网页 + 真实模型 + 用户截图端到端已实测，也不证明道路几何/轨迹能力**；未使用真实模型或上传私有截图。

## 用户最短测试步骤

1. 管理员私下为启动网页进程配置 `AMAP_API_KEY` 和既有模型配置；若进程早已启动且未继承新环境，需按原部署方式重新启动网页进程（不是重启 OpenClaw）。打开 `http://127.0.0.1:8765`。
2. 在主入口“地图服务”选择 **高德（仅 GCJ-02 定位线索）**，检查下方配置预检提示。缺 Key 时不要付费尝试模型，先请管理员配置。
3. 选择获授权截图，分别勾选本次模型上传和地图文字查询授权，点击“开始识别”；无需填写路名 JSON。后台自动刷新只是读状态，不会重复上传。
4. 若截图行政区/文字不清，按具体问题补充信息；若先前未授权地图查询，再勾两项授权并点击“授权后继续识路 / 查询地图”。无 Key/查询失败/无结果不会自动重试。
5. 成功时查看 GCJ-02 独立点示意和坐标列表，下载 `amap-location-clues.json`、安全查询诊断；复制“恢复已有任务”中的任务编号可只读刷新。**当前只能测读图地名与地理编码定位线索，不交付完整骑行轨迹/道路连通网络/WGS84 路线或导航。**
