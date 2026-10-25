# 原图 → 具体地图对象 → 经纬度候选（最小联网链路）

普通网页默认 `corridor`：上传原图，分别勾选**本次模型上传**与**本次地图查询**，点击开始。用户不需要填写道路名、控制点、OSM 或 hypothesis。未授权地图时模型应提出明确授权问题；可在普通界面的“授权后继续识路 / 查询地图”继续。刷新仅取状态，不重放网络请求；每次继续都重新授权。

流程：模型读取原图可见行政区、道路文字和行进证据 → `query_map(area_name,names,evidence)` → 固定 Overpass HTTPS 端点精确查询行政区（必须唯一）→ 行政区内精确 highway 名查询并取完整 way 节点 → 原子写任务 `map.osm` → 模型 `inspect_roads` 获取真实节点 → `build_corridor` 只能引用当前地图已查询节点 → 输出未确认 WGS84 GeoJSON/报告/预览。

CLI 示例（须先自行配置模型环境，不要提交密钥）：

```sh
python3 src/ride_agent.py start --image /path/to/original.png --task-dir outputs/my-task --goal corridor --consent-upload --consent-map
# 恢复：两种同意仅对这次运行有效，不能从旧任务继承
python3 src/ride_agent.py resume --task-dir outputs/my-task --consent-upload --consent-map
```

授权边界：`consent_map` 只允许向 `https://overpass-api.de/api/interpreter` 发送识别出的行政区、精确道路名（或旧严格配准流程中的受检范围），不发送图片。`consent_upload` 单独允许向已配置模型发送原图、派生预览、诊断、对话及查询节点；不上传完整 OSM。模型不能指定 URL、QL、路径或自由经纬度。旧 `fetch_map` 仍只供通过独立检查的严格配准目标，不能被 corridor 借用。

预算与失败：每次运行最多一个 `query_map`，内部最多两次 HTTP 请求，共用30秒预算、单响应2MB、最多3个精确道路名、32 ways、10000 nodes、20000 references；Overpass 端也设置 timeout/maxsize。不走代理、不跟随重定向、不压缩、不自动重试。查询截断可能返回33个way，明确拒绝而非当完整结果。地域歧义、无道路、超预算或网络失败会等待用户，网页不显示远端正文/URL异常细节。要再次查询须重新授权。禁止下载任意大范围路网。

证据：`map-query.json` 保存端点/OSM way 来源URL、查询及响应SHA256、生成OSM SHA256、计数及模型可见证据；交付包含该审计文件。替换地图即撤销旧 inspection / corridor；运行开始也核对现存地图 hash，旧文件只供审计。所有结果 `verified=false`, `original_gps=false`，未确认起终点、道路选择、顺序、完整性或骑行权限，不可导航。

验证：`test_named_map_query.py` 使用**合成图片和坐标**，mock 模型 HTTP 与 Overpass HTTP，经过真实 APIPlanner 图文请求、query_map → inspect_roads → build_corridor → 交付链；另测无授权、不重放、一次查询预算、行政区歧义、对象超额及地图改变失效。它不证明真实图片 OCR 成功、地域可唯一判断、OSM 存在完整道路或真实路线已识别。

仍待明确本次授权后用真实截图验证：模型能否正确读取行政区/地名；无城市文字时能否诚实询问而非猜城市；OSM 名称/行政区唯一性、路网缺漏、实际顺序/回转/起终点。当前只支持精确行政区和精确道路名，不包含通用地理搜索、模糊纠错或任意地标自动定位。不能用 mock 通过宣称真实图端到端交付成功。
