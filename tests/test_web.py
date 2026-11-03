import json
import re

from fastapi.testclient import TestClient

from metro import demo, webbuild


def test_live_page_build(tmp_path):
    out = webbuild.build_live(tmp_path / "index.html")
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "/*__" not in html.split("<script type=\"module\">")[0]
    assert webbuild.LOCAL_MAP in html and "const DEMO = /*__DEMO__*/null" in html
    assert html.count('<div id="app"') == 1 and "</body>" in html


def test_index_served():
    from metro.api import app
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200 and "מטרופולין" in r.text


def test_demo_page_embeds_data(tmp_path):
    demo.generate(years=1.6)
    html = webbuild.build_demo(tmp_path / "demo.html").read_text(encoding="utf-8")
    assert "/*__" not in html and "</script" not in html.split("const DEMO = ", 1)[1].split(";\n", 1)[0]
    m = re.search(r"const DEMO = (\{.*?\});\n", html, re.S)
    data = json.loads(m.group(1).replace("<\\/", "</"))
    assert data["city"]["mode"] == "demo" and len(data["city"]["companies"]) == 500
    assert len(data["days"]) == webbuild.N_CLOSES and len(data["co"]["PANW"]["c"]) == webbuild.N_CLOSES
    assert data["co"]["PANW"]["card"]["card"]["demo"] is True and data["co"]["MSFT"]["h"]


def test_static_site_is_a_full_page(tmp_path):
    demo.generate(years=1.6)
    html = webbuild.build_site(tmp_path / "index.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and '<link rel="icon"' in html and html.rstrip().endswith("</html>")
    assert "cdn.jsdelivr.net/npm/three@0.169.0/" in html and "/*__" not in html.split("const DEMO = ", 1)[0]
