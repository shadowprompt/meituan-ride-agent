# 受控 OSM 中转：当前默认与验收边界

## 配置与操作

独立服务仓库 `osm-evidence-service`（契约版本 b665ddd）必须先准备好。当前部署由 `osm-evidence.service` 常驻监听服务器 `127.0.0.1:18766`（本次不修改部署配置）。Agent端由管理员私下设置：

- `OSM_SERVICE_URL`：`https://<受控服务域名>` 或 SSH隧道 `http://127.0.0.1:18766`；无路径、query、userinfo、fragment。HTTP仅loopback，IPv6 loopback亦可。
- `OSM_SERVICE_TOKEN`：独立随机32–256位可打印ASCII token；不能复用模型/高德Key，不放命令参数、网页、任务状态或日志。

远程建议由用户建立现有SSH连接的本地转发：`ssh -N -L 18766:127.0.0.1:18766 <ssh-user>@<ssh-host>`。本仓库不配置SSH、HTTPS反代、防火墙、服务常驻或公网入口。HTTPS证书按系统CA校验，没有证书pinning；HTTPS主机是管理员信任的目的地，不是模型提供的URL。

```sh
# 环境变量通过安全配置注入；不要在聊天中给出真实token
python3 src/ride_web.py
# 网页默认OSM中转；选择道路走廊目标，分别同意本次模型上传、地图查询
# 或 CLI API模式：
python3 src/ride_agent.py start --image /本地/获授权截图.png \
  --task-dir outputs/新任务 --goal corridor --map-provider osm_relay \
  --consent-upload --consent-map
# 旧任务显式切换（尤其旧amap不会默认切换）：
python3 src/ride_agent.py resume --task-dir outputs/旧任务 \
  --map-provider osm_relay --consent-upload --consent-map
```

CLI/Web在调用模型、上传截图或修改任务前做所选服务配置预检；`/api/config`只返回配置状态/能力，不返回值、不探测服务。未配置时安全停止。已有任务 `status`/网页刷新只读，不自动上传、切换或重放。纯本地继续不联网，离线算法不依赖中转。

模型得到严格schema，先提出图文有据且未验证的国家/市区与道路名；缺国家依据就问用户，不凭城市猜全国、不写死上海。普通用户不必先准备道路JSON。地图查询授权仅允许文字/对象ID发送中转；模型上传授权涵盖原图、派生预览、历史对话及查询的公开OSM节点证据。两者互不替代，每次调用重新确认。

## 固定契约与预算

只允许两个POST，无任意路由/URL/QL/bbox/重定向、环境代理、重试或本地OSM直连：

- `/v1/named-roads`：`{admin_hierarchy:[{name,admin_level},...],road_names:[...]}`，2–6层，从level2严格递增到>=6，最多5个精确路名；禁止引号/反斜杠/QL分隔符/控制字符。
- `/v1/way`：`{way_id:正整数}`，最多15位；模型必须说明具体对象ID依据。

一次Agent invocation `search_places`/`query_map`共用一次动作预算；无自动重试或供应商回退。客户端25秒墙钟读预算、4MB响应、请求8192B；最多100 ways / 5000节点与引用。服务独立执行鉴权、20秒上游预算、逐层唯一行政解析与请求/并发/频率限制。客户端不回显服务正文/异常/凭证；非200、302、429、压缩、重复JSON字段、畸形对象、超限一律拒绝。

只接收WGS84、verified=false、精确请求回显；核对固定来源URL及64位SHA256格式，保留原way/node ID、nodes连接与坐标，geometry长度/节点重复冲突/缺失拒绝，转换本地XML。`map-query.json`含公开来源response/request hash、fetched_at、source_timestamp与本地XML SHA256，不含中转地址或token。**上游原始响应hash是可信中转提供的声明；客户端不重新下载原始响应，不能独立验证其真实性。** fetched_at不能替代源数据时间。

inspect/build要求当前XML hash与保存证据完全一致；换provider/换证据失效旧inspection/corridor/artifacts，旧地图/对话保留audit。Web只读刷新亦校验hash，防止旧产物显示为当前。服务当前投影不包含 access/bicycle/vehicle 等通行限制标签；本地算法不能据此证明可骑行或可通行。所有道路是未验证候选，不是截图配准、历史轨迹、真实起终点、道路通行许可或导航。

© OpenStreetMap contributors，https://www.openstreetmap.org/copyright ，使用OSM遵守ODbL。

## 验证层级与历史测试迁移

`PYTHONPATH=src:test python3 -m unittest test_osm_relay -v`：受控URL/token配置、严格结构/非法输入、鉴权、一次预算、无配置、来源/坐标/ID/hash失效、无直连/回退等。

当相邻独立服务checkout存在时，导入其真实HTTP服务，在随机loopback端口启动，注入mock上游：真实HTTP named-roads→逐层行政→原ID XML→Agent inspect→build→deliver，另验way/full与401。无真实OSM请求、模型调用、截图上传或真实Key。独立checkout不存在时明确skip集成，不把mock传输等同真实服务验证。

旧高德回归不删除：高德成功链改为显式选择amap；默认断言改为osm_relay；直连OSM退休、旧任务拒绝与离线XML算法测试保留。通用像素/网页fixture使用纯合成中转配置，只预检不联网。POI未暂存功能整合保留且许可门禁不放开。

本机通过不是远端上线。未配置公网TLS/域名/SSH入口、未实测外网OSM可达性/真实国家市区名称覆盖率、未跑真实模型原图识路；两张真实原图的WGS84走廊仍未证明。需要管理员远程部署参数和用户明确授权后另行验收。

## 本轮本地收尾结果

- 专项（relay、网页提示、高德兼容与地标许可门禁）：59项通过。
- 全量 `PYTHONPATH=src:test python3 -m unittest discover -s test -v`：395项，385通过、10项既有本地原图/离线夹具条件跳过，0失败；独立服务真实loopback HTTP的3项集成均实际执行通过。
- 前端专项18项通过；主网页与人工像素网页各1段内联JS通过 `node --check`。没有真实浏览器交互验收。
- 修复双供应商提示契约、Connection: close响应的剩余读预算，以及只读刷新时失效地图/摘要展示；未读取部署token、未改服务配置、未作真实上游/模型调用或截图上传。
