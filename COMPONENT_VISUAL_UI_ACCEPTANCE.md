# 候选像素可视选择验收

## 交互

1. 在候选区就地查看原图裁剪与候选像素：绿色保留，橙色排除。
2. 点击候选像素或各编号的“查看并局部放大”，该编号保持着色，其余淡化；下面显示最近邻放大的局部视图。按钮支持键盘操作，宽图可滚动。
3. 多选框立即更新浏览器内的保留/排除 mask 预览；无网络请求、不生成中心线，明确标注“本地待应用”。
4. 点击 **应用选择并重新生成**，仅提交选中编号和 `local_only:true`。无需第二次勾选；不夹带颜色、裁剪、地图或模型授权。至少选择一个编号。
5. 应用后仍是未验证候选，选择不确认路线真实性，不恢复顺序、连接断点或 GPS。未选区域及原始全部候选保留审计。

旧任务若没有新编号图，需要重新提取；不根据旧 bbox 猜测像素归属。修改颜色/裁剪导致旧产物失效时，不提供旧候选选择。

## 确定性与安全

- `route_components.select_components(..., return_labels=True)` 返回与面积/坐标排序编号完全对应的逐像素整数图，默认返回值兼容原调用。
- 提取新增固定产物 `source-crop.png`（EXIF 方向校正后的原图裁剪）与 `component-ids.png`（RGB 三字节无损编号，背景 0）。保留 `candidates-mask.png` 与审计报告；不以 bbox 填充候选。
- Web 仅增加上述两个固定白名单路径，无任意输入路径或任务路径接口；沿用回环 Host/Origin、CSRF、锁、拒绝符号链接/路径穿越、no-store、CSP。
- 浏览器读取的是本机已上传任务的固定裁剪产物，无外部图片/API/地图请求。

## 验证

- `RIDE_REAL_SCREENSHOT="$PWD/local-data/user-original-rgb.png" python3 -m unittest discover -v`：176 项全部通过，包含真实图本地回归，无跳过。
- 新测试覆盖嵌套/重叠 bbox 下真实像素编号可区分、mask 和编号一致性、显式选择后审计编号不变、Node 执行前端像素着色/淡化算法、白名单/路径穿越/符号链接/CSRF。
- `git diff --check` 通过。
- 使用本机 agent-browser/Chromium 检查合成嵌套候选，成功显示 focus=2、局部放大宽度 630；取消 #1 后明确本地预览，点击应用得到 `explicit / selected_ids:[2] / excluded_ids:[1]` 和 `needs_confirmation`，不调用 API。
- 私有本地证据（均在 gitignore 下，不提交）：
  - `outputs/component-ui-evidence/full-tests.log`
  - `outputs/component-ui-evidence/focused-viewport.png`
  - `outputs/component-ui-evidence/browser-check.log`

浏览器 CLI 的分离调用曾丢失页面/产生 CDP 关闭，改用单批处理完成上述验收；截图仅含合成图。
