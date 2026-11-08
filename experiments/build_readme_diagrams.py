"""產生 README 用的兩張 draw.io 圖（.drawio 原始檔，可在 diagrams.net 開啟編輯）。
色彩與儀表板一致：紙／墨／行動藍／赭黃，外部資料來源用青灰。節點以座標直接擺放，不用容器（容器的子節點座標是相對的，容易錯）。"""
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

OUT = Path(sys.argv[1])

FONT = "Microsoft JhengHei"
INK, SOFT, PAPER = "#1A2430", "#5B6673", "#F1F2EE"
BLUE, BLUE_WASH = "#26477C", "#E8EDF6"
TEAL, TEAL_WASH = "#2F5D62", "#E3ECEC"
OCHRE, OCHRE_WASH = "#BF8A1C", "#FBF3DF"


def base(extra: str) -> str:
    return f"html=1;whiteSpace=wrap;fontFamily={FONT};fontSize=12;fontColor={INK};align=center;verticalAlign=middle;spacing=6;{extra}"


STYLE = {
    "agent": base(f"rounded=1;arcSize=8;fillColor=#FFFFFF;strokeColor={INK};strokeWidth=1.5;"),
    "source": base(f"rounded=1;arcSize=8;fillColor={TEAL_WASH};strokeColor={TEAL};strokeWidth=1.2;"),
    "llm": base(f"rounded=1;arcSize=30;fillColor={BLUE_WASH};strokeColor={BLUE};strokeWidth=1.2;"),
    "human": base(f"rounded=1;arcSize=8;dashed=1;dashPattern=6 4;fillColor={OCHRE_WASH};strokeColor={OCHRE};strokeWidth=1.5;"),
    "db": base(f"shape=cylinder3;boundedLbl=1;backgroundOutline=1;size=9;fillColor=#FFFFFF;strokeColor={INK};strokeWidth=1.5;"),
    "app": base(f"rounded=1;arcSize=8;fillColor={BLUE};strokeColor={BLUE};fontColor=#FFFFFF;strokeWidth=1.5;"),
    "input": base(f"rounded=1;arcSize=8;fillColor=#FFFFFF;strokeColor={SOFT};strokeWidth=1.2;strokeDasharray=1 3;"),
    "frame": f"rounded=0;html=1;fontFamily={FONT};fontSize=12;fontColor={SOFT};align=left;verticalAlign=top;spacingLeft=12;spacingTop=6;"
             f"fillColor=none;strokeColor=#8A939D;strokeWidth=1;dashed=0;",
    "decision": base(f"rhombus;fillColor=#FFFFFF;strokeColor={INK};strokeWidth=1.5;spacing=2;"),
    "note": f"text;html=1;fontFamily={FONT};fontSize=11;fontColor={SOFT};align=center;verticalAlign=middle;whiteSpace=wrap;",
}
EDGE = (f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;endFill=1;strokeColor={SOFT};strokeWidth=1.5;"
        f"fontFamily={FONT};fontSize=11;fontColor={SOFT};labelBackgroundColor={PAPER};")


class Diagram:
    def __init__(self, width: int, height: int):
        self.w, self.h, self.cells, self.n = width, height, [], 1

    def node(self, key: str, x, y, w, h, label: str, style: str, **kw) -> str:
        self.n += 1
        cid = f"n{self.n}"
        self.cells.append(
            f'<mxCell id="{cid}" value={quoteattr(label)} style={quoteattr(STYLE[style] + kw.get("extra", ""))} vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
        return cid

    def edge(self, src: str, dst: str, label: str = "", style: str = "", points=()) -> None:
        self.n += 1
        pts = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in points)
        arr = f'<Array as="points">{pts}</Array>' if pts else ""
        self.cells.append(
            f'<mxCell id="e{self.n}" value={quoteattr(label)} style={quoteattr(EDGE + style)} edge="1" parent="1" source="{src}" target="{dst}">'
            f'<mxGeometry relative="1" as="geometry">{arr}</mxGeometry></mxCell>')

    def save(self, path: Path) -> None:
        xml = (f'<mxfile host="app.diagrams.net"><diagram name="1" id="d1"><mxGraphModel dx="{self.w}" dy="{self.h}" grid="0" gridSize="10" '
               f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{self.w}" pageHeight="{self.h}" '
               f'math="0" shadow="0" background="{PAPER}"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
               + "".join(self.cells) + "</root></mxGraphModel></diagram></mxfile>")
        path.write_text(xml, encoding="utf-8")


def two_line(name: str, desc: str, desc_color: str = SOFT) -> str:
    return f'<b style="font-size:13px">{escape(name)}</b><br><span style="font-size:11px;color:{desc_color}">{desc}</span>'


# ---------------------------------------------------------------- 圖一：整體架構
def architecture() -> Diagram:
    d = Diagram(1200, 690)
    # pipeline 外框（先畫，在最底層）
    frame = d.node("frame", 30, 148, 1140, 190, "LangGraph pipeline：依序執行", "frame",
                   extra="align=right;verticalAlign=bottom;spacingRight=12;spacingBottom=6;spacingLeft=0;spacingTop=0;")
    names = [("crawler", "讀課綱 Excel<br>存進資料庫"), ("parser", "LLM 從課綱<br>解析出書目"),
             ("discoverer", "沒有書目的課<br>由 LLM 推薦"), ("validator", "驗證書目<br>是否真的存在"),
             ("match_auditor", "配到的是不是<br>同一本書"), ("librarian", "查 Alma<br>館藏與冊數"),
             ("recommender", "算優先級與冊數<br>LLM 寫理由")]
    ids, x0, step, y, bw, bh = [], 54, 158, 208, 134, 96
    for i, (n, desc) in enumerate(names):
        ids.append(d.node(n, x0 + i * step, y, bw, bh, two_line(n, desc), "agent"))
    for a, b in zip(ids, ids[1:]):
        if a == ids[3]:      # validator → match_auditor
            d.edge(a, b)
        else:
            d.edge(a, b)
    # 回饋迴路：validator 查不到的 AI 推薦書，回到 discoverer 重推
    d.edge(ids[3], ids[2], "", "exitX=0.25;exitY=0;entryX=0.75;entryY=0;strokeColor=#BF8A1C;dashed=1;strokeWidth=2;",
           points=[(561, 190), (470, 190)])
    # 回圈的說明放在兩條 LLM 箭頭之間的空檔，不壓到任何線
    d.node("loopnote", 462, 160, 270, 22, "驗證失敗的 AI 推薦書，回去重推（有輪數上限）", "note", extra="fontColor=#7A4F00;")
    # 輸入
    src = d.node("xlsx", 54, 40, 134, 60, "課程大綱 Excel<br>政大課程查詢系統", "input")
    d.edge(src, ids[0])
    # LLM 帶：parser、discoverer、match_auditor、recommender 都會用
    llm = d.node("llm", 212, 40, 924, 44, "LLM（OpenAI 相容 API）：解析書目、推薦書目、判斷是不是同一本、寫採購理由", "llm")
    for i in (1, 2, 4, 6):
        d.edge(llm, ids[i], "", f"exitX={((x0 + i * step + bw / 2) - 212) / 924:.4f};exitY=1;entryX=0.5;entryY=0;strokeColor={BLUE};endSize=4;")
    # 外部書目來源（validator 下方）
    srcs = d.node("sources", 484, 388, 222, 168,
                  f'<b style="font-size:12px">書目來源，依序查到為止</b><br><br>NCL／NBINet（中文）<br>LOC（英文）<br>Google Books<br>三民、TAAZE（書商）', "source")
    d.edge(ids[3], srcs, "", "exitX=0.5;exitY=1;entryX=0.5;entryY=0;endArrow=classic;startArrow=classic;startFill=1;")
    # 稽核 → 待審
    hr = d.node("human_review", 730, 388, 134, 72, f'<b style="font-size:12px">human_review</b><br><span style="font-size:11px">存疑的書<br>送人工審核</span>', "human")
    d.edge(ids[4], hr, "", f"exitX=0.5;exitY=1;entryX=0.5;entryY=0;strokeColor={OCHRE};dashed=1;")
    # Alma
    alma = d.node("alma", 888, 388, 134, 72, f'<b style="font-size:12px">Alma</b><br><span style="font-size:11px">政大圖書館館藏</span>', "source")
    d.edge(ids[5], alma, "", "exitX=0.5;exitY=1;entryX=0.5;entryY=0;endArrow=classic;startArrow=classic;startFill=1;")
    # 資料庫與儀表板
    db = d.node("db", 54, 590, 640, 70,
                f'<b style="font-size:13px">PostgreSQL</b><br><span style="font-size:11px;color:{SOFT}">課程、書目、驗證結果、館藏、採購建議、購物車的決定</span>', "db")
    d.edge(frame, db, "所有 Agent 都讀寫這裡", "exitX=0.05;exitY=1;entryX=0.0713;entryY=0;endArrow=classic;startArrow=classic;startFill=1;")
    app = d.node("dash", 760, 590, 410, 70,
                 '<b style="font-size:13px">儀表板（FastAPI）</b><br><span style="font-size:11px">採購表、審核、單筆追溯、購物車，匯出 CSV</span>', "app")
    d.edge(db, app, "", "exitX=1;exitY=0.5;entryX=0;entryY=0.5;exitDx=0;exitDy=0;endArrow=classic;startArrow=classic;startFill=1;")
    d.edge(hr, app, "在儀表板審核", f"exitX=0.5;exitY=1;entryX=0.2;entryY=0;strokeColor={OCHRE};dashed=1;")
    return d


# ---------------------------------------------------------------- 圖二：一本書怎麼走到購物車
def journey() -> Diagram:
    d = Diagram(1380, 470)
    top, th = 70, 84
    a = d.node("a", 30, top, 122, th, two_line("課綱上的一行書目", "書名、作者"), "input")
    b = d.node("b", 192, top, 160, th, two_line("驗證", "NCL 或 LOC，再 Google Books，<br>再三民、TAAZE"), "agent")
    c = d.node("c", 392, top - 10, 150, th + 20, "配到的是<br>同一本嗎？", "decision")
    e = d.node("e", 642, top, 128, th, two_line("查 Alma 館藏", "有沒有、有幾冊"), "agent")
    f = d.node("f", 806, top, 138, th, two_line("算冊數", "需求減現有，<br>單本上限 5 冊"), "agent")
    g = d.node("g", 980, top, 138, th, two_line("採購建議", "附優先級與理由，<br>點 ▸ 看決策過程"), "agent")
    h = d.node("h", 1182, top, 168, th, two_line("購物車", "人決定冊數，<br>匯出 CSV 下單", "#DCE4F1"), "app")
    d.edge(a, b)
    d.edge(b, c)
    d.edge(c, e, "是，同一本")
    d.edge(e, f)
    d.edge(f, g)
    d.edge(g, h, "人來決定")
    bot, bh = 300, 84
    i = d.node("i", 392, bot, 150, bh, two_line("審核", "配錯、查無此書、<br>其他"), "human")
    j = d.node("j", 582, bot, 170, bh, two_line("人決定", "加入採購建議（可改書名），<br>或都不買"), "human")
    k = d.node("k", 792, bot, 150, bh, two_line("套用", "重查館藏、<br>重算採購建議"), "agent")
    d.edge(c, i, "不是，配錯", f"exitX=0.5;exitY=1;entryX=0.5;entryY=0;strokeColor={OCHRE};")
    d.edge(b, i, "都查不到（查無此書）", f"exitX=0.5;exitY=1;entryX=0;entryY=0.5;strokeColor={OCHRE};dashed=1;")
    d.edge(i, j, "", f"strokeColor={OCHRE};")
    d.edge(j, k, "", f"strokeColor={OCHRE};")
    d.edge(k, g, "", "exitX=1;exitY=0.5;entryX=0.5;entryY=1;")
    d.node("note", 392, 410, 640, 40, "決定存在資料庫裡，重跑 pipeline 不會被洗掉；書的識別鍵改變時，購物車的決定會跟著搬", "note")
    return d


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    architecture().save(OUT / "architecture.drawio")
    journey().save(OUT / "book-journey.drawio")
    print("wrote", OUT)
