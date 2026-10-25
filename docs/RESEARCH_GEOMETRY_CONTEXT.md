# 8d470d38 实际失败：节点证据可消费性与保存/停止分离

## 核查范围与证据边界

基线 `a2cd9fb`，开始 `git status --short` 为空。只读检查
`outputs/web/8d470d38-340b-4a92-ba25-51fd9ef1a2ad/task/` 的 task.json、原图和研究缓存。
未调用真实模型、地图、上传，未重跑旧任务，未改服务、环境文件或 nginx。
`test/fixtures/research-failure-8d470d38.json` 保存提取出的历史模型动作/工具结果、公开对象事实及基线 system 文本；**不含原图文件或有效授权**。
这里的 system 是从基线 Python 常量重建，历史目录没有保存完整 HTTP 请求体，因此不能声称拥有线上逐字 wire capture。
新增测试拦截当前 APIPlanner 的实际出站请求体，核对 system 和 CURRENT_STAGE_STATE；使用新建白图任务，不上传原图。

## 根因：不是旧版未部署，也不是经纬度从未返回

- 真实任务停在模型 **12/28**、对象/搜索请求 **4/8**、搜索 **3/3**；不是模型或地图请求耗尽。
- `way/1225879505/full` 返回 **7 个对象 = 1 way + 6 nodes**，不是 7 个节点。原始工具对象确实含真实坐标、节点 tags 与顺序。
- 当时 `spatial_evidence.ways` 仅将节点列成 ID，提供 bbox、端到端 southwest 和空 connections。未将每个节点的真实位置、端点/内点、弯曲量、已核查父道路状态转成可消费的紧凑结构；泛泛推荐两个端点，并不能说明哪个节点检验什么。
- 第一次 checkpoint 真实工具结果已经返回 `bounded_planning_with_current_consent`、可用网络额度和已见道路/节点。模型仍把“南北背景道路不承载东西横线”延伸成“没有可用于连接核查的道路对象”，又裁一次图后保存。不能把此行为归因于授权没有送达。
- 原 system 同时要求不要爬背景道路、先定位，却未明确分开“排除为骑行路段”与“仍可作定位/条件性连接检验的参照”。只补一句允许网络无济于事；本次重点改真实工具摘要及保存决策。
- 两次 checkpoint 占满 closure=2，代码强制停止并展示“保留动作/模型硬上限”；实际模型尚有 16 次。保存次数被错误当成终止预算。

## 实改

### 真实几何摘要，不做截图配准

对道路的 `known_geometry.nodes` / `research_spatial_evidence.ways[].nodes`，按原 way 顺序提供：

- 真实 WGS84 经纬度与相对该已返道路 bbox 中心的 `[东,北]` 米偏移；有完整坐标的当前六节点全部呈现。
- way 端点/内点、由前后真实坐标计算的有符号转角；节点 tags 原样作为非可信地图数据。
- 已知共享节点道路 ID、父道路查询是否执行、交点是否仍未知。`highway=crossing` 或道路端点 **不等于已证实路口**。
- 道路与独立检索 bounds 的地图内部相对方位、距离，截图对应单独保持 unknown。
- 可检验连接问题：在独立位置线索与具体转折/相对位置支持时，检验某个真实已见节点是否有与另一图像线段方向相容的 distinct parent road；若没有，或 full 后方向/位置不符，否定该连接假说，不宣布整个地区无路。

不自动推荐任何节点；移除了“只因是端点就建议 node/ways”。模型可拒绝错误位置节点，不要求查 N 次，不按背景路机械展开。node/ways 仅证实成员关系，缺坐标的连接路仍需 way/full。
摘要最多显示前 256 节点并显式标记截断/总数，不把未显示部分当不存在；不新增 bbox/路口搜索、Overpass 或地图数据库。

### 搜索结果非道路的明确结构反馈

当时“长风路”HTTP 搜索返回 found，但五个对象全为 non_road，candidate_kind/category/type 其实已提供；没有道路候选不等于该路不存在。本次增加 `hypothesis_outcome.search_feedback`：
`no_road_candidates_returned`、returned_count、name_reading=unconfirmed、road_existence=not_excluded，指向另一个独立图像线索或有依据的已见道路连接检验。
工具没有模糊路名搜索/区域路口查询能力，明确反馈这一缺口，不用提示词假装能查。

### checkpoint 保存与停止分离

checkpoint 仍消耗一次模型尝试并增加 no-progress；**不再消耗 ask/deliver 终止槽**。不是将 closure 2 改成更大数字，更未提高 28/8/3/6 任一上限。
旧 ledger 一次性从 closure 分类移除消息中可核对的 checkpoint 保存，保留真实 ask/deliver、模型、网络、失败和观察计数；重复恢复不重置。

- 有有效、非重复的 `next_local_action`，或当前独立地图授权下的 `next_action`（完整 inspect_map_object）可继续；实际执行仍重新核验 seen ID、真实道路父对象、严格 schema、授权、去重和预算。
- reason-only 规划修复按不同证据版本/观察/决策/请求组合去重，而非任务终身只能一次；保存本身不产生新证据。同一证据下不重复发修复。
- 连续重复/保存无进展仍最多三次停止，原模型预算及最后六轮 local-only closure 保留。不同合法动作不是“保存两次就停止”。
- 无相关可执行方向则 paused，保留 unknown 与具体原因；只有确实 model_calls>=28 才声称模型硬上限。可人工继续不意味着已有有价值测试，更不继承上传/地图授权或自动重放。

## 验证与能力边界

新增真实静态 fixture 核对：实际第一次 checkpoint 已授权、原对象坐标确实存在、旧摘要信息缺口、非道路搜索反馈、当前实际 API payload 的完整节点摘要/system schema。
合成 Mock 主链：南北道路拒绝为所经横线 → 独立位置/转折理由选择南端而拒绝北端 → node/ways → 新连接道路 full → 原图局部比较 → 含重复路段的未验证候选。另测错误位置端点不默认推荐、三次不同可执行保存不误停、无授权/未知节点不能获下一网络动作、重复无进展诚实停。
原有 unknown/歧义不能交付、最新原图 review、跨继续授权/预算、429/失败计数/SSRF/脱敏等回归仍保留。

**这证明证据能进入决策上下文、动作编排可达，不证明真实模型会自主选中正确节点，更不证明真实截图道路识别成功。**
旧 `local-data/georeference-research/approximate-sources.json` 与相邻 osm-single-objects 只用于理解过去单对象与成员查询能力；其中节点/道路答案没有进入运行时代码或新主链。
真实自主视觉/地理验收必须另经用户新授权；本轮仅代码，父任务验证后部署。

## 本轮离线验证结果

- `PYTHONPATH=src:test python3 -m unittest discover -s test -p 'test_research*.py'`：134 项通过，0 skipped。
- `PYTHONPATH=src:test python3 -m unittest discover -s test`：536 项运行，**526 项通过、10 项 skipped**（不是 536 项通过），133.830 秒。
- `python3 -m compileall -q src test`、提取 Web 内嵌脚本的 `node --check`（1 个脚本）、`git diff --check` 均通过。
- 全部真实外部调用禁止；仅 Mock HTTP 和合成/静态本地证据。测试通过不替代线上自主模型验收。
