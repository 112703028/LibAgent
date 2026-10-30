"""書目類別的判定：AI 推薦 > 指定 > 參考。dashboard 的明細表與追溯畫面共用同一份規則。"""


def is_ai_recommended(raw_mention: str | None) -> bool:
    """discoverer 產生的 AI 推薦書，raw_mention 會標 [AI推薦]（見 discoverer.py）。"""
    return bool(raw_mention) and "AI推薦" in raw_mention


def book_type_key(is_required: bool, raw_mention: str | None) -> str:
    """回傳 "ai" / "required" / "optional"。"""
    if is_ai_recommended(raw_mention):
        return "ai"
    return "required" if is_required else "optional"
