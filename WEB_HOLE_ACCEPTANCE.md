# 网页任务内逐孔纠错验收

## 使用与范围

不再需要用户运行逐孔脚本或手动运行中心线：在现有网页任务中完成颜色/裁剪提取，若提示分量歧义，先通过现有候选像素多选明确保留的分量；然后点击“生成当前任务逐孔预览”。编号总图与每孔原图/精确标红/二值掩码三联放大均来自该任务当前裁剪。点击图片可独立查看放大原图。所有孔默认不选，勾选只改变本地待应用预览；必须勾选“已逐孔对照未叠加原图”才能应用。

应用只补所选有界封闭小孔（每孔≤256像素、最长边≤32），更新当前 mask、叠加图、提取报告并重新生成中心线。旧候选像素映射与结构诊断标成失效；旧配准和道路匹配标志撤销。没有删除原前景、连接开放图标断裂、按环数自动选择或套用工程示例选择。混合边界孔暂不提供局部画笔，网页明确要求不能安全整孔填时保留。不验证连接关系、重复通行顺序、骑行方向或 GPS。

## 绑定、安全、审计

- 原图RGB像素、尺寸、二值掩码 SHA256 绑定；服务器生成复核 UUID，任务内只接受最新未消费候选。再次生成、原图/掩码变化、提取失效或应用成功后，旧选择拒绝。
- 与现有网页共用 loopback、Host/Origin/Fetch-Metadata、CSRF、变更锁和全任务树 symlink 检查；参数不接受路径、任意坐标或画笔指令。
- 动态产物仅服务端 UUID 目录、固定审计文件名及1—128孔图片可读取；静态 HTML 不通过服务公开，避免绕过页面脚本 CSP。
- 输入沿用逐孔工具的100万像素、20MB、128孔限制；每任务最多生成32次预览。空选、重复ID、布尔ID、未知ID、未确认全部拒绝。
- `hole-reviews/<uuid>/` 独立保存每次预览；`hole-audit/<uuid>/` 独立保存原图、前后掩码、选择文件、前后中心线JSON、原始提取报告、前后对照和指标，不修改上传输入或覆盖先前审计。当前状态始终 `verified=false`。

## 真实浏览器证据

使用本机 Chrome + Playwright 执行真实 DOM 操作（预先阅读 agent-browser 技能；该 CLI 的跨命令 CDP 连接不稳定，故实际验收改用已有本地 Playwright，无下载）。全程只访问 loopback，无外部视觉API或地图下载。

- 从网页上传 `turnback-original-rgb.png`，离线创建任务，填写 `#6A778B` 与 `[240,1140,1000,1580]`，仅本地继续。
- 明确勾选两个候选分量并重新生成，自动中心线仍2分量、cycle_rank=7。
- 点击逐孔预览，7孔全部图片加载成功，默认选中0孔，应用禁用。
- 勾选2、4及原图确认，按钮应用成功；仅新增300像素，中心线2分量、cycle_rank=5、1227骨架像素；`verified=false`，页面明确“已应用人工 mask 修正…未验证…GPS”。
- 相同选择重复提交 HTTP 409。页面无 JavaScript 异常。

证据全在忽略目录 `local-data/web-hole-browser/`：`browser.cjs`、`browser.log`、`default-state.json`、`selected-state.json`、`applied-state.json`、`repeat-rejected.json` 及 `default-preview.png`、`selected-preview.png`、`applied.png`。测试网页任务在该目录 `tasks/` 内，未接触已有用户任务。

**这不是“自动折返已修好”。** 1号真实回转孔、3号混合边界孔及5—7间隙碎孔保留，起点图标开放断裂保留。剩余环数不作成功标准；真图连接和重复骑行顺序仍不可确定。

## 自动回归

新增 `test_web_holes.py`，用真实本地HTTP覆盖默认不修正、并发重复提交仅一次生效、精确补孔、前后审计、中心线更新、重复/旧候选/source或mask变化拒绝、失效提取拒绝、预览上限、CSRF/Host/Origin、路径白名单和 symlink、真实2/4号选择。原有灰图与窄U/发夹/候选UI断言不放宽。

```sh
RIDE_REAL_SCREENSHOT="$PWD/local-data/user-original-rgb.png" \
RIDE_TURNBACK_SCREENSHOT="$PWD/local-data/turnback-original-rgb.png" \
python3 -m unittest discover -q
python3 -m py_compile ride_web.py ride_agent.py web_hole_review.py test_web_holes.py
git diff --check
```

全量200项通过，无跳过；日志 `local-data/web-hole-final-tests.log`。所有图片、任务、日志与环境文件均不提交。
