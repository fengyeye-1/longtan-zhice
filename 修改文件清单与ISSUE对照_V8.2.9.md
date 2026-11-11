# V8.2.9 修改文件清单与 ISSUE 对照

| 路径 | 说明 |
| --- | --- |
| backend/app/version.py | 版本升至 8.2.9 / v829-audit-20261111 |
| backend/app/services/store.py | 切断旧格式 Cookie；会话清扫与容量上限；项目 Token 轮换/撤销 |
| backend/app/main.py | 健康检查不建身份；scheme+host+port Origin；退出与凭证接口；数据边界说明 |
| backend/tests/test_extra_v829_selfcheck.py | 本轮新增 15 项回归 |
| backend/tests/test_extra_v824_selfcheck.py 等 | 版本号断言同步为 8.2.9 |
| backend/tests/test_v8_p0_g01_g20.py | 取会话改走 /api/session，不断言放宽 |
| frontend/src/api/client.ts | 退出 / 轮换 / 撤销接口，并清理本机凭证 |
| frontend/src/stores/app.ts | 退出后清理页面私有缓存 |
| frontend/src/App.vue | 「退出当前工作区」与凭证操作入口 |
| frontend/src/views/Help.vue | 退出、凭证与免费实例边界说明 |
| frontend/src/views/Retrofit.vue | 措施表窄屏卡片 |
| frontend/src/views/Devices.vue | 卡片排版，避免中文逐字换行 |
| frontend/src/views/Overview.vue、Enterprise.vue、Report.vue、CarbonRoi.vue | 宽表最小宽度 + 可见横向滚动 |
| frontend/src/style.css | 全局宽表滚动样式 |
| frontend/dist/ | 干净构建产物 index-DOlRAUFF.js / index-DgpQaokn.css |
| README.md、一键启动.bat、Dockerfile、render.yaml | 版本、Cookie 与持久化说明 |
| 变更说明_V8.2.9.md | 本说明 |
