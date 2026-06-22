from bs4 import BeautifulSoup
import httpx
import io
from pypdf import PdfReader

_TIMEOUT = 10.0
def fetch_syllabus(course_id: str, semester: str):
    base_id = course_id[:6]   # "000351"
    gop     = course_id[6:8]  # "02"
    s       = course_id[8]    # "1"
    yy, smt = semester.split("-")  # "114", "1"

    url = (
        f"https://newdoc.nccu.edu.tw/teaschm/{yy}{smt}/"
        f"schmPrv.jsp-yy={yy}&smt={smt}&num={base_id}&gop={gop}&s={s}.html"
    )

    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(url)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser") 
    
    course_description = soup.find('h2', class_='text-primary')
    next_element = course_description.find_next_sibling().get_text(strip=True)

    course_goal = soup.find('div', class_ = "col-p-8").get_text(strip=True)

    output = next_element + f"\n" + course_goal

    if output:
        return output
    return None

def fetch_syllabus_pdf(course_id: str, semester: str):
    base_id = course_id[:6]   # "000351"
    gop     = course_id[6:8]  # "02"
    s       = course_id[8]    # "1"
    yy, smt = semester.split("-")  # "114", "1"

    url = (
        f"https://newdoc.nccu.edu.tw/teaschm/{yy}{smt}/"
        f"schmPrv.jsp-yy={yy}&smt={smt}&num={base_id}&gop={gop}&s={s}.html"
    )

    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.get(url)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser") 

    # 找 name 屬性開頭是 "FileIdName_" 的 input
    file_name_input = soup.find("input", {"name": lambda n: n and n.startswith("FileIdName_")})
    file_url_input  = soup.find("input", {"name": lambda n: n and n.startswith("FileUrlStr_")})
    
    if file_name_input and file_url_input:
        pdf_url = file_url_input["value"] + file_name_input["value"]
    else:
        return None
    
    # 下載 PDF bytes
    with httpx.Client(timeout=_TIMEOUT) as client:
        pdf_response = client.get(pdf_url)
        pdf_bytes = pdf_response.content

    # 讀取 PDF 並提取文字
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join(page.extract_text() for page in reader.pages)

    return text

if __name__ == "__main__":
    course_id = "703009001" 
    semester = "114-2"
    syllabus_text = fetch_syllabus(course_id, semester)
    print(syllabus_text)