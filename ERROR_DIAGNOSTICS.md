# 新建 API 任务错误诊断修正

## 已证实的问题与边界

基线 `0217846` 的 `processing_category` 只保留 API 网络/超时类别；其它错误即使已经标记 `task_processing_failed`，只要 `extracted=false` 就显示 `extraction_required`，提示用户修正颜色/裁剪。缺少产物只是结果，不能证明错误原因。离线复现：缺少服务配置、模型非 JSON 响应、本地提取异常，都可能发生在首次有效提取之前。

此次修正是错误分类和可恢复提示，不声称已经确认用户任务 `f26737f9-2276-4fc8-90af-9ccbb2cdbdab` 的真实失败原因。该任务位于用户本机，当前不可访问。历史上存在后缀 PNG、实际 TIFF 的图片，但这不是此次任务原因的证据。

## 修改

- 固定分类区分图片输入校验、API 配置、HTTP 拒绝、API 响应结构、模型动作、本地颜色检查/提取/中心线/配准与道路处理。
- 运行循环记录固定 `error_category` 与 `error_stage`，错误正文改为固定文案，不保存 raw exception、远端响应正文或本地路径。API 网络/超时分类保留；本地工具错误不会冒充 API 网络故障。
- CLI 启动前置失败使用短 JSON 固定代码返回 stderr；网页仅接受已知代码，不回传 stdout/stderr 或按异常文本猜原因。图片格式仍按实际解码检查，TIFF 不因改后缀而通过。
- 网页优先使用结构化类别。旧任务缺少可信分类时使用通用处理失败，不再由 `not extracted` 推断颜色/裁剪问题。明确上传控制点或下载地图的前置条件提示仍保留。
- 新增合成图片和 mock API 回归：正常新建 API pixels 经真实提取与中心线生成进入 `needs_confirmation`，不需要控制点、不表示验证成功；覆盖配置错误、HTTP/非 JSON 响应、网络/超时、本地处理错误、未知旧状态以及伪装 TIFF 的真实 CLI 拒绝。

## 用户侧下一步所需最小证据

旧版本任务只需脱敏后的 `error`、`error_category`；升级后补充 `error_stage`、`status` 即可。不要发送密钥、`.env`、完整 task.json、截图、完整请求或远端响应正文。若旧 `error` 含路径、服务 URL/token 或响应正文，应由用户先删除敏感片段，只保留错误类型/安全短描述。旧任务已经写入的错误正文不会被本次分类修正倒推为真实根因。

未调用外部视觉 API，未下载地图；所有网络测试只用 mock 或 loopback。截图、环境、任务、outputs 和测试日志不提交。

## 验收

```sh
RIDE_REAL_SCREENSHOT="$PWD/local-data/user-original-rgb.png" \
RIDE_TURNBACK_SCREENSHOT="$PWD/local-data/turnback-original-rgb.png" \
python3 -m unittest discover -q
python3 -m py_compile ride_agent.py ride_web.py ride_errors.py test_error_diagnostics.py
git diff --check
```

双截图环境全量 **210 项通过、无跳过**（88.235 秒）；最终聚焦诊断/网页提示 17 项通过。Python 编译与 `git diff --check` 通过。全量日志：`local-data/error-diagnostics-all-tests.log`（不提交）。
