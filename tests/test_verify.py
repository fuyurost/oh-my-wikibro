import json
from pathlib import Path

from omwb.verify import verify_site

URLS = ["https://a.com/docs/intro", "https://a.com/docs/advanced"]
# page_rel_path("https://a.com/docs/intro") == "docs/intro"


def _write_site(tmp_path: Path, *, urls=URLS, formats=None, files=None, corpus=None) -> Path:
    """构造站点目录:manifest + 指定文件;files 为 {相对路径: 内容}。"""
    site = tmp_path / "demo"
    site.mkdir(exist_ok=True)
    manifest = {"name": "demo", "url": "https://a.com", "urls": list(urls), "pages": len(urls)}
    if formats is not None:
        manifest["formats"] = list(formats)
    (site / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for rel, content in (files or {}).items():
        p = site / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if corpus is not None:
        (site / "corpus.jsonl").write_text(corpus, encoding="utf-8")
    return site


def _complete_files():
    return {
        "md/docs/intro.md": "# a\n",
        "md/docs/advanced.md": "# b\n",
        "json/docs/intro.json": "{}",
        "json/docs/advanced.json": "{}",
    }


def test_verify_ok(tmp_path: Path):
    _write_site(tmp_path, files=_complete_files())
    rep = verify_site(tmp_path, "demo")
    assert rep["ok"] is True
    assert rep["missing_files"] == []
    assert rep["empty_files"] == []
    assert rep["files"]["md"] == {"expected": 2, "found": 2, "missing": 0, "empty": 0}
    assert rep["files"]["json"] == {"expected": 2, "found": 2, "missing": 0, "empty": 0}


def test_verify_missing(tmp_path: Path):
    _write_site(tmp_path, files={"md/docs/intro.md": "# a\n"})
    rep = verify_site(tmp_path, "demo")
    assert rep["ok"] is False
    assert "md/docs/advanced.md" in rep["missing_files"]
    assert "json/docs/intro.json" in rep["missing_files"]
    assert rep["files"]["md"] == {"expected": 2, "found": 1, "missing": 1, "empty": 0}
    assert rep["files"]["json"] == {"expected": 2, "found": 0, "missing": 2, "empty": 0}


def test_verify_empty(tmp_path: Path):
    _write_site(tmp_path, files={
        "md/docs/intro.md": "",  # 空文件
        "md/docs/advanced.md": "# b\n",
        "json/docs/intro.json": "{}",
        "json/docs/advanced.json": "{}",
    })
    rep = verify_site(tmp_path, "demo")
    assert rep["ok"] is False
    assert rep["empty_files"] == ["md/docs/intro.md"]
    assert rep["missing_files"] == []
    assert rep["files"]["md"]["empty"] == 1


def test_verify_pdf(tmp_path: Path):
    _write_site(tmp_path, formats=["md", "pdf"], files={
        "md/docs/intro.md": "# a\n",
        "md/docs/advanced.md": "# b\n",
        "pdf/docs/intro.pdf": "x",
        "pdf/docs/advanced.pdf": "x",
    })
    rep = verify_site(tmp_path, "demo")
    assert rep["formats"] == ["md", "pdf"]
    assert rep["ok"] is True


def test_verify_formats_defaulted(tmp_path: Path):
    # manifest 无 formats → 按默认 md,json 校验并标记
    _write_site(tmp_path, formats=None, files={"md/docs/intro.md": "# a\n"})
    rep = verify_site(tmp_path, "demo")
    assert rep["formats_defaulted"] is True
    assert rep["formats"] == ["md", "json"]
    assert rep["ok"] is False  # json 文件缺失


def test_verify_corpus_jsonl(tmp_path: Path):
    # corpus 引用存在 → 计入 records,不报缺失
    _write_site(tmp_path, files=_complete_files(),
                corpus=json.dumps({"file": "md/docs/intro.md"}) + "\n")
    rep = verify_site(tmp_path, "demo")
    assert rep["corpus"]["records"] == 1
    assert rep["corpus"]["missing_files"] == []
    assert rep["ok"] is True
    # corpus 引用缺失 → ok 为 False
    _write_site(tmp_path, files=_complete_files(),
                corpus=json.dumps({"file": "md/docs/gone.md"}) + "\n")
    rep = verify_site(tmp_path, "demo")
    assert rep["corpus"]["missing_files"] == ["md/docs/gone.md"]
    assert rep["ok"] is False
    # corpus 引用空文件 → 报空
    _write_site(tmp_path, files={**{k: v for k, v in _complete_files().items()},
                                 "md/docs/intro.md": ""},
                corpus=json.dumps({"file": "md/docs/intro.md"}) + "\n")
    rep = verify_site(tmp_path, "demo")
    assert rep["corpus"]["empty_files"] == ["md/docs/intro.md"]
    assert rep["ok"] is False


def test_verify_no_manifest(tmp_path: Path):
    rep = verify_site(tmp_path, "demo")
    assert rep["ok"] is False
    assert "error" in rep
    assert "manifest" in rep["error"]
