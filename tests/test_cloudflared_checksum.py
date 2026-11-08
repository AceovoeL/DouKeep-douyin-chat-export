"""cloudflared 下载的 SHA-256 校验（镜像只管传得快，真伪由官方摘要背书）。"""
import hashlib
import os

import pytest

import backend.cloudflared as cf


def test_file_matches_digest(tmp_path):
    path = tmp_path / "bin.exe"
    path.write_bytes(b"hello cloudflared")
    digest = hashlib.sha256(b"hello cloudflared").hexdigest()
    assert cf._file_matches_digest(str(path), digest) is True
    # 摘要不对 / 大小写不敏感
    assert cf._file_matches_digest(str(path), digest.upper()) is True
    assert cf._file_matches_digest(str(path), "0" * 64) is False
    # 文件不存在：校验不过（别把缺失当成通过）
    assert cf._file_matches_digest(str(tmp_path / "nope"), digest) is False
    # 没有期望值：无从校验，按通过处理（调用方自己保证只在大小时兜底）
    assert cf._file_matches_digest(str(path), "") is True


def test_latest_from_api_extracts_digest(monkeypatch):
    """发布接口的 assets 里带 sha256 摘要：本平台那个文件的摘要要被取出来。"""
    name = cf.binary_name()
    payload = {
        "tag_name": "2026.10.1",
        "assets": [
            {"name": "some-other-file", "digest": "sha256:" + "a" * 64},
            {"name": name, "digest": "sha256:" + "b" * 64},
        ],
    }

    class _Resp:
        def read(self):
            import json
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cf.urllib.request, "urlopen", lambda req, timeout: _Resp())
    found = cf._latest_from_api()
    assert found == ("2026.10.1", "b" * 64)


def test_latest_from_api_without_digest(monkeypatch):
    """老版 GitHub 接口没有 digest 字段：版本号照常返回，摘要为空串（退回只查大小）。"""
    payload = {"tag_name": "2026.10.1", "assets": [{"name": cf.binary_name()}]}

    class _Resp:
        def read(self):
            import json
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cf.urllib.request, "urlopen", lambda req, timeout: _Resp())
    assert cf._latest_from_api() == ("2026.10.1", "")


def test_download_rejects_mismatched_file(tmp_path, monkeypatch):
    """镜像给的文件对不上官方摘要：删掉、换下一个源重下；全都不对才算失败。"""
    monkeypatch.setattr(cf.paths, "CLOUDFLARED_DIR", str(tmp_path))
    good = b"x" * (cf._MIN_BINARY_SIZE + 1024)
    good_digest = hashlib.sha256(good).hexdigest()
    monkeypatch.setattr(cf, "cached_latest_digest", lambda: good_digest)
    monkeypatch.setattr(cf, "latest_version", lambda **kw: "2026.10.1")

    served = [b"y" * (cf._MIN_BINARY_SIZE + 1024), good]   # 第一个源投毒，第二个正常

    def fake_fetch(url, part, received, progress=None, cancelled=None):
        data = served.pop(0)
        with open(part, "wb") as handle:
            handle.write(data)
        return len(data)

    monkeypatch.setattr(cf, "_fetch_into", fake_fetch)
    monkeypatch.setattr(cf, "release_urls", lambda: ["https://mirror/", "https://official/"])
    path = cf.download_binary(promote=True)
    assert os.path.isfile(path)
    with open(path, "rb") as handle:
        assert handle.read() == good


def test_download_fails_when_all_sources_bad(tmp_path, monkeypatch):
    """所有源给的文件都对不上摘要：报 download_failed，不把坏文件留下。"""
    monkeypatch.setattr(cf.paths, "CLOUDFLARED_DIR", str(tmp_path))
    monkeypatch.setattr(cf, "cached_latest_digest", lambda: "0" * 64)
    monkeypatch.setattr(cf, "latest_version", lambda **kw: "2026.10.1")

    def fake_fetch(url, part, received, progress=None, cancelled=None):
        data = b"z" * (cf._MIN_BINARY_SIZE + 1024)
        with open(part, "wb") as handle:
            handle.write(data)
        return len(data)

    monkeypatch.setattr(cf, "_fetch_into", fake_fetch)
    monkeypatch.setattr(cf, "release_urls", lambda: ["https://mirror/"])
    with pytest.raises(cf.TunnelError) as exc:
        cf.download_binary(promote=True)
    assert exc.value.code == "download_failed"
    # .part 不该留着（下次续传会被污染）
    assert not os.path.exists(os.path.join(str(tmp_path), cf.binary_name() + ".part"))
