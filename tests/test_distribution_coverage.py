"""渠道注册表必须覆盖风险登记册真正暴露的岗位。

这个文件来自一次真实的漏网。演示跑完之后在控制台点「批准并分发」，结果是：

    岗位 role-dock 没有注册任何渠道，这份稿件已批准但没有送达任何人。

系统的行为是对的——它拒绝退回广播，并留下明确信号。错的是数据：默认渠道表
里叉车渠道写的是 ``role-forklift-operator``，而登记册里是 ``role-forklift``，
两个字符串对不上；登记册暴露 7 个岗位，表里只覆盖了 1 个。批准照常成功，
稿件一个人也收不到，而这条链上没有任何一个测试会因此变红。

两个字符串必须对上，而对上这件事只能由测试来钉，不能靠下次有人记得。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hindsight_agent.distribution import DEFAULT_CHANNELS
from hindsight_agent.factory_db import (
    DEFAULT_SEED_PATH,
    DEFAULT_ZH_TRANSLATIONS_PATH,
    create_factory_database,
)


@pytest.fixture(scope="module")
def demo_factory(tmp_path_factory) -> Path:
    """按 hindsight-factory-create-zh 的同一条路径建出演示工厂。

    登记册不在 seed 里——它由设备 × 作业 × 危险生成，暴露岗位也是在生成时
    选定的。所以能回答「哪些岗位真的会被分发命中」的，只有生成出来的库本身。
    """
    path = tmp_path_factory.mktemp("factory") / "factory_zh.db"
    create_factory_database(
        output_path=path, seed_path=DEFAULT_SEED_PATH, replace=True,
        translations_path=DEFAULT_ZH_TRANSLATIONS_PATH,
    )
    return path


def _query(database: Path, sql: str) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {row[0] for row in connection.execute(sql) if row[0]}
    finally:
        connection.close()


def _exposed_roles(database: Path) -> set[str]:
    return _query(database, "SELECT DISTINCT exposed_role_id FROM risk_assessments")


def test_the_register_still_exposes_roles(demo_factory):
    """这个测试的前提本身也要成立，否则下面几条会空转通过——写这一条的当下，
    它就当场逮到了上面那个遍历写错的版本。"""
    assert _exposed_roles(demo_factory), "登记册里读不出任何暴露岗位，测试失去意义"


def test_every_channel_points_at_a_role_the_site_actually_has(demo_factory):
    """``role-forklift-operator`` 这样的岗位不存在，指向它的渠道永远解析不到人。"""
    known = _query(demo_factory, "SELECT id FROM worker_roles")
    unknown = sorted(
        {c["role_id"] for c in DEFAULT_CHANNELS if c.get("role_id")} - known
    )
    assert not unknown, f"渠道指向了工厂里不存在的岗位：{unknown}"


def test_every_exposed_role_can_receive_something(demo_factory):
    """一个岗位没有渠道，等于它的案例批准之后无人收到。"""
    covered = {c["role_id"] for c in DEFAULT_CHANNELS if c.get("role_id")}
    missing = sorted(_exposed_roles(demo_factory) - covered)
    assert not missing, f"这些岗位在登记册里暴露，却没有任何渠道：{missing}"


def test_every_exposed_role_can_receive_it_in_both_languages(demo_factory):
    """稿件是双语生成的。只登记英文渠道，等于西语班组永远收不到。"""
    by_role: dict[str, set[str]] = {}
    for channel in DEFAULT_CHANNELS:
        if channel.get("role_id"):
            by_role.setdefault(channel["role_id"], set()).add(
                channel.get("language", "en")
            )
    incomplete = sorted(
        role for role in _exposed_roles(demo_factory)
        if not {"en", "es"} <= by_role.get(role, set())
    )
    assert not incomplete, f"这些岗位缺少英文或西语渠道：{incomplete}"


@pytest.mark.parametrize("field", ["channel_id", "name", "kind"])
def test_every_channel_is_fully_described(field):
    for channel in DEFAULT_CHANNELS:
        assert channel.get(field), f"{channel.get('channel_id')} 缺少 {field}"


def test_channel_ids_are_unique():
    ids = [c["channel_id"] for c in DEFAULT_CHANNELS]
    assert len(ids) == len(set(ids))
