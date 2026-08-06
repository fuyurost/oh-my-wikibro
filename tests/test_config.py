from pathlib import Path

import pytest

from omwb.config import SiteConfig, load_sites


def test_formats_validation():
    with pytest.raises(ValueError):
        SiteConfig(url="https://a.com", formats=["docx"])
    assert SiteConfig(url="https://a.com", formats=["all"]).formats == ["md", "json", "jsonl", "pdf"]


def test_site_name():
    assert SiteConfig(url="https://docs.python.org/zh-cn/3/", name="py").site_name == "py"
    assert SiteConfig(url="https://docs.python.org/zh-cn/3/").site_name == "docs.python.org"


def test_path_allowed():
    s = SiteConfig(url="https://a.com", include=["/docs/*"], exclude=["/docs/secret*"])
    assert s.path_allowed("/docs/intro")
    assert not s.path_allowed("/other/intro")
    assert not s.path_allowed("/docs/secret/x")
    # 内置噪音
    assert not s.path_allowed("/search")


def test_load_sites(tmp_path: Path):
    cfg = tmp_path / "sites.yaml"
    cfg.write_text(
        "sites:\n"
        "  - url: https://a.com/docs\n"
        "    formats: [md, pdf]\n"
        "    exclude: ['/api*']\n",
        encoding="utf-8",
    )
    sites = load_sites(cfg)
    assert len(sites) == 1
    assert sites[0].formats == ["md", "pdf"]

    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_sites(bad)
