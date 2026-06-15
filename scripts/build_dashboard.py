"""Jarvis Ops Dashboard 생성기 — 백엔드(SQLite/Airtable) + 사이클 리포트 → 단일 HTML.

서버 불필요: dashboard/index.html 을 브라우저로 열면 끝 (차트는 Chart.js CDN, 폰트는 G마켓 산스 CDN).

사용:
  python scripts/build_dashboard.py                          # SQLite 더미 백엔드
  python scripts/build_dashboard.py --backend airtable       # Airtable 실 API 백엔드 (.env 필요)
  python scripts/build_dashboard.py --report samples/jarvis_report.json --out dashboard/index.html
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _open_client(backend: str):
    if backend == "airtable":
        from src.airtable_client import AirtableClient
        return AirtableClient.from_env()
    from src.ecount_db import EcountDBClient
    return EcountDBClient.from_db()


def _money(n: int) -> str:
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}억"
    if n >= 10_000:
        return f"{n // 10_000:,}만"
    return f"{n:,}"


def collect_data(client, report_path: Path, backend: str) -> dict:
    from src.agent_tools import _top_receivable_partners

    items_total = len(client.list_item_catalog())
    breaches = client.list_safety_stock_breaches()
    pending = client.list_pending_payments()
    outstanding = client.list_outstanding_invoices()
    top = _top_receivable_partners(client, limit=5)

    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}

    breaches_sorted = sorted(breaches, key=lambda it: (it.safety_stock - it.total_stock), reverse=True)
    return {
        "backend": "Airtable (실 API)" if backend == "airtable" else "SQLite (더미)",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "items_total": items_total,
        "breach_count": len(breaches),
        "breach_top": [
            {"sku": it.sku, "name": it.name, "cur": it.total_stock, "safe": it.safety_stock,
             "short": it.safety_stock - it.total_stock}
            for it in breaches_sorted[:6]
        ],
        "unmatched_count": len(pending),
        "unmatched_total": sum(p.amount for p in pending),
        "partners_recv": top.get("partner_count_with_receivables", 0),
        "outstanding_total": sum(inv.amount for inv in outstanding),
        "top_partners": top.get("top", []),
        "summary": report.get("summary", {}),
        "rules": report.get("rules", []),
    }


HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Ops Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
@font-face{font-family:'GmarketSans';font-weight:300;font-display:swap;src:url('https://fastly.jsdelivr.net/gh/projectnoonnu/noonfonts_2001@1.1/GmarketSansLight.woff') format('woff');}
@font-face{font-family:'GmarketSans';font-weight:500;font-display:swap;src:url('https://fastly.jsdelivr.net/gh/projectnoonnu/noonfonts_2001@1.1/GmarketSansMedium.woff') format('woff');}
@font-face{font-family:'GmarketSans';font-weight:700;font-display:swap;src:url('https://fastly.jsdelivr.net/gh/projectnoonnu/noonfonts_2001@1.1/GmarketSansBold.woff') format('woff');}
:root{--accent:#8b7cff;--accent2:#5b8def;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185;--text:#eceaf6;--muted:#9b97b5;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'GmarketSans','Pretendard','Malgun Gothic',sans-serif;font-weight:300;color:var(--text);
  background:radial-gradient(1100px 480px at 50% -12%,rgba(124,108,246,.28),transparent 60%),
             radial-gradient(700px 400px at 88% 8%,rgba(91,141,239,.14),transparent 55%),#07070d;
  min-height:100vh;padding:40px 20px;}
.wrap{max-width:1080px;margin:0 auto;}
header{text-align:center;margin-bottom:8px;}
.brand{display:inline-flex;align-items:center;gap:8px;font-weight:700;font-size:13px;letter-spacing:.5px;
  color:var(--accent);background:rgba(139,124,255,.1);border:1px solid rgba(139,124,255,.25);
  padding:5px 14px;border-radius:999px;margin-bottom:18px;}
h1{font-weight:700;font-size:30px;letter-spacing:-.5px;
  background:linear-gradient(120deg,#fff 30%,#b9acff);-webkit-background-clip:text;background-clip:text;color:transparent;}
.sub{color:var(--muted);font-size:13px;margin-top:8px;}
.badges{margin-top:14px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap;}
.badge{font-size:11px;color:var(--muted);background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
  padding:4px 11px;border-radius:999px;}
.badge b{color:var(--accent);font-weight:500;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:28px 0;}
.card{position:relative;background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.08);
  border-radius:18px;padding:20px;backdrop-filter:blur(12px);overflow:hidden;}
.card::before{content:'';position:absolute;inset:0 0 auto 0;height:2px;
  background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.5;}
.kpi .num{font-weight:700;font-size:32px;line-height:1;color:#fff;}
.kpi .num small{font-size:15px;color:var(--muted);font-weight:300;}
.kpi .label{color:var(--text);font-size:13px;margin-top:10px;font-weight:500;}
.kpi .hint{color:var(--muted);font-size:11px;margin-top:4px;line-height:1.5;}
.kpi .ic{font-size:18px;opacity:.9;}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;}
@media(max-width:760px){.two{grid-template-columns:1fr;}}
.card h2{font-size:13px;color:var(--muted);font-weight:500;margin-bottom:14px;letter-spacing:.3px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;color:var(--muted);font-weight:500;font-size:11px;padding:6px 4px;border-bottom:1px solid rgba(255,255,255,.08);}
td{padding:9px 4px;border-bottom:1px solid rgba(255,255,255,.05);font-weight:300;}
td.r,th.r{text-align:right;}
.neg{color:var(--bad);font-weight:500;}
ul.rules{list-style:none;} ul.rules li{padding:9px 0;border-bottom:1px solid rgba(255,255,255,.06);font-size:13px;line-height:1.5;}
ul.rules li:last-child{border-bottom:none;}
.tag{display:inline-block;background:rgba(139,124,255,.14);color:var(--accent);border-radius:6px;padding:1px 8px;font-size:11px;margin-right:7px;}
footer{color:var(--muted);font-size:11px;margin-top:22px;line-height:1.7;text-align:center;}
</style>
</head>
<body><div class="wrap">
<header>
  <div class="brand">✦ KS QUANTUM JARVIS</div>
  <h1>운영 대시보드</h1>
  <div class="sub">ERP × 협업툴 AI 자동화 — 실시간 운영 관측</div>
  <div class="badges"><span class="badge">데이터: <b>__BACKEND__</b></span><span class="badge">생성 <b>__GENERATED_AT__</b></span><span class="badge">PoC 데모</span></div>
</header>

<div class="grid">
  <div class="card kpi"><div class="ic">📦</div><div class="num" style="color:var(--warn)">__BREACH_COUNT__<small> / __ITEMS_TOTAL__</small></div><div class="label">안전재고 미만 품목</div><div class="hint">즉시 발주 검토 대상</div></div>
  <div class="card kpi"><div class="ic">💸</div><div class="num" style="color:var(--bad)">__UNMATCHED_COUNT__<small>건</small></div><div class="label">미매칭 입금</div><div class="hint">합계 __UNMATCHED_AMT__ · 매칭 처리 필요</div></div>
  <div class="card kpi"><div class="ic">🧾</div><div class="num" style="color:var(--accent)">__PARTNERS_RECV__<small>곳</small></div><div class="label">미수 보유 거래처</div><div class="hint">미수 총액 __OUTSTANDING_AMT__</div></div>
  <div class="card kpi"><div class="ic">🧠</div><div class="num" style="color:var(--ok)">__RULES_COUNT__</div><div class="label">추출된 사내 판단 룰</div><div class="hint">대화에서 자동 추출 (적용 전 확인)</div></div>
</div>

<div class="two">
  <div class="card"><h2>미수 집중 거래처 TOP 5 (회수 우선순위)</h2><canvas id="recvChart" height="200"></canvas></div>
  <div class="card"><h2>최근 사이클 — 시나리오별 액션 분기</h2><canvas id="scenChart" height="200"></canvas></div>
</div>

<div class="two">
  <div class="card"><h2>🚨 안전재고 미만 — 부족 큰 순</h2>
    <table><thead><tr><th>SKU</th><th>품목</th><th class="r">현재고/안전</th><th class="r">부족</th></tr></thead>
    <tbody>__BREACH_ROWS__</tbody></table></div>
  <div class="card"><h2>🧠 추출된 사내 판단 룰 (적용 전 담당자 확인)</h2><ul class="rules">__RULES_HTML__</ul></div>
</div>

<footer>
  ⚠️ <b>더미 데이터 PoC 데모</b> — 분포는 시나리오 검증용 설계값이며 실제 운영 분포가 아닙니다.<br>
  데이터 원천: __BACKEND__ + 사이클 리포트 · 절감 시간 등 효율 지표는 운영 실측 후 산정 예정.
</footer>
</div>
<script>
const DATA = __DATA_JSON__;
const gridC='rgba(255,255,255,.06)', tickC='#9b97b5';
new Chart(document.getElementById('recvChart'),{type:'bar',
  data:{labels:DATA.top_partners.map(p=>p.partner_name),
    datasets:[{data:DATA.top_partners.map(p=>p.outstanding_total),
      backgroundColor:'rgba(139,124,255,.55)',borderColor:'#8b7cff',borderWidth:1,borderRadius:6}]},
  options:{indexAxis:'y',plugins:{legend:{display:false}},
    scales:{x:{grid:{color:gridC},ticks:{color:tickC,callback:v=>(v/10000)+'만'}},y:{grid:{display:false},ticks:{color:tickC}}}}});
const scen=DATA.summary||{}, acts=['auto_execute','request_confirm','manual_review','skip'];
const col={auto_execute:'#34d399',request_confirm:'#fbbf24',manual_review:'#fb7185',skip:'#9b97b5'};
new Chart(document.getElementById('scenChart'),{type:'bar',
  data:{labels:['① 안전재고','② 입금매칭','③ 구매입력'],
    datasets:acts.map(a=>({label:a,backgroundColor:col[a],borderRadius:4,
      data:['inventory','payments','purchases'].map(n=>(scen[n]||{})[a]||0)}))},
  options:{scales:{x:{stacked:true,grid:{display:false},ticks:{color:tickC}},y:{stacked:true,grid:{color:gridC},ticks:{color:tickC}}},
    plugins:{legend:{labels:{color:tickC,font:{size:10}}}}}});
</script>
</body></html>
"""


def build(report_path: Path, out_path: Path, backend: str) -> None:
    client = _open_client(backend)
    d = collect_data(client, report_path, backend)

    breach_rows = "".join(
        f"<tr><td>{b['sku']}</td><td>{b['name'][:18]}</td>"
        f"<td class='r'><span class='{'neg' if b['cur'] < 0 else ''}'>{b['cur']}</span>/{b['safe']}</td>"
        f"<td class='r'>{b['short']}</td></tr>"
        for b in d["breach_top"]
    ) or "<tr><td colspan=4 style='color:var(--muted)'>없음</td></tr>"

    rules_html = "".join(
        f"<li><span class='tag'>{r.get('scope', '?')}</span>"
        f"{('[' + r['target'] + '] ') if r.get('target') else ''}{r.get('condition', '')} → {r.get('action', '')}</li>"
        for r in d["rules"]
    ) or "<li style='color:var(--muted)'>추출된 룰 없음 — jarvis 사이클을 --out 으로 먼저 실행</li>"

    html = HTML
    for k, v in {
        "__BACKEND__": d["backend"], "__GENERATED_AT__": d["generated_at"],
        "__BREACH_COUNT__": str(d["breach_count"]), "__ITEMS_TOTAL__": str(d["items_total"]),
        "__UNMATCHED_COUNT__": str(d["unmatched_count"]), "__UNMATCHED_AMT__": _money(d["unmatched_total"]) + "원",
        "__PARTNERS_RECV__": str(d["partners_recv"]), "__OUTSTANDING_AMT__": _money(d["outstanding_total"]) + "원",
        "__RULES_COUNT__": str(len(d["rules"])),
        "__BREACH_ROWS__": breach_rows, "__RULES_HTML__": rules_html,
        "__DATA_JSON__": json.dumps({"top_partners": d["top_partners"], "summary": d["summary"]}, ensure_ascii=False),
    }.items():
        html = html.replace(k, v)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[+] dashboard ({d['backend']}): {out_path} ({out_path.stat().st_size:,} bytes)", file=sys.stderr)


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description="Jarvis Ops Dashboard 생성")
    p.add_argument("--backend", choices=["sqlite", "airtable"], default="sqlite")
    p.add_argument("--report", default=str(ROOT / "samples" / "jarvis_report.json"))
    p.add_argument("--out", default=str(ROOT / "dashboard" / "index.html"))
    args = p.parse_args()
    build(Path(args.report), Path(args.out), args.backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
