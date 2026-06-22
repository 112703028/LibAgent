from bs4 import BeautifulSoup
import httpx
import io
from pypdf import PdfReader

_TIMEOUT = 10.0

def fetch_student_number(course_id: str, semester: str):
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
    h4_tag = soup.find('h4', string="預收人數")

    if h4_tag:
        # 2. 找到 <h4> 附近的 <div class="icon"> 或是直接找 <i>
        # 這裡我們往上回溯到父層，再往下找 <i>
        parent_div = h4_tag.find_parent('div')
        target_i = parent_div.find('i', class_='sylview-icontextB')
    
    if target_i:
        student_number = target_i.text.strip()
        return student_number
    
    return None

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
    course_id = "703025001" 
    semester = "114-2"
    syllabus_text = fetch_syllabus(course_id, semester)
    student_number = fetch_student_number(course_id, semester)
    print(f"學生人數: {student_number}")
    print("\n" + "="*50)
    print(f"課程大綱: {syllabus_text}")