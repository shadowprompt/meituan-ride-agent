# Agent 本地道路候选闭环

本阶段把道路候选工具接入 Agent：先提线、中心线、人工地标配准，再在本地 OSM 图内寻找形状接近的连通道路候选。不是自动定位，不是导航。

## 完整离线演示

```powershell
python -m pip install -r requirements.txt
python examples/make_synthetic_screenshot.py
python examples/make_synthetic_roads.py
python ride_agent.py start --image examples/synthetic-ride.png --task-dir outputs/road-demo --demo --goal roads
python ride_agent.py resume --task-dir outputs/road-demo --answer-file examples/demo-answer.json
python ride_agent.py resume --task-dir outputs/road-demo --controls-file examples/synthetic-controls.json --osm-file examples/synthetic-roads.osm
```

若任务目录已有内容，换一个新的目录。最后检查 `outputs/road-demo/roads/road-overlay.png`。图中灰色是路网、蓝色是配准参考分段、红色是匹配候选；它是本地路网示意图，没有在线地图底图。

示例截图、控制关系、OSM 格式道路都是人造的，不是真实骑行，成功不代表真实识别准确率。

## Agent 行为

- `--goal roads`：先请求像素参数，再请求控制点及本地 OSM 文件。
- `--controls-file` 的规则见 REGISTRATION.md。独立配准误差门槛未通过时不允许匹配，先请求修正。
- `--osm-file`：用户指定本地 OSM XML，复制到任务目录；模型不能选择文件系统路径或网络地址。
- 可以只提供一种证据，之后再 resume 提供另一种；普通补充不重跑有效前置阶段。
- 新增 `match` 白名单动作。匹配失败也必须交付诊断，不能编造路线。
- 真实视觉模式每轮仍需 `--consent-upload`；匹配工具本身不联网。

## 工具单独运行

```powershell
python road_match.py --segments outputs/road-demo/registration/registered-segments.geojson --osm examples/synthetic-roads.osm --output-dir outputs/roads-only
```

输出 `road-match.json`、`road-candidates.geojson`、`road-overlay.png`。
报告应同时检查 matched_segment_count 和 input_segment_count：部分匹配不等于完整路线。没有候选时为 no_match；有候选也只是 needs_confirmation。

## 算法及限制

- 对每个有序几何分段独立选择附近道路节点，并在参考走廊内进行有界搜索，惩罚偏离；再检查有序形状距离及长度。
- 不把道路连通当作匹配正确；远离参考、断开的图不强制连接。
- 当前每个输入分段最多交付一条启发式候选，没有完成完整多候选覆盖率/排名评测。
- 不确定各分段实际骑行顺序和重叠次数，不输出整条骑行 GPX。
- 道路边内有界插值虚拟节点用于吸附，不产生跨道路连接；搜索有上限，复杂路网可能无结果。
- 使用无向图，虽然排除部分明确禁行边，但未完整核验单行、转向、桥梁层级、时效或骑行合法性。
- 小回转可能受采样/简化尺度影响。必须对照原图人工核验。
- 输出候选含虚拟插值点，不满足 route_eval.py 的“原始 OSM 相邻节点精确匹配”检查；不能把那个检查的失败当作本模块断路证据。
- OSM 文件自行从合规来源取得并遵守数据许可；实际 OSM 数据展示须保留 OpenStreetMap contributors 归属。示例生成器不下载数据。

当前仍缺截图自动地理定位、自动地标配准、真实视觉供应商联调及独立真实截图验收。


### Agent 下载地图

已完成配准且独立误差门槛通过后，可用 `resume --task-dir … --fetch-map --consent-map` 替代 `--osm-file`。程序从配准分段计算范围，模型不能指定地址或坐标；固定 Overpass 服务将收到该地理范围。真实模型任务另需 `--consent-upload`。下载与匹配仍是候选，不是自动定位或骑行导航。限额及隐私说明见 `AGENT.md`。
