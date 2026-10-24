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
PYTHONPATH=src python3 -m unittest discover -q
python3 -m py_compile src/ride_agent.py src/ride_web.py src/ride_errors.py test_error_diagnostics.py
git diff --check
```

双截图环境全量 **210 项通过、无跳过**（88.235 秒）；最终聚焦诊断/网页提示 17 项通过。Python 编译与 `git diff --check` 通过。全量日志：`local-data/error-diagnostics-all-tests.log`（不提交）。

## 追加：旧图全部失败反馈的 API 链路对照

不能以本地 mask 单测证明 API 新建链路正常。新增 `test_api_start_e2e.py`：使用真实 loopback 网页 HTTP 创建任务、真实 CLI 子进程、真实 APIPlanner 请求与响应解析，模型端由另一个 loopback HTTP 服务模拟。没有 mock CLI、提取器或中心线。测试覆盖先检查颜色再提取、两张历史原图 pixels 新建，以及模型 `tool_calls` 响应的安全拒绝。历史原图参数是已知解释性回归参数，不是猜测用户此次实际参数。

将同一套测试复制到 `git archive` 隔离导出的两个旧版本（全部在忽略目录 `local-data/baseline-api-e2e`）：

- `796b9aa`：合成图检查颜色→提取、历史原图新建、折返原图新建，共 3 项通过。
- `0217846`：相同 3 项通过。
- 当前版本：上述 3 项与 `tool_calls` 响应分类共 4 项通过。
- 两旧版本的 `tool_calls` 反例均复现 `status=error` 且错误类别为 `extraction_required`，当前版本明确返回 `api_response_invalid`。

版本差异证据：`796b9aa..0217846` 的 `ride_agent.py` 仅在失效提取时新增清理 `hole_review`、`hole_correction` 两行，没有变更模型请求、响应解析或首次提取调用。更早至 `2e417da` 的比较发现分量歧义停顿与显式选择是新增行为，但该行为生成候选且进入等待用户，不足以解释此次 `error + artifacts[]`。

当前 API 协议一直要求 `message.content` 内严格 JSON 动作，未声明 `tools` schema。因此未擅自加入未经约定的 `tool_calls` 执行兼容层；`tool_calls` 在两个旧版本同样不支持，不能把它标为此次新回归，也不能声称用户服务一定返回这种结构。若脱敏错误证明是响应结构问题，还需最小结构证据：HTTP 状态、`message.content` 类型、是否存在 `tool_calls`、`finish_reason`（仅字段类型/存在性，不要 arguments、响应全文或截图）。

结论仅限：在确定的本机配置与 mock 模型动作下，两个历史版本和当前版本均能走通真实 API 新建链路；已经复现并修正统一误报，但尚未复现用户环境的“全部旧图失败”底层原因。需继续等待其脱敏错误，不能归咎用户参数。

追加最终验收：双截图全量 **214 项通过、无跳过**，105.598 秒；日志 `local-data/error-api-e2e-all-tests.log` 不提交。新增测试编译和 `git diff --check` 通过。

## 用户确切错误已确认：服务进程配置缺失

用户提供旧任务 `error: Set RIDE_API_KEY and RIDE_MODEL; use a vision-capable model`、`error_category: task_processing_failed`。这条异常仅在 `APIPlanner.__init__` 的 `if not self.key or not self.model` 触发，因此能确定：**当时 CLI 进程看到的两项环境变量中至少一项缺少或为空**，不能推断两项都缺少。ConsentedPlanner 在首次需要视觉模型时才构造 APIPlanner，读取继承自网页进程的 `os.environ`；模型调用尚未发生，不是图片颜色或裁剪导致。

新增 `api_configuration_missing` 固定分类，并在 API 网页创建任务前进行同样的本地配置预检：不发网络请求、不保存截图/创建任务、不输出配置值。保留延迟客户端以免影响仅本地恢复。没有引入 dotenv、自动 source `.env` 或改变环境加载策略。测试逐一覆盖缺 key、缺 model、两项缺失、空字符串；另用真实 CLI 子进程覆盖三种缺字段路径。已有测试统一使用明确 mock 配置，不借用生产密钥。

已有旧 task.json 不按异常原文迁移或猜测；新的 API 创建会明确提示配置缺失。父代理将提供安全只读检查及在设置/导出变量的同一终端重启网页说明，无需用户发送密钥。

配置预检最终验收：双截图环境 **216 项通过、无跳过**（108.978 秒），聚焦诊断 **12 项通过**；编译、`git diff --check` 通过。日志 `local-data/config-preflight-tests-final.log` 不提交。
