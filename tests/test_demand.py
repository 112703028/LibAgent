"""demand：採購「需求冊數」的唯一一份算式（recommender、追溯畫面、儀表板共用）。

need     = ceil(總人數 / 每本服務人數)；總人數 0（人數不明）時為 1
uncapped = max(need − 現有實體冊數, 0)，**不套單本上限**；已有電子書時為 0
recommender._decide 的建議冊數 = min(uncapped, 上限)，這裡用測試把兩者鎖在一起。
"""
import math

import pytest

from library_agent import demand as D
from library_agent.agents import recommender as R
from library_agent.state import HoldingStatus as HS

PER = R._settings.students_per_copy
CAP = R._settings.max_purchase_copies


def test_need_copies_rounds_up_and_treats_zero_enrolment_as_one():
    assert D.need_copies(1) == 1 and D.need_copies(PER) == 1 and D.need_copies(PER + 1) == 2
    assert D.need_copies(0) == 1 and D.need_copies(-5) == 1


def test_uncapped_copies_ignores_the_cap():
    uncapped = D.uncapped_copies(HS.OWNED_PHYSICAL, 670, 1)

    assert uncapped == math.ceil(670 / PER) - 1 and uncapped > CAP
    assert R._decide(HS.OWNED_PHYSICAL, True, 670, 1)[1] == CAP        # _decide 仍套上限


def test_uncapped_is_zero_for_an_ebook_or_when_holdings_cover_the_need():
    assert D.uncapped_copies(HS.OWNED_EBOOK, 670, 0) == 0
    assert D.uncapped_copies(HS.OWNED_PHYSICAL, 100, 10) == 0


@pytest.mark.parametrize("status", [HS.MISSING, HS.PARTIAL, HS.OWNED_PHYSICAL, HS.OWNED_EBOOK])
@pytest.mark.parametrize("enrolled", [0, 1, PER, PER + 1, 300, 670])
@pytest.mark.parametrize("current", [0, 1, 3, 30])
def test_capped_uncapped_equals_decide_for_a_grid_of_cases(status, enrolled, current):
    _priority, copies = R._decide(status, True, enrolled, current)

    assert min(D.uncapped_copies(status, enrolled, current), CAP) == copies
