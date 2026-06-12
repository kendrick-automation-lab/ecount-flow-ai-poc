"""Jarvis Ops Dashboard 생성기 — DB + 실행 리포트 → 단일 HTML 파일.

서버 불필요: 생성된 dashboard/index.html 을 브라우저로 열면 끝.
(차트는 Chart.js CDN — 오프라인이면 차트만 빈 칸, 숫자 카드는 그대로 보임)

사용:
  python scripts/build_dashboard.py
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

from src.ecount_db import EcountDBClient  # noqa: E402


def collect_data(report_path: Path) -> dict:
    db = EcountDBClient.from_db()
    items_total = len(db.list_item_catalog())
    breaches = len(db.list_safety_stock_breaches())

    # 입금 상태 분포 (DB 직접 조회)
    conn = db._conn()
    pay_rows = conn.execute(
        "SELECT match_status, COUNT(*) AS n FROM payments GROUP BY match_status"
    ).fetchall()
    payment_status = {r["match_status"]: r["n"] for r in pay_rows}
    sales_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM sales GROUP BY status"
    ).fetchall()
    sales_status = {r["status"]: r["n"] for r in sales_rows}
    conn.close()

    report = {}
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "items_total": items_total,
        "breaches": breaches,
        "payment_status": payment_status,
        "sales_status": sales_status,
        "summary": report.get("summary", {}),
        "rules": report.get("rules", []),
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Ops Dashboard — 더미 환경 데모</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{ --bg:#0f172a; --card:#1e293b; --text:#e2e8f0; --muted:#94a3b8; --accent:#38bdf8; --ok:#34d399; --warn:#fbbf24; --bad:#f87171; }}
  * {{ box-sizing:border-box; margin:0; }}
  body {{ background:var(--bg); color:var(--text); font-family:'Pretendard','Malgun Gothic',sans-serif; padding:24px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  .sub {{ color:var(--muted); font-size:12px; margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:20px; }}
  .card {{ background:var(--card); border-radius:12px; padding:16px; }}
  .kpi .num {{ font-size:28px; font-weight:700; color:var(--accent); }}
  .kpi .label {{ color:var(--muted); font-size:12px; margin-top:4px; }}
  .charts {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; margin-bottom:20px; }}
  .card h2 {{ font-size:14px; margin-bottom:10px; color:var(--muted); }}
  ul.rules {{ list-style:none; }} ul.rules li {{ padding:8px 0; border-bottom:1px solid #334155; font-size:13px; }}
  ul.rules li:last-child {{ border-bottom:none; }}
  .tag {{ display:inline-block; background:#334155; border-radius:6px; padding:1px 7px; font-size:11px; margin-right:6px; color:var(--accent); }}
  footer {{ color:var(--muted); font-size:11px; margin-top:16px; line-height:1.6; }}
</style>
</head>
<body>
<h1>📊 Jarvis Ops Dashboard</h1>
<div class="sub">ERP × 협업툴 AI 자동화 PoC — 더미 환경 운영 관측 · 생성 {generated_at}</div>

<div class="grid">
  <div class="card kpi"><div class="num">{breaches}<span style="font-size:14px;color:var(--muted)"> / {items_total}</span></div><div class="label">안전재고 위반 SKU</div></div>
  <div class="card kpi"><div class="num">{matched_cnt}</div><div class="label">입금 매칭 완료 (부분 포함)</div></div>
  <div class="card kpi"><div class="num">{unmatched_cnt}</div><div class="label">미매칭 입금 (대기)</div></div>
  <div class="card kpi"><div class="num">{rules_cnt}</div><div class="label">추출된 사내 판단 룰</div></div>
</div>

<div class="charts">
  <div class="card"><h2>입금 매칭 상태 분포</h2><canvas id="payChart"></canvas></div>
  <div class="card"><h2>최근 사이클 — 시나리오별 액션 분기</h2><canvas id="scenarioChart"></canvas></div>
</div>

<div class="card">
  <h2>🧠 Flow 대화에서 추출된 사내 판단 룰 (적용 전 담당자 확인 필요)</h2>
  <ul class="rules">{rules_html}</ul>
</div>

<footer>
  ⚠️ 본 화면은 <b>더미 데이터 데모</b>입니다 — 분포는 시나리오 검증용 설계값이며 실제 운영 분포가 아닙니다.<br>
  데이터 원천: 가짜 이카운트 (SQLite) + mock Flow + Jarvis 사이클 리포트 · 절감 시간 등 효율 지표는 운영 실측 후 추가 예정.
</footer>

<script>
const DATA = {data_json};
new Chart(document.getElementById('payChart'), {{
  type: 'doughnut',
  data: {{
    labels: Object.keys(DATA.payment_status),
    datasets: [{{ data: Object.values(DATA.payment_status),
      backgroundColor: ['#94a3b8', '#34d399', '#fbbf24', '#f87171'] }}]
  }},
  options: {{ plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }} }}
}});
const scen = DATA.summary || {{}};
const names = ['inventory', 'payments', 'purchases'];
const actions = ['auto_execute', 'request_confirm', 'manual_review', 'skip'];
const colors = {{ auto_execute: '#34d399', request_confirm: '#fbbf24', manual_review: '#f87171', skip: '#94a3b8' }};
new Chart(document.getElementById('scenarioChart'), {{
  type: 'bar',
  data: {{
    labels: ['① 안전재고', '② 입금매칭', '③ 구매입력'],
    datasets: actions.map(a => ({{
      label: a, backgroundColor: colors[a],
      data: names.map(n => (scen[n] || {{}})[a] || 0)
    }}))
  }},
  options: {{
    scales: {{ x: {{ stacked: true, ticks: {{ color: '#e2e8f0' }} }}, y: {{ stacked: true, ticks: {{ color: '#e2e8f0' }} }} }},
    plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }}
  }}
}});
</script>
</body>
</html>
"""


def build(report_path: Path, out_path: Path) -> None:
    d = collect_data(report_path)
    matched = d["payment_status"].get("matched", 0) + d["payment_status"].get("partial", 0) \
        + d["payment_status"].get("manual", 0)
    unmatched = d["payment_status"].get("unmatched", 0)
    rules_html = "".join(
        f"<li><span class='tag'>{r.get('scope', '?')}</span>"
        f"{('[' + r['target'] + '] ') if r.get('target') else ''}"
        f"{r.get('condition', '')} → {r.get('action', '')} "
        f"<span style='color:var(--muted)'>(출처: {r.get('source', '?')})</span></li>"
        for r in d["rules"]
    ) or "<li style='color:var(--muted)'>추출된 룰 없음 — jarvis 사이클을 --out 옵션으로 먼저 실행</li>"

    html = HTML_TEMPLATE.format(
        generated_at=d["generated_at"],
        breaches=d["breaches"], items_total=d["items_total"],
        matched_cnt=matched, unmatched_cnt=unmatched,
        rules_cnt=len(d["rules"]), rules_html=rules_html,
        data_json=json.dumps(d, ensure_ascii=False),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[+] dashboard: {out_path} ({out_path.stat().st_size:,} bytes)", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description="Jarvis Ops Dashboard 생성")
    p.add_argument("--report", default=str(ROOT / "samples" / "jarvis_report.json"))
    p.add_argument("--out", default=str(ROOT / "dashboard" / "index.html"))
    args = p.parse_args()
    build(Path(args.report), Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
