"""项目根必须在「装成包之后」也指对地方。

这一条从源码检出里是看不见的：在仓库里跑，
``<repo>/src/hindsight_agent/config.py`` 往上两级正好是仓库根，一切正常。
把同一份代码装成 wheel，往上两级就变成 ``<venv>/lib/python3.11``——一个既没有
``data/`` 也永远不会有的地方。前置检查会理直气壮地报「缺少数据」，并建议你
往那里灌十万条事故。

所以这里钉的不是「函数返回什么」，而是「它靠什么认路」。
"""

from __future__ import annotations

from pathlib import Path

from hindsight_agent.config import ENV_HOME, PROJECT_ROOT, resolve_project_root


def test_an_explicit_home_wins_over_everything(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_HOME, str(tmp_path))
    assert resolve_project_root() == tmp_path.resolve()


def test_a_source_checkout_resolves_to_the_repository_root(monkeypatch):
    """在仓库里跑，根就是仓库根——data/ 和 .env 都在那儿。"""
    monkeypatch.delenv(ENV_HOME, raising=False)
    root = resolve_project_root()
    assert (root / "pyproject.toml").exists(), f"{root} 不像仓库根"
    assert (root / "src" / "hindsight_agent").is_dir()


def test_an_installed_package_falls_back_to_the_working_directory(
    tmp_path, monkeypatch
):
    """装进 site-packages 之后，往上两级不再是仓库根，只能用工作目录。

    这里通过把模块文件搬到一个 site-packages 形状的目录里来模拟安装布局——
    判断依据是父目录叫不叫 ``src``，所以搬过去就足以复现。
    """
    monkeypatch.delenv(ENV_HOME, raising=False)
    site_packages = tmp_path / "lib" / "python3.11" / "site-packages"
    package = site_packages / "hindsight_agent"
    package.mkdir(parents=True)
    installed_config = package / "config.py"
    installed_config.write_text("", encoding="utf-8")

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    import hindsight_agent.config as config

    monkeypatch.setattr(config, "__file__", str(installed_config))
    resolved = config.resolve_project_root()

    assert resolved == workdir.resolve(), (
        "装好的包应当以当前工作目录为项目根，"
        f"而不是 {resolved}"
    )
    assert "site-packages" not in str(resolved)


def test_the_module_level_constant_is_what_everything_else_imports():
    """五个模块曾经各算各的。它们必须是同一个值，否则采集与读取会分家。"""
    from hindsight_agent import factory_db, incident_db
    from hindsight_agent.tools import collect_grounding, collect_osha

    assert incident_db.PROJECT_ROOT is PROJECT_ROOT
    assert factory_db.PROJECT_ROOT is PROJECT_ROOT
    assert collect_osha.PROJECT_ROOT is PROJECT_ROOT
    assert collect_grounding.PROJECT_ROOT is PROJECT_ROOT
