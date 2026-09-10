from library_agent.integrations import alma, google_books, loc, nla


def test_alma_clean_title():
    assert alma._clean_title("計算機概論2023") == "計算機概論"
    assert alma._clean_title("經濟學（第五版）") == "經濟學"
    assert alma._clean_title("社會學概論——見樹又見林") == "社會學概論"
    assert alma._clean_title("策略新解：創造共享價值") == "策略新解"


def test_google_books_clean_and_strip():
    assert google_books._clean_title("Management, 14th ed.") == "Management"
    assert google_books._clean_title("Microeconomics (Global Edition)") == "Microeconomics"
    assert google_books._strip_subtitle("World Politics: Trend & Transformation") == "World Politics"


def test_google_books_strip_volume():
    # 尾端卷號後綴會讓 intitle:"..." 精確片語比對 miss（即使書真的存在），需剝除
    assert google_books._strip_volume("Technics and Time, 1") == "Technics and Time"
    assert google_books._strip_volume("Integrated Korean, Vol. 2") == "Integrated Korean"
    assert google_books._strip_volume("A History, Volume 3") == "A History"
    # 不影響正常書名（沒有卷號後綴的原樣返回）
    assert google_books._strip_volume("World Politics") == "World Politics"
    # 不誤剝內容中的數字（只剝結尾的卷號模式）
    assert google_books._strip_volume("Catch-22") == "Catch-22"


def test_loc_clean_and_strip():
    assert loc._clean_title("Management, 14th ed.") == "Management"
    assert loc._strip_subtitle("World Politics: Trend") == "World Politics"


def test_loc_strip_volume():
    assert loc._strip_volume("Technics and Time, 1") == "Technics and Time"
    assert loc._strip_volume("Integrated Korean, Vol. 2") == "Integrated Korean"
    assert loc._strip_volume("World Politics") == "World Politics"


_ALMA_XML = """<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:numberOfRecords>1</srw:numberOfRecords>
  <srw:records><srw:record><srw:recordData>
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">99123</controlfield>
      <datafield tag="AVA"><subfield code="f">3</subfield></datafield>
      <datafield tag="AVA"><subfield code="f">2</subfield></datafield>
    </record>
  </srw:recordData></srw:record></srw:records>
</srw:searchRetrieveResponse>"""

_NO_RECORDS = ('<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">'
               '<srw:numberOfRecords>0</srw:numberOfRecords></srw:searchRetrieveResponse>')


def test_alma_parse_response_found():
    r = alma._parse_response(_ALMA_XML)
    assert r.found is True
    assert r.physical_count == 5      # 兩個 AVA 的 $f 加總 3+2
    assert r.has_ebook is False
    assert r.mms_id == "99123"


def test_alma_parse_response_not_found():
    assert alma._parse_response(_NO_RECORDS).found is False


_LOC_XML = """<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:numberOfRecords>1</srw:numberOfRecords>
  <srw:records><srw:record><srw:recordData>
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <datafield tag="245"><subfield code="a">Clean Code</subfield><subfield code="b">A Handbook</subfield></datafield>
      <datafield tag="100"><subfield code="a">Martin, Robert C.</subfield></datafield>
      <datafield tag="020"><subfield code="a">9780132350884</subfield></datafield>
    </record>
  </srw:recordData></srw:record></srw:records>
</srw:searchRetrieveResponse>"""


def test_loc_parse_response():
    r = loc._parse_response(_LOC_XML)
    assert r is not None
    assert r.canonical_title == "Clean Code A Handbook"
    assert r.canonical_authors == ["Martin, Robert C."]
    assert r.isbn_13 == "9780132350884"
    assert r.source == "loc"


def test_loc_parse_response_not_found():
    assert loc._parse_response(_NO_RECORDS) is None


_NCL_XML = """<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:numberOfRecords>1</srw:numberOfRecords>
  <srw:records><srw:record><srw:recordData>
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <datafield tag="245"><subfield code="a">社會學</subfield></datafield>
      <datafield tag="100"><subfield code="a">王小明</subfield></datafield>
      <datafield tag="020"><subfield code="a">9789570533439</subfield></datafield>
    </record>
  </srw:recordData></srw:record></srw:records>
</srw:searchRetrieveResponse>"""


def test_nla_parse_response():
    records = nla._parse_response(_NCL_XML)
    assert len(records) == 1
    r = records[0]
    assert r.canonical_title == "社會學"
    assert r.canonical_authors == ["王小明"]
    assert r.isbn_13 == "9789570533439"
    assert r.source == "ncl"


def test_nla_parse_response_not_found():
    assert nla._parse_response(_NO_RECORDS) == []


# 多筆候選：書名「游泳」搜到兩筆，一筆作者不符、一筆作者相符
_NCL_MULTI_XML = """<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:numberOfRecords>2</srw:numberOfRecords>
  <srw:records>
    <srw:record><srw:recordData>
      <record xmlns="http://www.loc.gov/MARC21/slim">
        <datafield tag="245"><subfield code="a">(增訂)小學生文庫</subfield></datafield>
        <datafield tag="100"><subfield code="a">王雲五</subfield></datafield>
      </record>
    </srw:recordData></srw:record>
    <srw:record><srw:recordData>
      <record xmlns="http://www.loc.gov/MARC21/slim">
        <datafield tag="245"><subfield code="a">游泳</subfield></datafield>
        <datafield tag="100"><subfield code="a">周靜玉</subfield></datafield>
      </record>
    </srw:recordData></srw:record>
  </srw:records>
</srw:searchRetrieveResponse>"""


def test_nla_parse_response_multi_returns_all_candidates():
    records = nla._parse_response(_NCL_MULTI_XML)
    assert len(records) == 2
    assert records[0].canonical_title == "(增訂)小學生文庫"
    assert records[1].canonical_title == "游泳"


def test_pick_best_match_prefers_author_overlap():
    records = nla._parse_response(_NCL_MULTI_XML)
    # 查詢作者「周靜玉」應該選到第二筆，即使它不是第一筆結果
    best = nla._pick_best_match(records, ["周靜玉"])
    assert best.canonical_title == "游泳"


def test_pick_best_match_falls_back_to_first_when_no_author_match():
    records = nla._parse_response(_NCL_MULTI_XML)
    # 查詢作者跟任何候選都不符 → 退回第一筆（向後兼容既有行為）
    best = nla._pick_best_match(records, ["某個查無此人的作者"])
    assert best.canonical_title == "(增訂)小學生文庫"


def test_pick_best_match_no_authors_given_falls_back_to_first():
    records = nla._parse_response(_NCL_MULTI_XML)
    # 沒有查詢作者資訊時 → 退回第一筆
    best = nla._pick_best_match(records, None)
    assert best.canonical_title == "(增訂)小學生文庫"


def test_pick_best_match_handles_punctuation_and_spacing_differences():
    records = nla._parse_response(_NCL_MULTI_XML)
    # 查詢作者帶全形逗號/空白，仍應正確比對到「周靜玉」
    best = nla._pick_best_match(records, ["周, 靜玉"])
    assert best.canonical_title == "游泳"


def test_pick_best_match_empty_records_returns_none():
    assert nla._pick_best_match([], ["任何人"]) is None
