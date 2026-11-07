"""書商模組共用的工具：主書名、作者切分、跨執行緒限速。"""
import threading
import time

from library_agent.integrations.bookstore import Pacer, main_title, split_authors


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
