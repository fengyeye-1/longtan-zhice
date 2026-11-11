# V8.2.12 修改文件清单与 ISSUE 对照

| 文件 | 说明 |
| --- | --- |
| backend/app/version.py | 版本升至 8.2.12 / v8212-audit-20261111 |
| backend/app/services/compute.py | 电价展示行：来源为空时省略括号，有来源时保留「元/kWh（来源）」；不编造来源。未改计算公式 |
| backend/app/services/report_sections.py | HTML 封面/摘要数字改为普通十进制，避免 `:,.4g` 把 80000 写成 8e+04 |
| backend/tests/test_extra_v8212_selfcheck.py | 新增空括号、封面数字与公网核验情景导出/预览自检 |
| backend/tests/test_extra_v8211_selfcheck.py 等至 v824 | 当前版本号与 BUILD_ID 断言同步为 8.2.12；其余既有断言不放宽 |
| README.md、一键启动.bat | 当前版本与启动器健康检查改到 8.2.12；保留 8.2.10 CSRF、8.2.11 权限说明与 `一键打开公网.bat` |
| 变更说明_V8.2.12.md | 本说明 |
| 修改文件清单与ISSUE对照_V8.2.12.md | 本对照表 |
