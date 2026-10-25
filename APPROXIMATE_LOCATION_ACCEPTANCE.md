# 近似定位交付验收

## 实现与隔离

- `src/approximate_location.py`：带来源、不确定度的3个以上非共线证据锚点，复用原有局部仿射数学与退化校验，不更改严格算法/35米门槛。
- CLI：`resume --local-only --approximate-anchors-file`。网页：明确选择近似模式并上传对应点 JSON，固定 allowlist 下载报告和 GeoJSON。纯本地，无模型调用/自动下载。
- 独立状态 `approximate_complete`、目录 `approximation/`；不设置严格 `registered`、`registration_gate_passed`。逐集合/分段明确近似、未验证、非原始GPS、不可导航。
- 保留来源、声明不确定度、残差、凸包外推统计，不假设三点零残差意味着真实准确。道路读取器、地图下载、任务匹配都拒绝近似产物。
- 当前裁剪改变后锚点失效；提取、中心线、分量选择、人工补孔改变后撤销近似完成状态，旧产物仅供审计。重新上传严格控制点可显式回到严格流程。

## 自动化与实际 HTTP

`PYTHONPATH=src:test python3 -m unittest test_approximate_location -v`：8项专项回归通过。覆盖：来源/不确定度/坐标系/点退化校验，元数据与凸包外推，严格模式拒绝近似，缺失/失效/不同裁剪，不接受模型坐标注入，近似→严格切换，近似道路/地图准入拒绝，真实 loopback HTTP→CLI 子进程上传与下载。**专项全部经纬度均为明确标注的合成坐标，不代表真实轨迹验收。**

另用两张本地真实原图通过实际 loopback HTTP 验收创建与仅本地继续：

- `local-data/user-original-rgb.png`，裁剪 `[235,1200,1010,1420]`，颜色 `#6A778B`。
- `local-data/turnback-original-rgb.png`，裁剪 `[240,1140,1000,1580]`，颜色 `#6A778B`。

HTTP 200，执行来源 `local`，`api_responses=0`。保留所有候选只为测试链路，不冒称用户已确认这些像素。两者均在无真实锚点时安全停在 `waiting_for_user`，明确要求当前裁剪的真实 WGS84 锚点；没有创建经纬度文件，没有用合成点替代真实定位。

真实图未上传到外部模型；现有全量HTTP模型回归使用本机mock，不是外部视觉API。未下载地图/路网。私有图片、local-data研究文件、环境配置和生成产物不纳入提交。

最终全量命令同时设置 `RIDE_REAL_SCREENSHOT=$PWD/local-data/user-original-rgb.png` 与 `RIDE_TURNBACK_SCREENSHOT=$PWD/local-data/turnback-original-rgb.png`，执行 `PYTHONPATH=src python3 -m unittest discover -s test -v`：**285项全部通过，无跳过**（原277项加8项近似专项）。严格配准、本地恢复、像素/补孔、网页安全、模型mock链路及两张真实图片的既有回归均通过。完整本机日志 `/tmp/ride-approx-final-tests.log`，专项日志 `/tmp/ride-approx-http-final.log`，两张实图无锚点HTTP日志 `/tmp/ride-approx-real-http.log`（日志非版本库产物）。
