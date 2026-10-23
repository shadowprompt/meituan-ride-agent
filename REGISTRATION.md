# 人工控制点配准

`register_pixels.py` 将中心线分段从掩码局部像素映射到近似 WGS84 坐标。依赖沿用 NumPy；不调用大模型、地图接口，不做自动定位或道路吸附。

## 跑通合成流程

```powershell
python examples/make_synthetic_screenshot.py
python extract_route.py --image examples/synthetic-ride.png --color "#00BFA5" --output-dir outputs/demo
python centerline.py --mask outputs/demo/mask.png --output-dir outputs/demo-centerline
python register_pixels.py --graph outputs/demo-centerline/pixel-graph.json --controls examples/synthetic-controls.json --output-dir outputs/demo-registration
```

示例控制点使用人造坐标关系，不对应真实地理位置。独立检查误差接近零只证明软件在已知仿射关系上工作，不能证明截图定位准确率。

## 真实截图的控制点

复制示例 JSON 到 `local-data/controls.json`，替换为自己确认的地图地标：

```json
{
  "coordinate_system": "WGS84",
  "pixel_frame": "local_mask",
  "control_points": [
    {"pixel_xy": [10, 10], "wgs84": [0.0001, -0.0001]},
    {"pixel_xy": [200, 10], "wgs84": [0.002, -0.0001]},
    {"pixel_xy": [10, 200], "wgs84": [0.0001, -0.002]}
  ],
  "check_points": []
}
```

以上坐标也只是格式示例，不可用于真实截图。

- `pixel_xy`：**掩码局部**像素坐标，不是截图全图坐标。若全图控制点是 `[x,y]`，裁剪框左上角是 `[left,top]`，应填 `[x-left,y-top]`。
- `wgs84`：同一地物的 `[经度,纬度]`。应选明确路口或地物点，不能把道路文字标签的位置当路口。
- 至少三个不共线控制点，建议四个以上、覆盖整个地图区域；不要集中在一角。
- `check_points`：未参与拟合的独立检查地标，不能复用控制像素位置。至少两个才有可能通过误差门槛。工具只能检查坐标是否重复，不能验证地物来源是否真正独立。
- 输入只接受已知 WGS84，不推断、不转换未知坐标系。截图底图的偏移/畸变不会凭空消失。

```powershell
python register_pixels.py --graph outputs/ride-centerline/pixel-graph.json --controls local-data/controls.json --max-error-m 35 --output-dir outputs/ride-registration
```

## 输出

- `registration.json`：拟合/检查点逐点误差、RMSE、模型参数、独立检查门槛及警告。
- `registered-segments.geojson`：WGS84 `FeatureCollection`，每个骨架分段单独保存，**不把分段排列假设为骑行顺序**。

三个控制点通常可精确拟合，因此仅凭训练残差很小没有意义。`independent_error_gate_passed` 要求至少两个检查点，并且所有拟合及检查点误差均不超过指定阈值；35 米默认值尚未经真实数据校准。

无论门槛通过与否，始终 `verified=false`、`original_gps=false`、`status=needs_confirmation`。误差过大时仍输出诊断候选，不静默修正或伪装成功。

当前局部投影限 100 公里；超出控制点包围框会提示外推风险，包围框检查并非凸包覆盖检查。未知地图投影、错认地标及非仿射畸变仍可能使配准失效。

输出是分段集合，当前 `route_eval.py` 只接受有序单一 LineString，**不能把集合直接交给它评测**。仍需道路候选生成及歧义路径处理。该模块不检查道路连通或骑行合法性，不生成导航路线。
