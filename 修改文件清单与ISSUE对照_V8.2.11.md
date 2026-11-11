# V8.2.11 修改文件清单与 ISSUE 对照

| 文件 | 说明 |
| --- | --- |
| backend/app/version.py | 版本升至 8.2.11 / v8211-audit-20261111 |
| backend/app/services/store.py | 访客授权与所有权分离；恢复版本保留原所有权且仅所有者可恢复；轮换/撤销失效访客授权且不泄露新 Token；所有权管理必须按项目验证 owner_session；API 序列化仅所有者可见 access_token；数据准备项显示继承小时来源 |
| backend/app/main.py | 使用 `case_public_dict` 输出案例；会话说明写清访客≠所有者、轮换/撤销失效访客授权、恢复版本不改所有权 |
| backend/app/services/compute.py | 小时对照无效情景输出「超过该设备可用运行小时，不适用」；有效小时继承自设备时记录来源；`full_recalculate` 使用公开序列化。未改计算公式 |
| backend/app/services/report_sections.py | CSV 小时对照不再输出 None 占位，改写不适用说明 |
| frontend/src/views/Overview.vue | 网页预览对无效小时情景显示不适用说明 |
| frontend/src/views/Report.vue | 同上 |
| frontend/src/views/CarbonRoi.vue | 同上 |
| frontend/dist/assets/index--1-t8GYI.js、frontend/dist/assets/index-DDi3fy8t.css | 本轮重新构建的前端产物 |
| backend/tests/test_extra_v8211_selfcheck.py | 新增 F01–F16 及超额自检 |
| backend/tests/test_extra_v8210_selfcheck.py 等至 v829 | 当前版本号与 BUILD_ID 断言同步为 8.2.11；`test_a06` 按 BLOCKER-03 将访客轮换改为 403，其余既有断言不放宽 |
| README.md、一键启动.bat | 当前版本与启动器健康检查改到 8.2.11；权限文档补充访客/轮换撤销/恢复版本说明；保留 8.2.10 CSRF 历史说明与 `一键打开公网.bat` |
| 变更说明_V8.2.11.md | 本说明 |
| 修改文件清单与ISSUE对照_V8.2.11.md | 本对照表 |
