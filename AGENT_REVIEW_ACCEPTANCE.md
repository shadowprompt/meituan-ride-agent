# Agent 当前像素版本语义复核闭环

## 实现范围

核心修改 `ride_agent.py`，新增 `route_review.py`、`test_route_review.py` 和本文；集成阶段同时升级 `test_api_start_e2e.py`、`test_error_diagnostics.py` 的旧成功mock。未修改网页、颜色阈值、折返像素启发式、中心线算法或 API 配置。未访问真实视觉 API、未下载地图、未提交/推送本轮修改。并行人工编辑任务的文件不属于本实现。

原实现 `ConsentedPlanner.local_pipeline` 在生成中心线后直接本地交付，模型没有看到结果。本轮取消真实 API 模式下该旁路，保留确定性中心线执行，但交付必须让 APIPlanner 看当前原图、提取叠图和骨架叠图，并显式返回 `review_route`。

## 强制协议而非仅提示词

- `observe_region`：结构假设枚举包括回转、两臂、遮挡、重复顺序和不确定。输入只接受原图整数区域、假设及理由，不接受路径/URL/任意参数。单边最大512像素，局部图保持1:1。当前提取存在时必须在其范围内，返回原图→提取裁剪→观察局部坐标变换。通过 API 请求发送原图、当前提取和中心线三份局部证据，不落盘额外用户截图。每条复核观察区域必须先经observe_region，且对应当前版本1:1图像已在后续成功请求中呈现；只填写哈希和任意区域无法通过。
- `review_route`：必须列出1–8个原图区域的结构假设、可见证据、结论及剩余限制。只接受 `consistent/needs_revision/ambiguous`，无自动真值认证。
- SHA256版本同时绑定当前提取参数、原图、掩码、提取叠图、骨架和骨架叠图。APIPlanner仅在请求成功且响应有效后记录当前证据的呈现版本及局部区域；网络失败不能授予复核权限；复核和交付都必须匹配该版本。重提取、重中心线或用户参数修正撤销旧复核；外部修改文件也令旧复核无法交付。
- 已记录不符/歧义的当前版本不能单纯改口为consistent，必须取得新提取证据或提问。已质疑内容哈希跨重提取保留，重复运行完全相同参数/像素不能靠清除旧状态改口。
- 真实API模式交付缺少复核或复核非consistent时返回工具拒绝，不生成成功摘要。仍保留此前pixels不要求控制点、分量歧义先暂停的安全规则。
- 局部观察/复核总计最多6次，模型提取最多3次，提前交付最多纠错3次，每轮执行总预算12步；达到预算不伪造成功，停止并请求必要证据。计数跨普通resume保留，防止反复重启绕开预算；明确的用户颜色/裁剪修正刷新观察纠错预算。
- LocalPlanner/DemoPlanner保持离线可用，不伪造模型复核，并强制附加“未经过语义模型复核”。`verified=false/original_gps=false`在执行入口保持。API交付强制附加模型复核仅为可见一致性自报以及模型报告的剩余限制。

## 有意没有实现的能力

这是审阅→明确坐标问题→局部观察→有限次安全重提取/重新审阅→交付或提问的最小闭环，不是已解决自动折返恢复。

没有局部删环、剪枝、连接断点、强制顺序或人工重画替代Agent。修正仍只使用已有 `extract` 的颜色和裁剪参数；用户锁定参数不允许模型覆盖，可能因此无法自动修正，必须诚实停止提问。结构理解先通过提示要求/结构化观察表达；本地验证器只能保证工具及证据流，不能证明模型真的理解图像、其一致性判断为真、或发现了全部错误。遮挡和同路重复经过顺序仍可能不可从图片确定。

真实模型视觉效果未获授权验证；所有通过项只证明mock响应下编排/安全门工作，不能声称模型实际修好了用户图。

## 测试

`python3 -m unittest test_route_review -v`：15项通过。使用实际APIPlanner的请求构造，mock urllib传输响应；真实确定性提取/骨架工具执行。

覆盖：当前原图/叠图及1:1局部图进入请求、needs_revision→局部观察→重提取→重审→交付、含裁剪偏移的局部坐标元信息、缺少复核拒绝、ambiguous必须提问、不能无新证据改口确认、重提取撤销旧版本、旁路文件修改阻止交付、无进展/观察预算有限停止、ConsentedPlanner不能旁路交付、LocalPlanner离线无网络无伪复核、越界/路径参数拒绝。集成新增：伪造当前哈希/未呈现区域无法过门、用户参数修改撤销复核及局部呈现授权、传输失败不标记呈现、完全相同重提取不能消除已报歧义；局部PNG还验证尺寸及原图裁剪像素一致。

全量双本地图环境命令（本地图片不入Git）：

```sh
RIDE_REAL_SCREENSHOT="$PWD/local-data/user-original-rgb.png" RIDE_TURNBACK_SCREENSHOT="$PWD/local-data/turnback-original-rgb.png" python3 -m unittest discover -q
```

集成全量已包含人工编辑模块回归。双图环境启用旧图和折返图，不跳过这些用例。

- Python3.10.12 / Pillow11.3.0：242项全部通过，117.739秒。
- Python3.13.13 / Pillow12.2.0：242项全部通过，110.377秒。

日志：`/tmp/route-review-integrated-double-pillow11.log`、`/tmp/route-review-integrated-double-pillow12.log`。

原先4项失败的旧mock已升级为实际请求驱动的 `extract → 确定性centerline → observe_region → 当前版本review_route → deliver`，合成图路径还保留inspect_colors。mock从实际请求读取当前哈希、提取框及局部证据元信息，解码原图/反馈图，断言当前提取与骨架及三类局部图存在；不硬编码假哈希、不直接写review状态、不把成功期望改为失败。网页HTTP→真实CLI子进程→本地mock HTTP仍端到端运行。诊断网页用例也从通用Mock planner升级为实际ConsentedPlanner/APIPlanner，保留网络/HTTP/配置/无效响应错误分类及无秘密泄漏断言。

折返图现有分量歧义仍安全停问，不冒充通过语义复核；对应断言未削弱。mock consistent只是受控测试动作，不等同真实视觉判断。

`git diff --check`通过。未提交环境配置、凭据、截图、任务状态或outputs。未commit/push，留待父独立审核。
