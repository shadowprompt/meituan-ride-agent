# 把原调研方法接入 Agent：离线单对象证据回放

## 证据结论（不是历史成功的再猜测）

本机 `local-data/georeference-research/approximate-sources.json` 保存公共 API URL、响应 SHA256 和 `osm-single-objects/*.json` 原文。重新检查10份响应得到23个实际对象，入口是 **OSM 具体对象 JSON / node 所属 ways**。`APPROXIMATE-RESEARCH.md` 还记载 Nominatim 地名查询，但没有保存对应搜索原始响应；这只能算历史研究文字，不能冒充本次复验成功的检索。当前材料不能证明当时 `web_fetch`、Overpass、高德地点/静态图等哪个调用成功，不能因为某处有代码或说明就说使用成功。本轮没有会话 list/history 工具，不虚构消息取证。

`route-baseline-audit.md` 指出：完整道路序列没有已确认基准。附近路口/车站是真实公共对象，不等于骑行经过这些对象。既有三控制近似配准独立检查 RMSE765.45m、35m门槛未通过，不复用成成功验收。

## 已实现：现有 Agent 动作，不是新的下载前置链

显式 `--map-provider research --research-manifest …` 安装本地原始响应到任务，校验每份 hash、固定公共对象 URL、根目录边界、对象成员关系、坐标/引用/预算/冲突版本，转换为保留原始 node/way ID 的小型本地对象视图供**既有** `inspect_roads/build_corridor` 使用。这不是范围路网下载、索引扩展或任何网络请求。

模型循环：

1. 看当前原图，必要时 `observe_region` 看1:1局部；提出有依据且未验证的道路/地名假设。
2. `search_map_evidence(names,evidence)` 搜索实际保存对象的名称（精确匹配，不伪装在线通用搜索）。
3. `inspect_map_object(object_type,object_id,evidence)` 看返回对象/way 成员或 node 所属 ways；Agent 只接受已返回的对象或成员 ID。
4. `research_hypothesis(version,ordered_roads,evidence,limitations)` 保存模型有序假说，未知段用 null，折返必须保留重复项；修改撤销旧走廊/复核。
5. 证据足够才走既有 `inspect_roads`、`build_corridor`，全部坐标取真实对象，不能生成控制点/自由坐标。构建道路顺序必须与假说完全一致，不允许略过未知段或折返。
6. `review_research(version,result,evidence,limitations)` 比较截图结构、顺序假说和对象事实。needs_revision/ambiguous 禁交付；修正重建后重新复核。consistent 只是模型未发现矛盾的自报，不是像素配准、真实轨迹验证或原图叠图成功。
7. 实际走廊成功且当前复核 consistent 才能交付，附 `research-audit.json` 中原图SHA、证据版本、来源URL/响应SHA、假说、复核及未验证标记。

模型不需要用户填写地名、经纬度、控制点或行政区层级；缺档说明缺哪个对象/搜索和具体授权，不要求用户代做调研。原默认 osm_relay/amap 行为未被静默切换；`research` 当前为 **CLI 显式入口**，网页选择器尚未接此入口。没有新增在线地图工具、服务或配置。

## 命令与授权边界

从仓库根目录执行；manifest 的 saved_as 相对当前仓库目录，任务导入后改为相对任务目录。

```sh
# 仅离线初始化（demo 不使用模型，不代表模型已看图）
python3 src/ride_agent.py start --image local-data/turnback-original-rgb.png \
  --task-dir outputs/my-research --goal corridor --map-provider research \
  --research-manifest local-data/georeference-research/approximate-sources.json --demo
```

真实模型运行需要**另行明确本次授权**。用户确认上传/供应商/可能费用后才执行：

```sh
python3 src/ride_agent.py start --image local-data/turnback-original-rgb.png \
  --task-dir outputs/my-authorized-research --goal corridor --map-provider research \
  --research-manifest local-data/georeference-research/approximate-sources.json --consent-upload
python3 src/ride_agent.py resume --task-dir outputs/my-authorized-research \
  --map-provider research --consent-upload --review-continue
```

这里不需 `--consent-map`：**只读已保存公共证据，没有联网**。旧地图查询授权不授权新业务请求，也不授权上传真实截图/付费模型。manifest 更换须显式导入，会撤销旧假说/复核/走廊。原图输入会正规化存为 PNG，`source_sha256` 保存输入文件的 SHA，不混用编码后文件的 SHA。

## 本轮真实图离线复现与实际缺口

真实输入 `local-data/turnback-original-rgb.png`，1260×2720，文件 SHA256：
`ac9eafdca8dff978c56c3f462fe20696260ee7d8e6f00d17399f2e456821e5b7`。

本地打开原图，确认东侧贴近的南北往返形状、顶部小回转、西行中的短南北连接等仍需解释，不能将两张别的截图/历史候选混入。`outputs/research-replay-turnback/offline-replay.json` 实际回放23个对象：检索古北路/长宁路，再检查 node560400092，原始响应支持古北路 way45167733/1076615066 与长宁路 way15670287/1076615065 共享此节点。**没有证明截图像素恰是此节点，未构建完整真实路线。** 没有上传图、调用外部模型/地图或使用合成坐标作为真实验收。

真实E2E还缺：
- 本次明确许可向配置模型上传这张真实图、派生局部和当前公共证据并产生可能费用，才能实测模型决策与修正质量；本轮未执行。
- 档案不能覆盖上端回转、西行两个拐角及终点接入的已核实对象。若模型仍缺证据，需再授权具体地名检索/具体公共对象请求，列明目标、入口、数量、是否收费；当前实现**不会自行在线补查或下载路网**。
- 未提供已证明的截图—道路对应/完整顺序，不能用公共对象事实或 mock 工具循环宣布真实原图恢复成功。

`test/test_map_research.py` 使用明确标注的单元假对象测试协议、拒绝篡改、重复路段和复核 gate，仅是本地协议回归，不是合成图片验收真实路线。

## 兼容回归修复说明

首次全量发现现有 `test_osm_relay` 两项失败：相邻独立服务已使用单个 guarded hierarchy+roads 查询，旧 mock 仍返回单层行政区且断言3次请求，客户端也只接受3份来源。最小修复为更新 mock 对应真实服务契约，客户端仅在提供完整、唯一、逐级父关系和 `source_index=0` 的 `admin_chain` 时接受一份公开来源；缺链/错误归属仍拒绝，保留旧多来源协议。不改服务代码、配置、索引或下载行为。新增契约拒绝回归，不跳过原测试。

本轮验证：`PYTHONPATH=src python3 -m unittest discover -s test -q` 全量400项通过、10项沿用环境条件跳过（未新增跳过）；之后补充1项拒绝臆造ID/跳过未知段回归，research相关5项独立通过。relay相关13项独立通过。`git diff --check` 和三个改动模块 `py_compile` 通过。测试均无真实外部业务请求，不能作为真实截图模型E2E。
