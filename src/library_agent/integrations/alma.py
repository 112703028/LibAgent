import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

_SRU_BASE = "https://nccu.alma.exlibrisgroup.com/view/sru/886NCCU_INST"
_SRU_PARAMS = {"version": "1.2", "operation": "searchRetrieve", "recordSchema": "marcxml"}
_TIMEOUT = 15.0

_MARC_NS = "http://www.loc.gov/MARC21/slim"
_SRW_NS = "http://www.loc.gov/zing/srw/"


@dataclass
class HoldingResult:
    found: bool
    physical_count: int = 0
    has_ebook: bool = False
    mms_id: str | None = None


def _parse_response(xml_text: str) -> HoldingResult:
    root = ET.fromstring(xml_text)  # 把 XML 字串解析成樹狀結構，root 是最外層的節點

    # 找 <numberOfRecords> 這個節點。f"{{{_SRW_NS}}}numberOfRecords" 展開後是 {http://www.loc.gov/zing/srw/}numberOfRecords，
    # 這是 ElementTree 表示帶 namespace 的 tag 的語法（三個大括號：外面兩個是 f-string 的 {}，裡面一個是 namespace 用的 {}）
    # 。如果節點不存在或值為 0，代表找不到書，直接回傳 found=False。
    num_el = root.find(f"{{{_SRW_NS}}}numberOfRecords") 
    if num_el is None or int(num_el.text or 0) == 0:
        return HoldingResult(found=False)

    record_data = root.find(f".//{{{_SRW_NS}}}recordData") # // 代表在整棵樹裡任意深度搜尋。找 <recordData> 節點，裡面包著實際的 MARC 資料。
    if record_data is None:
        return HoldingResult(found=False)

    marc = record_data.find(f"{{{_MARC_NS}}}record") # 在 <recordData> 裡找 <record> 節點（MARC namespace），這就是一筆完整的書目記錄。
    if marc is None:
        return HoldingResult(found=False)

    # MMS ID from 001 control field
    mms_id = None
    ctrl = marc.find(f"{{{_MARC_NS}}}controlfield[@tag='001']")
    if ctrl is not None:
        mms_id = ctrl.text

    # AVA = physical holdings, AVE = electronic holdings
    physical_count = 0
    has_ebook = False

    # AVA 是 Alma 自訂的本地欄位，代表實體館藏。一本書可能在多個館各有館藏，每個館對應一個 AVA 欄位，所以用 findall 取全部再加總。$f subfield 是「total items」（總冊數）
    for field in marc.findall(f"{{{_MARC_NS}}}datafield[@tag='AVA']"):
        total_sub = field.find(f"{{{_MARC_NS}}}subfield[@code='f']")
        if total_sub is not None and total_sub.text:
            try:
                physical_count += int(total_sub.text)
            except ValueError:
                pass

    # AVE 是電子館藏欄位。只要找到一個 AVE 就代表有電子書，不需要計算數量，所以 break 立刻停止
    for _ in marc.findall(f"{{{_MARC_NS}}}datafield[@tag='AVE']"):
        has_ebook = True
        break

    return HoldingResult(
        found=True,
        physical_count=physical_count,
        has_ebook=has_ebook,
        mms_id=mms_id,
    )


def _query(cql: str) -> HoldingResult:
    params = {**_SRU_PARAMS, "query": cql}
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(_SRU_BASE, params=params)
        response.raise_for_status()
    return _parse_response(response.text)


def _clean_title(title: str) -> str:
    # Remove edition markers: 第十版, 第5版, ，第十版, （第五版）
    title = re.sub(r'[,，]?\s*第[〇一二三四五六七八九十百千\d]+[版刷]', '', title)
    # Remove full-width and half-width parenthetical content
    title = re.sub(r'（[^）]*）', '', title)
    title = re.sub(r'\([^)]*\)', '', title)
    # Remove embedded years: 計算機概論2023, Word 2019使用手冊
    title = re.sub(r'(?<!\d)20\d{2}(?!\d)', '', title)
    # Truncate at em-dash subtitle: 社會學概論——見樹又見林的社會學思維
    title = re.sub(r'——.*', '', title)
    # Truncate at full-width colon subtitle: 策略大師的企業社會責任新解：創造共享價值
    title = re.sub(r'：.*', '', title)
    # Truncate at hyphen subtitle: 多媒體導論與應用-新媒體藝術與互動科技
    title = re.sub(r'\s*-[^\-].*', '', title)
    return title.strip()


def check_holding(
    isbn: str | None,
    title: str,
) -> HoldingResult:
    if isbn:
        result = _query(f'alma.isbn="{isbn}"')
        if result.found:
            return result

    result = _query(f'alma.title="{title}"')
    if result.found:
        return result

    clean = _clean_title(title)
    if clean != title and clean:
        result = _query(f'alma.title="{clean}"')
    return result


if __name__ == "__main__":
    result = check_holding(isbn="9786263963849", title="")
    print(result)
