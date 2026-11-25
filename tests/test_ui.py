"""The dashboard UI ships as plain files: no build, no npm, no external CDN."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "agentbus" / "ui"
PAGES = (
    "pages/now.js",
    "pages/thread.js",
    "pages/mailboxes.js",
    "pages/history.js",
    "pages/stats.js",
)
SCRIPTS = ("app.js", *PAGES)
LOCAL_REF = re.compile(r'(?:src|href)="(?!https?:|#|data:)([^"]+)"')


def test_index_links_every_page(client: TestClient) -> None:
    page = client.get("/ui/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert LOCAL_REF.findall(page.text) == ["style.css", *SCRIPTS]
    for name in ("now", "thread", "mailboxes", "history", "stats"):
        assert f"#/{name}" in page.text, f"no navigation entry for {name}"


def test_every_linked_asset_is_served(client: TestClient) -> None:
    linked = LOCAL_REF.findall(client.get("/ui/").text)
    assert linked, "index.html links nothing"
    for asset in linked:
        response = client.get(f"/ui/{asset}")
        assert response.status_code == 200, asset
        content_type = response.headers["content-type"]
        if asset.endswith(".css"):
            assert "text/css" in content_type, asset
        else:
            assert "javascript" in content_type, asset


def test_app_exports_register_page(client: TestClient) -> None:
    assert "registerPage" in client.get("/ui/app.js").text


STUB_TEXT = "скоро — T7"


@pytest.mark.parametrize("page", PAGES[2:])
def test_t7_pages_replace_the_stubs(client: TestClient, page: str) -> None:
    # T6 shipped stubs («скоро — T7»); T7 replaces the bodies and keeps the
    # registration contract.
    text = client.get(f"/ui/{page}").text
    assert "registerPage" in text
    assert STUB_TEXT not in text


@pytest.mark.parametrize("page", PAGES[2:])
def test_t7_pages_build_dom_without_inner_html(client: TestClient, page: str) -> None:
    # Data from the API must only reach the page as text nodes, never as markup.
    assert "innerHTML" not in client.get(f"/ui/{page}").text


def test_ui_does_not_load_anything_from_the_network(client: TestClient) -> None:
    for asset in ("", *SCRIPTS, "style.css"):
        text = client.get(f"/ui/{asset}").text
        assert "http://" not in text, f"{asset or 'index.html'} references an external URL"
        assert "https://" not in text, f"{asset or 'index.html'} references an external URL"


@pytest.mark.parametrize(
    "path",
    sorted(UI_DIR.rglob("*.js")),
    ids=lambda path: str(path.relative_to(UI_DIR)),
)
def test_javascript_parses(path: Path, tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    module = tmp_path / "module.mjs"
    module.write_bytes(path.read_bytes())
    result = subprocess.run(
        [node, "--check", str(module)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
