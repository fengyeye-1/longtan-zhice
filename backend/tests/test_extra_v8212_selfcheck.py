"""V8.2.13 报告展示自检：空电价来源不加空括号，封面数字不用科学计数法。"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store
from app.services.compute import _format_tariff_usage_line
from app.services.report_sections import format_cover_number
from app.version import APP_VERSION, BUILD_ID

INAPPLICABLE = "超过该设备可用运行小时，不适用"
SCI_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])\d+(?:\.\d+)?e[+-]?\d+")
# 报告里的随机实体 ID（如 measure=rf-...3e9...、rf-4114e87942...）可能被误判为科学计数法。
# 判定前先去掉 case=/device=/measure= 以及 rf-/dev-/case- 标识，只检查剩余展示文本。
_ENTITY_ID_RE = re.compile(
    r"(?i)\b(?:case|device|measure)\s*=\s*[A-Za-z0-9_-]+"
    r"|\b(?:rf|dev|case)-[A-Za-z0-9_-]+"
)


def _text_without_entity_ids(text: str) -> str:
    return _ENTITY_ID_RE.sub(" ", text)


def _display_fragments(text: str) -> str:
    """HTML 封面字段、省电/节费等数值行，以及去掉实体 ID 后的全文。"""
    parts: list[str] = []
    parts.extend(re.findall(r"<td>[^<]*</td>", text))
    keys = ("省电", "节费", "节约电费", "所用电价", "减排", "kWh", "tCO2", "元/kWh")
    for line in text.splitlines():
        if any(k in line for k in keys):
            parts.append(line)
    parts.append(_text_without_entity_ids(text))
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()
    store.SESSION_TTL_SECONDS = 7 * 86400
    store.ANON_SESSION_IDLE_SECONDS = 30 * 60
    store.MAX_ANON_SESSIONS = 200
    store._LAST_SESSION_SWEEP_TS = 0
    assert "ltzs_pytest_" in str(store.DB_PATH) or "ltzs_pytest_" in str(store.ROOT)


def _project(client: TestClient, name: str = "8212公网展示核验企业"):
    r = client.post("/api/cases/empty", json={"name": name})
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    cid = body["case"]["case_id"]
    tok = body.get("project_token") or r.headers.get("X-Project-Token")
    assert tok
    return r, cid, tok


def _prepare_e2e_case(client: TestClient, *, use_reference_tariff: bool = False):
    _project(client, "公网页面核验空白企业")
    d = client.post(
        "/api/devices",
        json={"name": "公网页面核验球磨", "device_type": "球磨", "power_kw": 150, "hours": 4000},
    )
    assert d.status_code == 200, d.text[:300]
    did = [x for x in d.json()["case"]["devices"] if x["name"] == "公网页面核验球磨"][0]["id"]
    m = client.post(
        "/api/retrofits",
        json={
            "name": "降低功率 20kW",
            "device_id": did,
            "power_drop_kw": 20,
            "investment_cny": 100000,
            "annual_maintenance_cny": 0,
            "select": True,
        },
    )
    assert m.status_code == 200, m.text[:300]
    payload = {"hours": 4000, "emission_factor": 0.4471}
    if use_reference_tariff:
        payload["use_jinhui_tariff_reference"] = True
        payload["clear_energy_price"] = True
    else:
        payload["energy_price"] = 0.60
        payload["use_jinhui_tariff_reference"] = False
    u = client.put("/api/scenario", json=payload)
    assert u.status_code == 200, u.text[:300]
    return u.json()


def _assert_no_bad_placeholders(text: str) -> None:
    assert "None kWh" not in text
    assert "None t" not in text
    assert "None tCO2" not in text
    assert "元/kWh（）" not in text
    assert "NaN" not in text
    # 真实展示错误仍按全文检查（含加号的 8e+04 不会出现在十六进制 ID 中）。
    assert "8e+04" not in text
    assert "8e+4" not in text
    assert "8E+04" not in text
    assert "8E+4" not in text
    scanned = _display_fragments(text)
    assert not SCI_RE.search(scanned), f"scientific notation in report: {SCI_RE.findall(scanned)}"


def _cover_kwh(html: str) -> str:
    m = re.search(r"<th>预计一年省电</th><td>([^<]+)</td>", html)
    assert m, html[:800]
    return m.group(1)


def test_health_version_is_8212():
    c = TestClient(app, raise_server_exceptions=False)
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.13" == APP_VERSION
    assert h["frontend_expected"] == "8.2.13"
    assert h["build_id"] == BUILD_ID == "v8213-audit-20261111"


def test_format_helpers_price_parens_and_cover_numbers():
    assert _format_tariff_usage_line(0.6, "") == "所用电价：0.6 元/kWh"
    assert _format_tariff_usage_line(0.6, "   ") == "所用电价：0.6 元/kWh"
    assert _format_tariff_usage_line(0.6, None) == "所用电价：0.6 元/kWh"
    assert _format_tariff_usage_line(0.6, "用户手动覆盖") == "所用电价：0.6 元/kWh（用户手动覆盖）"
    assert _format_tariff_usage_line(0.454803, "", is_reference=True) == (
        "所用电价：0.454803 元/kWh（金徽参考电价（试算））"
    )
    assert format_cover_number(80000) in {"80,000", "80000", "80000.0"}
    assert format_cover_number(80000.0) in {"80,000", "80000", "80000.0"}
    assert format_cover_number(35.768) == "35.768"
    assert format_cover_number(48000) in {"48,000", "48000", "48000.0"}
    for n in (80000, 48000, 40000, 35.768, 0.6, 1_000_000, 1234567.89, 0.4471, 2.5):
        text = format_cover_number(n)
        assert not SCI_RE.search(text), (n, text)
        assert "e+" not in text.lower() and "e-" not in text.lower()


def test_export_empty_tariff_source_omits_parens_and_cover_is_readable():
    a = TestClient(app, raise_server_exceptions=False)
    body = _prepare_e2e_case(a, use_reference_tariff=False)
    sa = (body.get("results") or {}).get("selected_assessment") or {}
    assert abs(float(sa["saved_kwh"]) - 80000.0) < 1e-6
    assert abs(float(sa["co2_reduction_t"]) - 35.768) < 0.00051
    assert abs(float(sa["annual_cost_saving_cny"]) - 48000.0) < 0.01
    tariff = (sa.get("source_trace") or {}).get("tariff") or {}
    assert tariff.get("value") is not None
    assert not (tariff.get("source") or "").strip()
    assert not tariff.get("is_reference")

    preview = a.post("/api/reports/preview", json={"format": "txt", "preset": "full"})
    assert preview.status_code == 200, preview.text[:300]
    preview_text = preview.json().get("text") or ""
    assert "所用电价：0.6 元/kWh" in preview_text
    assert "所用电价：0.6 元/kWh（" not in preview_text
    _assert_no_bad_placeholders(preview_text)
    assert INAPPLICABLE in preview_text

    for fmt in ("txt", "csv", "html"):
        r = a.post("/api/reports/export", json={"format": fmt, "preset": "full"})
        assert r.status_code == 200, r.text[:200]
        text = r.text
        assert "80000" in text or "80,000" in text
        assert "35.768" in text
        assert "48000" in text or "48,000" in text
        assert INAPPLICABLE in text
        _assert_no_bad_placeholders(text)
        if fmt in ("txt", "html"):
            assert "所用电价：0.6 元/kWh" in text
            assert "所用电价：0.6 元/kWh（" not in text
            assert "元/kWh（）" not in text
        if fmt == "html":
            cover = _cover_kwh(text)
            assert cover.strip() in {"80,000 kWh", "80000 kWh", "80000.0 kWh"}
            assert "e+" not in cover.lower()
            assert "8e+04" not in text
            # 正文仍用普通十进制，不因封面改写计算值
            assert "一年预计省电：80000.0 kWh" in text
            assert "35.768" in text
            assert "一年预计节约电费：48000.0 元" in text


def test_export_keeps_parentheses_when_tariff_source_present():
    a = TestClient(app, raise_server_exceptions=False)
    body = _prepare_e2e_case(a, use_reference_tariff=True)
    sa = (body.get("results") or {}).get("selected_assessment") or {}
    tariff = (sa.get("source_trace") or {}).get("tariff") or {}
    src = (tariff.get("source") or "").strip()
    assert src, tariff
    r = a.post("/api/reports/export", json={"format": "txt", "preset": "full"})
    assert r.status_code == 200, r.text[:200]
    text = r.text
    assert "元/kWh（）" not in text
    assert re.search(r"所用电价：.+ 元/kWh（.+）", text), text[text.find("所用电价") : text.find("所用电价") + 80] if "所用电价" in text else text[:400]
    assert f"元/kWh（{src}）" in text
    preview = a.post("/api/reports/preview", json={"preset": "full"})
    assert preview.status_code == 200
    ptxt = preview.json().get("text") or ""
    assert f"元/kWh（{src}）" in ptxt
    assert "元/kWh（）" not in ptxt
