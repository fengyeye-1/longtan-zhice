# V8.2.10 修改文件清单与 ISSUE 对照

| 路径 | 说明 |
| --- | --- |
| backend/app/version.py | 版本升至 8.2.10 / v8210-audit-20261111 |
| backend/app/main.py | `_origin_is_trusted`：配置来源主机名锁定，避免反代 HTTP 下跨协议同主机被放行 |
| backend/tests/test_extra_v8210_selfcheck.py | 本轮新增反代 HTTP / 跨协议 / 本机同源回归 |
| backend/tests/test_extra_v824_selfcheck.py 等至 v829 | 当前版本号与 BUILD_ID 断言同步为 8.2.10；不放宽既有 CSRF 断言 |
| README.md、一键启动.bat | 当前版本与启动器健康检查改到 8.2.10；补充反代 HTTP 下来源锁定说明 |
| 变更说明_V8.2.10.md | 本说明 |
| 修改文件清单与ISSUE对照_V8.2.10.md | 本对照表 |

未改：`backend/app/calc/`、前端源码与 `frontend/dist`、Dockerfile、render.yaml，以及 V8.2.9 及更早的变更说明 / ZIP / SHA。
