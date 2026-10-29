"""Match Auditor：稽核「驗證器配到的權威記錄」是否真的是原始書目那一本。

validator 只要查得到記錄就算 verified=True，但實測發現相當比例配到的是不相關的書
（同作者的另一本、關於該書的研究/活動記錄、名稱相近的別本）。本節點不更動 validator
的配對邏輯，只在配對之後加一道稽核，把可疑的配對送進人工審核佇列。

階梯式判斷（experiments/same_book_judge_compare.py 在 300 筆人工標註上比較過三種方法）：
    1. 字串規則：書名正規化後比對，判「不同本」→ 直接標記，不呼叫 LLM
    2. 字串規則判「同一本」→ 問 LLM 複核，LLM 說不同本才標記

實測（300 筆，門檻 0.6）：配錯抓到 96.1%、誤送審 19.0%，LLM 呼叫量約 58%。
單用 LLM 抓到 92.2%；單用字串規則 71.9%；Laya 這類 typed-decision 模型僅 51.6%，已排除。

用法：
    python src/library_agent/agents/match_auditor.py        # 稽核尚未稽核的書目
    python src/library_agent/agents/match_auditor.py 20     # 只跑前 20 筆（測試）
"""
import json
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select

from library_agent.config import get_settings
from library_agent.db.models import Citation
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.state import AgentState

_settings = get_settings()

# 字串規則判「同一本」的門檻；低於此值直接判配錯送審（實測 0.6 最佳）
RULE_THRESHOLD = 0.6
_MAX_WORKERS = 4
_BATCH = 100

try:
    from opencc import OpenCC

    _t2s = OpenCC("t2s").convert
except Exception:  # noqa: BLE001 — 沒裝 opencc 就跳過繁簡折疊，規則仍可運作
    _t2s = None

_SYMBOL_EQUIV = [
    (r"[&＆]", " and "),              # Business & Economics == Business and Economics
    (r"[''`´]", ""),                  # ACSM's == ACSMs
    (r"[­‐-―]", "-"),  # 各種破折號統一
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    for pat, rep in _SYMBOL_EQUIV:
        s = re.sub(pat, rep, s)
    if _t2s:
        s = _t2s(s)
    return "".join(c for c in s if c.isalnum())


def _core(title: str) -> str:
    """去掉副標題（第一個冒號/等號之後）再正規化。"""
    return _norm(re.split(r"[:：=＝]", title or "", maxsplit=1)[0])


def _bigrams(s: str) -> set[str]:
    """連續兩字元集合，讓字序有意義（「上海」≠「海上」）。"""
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def rule_score(orig_title: str, matched_title: str) -> float:
    """0–1，越高越像同一本。互相包含給滿分，否則算 bigram Jaccard。

    包含關係要比「完整書名」和「核心書名（冒號前）」兩種：
    原書名有可能剛好是對方的副標題（「鍾怡雯精選集」vs「今晨有雨 : 鍾怡雯精選集」是同一本），
    只比冒號前會把這種情況算成 0 分而誤判配錯。
    """
    fa, fb = _norm(orig_title), _norm(matched_title)
    if not fa or not fb:
        return 0.0
    if fa in fb or fb in fa:
        return 1.0
    a, b = _core(orig_title) or fa, _core(matched_title) or fb
    if a in b or b in a:
        return 1.0
    ga, gb = _bigrams(a), _bigrams(b)
    return len(ga & gb) / len(ga | gb)


_LLM_SYSTEM = """你是圖書館編目專家。判斷「原始書目」與「書目資料庫配到的記錄」是否指向同一本書（同一部作品）。

判斷時以書名為主要依據，作者為輔助證據。

視為同一本：
- 繁簡體、異體字、全半形、標點差異
- 有無副標題、版次/增訂版/不同出版年、平裝精裝
- 原文書與其翻譯本（書名語言不同，但是同一部作品）
- 作者是同一人的不同寫法。中文書目這種情況非常普遍：
  * 筆名與本名（李昂／施淑端、孟樊／陳俊榮、焦桐／葉振富）
  * 原住民族名與漢名（達西烏拉彎‧畢馬／田哲益）
  * 外國學者的漢學名與音譯名（宇文所安／Stephen Owen／歐文‧斯蒂芬）
  * 姓名順序、標點、是否加「著」「編」「譯」
- 一邊列作者、另一邊列編者或譯者（同一本書的不同著錄角色）
- 一邊多列或少列共同作者

視為不同本：
- 關於這本書的研究、評論、導讀、論文集、研討會實錄、展演或活動記錄
- 索引、情節檢索、精華版/節本、教師手冊、習題本、學習指引等衍生或配套出版物
- 回憶錄、自述、傳記（即使書名裡含有原書名或原書主題）
- 以「某人的作品」為對象的賞析、選評（例如原書是《新詩賞析》，
  配到的是《蕭蕭新詩賞析》——後者是賞析蕭蕭的詩，不是同一本）
- 只是收錄它的叢書、全集、套書，或名稱相近但主題不同的書
- 書名相同但確實是不同作者各自撰寫的不同作品

作者判斷的重要原則：
- 書名是通用的教科書名時（例如 Macroeconomics、Economics、經濟學、唐詩選注），
  不同作者各自出版同名教科書非常常見。此時作者不同就應判為不同本。
- 反之，書名具體而獨特時（例如《北港香爐人人插》《邵族神話與傳說》《完全壯陽食譜》），
  幾乎不可能存在第二本同名書。此時若作者寫法不同，應優先推定為「同一人的別名或
  不同著錄角色」，判為同一本，不要因為認不出這兩個名字是同一人就判為不同本。
- 但上面這條寬鬆推定「只適用於兩邊書名實質相同」的情況（去掉副標題、叢書前綴、
  繁簡與標點差異之後相同）。如果配到的書名雖然包含原書名、但整體指向上面列舉的
  另一種出版物，那就是不同本——即使兩邊作者相同也一樣。
- 原始書目沒有提供作者資訊時，不得僅因為對方有作者就判為不同本。

只輸出 JSON：{"same_book": true 或 false, "reason": "一句話理由"}"""


def _llm_same_book(client, model: str, orig_title: str, orig_authors: list[str],
                   matched_title: str, matched_authors: list[str]) -> tuple[bool, str]:
    """回傳 (是否同一本, 理由)。LLM 失敗時保守地判「同一本」，避免整批誤送審。"""
    user = (
        f"原始書目：書名＝{orig_title}；作者＝{'、'.join(orig_authors) or '（無）'}\n"
        f"配到的記錄：書名＝{matched_title}；作者＝{'、'.join(matched_authors) or '（無）'}"
    )
    last_err = None
    for _ in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _LLM_SYSTEM},
                    {"role": "user", "content": user},
                ],
            )
            data = json.loads(resp.choices[0].message.content)
            return bool(data.get("same_book")), str(data.get("reason", ""))[:300]
        except Exception as e:  # noqa: BLE001
            last_err = e
    return True, f"LLM 判斷失敗，保守視為同一本：{last_err}"


def _pending_query(course_ids: list[str] | None, limit: int = 0):
    """待稽核：驗證成功、尚未稽核過（pairing_verdict 為 NULL）的書目。"""
    q = (
        select(VerifiedBookDB, Citation)
        .join(Citation, Citation.id == VerifiedBookDB.citation_id)
        .where(
            VerifiedBookDB.verified.is_(True),
            VerifiedBookDB.pairing_verdict.is_(None),
            Citation.superseded_by_iteration.is_(None),
        )
        .order_by(VerifiedBookDB.id)
    )
    if course_ids:
        q = q.where(Citation.course_id.in_(course_ids))
    if limit:
        q = q.limit(limit)
    return q


def _authors(raw: str | None) -> list[str]:
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


def _script(s: str) -> str:
    cjk = bool(re.search(r"[぀-ヿ一-鿿]", s or ""))
    lat = bool(re.search(r"[A-Za-z]", s or ""))
    if cjk and not lat:
        return "cjk"
    if lat and not cjk:
        return "lat"
    return "mix" if cjk else "none"


def _cross_script(a: str, b: str) -> bool:
    """一邊純拉丁、另一邊純中日文 → 規則在這種配對上沒有信號。

    bigram 比對的是字元，中譯本與原文書名字元完全不重疊，分數必然是 0，
    但「Statistics for Business and Economics」配到「商务与经济统计」其實是同一本。
    這種情況不讓規則下結論，交給 LLM 判斷是不是翻譯版。
    """
    return {_script(a), _script(b)} == {"lat", "cjk"}


def _apply_verdict(session, it: dict) -> bool:
    """把單筆稽核結果寫進 session（不 commit）。回傳是否標記為配錯。"""
    vb = session.get(VerifiedBookDB, it["vb_id"])
    if vb is None:
        return False
    vb.pairing_verdict = it["verdict"]
    vb.pairing_judge = it["judge"]
    vb.pairing_reason = it["reason"]

    if it["verdict"] == "mismatch":
        vb.requires_human_review = True
        if vb.review_status not in ("approved", "rejected"):
            vb.review_status = "pending"
        return True

    # 改判同一本時收回「自己標的」待審旗標，否則清掉 pairing_verdict 重跑會留下殘渣
    # （佇列裡躺著一筆沒有理由的待審，review 介面還會顯示成「低信心」）。
    # 只在 validator 不會標它時才收回：信心度夠高 → 唯一可能的來源就是本節點。
    if (vb.review_status == "pending"
            and it["confidence"] >= _settings.human_review_confidence_threshold):
        vb.requires_human_review = False
        vb.review_status = None
    return False


def match_auditor_node(state: AgentState, limit: int = 0) -> AgentState:
    """limit 只給 __main__ 測試用；LangGraph 只會傳 state，預設跑全部待稽核書目。"""
    errors: list[str] = []
    course_ids = state.get("course_ids")

    with SessionLocal() as session:
        rows = session.execute(_pending_query(course_ids, limit)).all()
        # 先在 session 外備妥純資料，避免跨執行緒碰 ORM 物件
        items = [
            {
                "vb_id": vb.id,
                "orig_title": c.title,
                "orig_authors": _authors(c.authors),
                "matched_title": vb.canonical_title,
                "matched_authors": _authors(vb.canonical_authors),
                "confidence": c.confidence,
            }
            for vb, c in rows
        ]

    if not items:
        print("[match_auditor] 沒有待稽核的書目")
        return {"errors": errors}

    # 第一層：字串規則（純本機計算，不花錢）
    rule_mismatch, need_llm = [], []
    for it in items:
        it["rule_score"] = rule_score(it["orig_title"], it["matched_title"])
        if it["rule_score"] >= RULE_THRESHOLD:
            need_llm.append(it)
        elif _cross_script(it["orig_title"], it["matched_title"]):
            need_llm.append(it)  # 規則在跨文字系統上沒有信號，交 LLM 判是否為翻譯版
        else:
            it["verdict"] = "mismatch"
            it["judge"] = "rule"
            it["reason"] = f"書名相似度 {it['rule_score']:.2f} < {RULE_THRESHOLD}"
            rule_mismatch.append(it)

    print(f"[match_auditor] 待稽核 {len(items)} 筆；"
          f"規則判配錯 {len(rule_mismatch)} 筆（不花 LLM），"
          f"其餘 {len(need_llm)} 筆交 LLM 複核", flush=True)

    # 規則層的結果先落地：不花錢也不會失敗，先存起來就算後面斷掉也不用重算
    flagged = 0
    with SessionLocal() as session:
        for it in rule_mismatch:
            flagged += _apply_verdict(session, it)
        session.commit()

    # 第二層：規則認為像同一本的，交 LLM 複核。
    # 邊跑邊 commit（每 _BATCH 筆）：2000 次呼叫中途斷掉也只損失最後不足一批的進度，
    # 重跑時 pairing_verdict IS NULL 會自動只挑還沒做完的，不會重複付費。
    if need_llm:
        from openai import OpenAI

        client = OpenAI(api_key=_settings.openai_api_key, base_url=_settings.openai_base_url)
        model = _settings.llm_model_mini

        def judge(it: dict) -> None:
            same, reason = _llm_same_book(
                client, model, it["orig_title"], it["orig_authors"],
                it["matched_title"], it["matched_authors"],
            )
            it["verdict"] = "same" if same else "mismatch"
            it["judge"] = "llm"
            it["reason"] = reason

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex, SessionLocal() as session:
            futures = {ex.submit(judge, it): it for it in need_llm}
            for n, fut in enumerate(as_completed(futures), 1):
                it = futures[fut]
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001
                    errors.append(f"[match_auditor] vb={it['vb_id']}: {e}")
                    it["verdict"], it["judge"], it["reason"] = "same", "llm", f"ERROR: {e}"
                flagged += _apply_verdict(session, it)
                if n % _BATCH == 0:
                    session.commit()
                    print(f"  LLM 複核 {n}/{len(need_llm)}（已存檔）", flush=True)
            session.commit()
            print(f"  LLM 複核 {len(need_llm)}/{len(need_llm)}（已存檔）", flush=True)

    print(f"[match_auditor] 完成：{flagged}/{len(items)} 筆配對可疑，已送人工審核")
    return {"errors": errors}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp950 遇到書名裡的符號會炸
    _limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if _limit:
        print(f"測試模式：只稽核前 {_limit} 筆")
    match_auditor_node({}, limit=_limit)
