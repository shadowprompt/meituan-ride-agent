> **策略更新：当前默认为 `osm_relay`，本地OSM/Overpass直连仍禁止；高德仅显式兼容。本文旧“唯一/默认高德”或旧直连协议属于历史记录；当前配置、迁移和验收边界以 [OSM_RELAY.md](OSM_RELAY.md) 为准。

> **历史OSM实现说明（已停用）：** 默认且唯一联网地图服务为高德 amap。本文Overpass联网链、fetch_map、模型OSM节点上传不再执行；所有旧入口拒绝，无失败回退。旧产物保留只读；迁移必须显式选择amap并重新给予本次地图与模型上传双授权。当前使用方式见 [高德定位线索](AMAP_PROVIDER.md)。离线OSM文件仅限CLI --local-only 调试。

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

## 授权后等待用户：先看具体诊断，不盲目重复授权

`execution_state=succeeded` 只说明后台 CLI 本轮正常退出，不表示业务成功；`status=waiting_for_user` 仍需补充证据/处理问题。成功模型响应数为 1 也只证明收到一个模型响应，不证明地图成功或经纬度已交付。

现在每次**已授权且未超出本轮一次查询预算**的 `query_map`，会在发送前保存 `task.json` 的 `map_query_diagnostic` 与白名单产物 `map-query-diagnostic.json`。即使没有 `map.osm`，状态 API 与网页主结果仍显示查询行政区、具体道路、固定中文问题和恢复建议，无需翻 JSON。诊断包含本次 `attempt_id`、`started/failed/succeeded`、有界请求 `area_name/names/evidence`、`evidence_source=model_visible_text_unverified`，失败原因码或成功计数/OSM hash。`started` 而无终态只能证明记录了尝试，不能断言远端已收到或未收到。模型证据不是用户确认；非法/过长输入的诊断可能经过清理或截断。不会保存远端正文、任意异常字符串或模型密钥，原图也不包含在地图诊断中。成功地图来源审计仍在 `map-query.json`。

固定原因码：
- `area_ambiguous`：行政区匹配多个对象；补充可见城市/区县上下文，不自动选一个或扩大范围。
- `area_not_found`：行政区精确匹配无结果；核对全称/OSM 收录，重新授权本身不会修正名称。
- `roads_not_found`：该行政区精确 highway 名无结果；核对道路文字和所属地域。
- `http_429` / `http_error`：HTTP 限流/其他非成功状态，仅保存整数状态，不回显远端正文；稍后由用户决定是否重新授权。
- `timeout` / `network_error`：时间预算耗尽/网络连接故障；不混为行政区歧义，也不自动重试。
- `response_limit`：响应字节/对象预算超限；缩小请求，不能自动提高预算。
- `response_incomplete` / `response_invalid`：服务端报告不完整/响应结构或对象校验不通过；拒绝作为地图证据。
- `invalid_input` / `consent_required` / `output_error` / `redirect_refused`：非法名称、缺本次同意、本地保存失败、重定向拒绝。先处理对应问题，不盲目扩大授权。

失败不会覆盖既有 `map.osm` 或撤销仍有效的旧地图候选；诊断仅说明**最新查询尝试**，不把旧地图冒充新查询结果。本轮没有授权或查询预算已用时不发请求，也不覆盖上一次实际尝试的诊断。

### 保留并恢复旧任务

1. 不删除旧任务目录、不重新上传私图。网页“恢复已有任务”粘贴原 UUID，点“刷新状态”；这是只读操作，不重放模型/地图网络请求。任务必须在原服务器原 `outputs/web/<UUID>/task/` 下，或启动网页时配置的同一输出根目录中。本机没有的远端任务不能靠 UUID 在另一台机器恢复。
2. 新版本对已有新诊断的任务会直接显示具体问题；可下载 `map-query-diagnostic.json`。管理员可在原服务器只读查看 `task.json` 中的同名字段。旧版本失败没有留存这些字段/文件时，升级不会凭空恢复当次请求或根因，也不会自动重放历史请求；保留旧 `task.json`/执行记录用于核对。历史泛化文案无法区分真实的歧义、无结果、429、超时等。
3. 补充原图可见的正确行政区/道路文字时，展开“回答模型问题”填写事实，再按需要重新勾选**本次**模型上传和地图查询授权，点击“授权后继续识路 / 查询地图”。这会继续原任务但产生新一次查询诊断，不是还原旧失败；若当前诊断是网络/限流，先处理服务或等待，不改变地域/权限。刷新或只读读取不会触发继续。
4. CLI 原任务可用 `python3 src/ride_agent.py resume --task-dir <原任务目录> --consent-upload --consent-map` 手动继续（只在决定重新授权时执行）。仍保留每次一个查询、最多两次 HTTP、30秒共享时间、2MB响应及对象上限，无自动重试。

此次用户报告的旧任务 `dbbfec65-2d07-49d9-8efc-4922d09301d5` 不在当前本机目录中。已知代码路径确实可能在授权后进入 `fetch_named_roads` 并将所有 `MapFetchError` 合并为泛化等待问题；不能据此断言该远端任务实际使用的行政区、道路或具体失败原因。新增回归全部使用合成文字/图像及 mock 网络，不能套用模拟根因到该旧任务，也未重放私图、模型或真实地图服务。

### 实现审计补充：唯一性与多道路完整性

唯一行政区仍严格按 `name` 精确匹配且 `boundary=administrative`，不自行添加父行政区、不选同名第一项；Overpass 的 `out tags 3` 返回上限足以发现不唯一（多个匹配一律拒绝）。现在先验证每个返回对象的类型/ID/标签并拒绝重复ID，再把多个**有效且不同**对象归为 `area_ambiguous`，避免把污染响应误诊为真实地域歧义。道路名继续使用 JSON 字符串转义构造精确 QL，不接受模型QL/URL，行政区引用来自校验过的整数ID。33条way哨兵仍用于拒绝超过32条预算，而不是接受截断。

另修复确定缺陷：之前请求2–3条道路时只要返回任意一条就被当作查询成功，可能掩盖缺失道路。现在逐项核对请求道路均有返回，缺少任意一条即 `roads_partial`，不发布残缺的新地图、不替换旧证据；不做模糊匹配或额外网络补查。合成回归覆盖部分命中、重复行政区ID和非法多对象响应；这不是对远端旧任务的根因判断。
