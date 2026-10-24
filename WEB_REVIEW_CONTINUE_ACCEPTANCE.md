# 网页候选应用后继续模型复核

## 使用与兼容

- “应用选择并重新生成”保持仅本地执行，不继承创建上传授权，不触发模型或地图请求。
- 状态区明确显示“候选已应用、本地中心线已生成，尚未模型复核”，同时列出实际执行来源与本轮成功模型响应数。`mode: api`仅为任务配置。
- API任务候选区新增“授权模型复核并继续”，旁边是**单独的本次上传授权**，不是第二组候选确认。默认不勾选，完成、失败、刷新后取消，不继承创建授权。
- 继续只发`{review_continue:true,consent_upload:true}`，不读取回答、颜色、裁剪、仅本地、文件或地图表单。CLI新增`resume --review-continue --consent-upload`；无需新answer，保留当前任务已保存的用户component_ids，使用ConsentedPlanner进入当前证据观察/复核/补孔提议。
- 未授权HTTP 403；离线任务/与local_only冲突拒绝；复核请求混入参数修改拒绝。现有CSRF、Origin、Host、loopback、产物白名单不变。
- 复核状态仅匹配当前图像版本。模型失败、只提出问题/补孔不能显示“已复核一致”。真实GPS与路线真值始终未验证。
- 旧task.json没有新字段仍可刷新、继续，无需迁移、重建、重传截图或重选。来源未知时保守显示未知，不按mode猜测。用户原任务`325da222-9fb4-430d-8fee-1a6cf569f471`未出现在本工作区，未修改其数据；升级实际网页进程后可粘贴原编号刷新，用原任务继续。

## 验证范围

`test_web_review_continue.py`新增4项：HTTP→真实CLI子进程→loopback模型，API创建→歧义候选→用户选1/2本地重建（模型请求零新增）→同任务本次授权→当前原图、当前提取与保留选择进入模型→observe/review/deliver；掩码/骨架哈希不变。另覆盖歧义具体追问、补孔提议默认零修改、旧任务无字段、未授权/本地任务边界、CSRF/Origin/Host、失败不假复核、真实前端处理器忽略陈旧表单与创建授权。

浏览器使用agent-browser真实DOM完成创建/候选勾选、本地应用、未授权报错、授权后继续。环境中浏览器CDP多次断开，保留同任务刷新恢复，并使用DOM `.click()`执行页面真实处理器；未用mock DOM冒充浏览器HTTP路径。最终本地与复核截图已目视检查。陈旧`color=#ffffff,crop=[0,0,1,1],localOnly=true`未影响复核，1/2仍选中，本次授权自动取消。仅合成图片与127.0.0.1模型mock，无真实外部上传/地图。

证据均在忽略目录`local-data/`：
- `review-continue-browser-local.png`：醒目显示本地中心线、尚未复核、成功响应0。
- `review-continue-browser-reviewed.png`：当前版本模型一致自报、成功响应3，非真值验证。
- `review-continue-browser-entry.png`：新入口及单独授权。
- `review-continue-browser-*-verdict.json`、`review-continue-browser-*.json`：DOM与交互快照。
- `review-continue-browser-calls.jsonl`：本机mock实际请求（含合成图，不入Git）。
- `review-continue-all-tests-release.log`：双RIDE本地原图环境全量回归。

未修改任何mask选项、提取阈值、补孔约束、中心线算法或地图逻辑。真实模型能否理解/修好用户截图不在本次授权范围，mock只验证编排、安全门与显示，不证明视觉效果。

最终双图全量命令：
```sh
RIDE_REAL_SCREENSHOT="$PWD/local-data/user-original-rgb.png" RIDE_TURNBACK_SCREENSHOT="$PWD/local-data/turnback-original-rgb.png" PYTHONPATH=src python3 -m unittest discover -q
```

最终结果：
```text
----------------------------------------------------------------------
Ran 252 tests in 153.204s

OK
```

单独新增4项全部通过，`py_compile`与`git diff --check`通过。
