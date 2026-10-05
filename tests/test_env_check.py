"""环境检测（backend/env_check.py）的规则与返回结构。

面板「关于 → 环境检测」和 start.html 的首次运行弹窗都靠这份数据，所以这里钉住：
必需/可选的判定、Node.js 的版本规则（跟 Vite 7 的 engines 对齐）、英文文案回退，
以及「单项检查炸了也不能让整个检测失败」。
"""
import backend.env_check as env_check


def test_node_version_rule_matches_vite_engines():
    """frontend/package.json 的 engines 是 ``^20.19.0 || >=22.12.0``。"""
    for version in ((20, 19, 0), (20, 20, 1), (22, 12, 0), (22, 20, 0), (23, 0, 0), (24, 20, 0)):
        assert env_check.node_version_ok(version), version
    for version in ((18, 20, 0), (20, 18, 9), (21, 7, 0), (22, 11, 9)):
        assert not env_check.node_version_ok(version), version
    assert not env_check.node_version_ok(None)


def test_semver_extracts_versions():
    assert env_check.semver("v24.20.0") == (24, 20, 0)
    assert env_check.semver("Python 3.13.14") == (3, 13, 14)
    assert env_check.semver("git version 2.55.0.windows.5") == (2, 55, 0)
    assert env_check.semver("pip 26.1.2 from D:\\x (python 3.13)") == (26, 1, 2)
    assert env_check.semver(None) is None
    assert env_check.semver("这个字符串里没有版本号") is None


def test_python_item_uses_the_running_interpreter():
    item = env_check._check_python()
    assert item["id"] == "python"
    assert item["required"] is True
    assert item["ok"] is True                 # 测试本身就跑在 3.10+ 上
    assert item["requirement"] == ">= 3.10"
    assert "Python" in item["current"]


def test_required_checks_come_before_optional_ones():
    """必需项要整段排在可选项前面：面板和首次运行检测页都是照这个顺序往下画的。

    Playwright 浏览器内核吃过这个亏 —— 它早就是必需项，却排在 Git / ffmpeg 两个可选项后面，
    用户看到的就成了「一项必需的夹在一堆可选的中间」。新加必需项时放进前面那一段。
    """
    flags = [bool(check()["required"]) for check in env_check.CHECKS]
    assert flags == sorted(flags, reverse=True), [f.__name__ for f in env_check.CHECKS]


def test_item_falls_back_to_chinese_text():
    """英文没写的时候用中文兜底，前端就不用判断字段是否存在。"""
    item = env_check._item("demo", "演示", True, True, ">= 1", "1.0", detail="说明")
    assert item["name_en"] == "演示"
    assert item["requirement_en"] == ">= 1"
    assert item["detail_en"] == "说明"
    assert item["hint_en"] == ""

    item = env_check._item("demo", "演示", True, True, name_en="Demo", current_en="1.0")
    assert item["name_en"] == "Demo"
    assert item["current_en"] == "1.0"


def test_playwright_item_is_required_but_assumed_ready(monkeypatch):
    """浏览器内核是必需项，但「缺了自动下载」由启动脚本负责，所以这一项默认算已满足。

    否则第一次用的人会被一项本来不用他管的检查（start.html 上「必要条件未通过」根本关不掉）
    拦在门外 —— 那正是这个项目最想避免的体验。
    """
    import backend.env_check as env_check_module

    item = env_check_module._check_playwright()
    assert item["id"] == "playwright_chromium"
    assert item["required"] is True
    assert item["ok"] is True
    assert "自动" in item["requirement"]
    assert "自动" in item["detail"]
    assert "playwright install chromium" in item["hint"]

    # 就算内核真的一个都没装，也不能翻脸判失败
    monkeypatch.setattr(env_check_module.pw_browsers, "summarize",
                        lambda: "启动时自动安装（下载约 300 MB）")
    assert env_check_module._check_playwright()["ok"] is True


def test_collect_summary_counts_required_and_optional(monkeypatch):
    def fake_ok():
        return env_check._item("a", "A", True, True, ">= 1", "1.0")

    def fake_bad():
        return env_check._item("b", "B", True, False, ">= 2", "1.0", hint="升级")

    def fake_optional():
        return env_check._item("c", "C", False, False, "可选", "无")

    monkeypatch.setattr(env_check, "CHECKS", (fake_ok, fake_bad, fake_optional))
    data = env_check.collect()

    assert data["summary"] == {
        "ok": False,
        "required_total": 2,
        "required_passed": 1,
        "optional_total": 1,
        "optional_passed": 0,
    }
    assert [item["id"] for item in data["items"]] == ["a", "b", "c"]
    assert data["source"] == "python"
    assert data["ts"] > 0 and data["generated_at"]


def test_collect_survives_a_broken_check(monkeypatch):
    def boom():
        raise RuntimeError("检测炸了")

    monkeypatch.setattr(env_check, "CHECKS", (boom,))
    data = env_check.collect()
    item = data["items"][0]
    assert item["ok"] is False
    assert "检测炸了" in item["hint"]
    assert data["summary"]["ok"] is False


def test_every_check_returns_the_same_fields():
    """检查项字段齐全，前端才不用做兼容（PS 版有同样的字段）。"""
    fields = {"id", "name", "name_en", "required", "ok", "requirement", "requirement_en",
              "current", "current_en", "path", "detail", "detail_en", "hint", "hint_en"}
    for check in env_check.CHECKS:
        item = check()
        assert fields <= set(item), f"{check.__name__} 缺字段: {fields - set(item)}"


def test_collect_endpoint_is_wired_up(monkeypatch):
    """面板按钮打的就是这个接口（路由契约另有测试冻结）。"""
    from fastapi.testclient import TestClient

    import backend.main as main

    monkeypatch.setattr(env_check, "CHECKS",
                        (lambda: env_check._item("a", "A", True, True, ">= 1", "1.0"),))
    client = TestClient(main.app)
    response = client.get("/panel/api/env/check")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [env_check._item("a", "A", True, True, ">= 1", "1.0")]
    assert body["summary"]["ok"] is True
