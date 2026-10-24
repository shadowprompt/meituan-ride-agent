# API 失败诊断与同任务恢复验收

## 修复边界

- 基线 `a7984d9`；开始时工作树干净。仅修改错误分类、Agent/网页恢复及相应测试，不改变颜色阈值、像素算法或GPS能力。
- 实际调用路径为 `ConsentedPlanner → APIPlanner.decide → urllib`。原先 APIPlanner 把 HTTPError 全包装成 `api_http`；而直接抵达分类器的 HTTPError 又会被其父类 URLError 吞成网络失败。现在两条路径都保留正确的安全类别。
- 401/403 → `api_auth`；429 → `api_rate_limit`；5xx → `api_server`；404 等其他 HTTP → `api_http`。连接/DNS/TLS仍兼容 `api_network`，以安全 code 区分；直接或 URLError 包装超时 → `api_timeout`。
- 网页诊断只允许 code、整数 http_status、0–86400 的 retry_after_seconds；不暴露 endpoint/query、请求头、密钥、响应正文或原始异常。旧任务没有这些字段仍可读；错误阶段和配置 preflight 优先级保留。
- 429 支持数字秒及HTTP日期。有效一天内窗口会阻止同任务重新发送；不自动重试、不阻塞等待。超出一天/无效值不输出倒计时，提示在本机核对服务限制。窗口结束后仍必须重新勾选本次授权。
- review_continue 不清除原交付产物清单；失败仅保存安全诊断，不重提取、不丢失 explicit 选择和锁定约束、不使当前 mask/centerline 失效。测试比较失败前后哈希与 current 标记。独立任务的 ambiguous/无中心线状态绝不能认定为旧任务数据丢失。
- “成功模型响应0”只表示未收到有效动作响应，不代表没有发送原图。页面明确提示可能已发送但未收到有效响应。模型复核一致性仍不是真值验证。

## 可重现测试

```sh
PYTHONPATH=src python3 -m unittest test_api_failure_recovery test_error_diagnostics test_web_review_continue
RIDE_REAL_SCREENSHOT="$PWD/local-data/user-original-rgb.png" \
RIDE_TURNBACK_SCREENSHOT="$PWD/local-data/turnback-original-rgb.png" \
PYTHONPATH=src python3 -m unittest discover -v
git diff --check
```

最终结果：focused **22项通过，0 skip**；双图环境全量 **258项通过，0 skip**；`git diff --check` 通过。日志为 `local-data/api-recovery-focused-final.log` 与 `local-data/api-recovery-all-final.log`（前一轮全量256项，后新增连接恢复及网页白名单测试，最终258项，不混用计数）。

全部模型传输均为本地 mock/loopback，包含真实 HTTP 401/403/404/429/500/503、真实连接拒绝、同一任务失败后授权重试成功、限流窗口不发送、未授权零网络、红线字段清除、旧任务兼容和首次无中心线/尚未选候选状态。双图只做本地回归，不向真实外部模型上传。

## 浏览器 DOM 闭环证据（仅本地忽略目录）

使用 agent-browser + 独立 Chrome CDP（9335），网页与 mock 模型均绑定 127.0.0.1。合成图经真实网页上传 → API mock提取并提问 → 勾选1/2 → DOM按钮本地应用生成中心线 → 本次授权复核得到真实HTTP503 → 失败后授权自动清空 → 重新授权，同一任务成功复核。未通过伪造网页JSON或网络拦截替代真实后端；按钮通过DOM click执行。

- 当前验收任务：`c4e40801-cf6b-4bb6-b136-8e3a814eff4f`。
- `local-data/api-recovery-browser-{created,local,failed,recovered}.json`：DOM快照/页面状态。
- `local-data/api-recovery-browser-{failed,recovered}.png`：网页截图。
- `local-data/api-recovery-browser-verdict.json`：父代理可独立核对同任务ID、选择、授权清空、503安全诊断、恢复响应数3和当前产物哈希。
- `local-data/api-recovery-browser2/<任务ID>/task`：本地合成任务。证据、日志、截图均不提交Git。
- 浏览器最初默认session曾丢失活动页，切换独立本地CDP后重新完整验收；最终证据来自8892网页，不以初始失败命令充数。

这验证的是诊断与恢复闭环，不代表真实服务凭据、连通性或识别效果已验证。定位实际故障只需当前 task_id、error_category、error_stage、error_diagnostic，不应要求用户发送完整.env、task.json或含密钥日志。
