# V8.2.8 修改文件清单与 ISSUE 对照

| 文件 | 对应 ISSUE |
| --- | --- |
| backend/app/version.py | 版本升至 8.2.8 / v828-audit-20261110 |
| backend/app/main.py | P0-01 Cookie 签发；P1-01 CSRF/Origin；P1-06 接口返回；P2 Cookie Secure/SameSite；注销接口；电价互斥；上传频率 |
| backend/app/services/store.py | P0-01/P0-02 会话与 Token 权限；P1-05 写盘回滚；P1-06 备份警告；P2 票据过期/撤销；LTZS_DATA_DIR |
| backend/app/services/import_data.py | P1-02 映射校验；P2 XLSX 资源限制与上传频率 |
| backend/app/services/change_validation.py | P1-03 失效时间戳 |
| backend/app/services/compute.py | P1-03 报告正文失效报价口径 |
| backend/app/services/report_sections.py | P1-03 HTML/模块状态与历史报价 |
| backend/app/models/schema.py | QuoteChoice.invalidated_at |
| frontend/src/views/Enterprise.vue | P1-02 前端查重；P1-07 电价策略入口 |
| frontend/src/views/Upload.vue | P1-02 前端查重（未挂载页同步） |
| frontend/src/views/Report.vue | P1-03 当前/历史报价；P1-04 预览错误不再回退 |
| frontend/src/views/CarbonRoi.vue | P1-03 失效报价不再标为当前采用 |
| frontend/dist/* | 由 npm ci && npm run build 重建 |
| README.md、render.yaml、一键启动.bat | 版本号、权限模型、部署风险 |
| backend/tests/conftest.py | 隔离临时库 |
| backend/tests/test_extra_v828_selfcheck.py | 新增 P0/P1/P2 回归 |
| backend/tests/test_extra_v824/825/826/827_selfcheck.py | 仅同步版本字符串 |
| backend/tests/test_independent_v823_gaps.py、test_independent_v824_remaining.py、test_extra_v825/826_selfcheck.py | 子进程重启用例改为携带合法签名 Cookie（不断言放宽） |
