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


def test_loc_clean_and_strip():
    assert loc._clean_title("Management, 14th ed.") == "Management"
    assert loc._strip_subtitle("World Politics: Trend") == "World Politics"


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
    r = nla._parse_response(_NCL_XML)
    assert r is not None
    assert r.canonical_title == "社會學"
    assert r.canonical_authors == ["王小明"]
    assert r.isbn_13 == "9789570533439"
    assert r.source == "ncl"


def test_nla_parse_response_not_found():
    assert nla._parse_response(_NO_RECORDS) is None
