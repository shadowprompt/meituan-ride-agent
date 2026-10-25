# 唯一高德联网迁移验收

## 本轮范围

- 新建任务默认 `map_provider=amap`；网页仅提供高德。
- 旧 `provider=osm` / 无provider字段任务不自动改写；CLI/API联网继续要求显式amap与本次地图、模型上传双授权。未满足时在模型创建、地图配置预检和任务写入之前拒绝。
- `map_fetch._download/fetch_roads/_fetch_named_roads/fetch_named_roads` 均无条件返回 `osm_network_disabled`；原Overpass传输实现已移除，授权或mock底层传输也不能重新启用。`download_task_map` 和模型 `fetch_map` 动作同样拒绝。
- 高德出错不回退OSM。预算/服务端Key/脱敏/GCJ-02点输出边界不变，不增加道路恢复能力。
- 显式授权迁移后旧map.osm、对话改名保留审计，旧道路证据失效，旧产物文件不删除。CLI `--local-only --map-provider osm` 可读本地获授权文件调试，不调用模型或地图网络。

## Mock回归命令与结果

```sh
PYTHONPATH=src:test python3 -m unittest test_web_simple_ui.SimplePageTests test_amap_only test_amap_provider
# 33 tests, OK
PYTHONPATH=src:test python3 -m unittest test_amap_only test_amap_provider test_api_start_e2e test_api_failure_recovery test_candidate_review test_web_async test_web_review_continue test_pixel_route test_road_corridor
# 85 tests, OK (skipped=5)；其后另增加一个显式授权CLI迁移保留产物用例，包含在上方33项中
```

覆盖默认provider、显式OSM拒绝、无授权旧任务不外呼不修改、显式双授权迁移、审计文件保留、所有旧下载入口拒绝、无OSM失败回退、本地文件调试、GCJ-02结果与本地点预览，以及既有API失败恢复/后台队列/候选复核/本地像素标注/道路本地算法。网页脚本 `node --check`、Python编译、`git diff --check` 通过。

全量旧测试也已实际执行，**并非全绿**：旧测试仍断言OSM联网成功、网页OSM上传/范围下载、默认OSM或模型使用OSM节点构建走廊，这些与新政策冲突；未为获得绿灯而恢复旧联网、注入测试后门或批量跳过旧测试。需后续单独将这些历史场景改写成退役/迁移断言。最后一次全量：374 tests / 100.563秒；failures=74（含subTest条目）、errors=12、skipped=10。精确失败清单保留于开发机 `/tmp/amap-final-full.log`，不要将其误报为全绿。

本轮仅合成图片、回环HTTP与mock外部传输；没有读取真实Key、发真实高德/OSM请求或向外部模型上传图片。不能据此宣称真实高德权限、配额、网络或真实截图路线已验收。
