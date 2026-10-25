# 有界行政区层级查询

## 修复动机与真实边界

任务 `d1b0c146-b25e-403c-8d8b-6eb29f4e98a8` 已报告 `area_not_found`，输入为 `上海市普陀区`、`长风路 / 祁连山南路`。旧实现将拼接字符串直接用于全球 `area[name=完整字符串]` 精确查询，不能表达父子行政关系。最初修复仅调整查询表达及安全诊断，未重跑该任务；后续timeout审计的一次公开层级请求见文末，未重放原图。道路文字、道路归属、真实路线及 OSM 当前收录均未核实；不能把这两个路名当作已确认路线。

## 输入与拒绝规则

模型动作仍为 `query_map`，可额外提供显式结构：

```json
{"action":"query_map","area_name":"上海市普陀区","area_hierarchy":["上海市","普陀区"],"names":["有原图依据的道路名"],"evidence":"说明可见文字、父级依据及不确定性"}
```

- 结构为2–3个精确全称，拼接必须等于原始 `area_name`，不得猜父级。
- 未给结构时，只保守解析纯汉字、以省/市/区/县结尾的1–3级字符串。不移除后缀、不做别名转换、不硬编码上海。内含行政后缀的名称（如市中区）及自治区等复杂名称应显式提供结构；无法完整拆分即停止。
- 单级输入仍走原有全局精确行政 area 查询；零个停止，多个停止，不挑第一个、不因“普陀区”而猜上海或舟山。若需明确父级，必须由图文证据补齐。
- 恶意QL片段、URL、控制符、非汉字行政名称、错误结构在联网前拒绝。道路名仍按原有有界字符串精确查询及JSON转义，不变成QL程序。

## 层级解析和证据

第一条 Overpass QL 查询全球精确父行政 **relation**；只有父集合数量恰好为1才继续。每级子对象必须同时满足：

1. 是父 relation 直接 `subarea` 成员；
2. 满足父 relation 派生 area 的 `area` 空间过滤；
3. 精确名称、`boundary=administrative`、`type=boundary`，且唯一；
4. 返回固定QL生成的 parent_id/child_id 见证可本地复核子 relation ID，`admin_level` 为2–12整数并严格递增。

最终仅接受与末级 relation ID 对应的派生 area ID（relation ID + 3600000000）。父歧义不继续，子歧义不挑选；成员缺失、跳级、area缺失/不匹配均停止，不回退全国搜子行政区。OSM 很可能缺少完整 `subarea` 归属，此时宁可返回 `area_membership_unverified`，不宣称真实不存在该行政区。空间约束由固定QL执行，本地不下载边界几何、不声称独立几何包含验证或OSM数据真实性。

## 授权和预算不增加

- 一轮仍最多一次 `query_map`，内部最多 **两次** HTTP 请求，无自动重试。
- 第一次QL完成全部父/子层级解析；成功后第二次仅获取末级 area 内1–3个精确路名，不下载区域路网。
- 固定端点 `https://overpass-api.de/api/interpreter`，不跟随重定向、不使用环境代理。地图授权与模型截图上传授权独立，每轮重新确认。
- 两请求共享30秒截止时间，QL分别10/15秒；每响应上限2,000,000字节（两次合计最多4,000,000字节），QL内存上限2,097,152字节。层级响应最多12个对象；道路最多32 ways、10,000 nodes、20,000 references。完整way可能延伸出行政边界，不代表经过顺序或导航许可。
- 第一请求可能发送各级名称并取得行政 relation tags 与有限父子见证（不返回整个成员表），不含截图；结果诊断及道路证据仍可能随用户本次授权的后续模型请求进入对话。

诊断保存原始输入、显式结构（若有）、实际查询层级及候选对象ID，不保存/回显远端正文。网页主结果显示层级和候选；地图失败仍保持等待，后台执行成功不代表业务交付成功。旧任务只读取时不自动更新旧失败诊断，不删除任务、不代用户重试。

## 离线验收

`test/test_admin_area.py` 使用合成OSM对象覆盖上海/舟山同名区的独立父级约束、三层级、显式结构、父级不明/歧义、零结果、缺失归属、恶意名称、旧文件保护及两请求预算。所有测试为mock，不证明公网QL兼容性、OSM收录情况、真实模型识字或用户路线正确。


## 真实 timeout 后续审计（2026-10-25）

任务 `15b2067f-8946-41ab-99a5-fbb9f986e73f`，attempt `f435d0b4-de44-437d-a2ee-84258da0e979` 报告 timeout、`candidates=[]`。这只表示尚未取得/解析候选，**不代表无候选，更不能声称层级成功**。本机没有远端网络阶段证据，仍不知道该次失败发生在连接、等待响应头、读正文还是服务端执行。

### 查实的查询问题与优化

旧 `.p0 out body 3` 的 `3` 只限制 relation 对象数，**不限制每个 relation 的成员数**；父市边界可能返回巨大 way 成员列表，而这些并非本地归属验证所必需。旧QL在递归全部 subarea 后才做名称/范围过滤，也增加了不必要中间候选。

改为每级 `out tags 3`；直接在 `rel(r.parent:"subarea")` 上同时限定精确子名与行政标签，再对这个小集合执行父 area 空间过滤。仍只在父唯一时执行子查询。固定QL仅在子唯一时 `make evidence` 输出父/子ID；本地要求每条边恰有一份匹配见证、admin_level严格递增、末级派生area一致。没有取消 subarea 或父空间过滤，没有改成全国子级查询，没有硬编码上海ID。

这减少传输与中间集合，但父派生area/空间过滤仍可能昂贵；`out tags 3` 也不代表服务端只扫描三个对象。现有10秒QL和2MB内存预算不变，服务端排队/网络等待不受QL执行秒数保证。

### 分阶段诊断

`transport` 仅含安全枚举/数值/布尔值：`query_stage`（admin_area/roads）、`phase`（connect/response_headers/read_response/total_budget/server_execution）、`elapsed_seconds`、`total_elapsed_seconds`、`connection_established`、`response_received`、`bytes_received`。connect包含DNS/TCP/TLS，不假称已分离三者；response_received指收到HTTP响应头，不表示正文完整。每次正文读取收紧socket剩余时限，采用read1，避免read内部多次读取各耗一个完整超时。

服务端JSON `remark` 有 `timed out` 时返回 `server_timeout`，其他remark为 `response_incomplete`；不输出remark、异常字符串、URL或响应正文。部分/未收到有效层级响应时 `area_resolution.status=unavailable`，候选数组只代表已验证记录；UI明确显示未取得而非无候选。请求数、总30秒截止时间、授权与固定端点均不增加；不轮换端点、不自动重试。

### 最小公网实测（仅一次）

命令见 `docs/evidence/admin-timeout-probe-command.txt`，安全结果与完整QL见 `docs/evidence/admin-timeout-probe.json`。本机单次实际请求HTTP 200，26.817秒，7908字节；上海市relation 913067、普陀区relation 398886、父子见证及area 3600398886均通过校验。证据文件仅保留必要公开tags，不保存完整远端正文。

这次证实固定端点接受新QL、当前公开OSM数据在该次请求可提供严格层级证据；也表明30秒中已花去约26.8秒，后续道路可能仅剩约3.2秒，**不能据此保证原任务恢复，也不能断定优化前超时就是成员传输所致**。未请求长风路/祁连山南路，未下载范围路网，未上传图片、调用模型、重跑/修改用户任务。路名识别、道路归属和真实路线仍未经验证。

补充限制：Python标准库DNS解析不提供独立可中断的30秒硬上限；当前连接阶段包含DNS、TCP、TLS，不能把`connect`误读成TCP已建立。总截止时间在连接完成及各次读响应时检查；无额外网络请求或重试。

离线新增 `test/test_map_timeout.py` 覆盖连接、连接后等响应头、部分正文读取、预耗尽总预算、真实HTTPResponse/read1路径、服务端超时remark与安全诊断；层级测试覆盖缺失/错误/重复见证及三层级。`budget_exhausted` 在底层超时/网络异常时额外标识当前总截止时间是否已耗尽，phase仍保留实际等待阶段。

验收命令：`PYTHONPATH=src python3 -m unittest discover -s test -q`（343项，10项跳过）；`PYTHONPATH=src:test python3 -m unittest test_admin_area test_map_timeout test_map_fetch test_named_map_query test_web_guidance -q`（42项通过）；从`ride_web.py`提取内联script后`node --check`通过；`git diff --check`通过。测试均未调用公网/模型；只有前述单次手动公开层级探针联网。
