from omwb.config import SiteConfig
from omwb.discover import (
    DROP_EXTENSIONS,
    _check,
    normalize_url,
    parse_llms_links,
)
from omwb.adapters import get_adapter


def test_normalize_url():
    assert normalize_url("https://A.com:443/docs/x/index.html") == "https://a.com/docs/x"
    assert normalize_url("https://a.com/docs/x/") == "https://a.com/docs/x"
    assert normalize_url("https://a.com/") == "https://a.com/"
    assert normalize_url("https://a.com/docs?foo=1&hl=zh#frag") == "https://a.com/docs?hl=zh"
    assert normalize_url("ftp://a.com/x") is None
    assert normalize_url("https://a.com/docs?v=2") == "https://a.com/docs"


def test_check_filters():
    site = SiteConfig(url="https://a.com/docs", exclude=["/docs/private*"])
    adapter = get_adapter("generic")
    assert _check("https://a.com/docs/intro", site, adapter)
    assert not _check("https://a.com/docs/intro.pdf", site, adapter)
    assert not _check("https://a.com/docs/private/x", site, adapter)
    assert not _check("https://a.com/docs/search", site, adapter)
    assert not _check("https://a.com/docs/a.md", site, adapter)
    # allow_md 模式(llms.txt 链路)放行 .md
    assert _check("https://a.com/docs/a.md", site, adapter, allow_md=True)


def test_parse_llms_links():
    text = (
        "# Site\n\n"
        "> intro\n\n"
        "## Docs\n\n"
        "- [Guides](https://a.com/guides)\n"
        "- [Full](llms-full.txt)\n"
        "https://a.com/raw.md\n"
        "not a link\n"
    )
    links = parse_llms_links(text, "https://a.com/")
    assert "https://a.com/guides" in links
    assert "https://a.com/llms-full.txt" in links
    assert "https://a.com/raw.md" in links


def test_drop_extensions_sanity():
    assert ".pdf" in DROP_EXTENSIONS
    assert ".html" not in DROP_EXTENSIONS


def test_candidate_from_href():
    from omwb.discover import _candidate_from_href
    origin = "requests.readthedocs.io"
    # 相对链接:基于带尾斜杠的最终 URL 正确解析 + 前缀放宽
    assert _candidate_from_href("user/install/", "https://requests.readthedocs.io/en/latest/",
                                origin, "/en/latest") == "https://requests.readthedocs.io/en/latest/user/install"
    # 绝对链接:必须前缀匹配
    assert _candidate_from_href("https://requests.readthedocs.io/en/stable/user/install/",
                                "https://requests.readthedocs.io/en/latest/", origin,
                                "/en/latest") is None
    assert _candidate_from_href("https://requests.readthedocs.io/en/latest/user/install/",
                                "https://requests.readthedocs.io/en/latest/", origin,
                                "/en/latest") == "https://requests.readthedocs.io/en/latest/user/install"
    # 前缀内相对根链接排除
    assert _candidate_from_href("/", "https://requests.readthedocs.io/en/latest/", origin,
                                "/en/latest") is None
    # 异源排除
    assert _candidate_from_href("https://pypi.org/project/requests/",
                                "https://requests.readthedocs.io/en/latest/", origin,
                                "/en/latest") is None
