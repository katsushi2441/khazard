"""khazard — 住所を入れると土砂災害警戒区域の内外を判定する。

設計の芯（ここを崩さない）:
  1. 判定結果には必ずデータ時点を添える。datasets 表に時点が無いデータは
     判定に使わない（黙って古いデータで「区域外」と答えるほうが危険なため）。
  2. 古いデータ（既定5年超）を使った判定には必ず注意書きを付ける。
  3. 一次情報（自治体のハザードマップ）への導線を必ず返す。
     本サービスの判定は参考であって公的な証明ではない。
  4. 出典と「加工したこと」を明記する（公共データ利用規約 PDL1.0 の要件）。

構成:
  ジオコーディング: 国土地理院 AddressSearch API（無料・キー不要）
  判定            : PostGIS（khazard-db）ST_Contains
  データ          : 国土数値情報 土砂災害警戒区域(A33)
"""
import json
import os
import time
from datetime import date, datetime

import psycopg2
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

PORT = int(os.environ.get("KHAZARD_PORT", "18376"))
DB = dict(host="127.0.0.1", port=55433, dbname="khazard", user="postgres",
          password=os.environ.get("KHAZARD_DB_PASS", "khazard_local"))
GSI = "https://msearch.gsi.go.jp/address-search/AddressSearch"
STALE_YEARS = 5     # これより古いデータを使ったら注意書きを出す
NEAR_M = 250        # これより近くに区域があれば「区域外」と言い切らない（住所は町丁目の代表点のため）

ZONE_KIND = {1: "土砂災害警戒区域（イエローゾーン）",
             2: "土砂災害特別警戒区域（レッドゾーン）"}
PHENOMENON = {1: "急傾斜地の崩壊", 2: "土石流", 3: "地すべり"}

app = FastAPI(title="khazard — 土砂災害警戒区域の判定")
_rate = {}


def limited(ip, per_min=20):
    now = time.time()
    q = [t for t in _rate.get(ip, []) if now - t < 60]
    if len(q) >= per_min:
        return True
    q.append(now)
    _rate[ip] = q
    return False


def geocode(q: str):
    """国土地理院の住所検索。施設名だと別地方の類似住所が先頭に来るので、
    クエリを含む候補を優先する（kshoken で実測した挙動と同じ）。"""
    r = requests.get(GSI, params={"q": q}, timeout=10,
                     headers={"User-Agent": "khazard/1.0 (kurage.exbridge.jp)"})
    r.raise_for_status()
    items = r.json()
    if not items:
        return None
    def score(it):
        t = it.get("properties", {}).get("title", "")
        return (q in t, t.startswith(q), -len(t))
    it = max(items, key=score)
    lon, lat = it["geometry"]["coordinates"]
    return {"lat": lat, "lon": lon, "label": it.get("properties", {}).get("title", q)}


def conn():
    return psycopg2.connect(**DB)


def datasets_in_use(cur, pref_code):
    """判定に使うデータの出典と時点。ここが空なら判定を返さない。"""
    cur.execute("""SELECT key, name, source_url, data_vintage, attribution
                   FROM datasets WHERE key LIKE %s ORDER BY key""", (f'%\\_{pref_code}',))
    rows = cur.fetchall()
    return [dict(key=k, name=n, source_url=u, data_vintage=v, attribution=a)
            for k, n, u, v, a in rows]


def staleness(vintage: str):
    """データ時点からの経過年。古ければ注意書きの材料にする。"""
    try:
        d = datetime.strptime(vintage, "%Y-%m-%d").date()
    except Exception:
        return None, True
    years = (date.today() - d).days / 365.25
    return round(years, 1), years >= STALE_YEARS


def pref_of(cur, lat, lon):
    """点がどの都道府県のデータ範囲にあるか。取り込み済みでない県は判定しない。"""
    cur.execute("""SELECT pref_code FROM hazard_sediment
                   WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s,%s),6668), 0.5)
                   LIMIT 1""", (lon, lat))
    r = cur.fetchone()
    return r[0] if r else None


@app.get("/api/check")
def check(request: Request, q: str):
    ip = request.client.host if request.client else "?"
    if limited(ip):
        raise HTTPException(429, "アクセスが集中しています。1分ほど待って再度お試しください")
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "住所を入力してください")

    g = geocode(q)
    if not g:
        raise HTTPException(404, "住所を特定できませんでした。市区町村から入れてみてください")

    with conn() as c, c.cursor() as cur:
        pref = pref_of(cur, g["lat"], g["lon"])
        if not pref:
            return JSONResponse({
                "query": q, "location": g, "judged": False,
                "reason": "この地点を含む都道府県のデータをまだ取り込んでいないため、判定できません。",
                "primary_source": "お住まいの自治体のハザードマップをご確認ください。",
            })

        srcs = datasets_in_use(cur, pref)
        if not srcs:
            # 時点が分からないデータで判定しない（黙って「区域外」と答えるほうが危険）
            return JSONResponse({
                "query": q, "location": g, "judged": False,
                "reason": "使用データの時点を確認できないため、判定を行いません。",
            })

        cur.execute("""SELECT zone_kind, phenomenon, zone_no, zone_name, address, designated_on
                       FROM hazard_sediment
                       WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s,%s),6668))
                       ORDER BY zone_kind DESC""", (g["lon"], g["lat"]))
        hits = [dict(zone_kind=zk, zone_kind_label=ZONE_KIND.get(zk, "不明"),
                     phenomenon=ph, phenomenon_label=PHENOMENON.get(ph, "不明"),
                     zone_no=zn, zone_name=nm, address=ad,
                     designated_on=str(dt) if dt else None)
                for zk, ph, zn, nm, ad, dt in cur.fetchall()]

        # 区域外と言い切る前に、近くに区域があるかを見る。
        # 「すぐ隣が区域」を黙って区域外と返すと判断を誤らせるため。
        cur.execute("""SELECT round(ST_Distance(geom::geography,
                         ST_SetSRID(ST_MakePoint(%s,%s),6668)::geography)::numeric) AS m,
                         zone_kind, zone_name
                       FROM hazard_sediment
                       ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s,%s),6668)
                       LIMIT 1""", (g["lon"], g["lat"], g["lon"], g["lat"]))
        near = cur.fetchone()

    vint = min(s["data_vintage"] for s in srcs)
    years, is_stale = staleness(vint)
    notes = [
        "本サービスの判定は参考情報です。公的な証明ではありません。",
        "最終的な確認は、必ず当該自治体が公表する最新のハザードマップで行ってください。",
    ]
    if is_stale:
        notes.insert(0, f"使用データは{years}年前の時点のものです。"
                        "指定区域はその後に追加・変更されている可能性があります。")
    if hits:
        notes.append("区域内と判定されました。土地の利用や建築に制限がかかる場合があります。")
    elif near and int(near[0]) <= NEAR_M:
        # 住所の座標は町丁目の代表点なので、番地単位の内外は判定できない。
        # 実測では区域の縁から14〜50mずれた代表点が「区域外」と出た（2026-09-05）。
        # 近接している場合に黙って「区域外」とだけ返すのは危険なので必ず添える。
        notes.insert(0, f"最も近い区域まで約{int(near[0])}mしかありません。"
                        "住所から求めた座標は町丁目のおおよその位置のため、"
                        "実際の敷地が区域内である可能性があります。必ず現地の地番で確認してください。")

    return JSONResponse({
        "query": q,
        "location": g,
        "judged": True,
        "in_hazard_zone": bool(hits),
        "zones": hits,
        "nearest": (None if hits or not near else
                    {"distance_m": int(near[0]), "zone_kind_label": ZONE_KIND.get(near[1], "不明"),
                     "zone_name": near[2]}),
        # 判定結果と時点を分離できない形で返す
        "data_as_of": vint,
        "data_age_years": years,
        "sources": srcs,
        "notes": notes,
        "primary_source_hint": "『<市区町村名> ハザードマップ』で検索すると自治体の最新版が見つかります。",
    })


@app.get("/healthz")
def healthz():
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT pref_code) FROM hazard_sediment")
            n, prefs = cur.fetchone()
            cur.execute("SELECT min(data_vintage), max(data_vintage) FROM datasets")
            lo, hi = cur.fetchone()
        return {"ok": True, "zones": n, "prefectures": prefs,
                "data_as_of": {"oldest": lo, "newest": hi}}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


INDEX = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>土砂災害警戒区域の判定 — khazard</title>
<style>
:root{color-scheme:light}
body{margin:0;background:#f7f9fc;color:#12202f;font-family:-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;line-height:1.8}
.wrap{max-width:760px;margin:0 auto;padding:30px 18px 70px}
h1{font-size:23px;margin:0 0 6px}
.lead{color:#5b6b7a;font-size:14.5px;margin:0 0 20px}
.card{background:#fff;border:1px solid #e5ebf1;border-radius:14px;padding:22px;box-shadow:0 1px 3px rgba(16,24,40,.05)}
form{display:flex;gap:8px;flex-wrap:wrap}
input{flex:1 1 260px;min-width:0;padding:12px 14px;font-size:16px;border:2px solid #cfdae4;border-radius:10px}
input:focus{outline:none;border-color:#0a9a8f}
button{padding:12px 22px;font-size:16px;font-weight:800;color:#fff;background:#0a9a8f;border:0;border-radius:10px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.res{margin-top:18px}
.verdict{font-size:21px;font-weight:900;padding:14px 16px;border-radius:12px;margin-bottom:14px}
.in{background:#fdecec;color:#b3261e;border:2px solid #f0b4b0}
.out{background:#eaf7f5;color:#08776e;border:2px solid #a8ded8}
.unknown{background:#fff6e5;color:#8a5a00;border:2px solid #f0d089}
table{width:100%;border-collapse:collapse;font-size:14px;margin:10px 0}
th,td{border:1px solid #e5ebf1;padding:7px 9px;text-align:left;vertical-align:top}
th{background:#f4f8fb;width:32%;font-weight:700}
.asof{font-size:14px;font-weight:800;background:#f4f8fb;border-left:5px solid #0a9a8f;padding:10px 14px;border-radius:0 8px 8px 0;margin:12px 0}
.notes{font-size:13.5px;color:#5b6b7a;margin:12px 0 0;padding-left:20px}
.notes li{margin:5px 0}
.notes .warn{color:#b3261e;font-weight:700}
.src{font-size:12px;color:#7d8a97;margin-top:16px;border-top:1px solid #e5ebf1;padding-top:12px}
.src a{color:#0a9a8f}
.err{color:#b3261e;font-weight:700}
</style></head><body><div class="wrap">
<h1>土砂災害警戒区域の判定</h1>
<p class="lead">住所を入れると、国土交通省が公開している土砂災害警戒区域のデータと照らして、
区域の内外を判定します。判定に使ったデータの時点も必ず表示します。</p>
<div class="card">
  <form id="f"><input id="q" placeholder="例: 愛知県犬山市大字継鹿尾字川端" autocomplete="off">
  <button id="b">判定する</button></form>
  <div class="res" id="r"></div>
</div>
<p class="src">出典: 国土数値情報（土砂災害警戒区域データ）国土交通省 を加工して作成。
この地図の作成にあたっては、国土地理院長の承認を得て、同院発行の基盤地図情報を使用した（承認番号 平27情使、第585号）。
住所の座標変換に国土地理院 地名検索APIを利用しています。<br>
提供: <a href="https://exbridge.jp/">株式会社エクスブリッジ</a></p>
</div>
<script>
const f=document.getElementById('f'),q=document.getElementById('q'),b=document.getElementById('b'),r=document.getElementById('r');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
f.addEventListener('submit',async e=>{
  e.preventDefault(); const v=q.value.trim(); if(!v) return;
  b.disabled=true; r.innerHTML='<p>判定中…</p>';
  try{
    const res=await fetch('api/check?q='+encodeURIComponent(v));
    const d=await res.json();
    if(!res.ok){ r.innerHTML='<p class="err">'+esc(d.detail||'判定できませんでした')+'</p>'; return; }
    let h='';
    if(!d.judged){
      h+='<div class="verdict unknown">判定できません</div><p>'+esc(d.reason||'')+'</p>';
      if(d.primary_source) h+='<p>'+esc(d.primary_source)+'</p>';
    }else{
      h+='<div class="verdict '+(d.in_hazard_zone?'in':'out')+'">'
        +(d.in_hazard_zone?'土砂災害警戒区域内と判定されました':'土砂災害警戒区域外と判定されました')+'</div>';
      h+='<table><tr><th>入力</th><td>'+esc(d.query)+'</td></tr>'
        +'<tr><th>判定した地点</th><td>'+esc(d.location.label)+'</td></tr></table>';
      if(d.zones&&d.zones.length){
        h+='<table><tr><th>区域区分</th><th>現象</th><th>区域名</th><th>指定年月日</th></tr>';
        for(const z of d.zones) h+='<tr><td>'+esc(z.zone_kind_label)+'</td><td>'+esc(z.phenomenon_label)
          +'</td><td>'+esc(z.zone_name)+'</td><td>'+esc(z.designated_on||'—')+'</td></tr>';
        h+='</table>';
      }
      if(d.nearest) h+='<p>最も近い区域まで約 '+esc(d.nearest.distance_m)+' m（'+esc(d.nearest.zone_kind_label)+'・'+esc(d.nearest.zone_name)+'）</p>';
      h+='<div class="asof">この判定に使ったデータの時点: '+esc(d.data_as_of)
        +'（約'+esc(d.data_age_years)+'年前）</div>';
    }
    if(d.notes&&d.notes.length){
      h+='<ul class="notes">';
      for(const n of d.notes) h+='<li'+(/年前|制限/.test(n)?' class="warn"':'')+'>'+esc(n)+'</li>';
      h+='</ul>';
    }
    if(d.primary_source_hint) h+='<p class="notes">'+esc(d.primary_source_hint)+'</p>';
    if(d.sources&&d.sources.length){
      h+='<p class="src">判定に使用したデータ:<br>';
      for(const s of d.sources) h+=esc(s.name)+'（時点 '+esc(s.data_vintage)+'）<br>';
      h+='</p>';
    }
    r.innerHTML=h;
  }catch(err){ r.innerHTML='<p class="err">通信に失敗しました。時間をおいてお試しください。</p>'; }
  finally{ b.disabled=false; }
});
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX
