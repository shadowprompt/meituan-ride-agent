# 离线路线候选评测

## 当前实现

`route_eval.py` 是仅依赖 Python 标准库的 CLI。输入为一条**有序、已配准的参考折线**及若干道路候选，不直接接收截图。参考可以来自人工标注或原始 GPS，但必须记录其来源、取得使用授权；文件名含 corrected 不代表真值。

它计算：
- 局部米制坐标下的离散 Fréchet 距离：比较轨迹顺序，而非只看覆盖区域。
- 起终点误差、长度比、等弧长采样方向一致性。
- 可选的 OSM 相邻节点边检查：检测跨路跳跃，不自动吸附、不改变候选。
- 启发式综合分、候选分差、几何门槛及待确认状态。

输入只支持 GeoJSON `Feature` + `LineString`，必须声明 `properties.coordinate_system: WGS84`。暂不支持 FeatureCollection，不静默合并或转换坐标系。限制每个 JSON 5 MB、20000 顶点、100 条候选。仅删除相邻重复点，保留真实重访和折返。非法候选单独记录错误；参考错误直接失败。

## 运行

在项目目录中：

```sh
PYTHONPATH=src python3 -m unittest discover -s . -p 'test_*.py' -v
python3 src/route_eval.py \
  --reference reconstruction/blind6-corrected-road-candidate.geojson \
  --candidate reconstruction/blind6-corrected-road-candidate.geojson \
  --candidate reconstruction/blind6-road-candidate.geojson \
  --osm reconstruction/blind4.osm \
  --output reconstruction/evaluation-smoke.json
```

以上是**软件冒烟检查**，不是独立准确率评测：参考本身也是人工假设候选。自比较得分 1 是数学一致性检查，不能证明道路识别正确。

实际冒烟结果：两条候选均为现有 OSM 连通边；相对于指定参考，另一条候选 Fréchet 距离约 428 米、长度比约 0.567，几何门槛未通过。这说明“道路连通”不能替代“形状匹配”。

## 结果解释与边界

- 所有结果保持 `verified=false`、`original_gps=false`、`status=needs_confirmation`。这描述输出为候选评测结果，不把参考或输入元数据升级成验证结论。
- `score` 为人工权重启发式分，**不是准确率或置信概率**，未连接合成训练演示中的模型。
- 默认 35 米容差、0.08 分差阈值未经真实数据校准。分差大也不说明候选集合里一定有正确路线。
- 采用 96 点等弧长采样。很小的回转可能漏检，极端稀疏顶点下方向分数也可能变化；当前不保证所有复杂折返都能检测。
- 投影限于参考起点周边 100 公里范围；非大范围测地算法。
- OSM 检查要求完整原始节点折线；简化线、插值点也会不匹配。它不是一般的道路吸附匹配器。
- OSM 检查是无向的，未核验骑行限制、单行方向、转向限制、桥下层级及路网时效。不能用于导航。
- 没有 OSM 输入时道路检查为 null，而不是通过。
- 一张静态截图可能不能确定重叠段的次数和先后顺序；参考顺序需另行核实。
- 尚无自动 OCR、截图定位、像素轨迹提取、控制点配准和候选生成；这些仍需实现。

## 下一步

1. 从截图自动提取像素轨迹与起终点，并保存不确定区域。
2. 独立地标配准，保留坐标系及控制点误差证据。
3. 道路候选生成、多尺度形状检查、双向空间覆盖指标。
4. 收集授权真值，以完整骑行任务及地理区域隔离训练/测试；单独评测候选覆盖率和排序质量。
