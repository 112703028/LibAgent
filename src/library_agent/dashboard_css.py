"""儀表板的樣式表。

從 dashboard.py 的 f-string 抽出來（那裡大括號要寫兩遍，改樣式很容易出錯），這裡是一般字串。
設計重點：採購表是主角；優先級做成「書脊」——每一列最左邊一條細細的彩色竖條，取代每列一顆藥丸；
整個工作區是一張紙、用細線分段，不把內容切成一堆圓角卡片。
色彩：紙 / 墨 / 行動藍 + 四種書脊色（高＝氧化紅、中＝赭黃、低＝書架綠、略過＝石灰）。
"""

CSS = """
:root {
  color-scheme: light;                /* 強制淺色，不隨 OS 深色模式變黑 */
  --paper:#f1f2ee; --sheet:#ffffff; --wash:#f6f7f4;
  --ink:#1a2430; --ink-soft:#5b6673; --ink-faint:#8a939d;
  --rule:#d8dbd5; --rule-soft:#e8eae5;
  --high:#9b3a32; --mid:#bf8a1c; --low:#4b8661; --skip:#a3a8a8;
  --action:#26477c; --action-wash:#e8edf6;
  --warn:#7a4f00; --warn-wash:#fbf3df; --danger:#9b3a32;
  --r:4px;
  --font:"Noto Sans TC","Microsoft JhengHei UI","Microsoft JhengHei","PingFang TC",system-ui,sans-serif;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--font); font-size:14px; line-height:1.6; }
.wrap { max-width:1600px; margin:0 auto; padding:0 28px 80px; }
h1 { font-size:17px; font-weight:600; margin:0; letter-spacing:.02em; }
h2 { font-size:15px; font-weight:600; margin:0 0 12px; }
button, input, select { font-family:inherit; }
:focus-visible { outline:2px solid var(--action); outline-offset:2px; }
@media (prefers-reduced-motion:reduce) { * { transition:none !important; animation:none !important; } }

/* ---- 頂欄：購物車、標題、學系、管線狀態 ---- */
.topbar { display:flex; align-items:center; gap:14px; padding:16px 0 12px; flex-wrap:wrap; }
.tb-spacer { flex:1; }
.carttop { position:relative; display:inline-flex; align-items:center; justify-content:center; width:40px; height:40px;
  padding:0; border:1px solid var(--rule); border-radius:var(--r); background:var(--sheet); color:var(--ink); cursor:pointer; }
.carttop:hover { border-color:var(--ink-soft); }
.carttop svg { width:21px; height:21px; }
.cartcount { position:absolute; top:-7px; right:-7px; min-width:20px; height:20px; padding:0 5px; border-radius:10px;
  background:var(--action); color:#fff; font-size:12px; font-weight:600; line-height:20px; text-align:center; font-variant-numeric:tabular-nums; }
.cartcount.zero { background:var(--ink-faint); }

/* ---- 管線：平常只有一行狀態，展開才有控制與終端機輸出 ---- */
.pipeline { border-top:1px solid var(--rule); border-bottom:1px solid var(--rule); background:var(--paper); }
.pipe-sum { display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding:8px 0; cursor:pointer; list-style:none; font-size:13px; }
.pipe-sum::-webkit-details-marker { display:none; }
.pipe-sum::before { content:"▸"; color:var(--ink-soft); font-size:11px; width:10px; }
.pipeline[open] > .pipe-sum::before { content:"▾"; }
.pipe-name { font-weight:600; }
.runstatus { font-weight:600; color:var(--ink-soft); }
.runstatus.running { color:var(--warn); }
.runstatus.done { color:var(--low); }
.runstatus.error { color:var(--danger); }
.counts { color:var(--ink-faint); font-variant-numeric:tabular-nums; font-size:12.5px; }
.runstatus:not(.running) ~ .counts { display:none; }
.pipe-body { padding:4px 0 16px; }
.runbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
.runlog { margin:0; padding:12px 14px; border-radius:var(--r); background:var(--ink); color:#dfe5ec;
  font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,"Microsoft JhengHei",monospace;
  max-height:340px; min-height:96px; overflow:auto; white-space:pre-wrap; word-break:break-all; }
.runlog:empty::before { content:"（尚無執行紀錄；按「執行」後這裡會即時顯示終端機的輸出）"; color:#8b949e; }
.flownote { margin:10px 0 0; font-size:12.5px; line-height:1.7; color:var(--ink-soft); max-width:80ch; }
.flownote b { color:var(--ink); font-weight:600; }

/* ---- 按鈕與輸入：同一套小圓角 ---- */
.runbtn { font-size:14px; font-weight:600; padding:7px 16px; border-radius:var(--r); cursor:pointer;
  border:1px solid var(--action); background:var(--action); color:#fff; }
.runbtn:hover { background:#1d3865; }
.runbtn:disabled { opacity:.45; cursor:not-allowed; }
.uploadbtn { font-size:13px; padding:5px 12px; border-radius:var(--r); cursor:pointer;
  border:1px solid var(--rule); background:var(--sheet); color:var(--ink); }
.uploadbtn:hover { border-color:var(--ink-soft); }
.abtn { font-size:13px; padding:4px 10px; margin:0 4px 4px 0; border-radius:var(--r); cursor:pointer;
  border:1px solid var(--rule); background:var(--sheet); color:var(--ink); white-space:nowrap; }
.abtn:hover { border-color:var(--ink-soft); }
.abtn:disabled { opacity:.5; cursor:not-allowed; }
.abtn-primary { border-color:var(--action); background:var(--action); color:#fff; }
.abtn-primary:hover { background:#1d3865; border-color:#1d3865; }
.abtn-no { color:var(--danger); }
.searchbox, .atitle, .aisbn, .dec-in, .ci-in { font-size:13.5px; padding:5px 9px; border:1px solid var(--rule);
  border-radius:var(--r); background:var(--sheet); color:var(--ink); }
.searchbox { width:220px; }
.ffwrap { position:relative; }
.filefilter { position:absolute; top:calc(100% + 4px); left:0; z-index:20; min-width:220px;
  max-height:300px; overflow-y:auto; display:flex; flex-direction:column; gap:6px;
  padding:10px 12px; font-size:13px; color:var(--ink-soft);
  background:var(--sheet); border:1px solid var(--rule); border-radius:var(--r);
  box-shadow:0 6px 18px rgba(26,36,48,.12); }
.filefilter[hidden] { display:none; }
.filefilter label { display:flex; align-items:center; gap:6px; cursor:pointer; white-space:nowrap; }
.filefilter .ff-actions { display:flex; gap:12px; padding-bottom:6px; margin-bottom:2px; border-bottom:1px solid var(--rule-soft); }
.filefilter .ff-actions a { color:var(--action); cursor:pointer; }
.filefilter .ff-empty { color:var(--ink-faint); }
.dept-college { margin-top:4px; padding-top:4px; border-top:1px solid var(--rule-soft); }
.dept-col-lbl { font-size:13px; }
.dept-item { padding-left:18px; color:var(--ink-soft); }
#exppanel { right:0; left:auto; }
.exp-sec { font-weight:600; color:var(--ink-soft); padding-top:4px; margin-top:2px; border-top:1px solid var(--rule-soft); }
.exp-sec:first-child { border-top:none; margin-top:0; padding-top:0; }

/* ---- 帳本式數字（取代七張 KPI 卡）---- */
.kpi-grid { display:flex; flex-wrap:wrap; gap:6px 32px; align-items:baseline; padding:18px 0 14px; }
.kpi { display:flex; align-items:baseline; gap:7px; color:var(--ink); text-decoration:none; }
a.kpi:hover .lbl { color:var(--ink); text-decoration:underline; }
.kpi .val { font-size:24px; font-weight:600; letter-spacing:-.01em; font-variant-numeric:tabular-nums; line-height:1.2; }
.kpi .val.accent { color:var(--action); }
.kpi .val.warn { color:var(--warn); }
.kpi .lbl { font-size:13px; color:var(--ink-soft); }

/* ---- 書脊比例帶：整批書一眼看完 ---- */
.overview { display:grid; grid-template-columns:1fr 1fr; gap:14px 40px; padding:0 0 22px; }
.strip-h { font-size:13px; color:var(--ink-soft); margin-bottom:6px; }
.strip-bar { display:flex; height:12px; gap:2px; }
.strip-bar .seg { display:block; min-width:2px; border-radius:1px; }
.strip-key { list-style:none; margin:8px 0 0; padding:0; display:flex; flex-wrap:wrap; gap:2px 18px; font-size:13px; color:var(--ink-soft); }
.strip-key li { display:flex; align-items:center; gap:6px; }
.strip-key b { color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums; }
.sw { width:9px; height:9px; border-radius:1px; display:inline-block; }
.p-high { background:var(--high); } .p-medium { background:var(--mid); } .p-low { background:var(--low); }
.p-skip, .p-none { background:var(--skip); }
.s-missing { background:#2f5d62; } .s-partial { background:#5e8b8f; } .s-owned_physical { background:#8fb1b3; }
.s-owned_ebook { background:#bcd3d4; } .s-unknown { background:#dde6e6; }
@media (max-width:820px) { .overview { grid-template-columns:1fr; } }

/* ---- 工作區：一張紙 ---- */
.card { background:var(--sheet); border:1px solid var(--rule); border-radius:var(--r); padding:0 20px 20px; margin-bottom:20px; }
.cardhead { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; padding-top:16px; }
.cardhead h2 { margin:0; }
.toplevel { display:flex; gap:2px; margin:0 -20px 0; padding:0 20px; border-bottom:1px solid var(--rule); }
.toptab { font-size:15px; font-weight:600; padding:14px 18px 12px; margin-bottom:-1px; border:none;
  border-bottom:3px solid transparent; background:none; color:var(--ink-soft); cursor:pointer; }
.toptab:hover { color:var(--ink); }
.toptab.active { color:var(--ink); border-bottom-color:var(--action); }
.filters { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
.maintabs { padding:14px 0 12px; margin-bottom:0; border-bottom:1px solid var(--rule-soft); }
.fbtn, .cbtn, .rtab { font-size:13px; padding:4px 11px; border-radius:var(--r); cursor:pointer;
  border:1px solid var(--rule); background:var(--sheet); color:var(--ink-soft); }
.fbtn:hover, .cbtn:hover, .rtab:hover { border-color:var(--ink-soft); color:var(--ink); }
.fbtn.active, .cbtn.active, .rtab.active { background:var(--ink); color:#fff; border-color:var(--ink); }
.fbtn .n { margin-left:4px; color:var(--ink-faint); font-variant-numeric:tabular-nums; }
.fbtn.active .n { color:#c4ccd6; }
.maintabs[hidden], .rtab[hidden], .rapply[hidden], .applyhint[hidden] { display:none; }
.toolbar { display:flex; gap:10px 14px; flex-wrap:wrap; align-items:center; padding:12px 0;
  position:sticky; top:0; z-index:6; background:var(--sheet); border-bottom:1px solid var(--rule-soft); }
.tb-prio, .tb-cat { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.tb-prio[hidden], .tb-cat[hidden], .exp-prio[hidden] { display:none; }
.tb-cat { padding-left:14px; border-left:1px solid var(--rule); }
.tb-export { margin-left:auto; }
.rbatch { margin:10px 0 0; font-size:13px; color:var(--ink-soft); }
.rnote { margin:12px 0 8px; font-size:13px; color:var(--ink-soft); max-width:90ch; }
.rapply, .applyhint { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin:14px 0 0; padding:9px 14px;
  border-radius:var(--r); border-left:3px solid var(--mid); background:var(--warn-wash); font-size:13.5px; }
.applyhint .abtn { margin:0; }
.empty { color:var(--ink-faint); font-size:13.5px; padding:16px 0; }
.headtools { display:flex; align-items:center; gap:8px; }
.tabspacer { flex:1; }

/* ---- 表格 ---- */
table { border-collapse:separate; border-spacing:0; width:100%; font-size:13.5px; }
th, td { border-bottom:1px solid var(--rule-soft); padding:10px 12px; text-align:left; vertical-align:top; }
thead th { position:sticky; top:var(--tbh,58px); z-index:5; background:var(--sheet); color:var(--ink-soft);
  font-weight:600; font-size:12.5px; white-space:nowrap; border-bottom:1px solid var(--rule); }
tbody tr:hover > td { background:var(--wash); }
th.chk, td.chk { width:30px; text-align:center; padding-left:12px; padding-right:4px; }
td.num { text-align:right; font-variant-numeric:tabular-nums; font-weight:600; white-space:nowrap; }
td.rationale { color:var(--ink-soft); font-size:12.5px; min-width:200px; max-width:360px; }
.clamp { display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; line-clamp:2; overflow:hidden; }
.clamp3 { -webkit-line-clamp:3; line-clamp:3; }
#rectable td:nth-child(3) { min-width:150px; max-width:240px; }
#rectable thead th:nth-child(7) { text-align:right; }
.rtbl td { font-size:13px; }
.rtbl td:nth-child(2) { white-space:nowrap; color:var(--ink-faint); font-size:12.5px; }
.rtbl td:nth-child(2) .pill { color:var(--ink); }
.rdim { color:var(--ink-soft); }
tr.rdone td { opacity:.45; transition:opacity .4s; }
/* 書脊：每一列最左邊一條彩色竖條（顏色不是唯一資訊：旁邊的格子仍寫著文字） */
tr[data-priority] > td:first-child { box-shadow:inset 4px 0 0 var(--spine, transparent); }
tr[data-priority="high"] { --spine:var(--high); } tr[data-priority="medium"] { --spine:var(--mid); }
tr[data-priority="low"] { --spine:var(--low); } tr[data-priority="skip"] { --spine:var(--skip); }
tr[data-priority="none"] { --spine:var(--rule); }
.pill { display:inline-flex; align-items:center; gap:6px; font-size:13px; white-space:nowrap; color:var(--ink); background:none; padding:0; }
.pill::before { content:""; width:8px; height:8px; border-radius:50%; background:var(--dot, var(--skip)); flex:none; }
.pill[class*=" p-"]::before { display:none; }
.pill.p-high { --dot:var(--high); background:none; } .pill.p-medium { --dot:var(--mid); background:none; }
.pill.p-low { --dot:var(--low); background:none; } .pill.p-skip, .pill.p-none { --dot:var(--skip); background:none; }
.pill.s-missing { --dot:#2f5d62; background:none; } .pill.s-partial { --dot:#5e8b8f; background:none; }
.pill.s-owned_physical { --dot:#8fb1b3; background:none; } .pill.s-owned_ebook { --dot:#bcd3d4; background:none; }
.pill.s-unknown { --dot:#c9d2d2; background:none; }
.tag { display:inline-block; padding:0 7px; border-radius:var(--r); font-size:12px; border:1px solid var(--rule); color:var(--ink-soft); white-space:nowrap; }
.tag-ai { border-color:var(--mid); color:var(--warn); background:var(--warn-wash); }
.c-in { font-weight:600; color:var(--ink); }
.unv { display:inline-block; margin-left:6px; padding:0 6px; border-radius:var(--r); border:1px solid var(--mid);
  color:var(--warn); font-size:11.5px; vertical-align:middle; cursor:help; background:var(--warn-wash); }
.unv::after { content:"未驗證"; }
.tbtn { width:20px; height:20px; margin-right:6px; padding:0; border:1px solid var(--rule); border-radius:var(--r);
  background:var(--sheet); color:var(--ink-soft); cursor:pointer; vertical-align:middle; }
.tbtn:hover { border-color:var(--ink-soft); }
.tbtn::before { content:"▸"; font-size:11px; line-height:1; }
.tbtn[aria-expanded="true"]::before { content:"▾"; }
.bt { font-weight:600; }

/* ---- 採購冊數：輸入框是決定，系統建議只是參考 ---- */
td.num.cut::after { content:"（需求 " attr(data-need) "）"; font-weight:400; font-size:11.5px; color:var(--warn); white-space:nowrap; }
td.dec { width:208px; min-width:208px; white-space:nowrap; }
.dec-in { display:inline-block; width:76px; box-sizing:border-box; text-align:right; vertical-align:middle; }
.dec-in.has { border-color:var(--action); font-weight:600; background:var(--action-wash); }
.cartbtn { display:inline-block; width:auto; margin:0 0 0 6px; white-space:nowrap; vertical-align:middle; }
td.dec .aerr { white-space:normal; }
td.dec[data-stale]::after { content:"建議已從 " attr(data-stale) " 變成 " attr(data-now); display:block; font-size:11.5px; color:var(--warn); margin-top:2px; }

/* ---- 審核列的處理欄 ---- */
.ract { min-width:320px; }
.atitle, .aisbn { display:block; width:100%; margin-bottom:5px; }
.aerr { color:var(--danger); font-size:12.5px; margin-top:2px; }

/* ---- 追溯（展開的決策過程）---- */
tr.trace-row > td { background:var(--wash); padding:16px 20px 18px 28px; }
.trace { font-size:13.5px; line-height:1.65; max-width:1100px; }
.tr-title { margin:0 0 4px; font-size:16px; }
.tr-rationale { color:var(--ink-soft); margin-bottom:6px; }
.tr-sec { border-top:1px solid var(--rule); padding:10px 0 4px; margin-top:12px; }
.tr-sec h4 { margin:0 0 6px; font-size:13.5px; font-weight:600; }
.tr-dim { color:var(--ink-soft); }
.tr-mention { margin:4px 0; padding:4px 12px; border-left:3px solid var(--rule); color:var(--ink-soft); }
.tr-pre { white-space:pre-wrap; max-height:240px; overflow:auto; font:inherit; font-size:12.5px; }
.tr-tbl th, .tr-tbl td { padding:4px 8px; font-size:12.5px; }
.tr-pair { display:flex; gap:14px; flex-wrap:wrap; align-items:flex-start; padding:10px 12px; border:1px solid var(--rule); border-radius:var(--r); margin-bottom:8px; background:var(--sheet); }
.tr-side { flex:1 1 280px; }
.tr-side h5 { margin:0 0 4px; font-size:12.5px; font-weight:600; color:var(--ink-soft); }
.tr-mid { align-self:center; color:var(--ink-soft); }
.tr-verdict { flex-basis:100%; }
.tr-bad { border-color:var(--high); background:#faf0ee; }
.tr-tag { display:inline-block; padding:0 6px; border-radius:var(--r); border:1px solid var(--rule); font-size:12px; }
.tr-note { padding:6px 12px; border-radius:var(--r); background:var(--sheet); border:1px solid var(--rule); }
.tr-calc { margin:4px 0 0; padding-left:20px; }
.tr-drift { margin-top:8px; padding:6px 12px; border-radius:var(--r); border-left:3px solid var(--mid); background:var(--warn-wash); }

/* ---- 購物車抽屜 ---- */
#cartshade { position:fixed; inset:0; background:rgba(26,36,48,.32); z-index:40; }
.cartdrawer { position:fixed; top:0; left:0; bottom:0; width:min(480px,100vw); background:var(--sheet);
  border-right:1px solid var(--rule); box-shadow:4px 0 24px rgba(26,36,48,.16); z-index:50; display:flex; flex-direction:column; }
.cartdrawer[hidden], #cartshade[hidden] { display:none; }
.cdhead { display:flex; align-items:center; justify-content:space-between; padding:16px 20px; border-bottom:1px solid var(--rule); }
.cdhead h2 { margin:0; font-size:17px; }
.cdclose { font-size:24px; line-height:1; border:none; background:none; cursor:pointer; color:var(--ink-soft); }
.cdlist { flex:1; overflow-y:auto; padding:0 20px; }
.ci { display:grid; grid-template-columns:1fr auto auto; gap:4px 12px; align-items:center; padding:12px 0 12px 12px;
  border-bottom:1px solid var(--rule-soft); box-shadow:inset 4px 0 0 var(--spine, transparent); margin-left:-12px; }
.ci[data-p="high"] { --spine:var(--high); } .ci[data-p="medium"] { --spine:var(--mid); }
.ci[data-p="low"] { --spine:var(--low); } .ci[data-p="skip"] { --spine:var(--skip); }
.ci-title { font-size:14px; font-weight:600; overflow-wrap:anywhere; }
.ci-sub { grid-column:1; font-size:12.5px; color:var(--ink-soft); }
.ci-in { width:76px; font-weight:600; text-align:right; border-color:var(--action); background:var(--action-wash); }
.ci .abtn { margin:0; }
.ci .aerr { grid-column:1 / -1; }
.ci-n { font-size:14px; font-weight:600; font-variant-numeric:tabular-nums; }
.cdfoot { padding:14px 20px; border-top:1px solid var(--rule); display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.cdtotal { flex:1 1 100%; font-size:15px; font-weight:600; font-variant-numeric:tabular-nums; }
.cdorph { margin:0 20px; padding:10px 0 4px; border-top:1px dashed var(--rule); }
.cdorph[hidden] { display:none; }
.cdorph-h { font-size:13.5px; font-weight:600; color:var(--warn); }

/* ---- 窄螢幕 ---- */
@media (max-width:1100px) { #rectable td.rationale, #rectable th:nth-child(8) { display:none; } }
@media (max-width:900px) {
  .wrap { padding:0 14px 60px; }
  .card { padding:0 12px 16px; }
  .toplevel { margin:0 -12px; padding:0 12px; }
  table { display:block; overflow-x:auto; }
  thead th { position:static; }
  .toolbar { position:static; }
  .searchbox { width:100%; }
  .tb-cat { padding-left:0; border-left:none; }
  #rectable th:nth-child(2), #rectable td:nth-child(2) { display:none; }   /* 書脊已經是優先級 */
  #rectable td:nth-child(3) { min-width:110px; }
  .tb-export { margin-left:0; }
  .kpi-grid { gap:4px 20px; }
  .kpi .val { font-size:20px; }
}
"""
