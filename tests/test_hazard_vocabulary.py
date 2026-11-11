"""危险词表的精度：一条词命中什么，不该命中什么。

这个文件来自一次真实的演示输出。当时最高优先级的红色告警，是把一起「更换
电机皮带时手指被绞入」的事故当成了焊接工位触电，推给焊工——只因为叙述里
有 "electric motor" 这个词。在最近 500 条事故里，有 30 条仅凭 "electric"
一个词被贴上电气危险，其中包括一起在收银台绊到风扇线的摔伤。

这正是本项目用定向分发想避免的警报疲劳。词表的精度不是润色，它决定了
第 ③ 层送到人面前的东西值不值得看。
"""

from __future__ import annotations

import pytest

from hindsight_agent.matching import HAZARD_TERMS, matched_terms
from hindsight_agent.tools.collect_grounding import tag_hazards


# --------------------------------------------------------------------------
# 词边界
# --------------------------------------------------------------------------

def test_a_negated_word_is_not_read_as_its_opposite():
    """"unenergized" 里含有 "energized"。子串匹配会把「已断电」读成「带电」。"""
    assert matched_terms("the unenergized nip point", ["energized"]) == []
    assert matched_terms("an energized conductor", ["energized"]) == ["energized"]


@pytest.mark.parametrize("text, term", [
    ("his finger was crushed", "crush"),
    ("resulting in burns to his left side", "burn"),
    ("the employee was electrocuted", "electrocut"),
    ("死因是 electrocution", "electrocut"),
    ("greater than 220 volts", "volt"),
])
def test_one_term_still_covers_its_inflections(text, term):
    """词干后面留通配是有意的：不然要为每个时态各写一条词。"""
    assert matched_terms(text, [term]) == [term]


# --------------------------------------------------------------------------
# 电气危险
# --------------------------------------------------------------------------

@pytest.mark.parametrize("narrative", [
    "An employee was replacing a belt on an electric motor.",
    "A temporary employee was walking beside an electric pallet jack.",
    "She tripped over a fan cord at the register and fell.",
])
def test_an_electric_appliance_is_not_an_electrical_hazard(narrative):
    assert matched_terms(narrative, HAZARD_TERMS["haz-electrical"]) == []


@pytest.mark.parametrize("narrative", [
    "An aluminum pole contacted a power line and the employee was shocked.",
    "He came into contact with a powerline and was electrocuted.",
    "A crew was trimming limbs away from a 7.2-kilovolt primary electrical line.",
    "The employee sustained an electrical burn to their leg.",
    "Direct exposure to electricity greater than 220 volts.",
])
def test_real_electrical_contact_is_still_caught(narrative):
    assert matched_terms(narrative, HAZARD_TERMS["haz-electrical"])


# --------------------------------------------------------------------------
# 一套词表，三个使用点
# --------------------------------------------------------------------------

def test_the_corpus_tagger_uses_the_same_rule_as_the_screen():
    """语料打标、硬过滤、匹配器必须说同一种话，否则检索与筛选会各走各的。"""
    assert "haz-electrical" in tag_hazards("contact with an energized conductor")
    assert "haz-electrical" not in tag_hazards("the electric motor drives the belt")
