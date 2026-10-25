# 地图服务切换：OSM 与高德定位线索

主界面直接选择地图服务，分别勾选**本次模型上传授权**与**本次地图查询授权**，再开始识别或继续查询。普通用户不需要填写道路 JSON、道路名或 OSM。模型从原图读取有依据的行政区和道路/地点文字；文字或地域不清应提问，不猜城市。

## 实际交付边界

- **OSM / Overpass**：原有精确行政区/道路查询及 OSM way/node 的 WGS84 道路走廊候选流程。
- **高德 / amap**：本次实现的是可执行的 **Web服务地理编码适配**（GET `https://restapi.amap.com/v3/geocode/geo`）。以行政区文字 + 道路/地点文字构成结构化地址。返回的只是 **GCJ-02 位置线索**，不是完整道路几何、道路连通节点或 WGS84 轨迹；**不支持 corridor 构建**，不声称完全替代 OSM。
- 地理编码可能多解、无结果、地域不符或返回粗略中心点；即使仅一个点也未验证道路精确匹配/行政区归属。不会把候选点连成骑行轨迹，不进行坐标转换或静默吸附。
- 不调用骑行/驾车/步行规划，不把规划路线当作历史路线；高德授权不允许访问 OSM，失败也不回退到 OSM。不会自动重试或扩大地域/权限。
- 切换 provider 时失效旧地图查询、道路检查、走廊、匹配状态及旧对话中的地图证据（旧对话另存本地审计，不作为当前证据；用户锁定的提取约束仍保留）；原 OSM 文件改名保留在任务目录作本地审计，不会在切回 OSM 时自动恢复使用。须重新授权查询或重新上传获授权地图。

## 管理员配置（勿向网页或群聊提供 Key）

**任何已在群聊、截图或公开日志出现的 Key 应立即撤销并轮换。不要使用泄露的 Key 做测试。**

申请高德 **Web服务**类型 Key，并通过服务器进程的私密环境配置 `AMAP_API_KEY`。示例仅使用占位符：

```sh
# 仅表示环境变量名称；部署时通过秘密管理或私密进程环境注入，不在共享终端记录真实值。
export AMAP_API_KEY='<新的高德Web服务Key占位符>'
# 既有模型环境仍单独配置，不要复用高德 Key。
PYTHONPATH=src python3 src/ride_web.py --port 8765
```

网页不提供 Key 输入框；HTTP 创建/继续接口拒绝 `key`、`amap_key`、`AMAP_API_KEY` 等未知参数。Key 只在服务端适配器读取，不放入 CLI 参数、任务状态、模型上下文或诊断。按高德官方 GET 协议，凭证通过 TLS 请求查询参数发往固定高德端点；应用不打印/保存该请求 URL，也不回显 HTTP 异常或远端正文。缺 Key 时返回 `amap_key_missing` 明确提示，**不会发高德请求**，无需盲目重新授权。

CLI 同样支持显式选择，真实模型模式仍须每次上传授权：

```sh
PYTHONPATH=src python3 src/ride_agent.py start --image examples/input.png \
  --task-dir outputs/amap-demo --goal corridor --map-provider amap \
  --consent-upload --consent-map
PYTHONPATH=src python3 src/ride_agent.py resume --task-dir outputs/amap-demo \
  --map-provider osm --consent-upload --consent-map
```

以上图片路径仅为使用形式示例，请替换成本地获授权的截图。高德模式下 `--fetch-map` 或 OSM 上传被拒绝，须显式切换 OSM；provider 由用户选择，模型动作无 provider/key/URL 参数。

## 限额、结果与诊断

- 每轮最多一次 `query_map`；接受一个 1–80 字行政区及 1–3 个不重复道路/地点名，有界可见依据；仅发送这些文字，不发送截图。
- 高德最多 3 次地理编码（每名称一次），共同 30 秒总预算，每响应最多 512,000 字节、最多 10 个候选，无翻页、无搜索扩展、无自动重试。
- 固定 HTTPS 主机与路径；不使用环境代理，不跟随重定向；校验状态、响应大小及有限经纬度。响应任意文本不进入产物，仅保留请求名称、坐标数值和固定标记。
- 页面主结果直接显示本次行政区、名称、候选点及 `GCJ-02 / 未验证`，不要求用户翻 JSON。失败显示安全原因码/建议；含 `amap_key_missing`、`amap_credentials_rejected`、`amap_quota`、`amap_service_error`、超时、HTTP 429、响应超限等。
- `amap-location-clues.json` 保存未验证定位线索；`map-query-diagnostic.json` 保存本次有界请求/可见依据/安全摘要。无地图也有安全诊断；不保存凭证、远端响应正文或带凭证 URL。导出不是 WGS84 GeoJSON 路线。
- 查询结果会在用户明确模型上传授权下进入当前任务模型上下文；没有为此额外上传截图或自动继续模型调用。高德查询结束即停在定位线索状态，不继续构建走廊。

## 官方文档核对

本实现核对以下高德官方 Web 服务文档（仅抓取文档，没有实际调用高德 API）：

- [地理/逆地理编码](https://lbs.amap.com/api/webservice/guide/api/georegeo)：地理编码返回经纬度位置与匹配级别；逆编码的道路/路口返回位置，并不等同于完整可遍历道路几何。
- [POI 搜索 2.0](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch)：关键词接口 `https://restapi.amap.com/v5/place/text` 返回 POI 位置，支持 region/city_limit，但不是道路 way/node 连通网络。**本轮不调用 POI 接口**，不叠加回退查询预算。
- [高德官方坐标系说明](https://lbs.amap.com/api/javascript-api/guide/transform/convertfrom)：明确高德坐标属于 GCJ-02，区别于 WGS84。
- [坐标转换](https://lbs.amap.com/api/webservice/guide/api/convert)：区分 GPS 与高德坐标，不能把高德输出直接贴 WGS84 标签；本轮不调用转换接口。

测试只用合成图和 mock 传输：

```sh
PYTHONPATH=src:test python3 -m unittest test_amap_provider -v
PYTHONPATH=src python3 -m unittest discover -s test -v
```

未用真实 Key 做连通性/权限/计费验证，未上传任何真实截图；部署是否具备调用配额需管理员私下核验。
