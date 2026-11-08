"""書商模組共用的工具：主書名、作者切分、跨執行緒限速。"""
import threading
import time

import httpx

from library_agent.integrations.bookstore import Cooldown, Pacer, main_title, split_authors


def test_main_title_cuts_at_the_first_separator():
    assert main_title("語言、社會與族群意識—台灣語言社會學研究") == "語言、社會與族群意識"
    assert main_title("基礎經濟學 / Essential Foundations of Economics") == "基礎經濟學"
    assert main_title("強制移住──臺灣高山原住民的分與離") == "強制移住"
    assert main_title("經濟均衡發展策略 - 歐洲轉型國家之案例與啟示") == "經濟均衡發展策略"
    assert main_title("Istanbul: City of the future") == "Istanbul"


def test_main_title_is_none_when_nothing_to_cut_or_too_short():
    assert main_title("被討厭的勇氣") is None                  # 沒有分隔符號
    assert main_title("A: something") is None                  # 主書名不到 2 個字
    assert main_title("") is None


def test_split_authors_handles_prefix_and_separators():
    assert split_authors("作者：岸見一郎、古賀史健") == ["岸見一郎", "古賀史健"]
    assert split_authors("岸見一郎/ 古賀史健") == ["岸見一郎", "古賀史健"]
    assert split_authors("Tess Ferree, Kim Sanabria") == ["Tess Ferree", "Kim Sanabria"]
    assert split_authors("") == []


def test_pacer_spaces_calls_from_two_threads():
    pacer, stamps, lock = Pacer(min_interval=0.2), [], threading.Lock()

    def worker():
        for _ in range(2):
            pacer.wait()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 4 and min(gaps) >= 0.13, gaps          # 0.2 × 0.7 = 0.14，留一點計時誤差


# ---------- Cooldown：書商回 429 時退讓 ----------

def clocked():
    now = [1000.0]
    return Cooldown(default=300.0, maximum=1800.0, clock=lambda: now[0]), now


def test_cooldown_is_inactive_until_started():
    cd, _now = clocked()

    assert cd.active() is False


def test_cooldown_without_retry_after_uses_the_default_then_expires():
    cd, now = clocked()

    wait = cd.start(httpx.Response(429))

    assert wait == 300.0 and cd.active() is True
    now[0] += 299
    assert cd.active() is True
    now[0] += 2
    assert cd.active() is False


def test_cooldown_honors_retry_after_but_caps_it():
    cd, now = clocked()

    assert cd.start(httpx.Response(429, headers={"Retry-After": "60"})) == 60.0
    assert cd.start(httpx.Response(429, headers={"Retry-After": "99999"})) == 1800.0
    assert cd.start(httpx.Response(429, headers={"Retry-After": "-5"})) == 0.0          # 負數當作不用等
    assert cd.start(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) == 300.0   # 看不懂的格式用預設
