# V8.2.13 修改文件清单与 ISSUE 对照

| 文件 | 说明 |
| --- | --- |
| backend/app/version.py | 版本升至 8.2.13 / v8213-audit-20261111 |
| backend/app/main.py | 注销后有效 SID 经 `request.state` 在中间件父子任务间共享；路由已下发 Cookie 时中间件不再重复附加；注销响应不得续签已撤销的旧 SID，也不按父任务 ContextVar 回写项目 Token。CSRF 规则未改 |
| backend/app/services/store.py | `maybe_rotate_session_mac` 对已撤销或校验失败的票据拒绝续期/重签 |
| backend/tests/test_v8212_logout_final_gate.py | 终验资料附带的 2 项注销回归（保留原断言并加强） |
| backend/tests/test_extra_v8213_selfcheck.py | 新增 8.2.13 健康检查、注销票据、所有者会话与报告展示自检 |
| backend/tests/test_extra_v8212_selfcheck.py | 修正科学计数法误报：排除实体 ID 后再判定；保留 8e+04 / 空括号等有效断言；版本号同步为 8.2.13 |
| backend/tests/test_extra_v8211_selfcheck.py 等至 v824 | 当前版本号与 BUILD_ID 断言同步为 8.2.13；其余既有断言不放宽 |
| README.md、一键启动.bat | 当前版本与启动器健康检查改到 8.2.13；说明注销只下发新匿名 Cookie；保留 8.2.10 CSRF、8.2.11 权限说明与 `一键打开公网.bat` |
| 变更说明_V8.2.13.md | 本说明 |
| 修改文件清单与ISSUE对照_V8.2.13.md | 本对照表 |
