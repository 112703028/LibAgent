"""match_auditor 字串規則層的單元測試（純函式，不需 DB / LLM）。

規則層的職責只有一個：把「明顯不是同一本」的配對擋下來，不必問 LLM。
它刻意偏寬鬆（寧可交給 LLM 複核，也不要誤擋），因此這裡同時釘住
「規則擋不住、要靠 LLM 的案例」，避免日後有人誤以為規則可以單獨使用。
"""
from library_agent.agents.match_auditor import RULE_THRESHOLD, _cross_script, rule_score


def test_identical_titles_score_full():
    assert rule_score("經濟學原理", "經濟學原理") == 1.0


def test_subtitle_only_difference_is_same_book():
    # 核心書名（冒號前）相同 → 滿分
    assert rule_score("Python 程式設計", "Python程式設計：從入門到實作") == 1.0
    assert rule_score("社會學: 導論", "社會學") == 1.0


def test_original_title_as_other_subtitle_is_same_book():
    """原書名剛好是對方的副標題：「鍾怡雯精選集」vs「今晨有雨 : 鍾怡雯精選集」是同一本。

    只比冒號前的核心書名會算成 0 分而誤判配錯，所以要同時比完整書名的包含關係。
    """
    assert rule_score("鍾怡雯精選集", "今晨有雨 : 鍾怡雯精選集") == 1.0
    assert rule_score("中國話的文法", "國語語法 : 中國話的文法") == 1.0
    assert rule_score("日本語作文的方法", "表現主題別.日本語作文的方法") == 1.0


def test_traditional_simplified_folded():
    assert rule_score("資本論", "资本论") == 1.0


def test_symbol_equivalence():
    assert rule_score("Business & Economics", "Business and Economics") == 1.0
    assert rule_score("ACSM's Guidelines", "ACSMs Guidelines") == 1.0


def test_full_width_and_case_folded():
    assert rule_score("ＰＹＴＨＯＮ", "python") == 1.0


def test_unrelated_titles_below_threshold():
    assert rule_score("經濟學原理", "社會學") < RULE_THRESHOLD
    assert rule_score("Linear Algebra", "Organic Chemistry") < RULE_THRESHOLD


def test_empty_title_scores_zero():
    assert rule_score("", "經濟學原理") == 0.0
    assert rule_score("經濟學原理", "") == 0.0
    assert rule_score("", "") == 0.0


def test_bigrams_make_word_order_matter():
    # 若用單字元集合比對，「上海」與「海上」會被判成同一本；bigram 讓字序有意義
    assert rule_score("上海的故事", "海上的故事") < 1.0


def test_cross_script_pairs_defer_to_llm():
    """中譯本：規則分數必然是 0，但不該因此判配錯——交給 LLM。"""
    assert _cross_script("Statistics for Business and Economics", "商务与经济统计") is True
    assert _cross_script("日本語文法辞典", "A Dictionary of Japanese Grammar") is True
    # 規則確實給 0 分，所以若不特判就會被直接判配錯
    assert rule_score("Statistics for Business and Economics", "商务与经济统计") < RULE_THRESHOLD


def test_same_script_pairs_let_rule_decide():
    assert _cross_script("經濟學原理", "社會學") is False
    assert _cross_script("Linear Algebra", "Organic Chemistry") is False
    # 中英混排的書名不算跨文字系統（有共同的拉丁字元可比）
    assert _cross_script("Python 程式設計", "Python 入門") is False


def test_containment_cases_need_llm_layer():
    """規則層故意放過的案例：被包含的書名一律滿分。

    「資本論」與「資本論導讀」是不同的書（後者是關於前者的導讀），但核心書名互相包含，
    規則必然給滿分。這就是階梯式設計第二層（LLM 複核）存在的原因——
    若哪天想拿掉 LLM 層，這個測試會提醒你規則單獨擋不住這類配錯。
    """
    assert rule_score("資本論", "資本論導讀") == 1.0
    assert rule_score("紅樓夢", "紅樓夢研究") == 1.0
