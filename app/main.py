"""Kurage 土砂災害ハザードマップ（内部の略称 khazard）

住所を入れると土砂災害警戒区域の内外を判定する。

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
import re
import time
from datetime import date, datetime

import psycopg2
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

PORT = int(os.environ.get("KHAZARD_PORT", "18376"))
DB = dict(host="127.0.0.1", port=55433, dbname="khazard", user="postgres",
          password=os.environ.get("KHAZARD_DB_PASS", "khazard_local"))
GSI = "https://msearch.gsi.go.jp/address-search/AddressSearch"
STALE_YEARS = 5     # これより古いデータを使ったら注意書きを出す
NEAR_M = 250        # これより近くに区域があれば「区域外」と言い切らない（住所は町丁目の代表点のため）

# 区域区分（A33 の ksj:coz）。
# 公式コードリスト CodeOfZone.html には 1・2 しか載っていないが、
# 実データには 3・4 が 25,754件ある（15県）。意味は実測で確定させた（2026-09-13）:
#   ・3/4 は指定年月日が **全件 9999**（例外 0件。1/2 には実日付が入る）
#   ・千葉県の「データ利用時の注意事項」が原典レイヤ名を列挙しており、そこに
#     「(指定予定)土砂災害警戒区域（急傾斜地の崩壊）」「(指定予定)土砂災害特別警戒区域（急傾斜地の崩壊）」がある
#   ・実際、千葉県の 3/4 は急傾斜地の崩壊のみ（土石流・地すべりは 0件）で上記と一致する
#   ・3 の区域は既指定区域と重ならないものが大半＝同じ区域の重複登録ではない
# → 3=指定予定の警戒区域、4=指定予定の特別警戒区域。
# **指定予定はまだ法的な指定を受けていない。** 「区域内」と一緒くたにすると、
# 現時点で存在しない建築制限があるかのように誤解させるので、必ず分けて返す。
ZONE_KIND = {1: "土砂災害警戒区域（イエローゾーン）",
             2: "土砂災害特別警戒区域（レッドゾーン）",
             3: "土砂災害警戒区域（指定予定）",
             4: "土砂災害特別警戒区域（指定予定）"}
PLANNED_KINDS = (3, 4)      # まだ指定されていない＝法的効果はない
PHENOMENON = {1: "急傾斜地の崩壊", 2: "土石流", 3: "地すべり"}


def fmt_designated(dt):
    """指定年月日。A33 では不明・未定を 9999年 で表す（1/2 にも 33,231件ある）。
    そのまま出すと「9999-01-01に指定」と読めてしまうので必ず言い換える。"""
    if not dt:
        return None
    return "不明" if dt.year >= 9999 else str(dt)

app = FastAPI(title="Kurage 土砂災害ハザードマップ")
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
        rows = [dict(zone_kind=zk, zone_kind_label=ZONE_KIND.get(zk, "不明"),
                     phenomenon=ph, phenomenon_label=PHENOMENON.get(ph, "不明"),
                     zone_no=zn, zone_name=nm, address=ad,
                     designated_on=fmt_designated(dt))
                for zk, ph, zn, nm, ad, dt in cur.fetchall()]
        # 指定済みと指定予定は法的な意味がまったく違うので分ける
        hits = [r for r in rows if r["zone_kind"] not in PLANNED_KINDS]
        planned = [r for r in rows if r["zone_kind"] in PLANNED_KINDS]

        # 区域外と言い切る前に、近くに区域があるかを見る。
        # 「すぐ隣が区域」を黙って区域外と返すと判断を誤らせるため。
        cur.execute("""SELECT round(ST_Distance(geom::geography,
                         ST_SetSRID(ST_MakePoint(%s,%s),6668)::geography)::numeric) AS m,
                         zone_kind, zone_name
                       FROM hazard_sediment WHERE zone_kind NOT IN (3,4)
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
    if planned:
        notes.append("この地点は、都道府県が今後の指定を予定している区域に含まれています。"
                     "現時点では指定されていないため法律上の制限はかかっていませんが、"
                     "危険性があると判断されている場所です。指定されると制限の対象になります。")
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
        "in_planned_zone": bool(planned),
        "planned_zones": planned,
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
<script async src="https://www.googletagmanager.com/gtag/js?id=G-BP0650KDFR"></script><script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}gtag('js',new Date());gtag('config','G-BP0650KDFR');</script>
<title>土砂災害ハザードマップを住所から判定｜Kurage</title>
<meta name="description" content="住所を入れると、土砂災害ハザードマップの警戒区域（イエローゾーン）・特別警戒区域（レッドゾーン）の内外を判定します。全国47都道府県・約179万区域を収録。区域区分・現象・指定年月日と、判定に使ったデータの時点まで表示します。無料で試せます。">
<link rel="canonical" href="https://kurage.exbridge.jp/khazard.php/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Kurage">
<meta property="og:locale" content="ja_JP">
<meta property="og:title" content="土砂災害ハザードマップを住所から判定｜Kurage">
<meta property="og:description" content="住所を入れるだけで、イエローゾーン・レッドゾーンの内外を判定。全国47都道府県・約179万区域。判定に使ったデータの時点も必ず表示します。">
<meta property="og:url" content="https://kurage.exbridge.jp/khazard.php/">
<meta property="og:image" content="https://kurage.exbridge.jp/images/khazard-ogp.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://kurage.exbridge.jp/images/khazard-ogp.png">
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
.doc{margin-top:34px}
.doc h2{font-size:19px;margin:30px 0 10px;padding-left:12px;border-left:5px solid #0a9a8f}
.doc p,.doc li,.doc dd{font-size:14.5px}
.doc ul{padding-left:22px}
.doc li{margin:6px 0}
.t2{width:100%;border-collapse:collapse;margin:10px 0;font-size:14px}
.t2 th,.t2 td{border:1px solid #e5ebf1;padding:9px 11px;text-align:left;vertical-align:top}
.t2 th{background:#f4f8fb;width:36%;font-weight:700}
.faq dt{font-weight:800;margin-top:14px;font-size:15px}
.faq dd{margin:5px 0 0;padding-left:16px;border-left:3px solid #e5ebf1;color:#37485a}
.pv{margin:12px 0}
.note-sm{font-size:12.5px;color:#7d8a97;margin:4px 0 0}
.pv video{width:100%;height:auto;border-radius:12px;border:1px solid #e5ebf1;background:#000;display:block}
.cta{display:inline-block;margin-top:8px;padding:12px 22px;font-size:15.5px;font-weight:800;color:#fff;background:#0a9a8f;border-radius:10px;text-decoration:none}
</style></head><body><div class="wrap">
<h1>Kurage 土砂災害ハザードマップ</h1>
<p class="lead">住所を入れると、国土交通省が公開している土砂災害警戒区域のデータと照らして、
イエローゾーン・レッドゾーンの内外を判定します。全国47都道府県・約179万区域を収録。
判定に使ったデータの時点も必ず表示します。</p>
<div class="card">
  <form id="f"><input id="q" placeholder="例: 愛知県犬山市大字継鹿尾字川端" autocomplete="off">
  <button id="b">判定する</button></form>
  <div class="res" id="r"></div>
</div>
<section class="doc">
<h2>土砂災害警戒区域とは</h2>
<p>土砂災害防止法に基づいて都道府県が指定する区域です。危険度によって2段階に分かれています。</p>
<table class="t2">
<tr><th>土砂災害警戒区域<br>（イエローゾーン）</th><td>土砂災害のおそれがある区域です。市町村に警戒避難体制の整備が義務づけられます。</td></tr>
<tr><th>土砂災害特別警戒区域<br>（レッドゾーン）</th><td>建築物に損壊が生じ、住民の生命に著しい危害が生ずるおそれがある区域です。特定の開発行為の制限、建築物の構造規制、移転勧告の対象になります。</td></tr>
</table>
<p>対象となる現象は<strong>急傾斜地の崩壊・土石流・地すべり</strong>の3つで、同じ場所が複数の区域に重なって指定されていることもあります。</p>

<h2>この判定でわかること</h2>
<ul>
<li>入力した住所が、警戒区域または特別警戒区域に入るか</li>
<li>入る場合は、区域区分・現象・区域名・<strong>指定年月日</strong></li>
<li>入らない場合は、いちばん近い区域までの距離</li>
<li>判定に使ったデータが<strong>いつ時点</strong>のものか</li>
</ul>

<h2>この判定でわからないこと</h2>
<p>先に限界をお伝えします。ここを知らずに使うと危険だからです。</p>
<ul>
<li><strong>番地単位の判定はできません。</strong>住所から求める座標は町丁目のおおよその位置です。実測では、区域の縁から14〜50mずれた地点が「区域外」と出ました。250m以内に区域がある場合は、その旨を表示します</li>
<li><strong>公的な証明にはなりません。</strong>参考情報です。最終的な確認は、必ず当該自治体が公表する最新のハザードマップで行ってください</li>
<li><strong>洪水の浸水想定は扱っていません。</strong>理由は次の項に書いています</li>
</ul>

<h2>なぜ洪水の浸水想定を扱わないのか</h2>
<p>国土数値情報で配布されている都道府県別の洪水浸水想定区域データは、<strong>2012年版（データ時点 平成23年度）が最新</strong>です。2015年の水防法改正で「想定最大規模」へ基準が変わる前のもので、現在の指定とは食い違います。</p>
<p>古い基準で「浸水想定区域外です」と答えるほうが、答えないより危険だと判断しました。洪水については、お住まいの自治体が公表しているハザードマップをご確認ください。</p>

<h2>使っているデータ</h2>
<table class="t2">
<tr><th>出典</th><td>国土数値情報 土砂災害警戒区域データ（A33）／国土交通省</td></tr>
<tr><th>データ時点</th><td>2026-03-06（提供元メタデータの作成日）</td></tr>
<tr><th>収録範囲</th><td>全国47都道府県・約179万区域</td></tr>
<tr><th>住所の座標変換</th><td>国土地理院 地名検索API</td></tr>
</table>
<p>判定するたびに、使ったデータの時点を画面に表示します。<strong>時点を確認できないデータでは、判定そのものを行いません。</strong></p>

<h2>よくあるご質問</h2>
<dl class="faq">
<dt>この判定は不動産の重要事項説明に使えますか。</dt>
<dd>そのままでは使えません。本サービスの判定は参考情報であり、公的な証明ではありません。宅地建物取引業者の説明義務は、自治体が公表する最新のハザードマップに基づいて果たしてください。本サービスは、その前の当たりを付ける用途に向いています。</dd>
<dt>データはいつ更新されますか。</dt>
<dd>国土数値情報の更新に合わせて取り込み直します。現在のデータ時点は2026-03-06です。判定結果には常にその時点を表示するので、古いまま使われることがありません。</dd>
<dt>「区域外」と出れば安全ということですか。</dt>
<dd>いいえ。区域の指定は「調査済みで危険と判定された場所」に付きます。未調査の場所や、指定の対象外だが傾斜がある場所は区域外になります。また住所の座標は町丁目のおおよその位置なので、実際の敷地が区域内であることもあります。</dd>
<dt>自社のサーバーに置けますか。</dt>
<dd>置けます。ソースコード一式と設置手順書を同梱した買い切り版を用意しています。住所を外部に送りたくない場合や、自社システムに組み込みたい場合に向きます。</dd>
</dl>

<h2>30秒でわかる動画</h2>
<p>実際の画面で、判定から注意書きまでの流れをまとめました。</p>
<p class="note-sm">冒頭の実写カットは MiniMax H3 を自社サーバーで動かして生成しています。</p>
<div class="pv">
<video src="https://kurage.exbridge.jp/pv/khazard-pv-30s.mp4"
       poster="https://kurage.exbridge.jp/pv/khazard-pv-poster.jpg"
       controls playsinline preload="none" width="1920" height="1080"></video>
</div>

<h2>自社サーバーに置く（買い切り版）</h2>
<p>同じ仕組みを自社のサーバーで動かせます。ソースコード同梱・MITライセンス・外部の有料APIなしで、追加費用はかかりません。</p>
<p><a class="cta" href="https://kappstore.exbridge.jp/app.php?id=02b945f9c87c9d86&amp;ref=khazard-lp">買い切り版を見る（税込55,000円）</a></p>
</section>

<p style="font-size:12.5px;color:#7d8a97;margin-top:10px">議員・政党事務所の方へ: このページを事務所の名前で運用できます → <a href="/bousai-giin.html">地域防災情報サービス</a></p>
<p style="font-size:13px;margin-top:14px"><a href="map/"><b>地図で見る</b></a>（区域を地図に重ねて表示・クリックで判定）</p>
<p style="font-size:13px;margin-top:14px">地域ページから入る: <a href="area/aichi-nagoya">名古屋市</a>・<a href="area/kanagawa-yokohama">横浜市</a>・<a href="area/hiroshima-hiroshima">広島市</a>・<a href="area/shizuoka-atami">熱海市</a>・<a href="area/aichi-toyota">豊田市</a>／<a href="area/">全国1,603市区町村の指定状況</a></p>
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
      if(d.planned_zones&&d.planned_zones.length){
        h+='<p style="margin:12px 0 4px"><b>今後の指定が予定されている区域</b>（現時点では指定されておらず、法律上の制限はかかっていません）</p>'
          +'<table><tr><th>区分</th><th>現象</th><th>区域名</th></tr>';
        for(const z of d.planned_zones) h+='<tr><td>'+esc(z.zone_kind_label)+'</td><td>'+esc(z.phenomenon_label)
          +'</td><td>'+esc(z.zone_name)+'</td></tr>';
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
(function(){
  var p=new URLSearchParams(location.search).get('q');
  if(p){ q.value=p; f.dispatchEvent(new Event('submit',{cancelable:true})); }
})();
</script>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebApplication","@id":"https://kurage.exbridge.jp/khazard.php/#app","name":"Kurage 土砂災害ハザードマップ","url":"https://kurage.exbridge.jp/khazard.php/","applicationCategory":"BusinessApplication","operatingSystem":"Web","inLanguage":"ja","description":"住所を入れると、土砂災害ハザードマップの警戒区域（イエローゾーン）・特別警戒区域（レッドゾーン）の内外を判定します。全国47都道府県・約179万区域を収録し、判定に使ったデータの時点を必ず表示します。","image":"https://kurage.exbridge.jp/images/khazard-ogp.png","offers":{"@type":"Offer","price":"0","priceCurrency":"JPY","description":"Webでの判定は無料"},"provider":{"@type":"Organization","name":"株式会社エクスブリッジ","url":"https://exbridge.jp/"},"featureList":["土砂災害警戒区域の内外判定","特別警戒区域（レッドゾーン）の判別","現象（急傾斜地の崩壊・土石流・地すべり）の表示","区域の指定年月日の表示","最寄り区域までの距離","判定に使ったデータ時点の表示"],"isBasedOn":{"@type":"Dataset","name":"国土数値情報 土砂災害警戒区域データ（A33）","creator":{"@type":"Organization","name":"国土交通省"},"temporalCoverage":"2026-03-06","url":"https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A33.html"}},{"@type":"FAQPage","@id":"https://kurage.exbridge.jp/khazard.php/#faq","mainEntity":[{"@type":"Question","name":"この判定は不動産の重要事項説明に使えますか。","acceptedAnswer":{"@type":"Answer","text":"そのままでは使えません。本サービスの判定は参考情報であり、公的な証明ではありません。宅地建物取引業者の説明義務は、自治体が公表する最新のハザードマップに基づいて果たしてください。本サービスは、その前の当たりを付ける用途に向いています。"}},{"@type":"Question","name":"データはいつ更新されますか。","acceptedAnswer":{"@type":"Answer","text":"国土数値情報の更新に合わせて取り込み直します。現在のデータ時点は2026-03-06です。判定結果には常にその時点を表示するので、古いまま使われることがありません。"}},{"@type":"Question","name":"「区域外」と出れば安全ということですか。","acceptedAnswer":{"@type":"Answer","text":"いいえ。区域の指定は調査済みで危険と判定された場所に付きます。未調査の場所や、指定の対象外だが傾斜がある場所は区域外になります。また住所の座標は町丁目のおおよその位置なので、実際の敷地が区域内であることもあります。"}},{"@type":"Question","name":"土砂災害警戒区域と特別警戒区域の違いは何ですか。","acceptedAnswer":{"@type":"Answer","text":"土砂災害警戒区域（イエローゾーン）は土砂災害のおそれがある区域で、市町村に警戒避難体制の整備が義務づけられます。土砂災害特別警戒区域（レッドゾーン）は建築物に損壊が生じ住民の生命に著しい危害が生ずるおそれがある区域で、特定の開発行為の制限、建築物の構造規制、移転勧告の対象になります。"}},{"@type":"Question","name":"洪水の浸水想定区域も判定できますか。","acceptedAnswer":{"@type":"Answer","text":"扱っていません。国土数値情報で配布されている都道府県別の洪水浸水想定区域データは2012年版（データ時点 平成23年度）が最新で、2015年の水防法改正前の基準のためです。古い基準で区域外と答えるほうが危険だと判断しました。"}}]},{"@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,"name":"Kurage","item":"https://kurage.exbridge.jp/"},{"@type":"ListItem","position":2,"name":"土砂災害ハザードマップ","item":"https://kurage.exbridge.jp/khazard.php/"}]}]}</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX


# ---- 地域ページ（「名古屋 ハザードマップ」等の無競合ロングテールを取る） ----
# 2026-09-06 実測: 名古屋ハザードマップ1,900/指数0・大阪ハザードマップ2,900/指数0。
# 都市名を主題にした個別ランディングで拾う。CSS/JSは本体INDEXから取り出して共有。
_STYLE = re.search(r"<style>.*?</style>", INDEX, re.S).group(0)
# /area/<slug> は1階層深いので相対 fetch を ../ に補正する
_SCRIPT = re.search(r"<script>(?:(?!application/ld).)*?</script>", INDEX, re.S).group(0).replace("'api/check", "'../api/check")

# 既にインデックス済みの10本の romaji スラッグは canonical として据え置く
# （団体コードのURLへ移すと積み上がった評価が切れる）。新規は5桁の全国地方公共団体コード。
LEGACY_SLUG = {
    "23100": "aichi-nagoya", "14100": "kanagawa-yokohama", "28100": "hyogo-kobe",
    "34100": "hiroshima-hiroshima", "22205": "shizuoka-atami", "26100": "kyoto-kyoto",
    "40130": "fukuoka-fukuoka", "42201": "nagasaki-nagasaki", "27100": "osaka-osaka",
    "23211": "aichi-toyota",
}
SLUG_BY_CODE = dict(LEGACY_SLUG)
CODE_BY_SLUG = {v: k for k, v in LEGACY_SLUG.items()}
# 大阪市は土砂災害警戒区域が1件も無い（実測 2026-09-14）。muni_stats には行が無いが
# 既存の公開URLなので、「指定なし」と書くために名前だけ持っておく。
ZERO_MUNI = {"27100": ("大阪府", "大阪市")}

PHEN_LABEL = {"steep": "急傾斜地の崩壊（がけ崩れ）", "debris": "土石流", "slide": "地すべり"}


def _load_muni():
    """muni_stats（scripts/build_muni_stats.py が作る）を起動時に読む。1,603件・数百KB。"""
    out = {}
    try:
        with conn() as cn, cn.cursor() as cur:
            cur.execute("SELECT muni_code,pref_code,pref,muni,zones,yellow,red,planned,"
                        "steep,debris,slide,first_on,last_on,unknown_on,samples FROM muni_stats")
            cols = ("code", "pref_code", "pref", "muni", "zones", "yellow", "red", "planned",
                    "steep", "debris", "slide", "first_on", "last_on", "unknown_on", "samples")
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                out[d["code"]] = d
    except Exception as e:  # noqa: BLE001
        print("muni_stats を読めません（地域ページは主要都市のみ）:", e)
    return out


def _load_wagamachi():
    """市区町村の公式ハザードマップへのリンク（scripts/fetch_wagamachi.py が作る）。

    国のデータでの判定は参考情報で、正式なものは市区町村が作るハザードマップ。
    そこへ必ず送れるようにしておく。リンク先の著作権は市区町村にあり、
    ポータル側のリンクが最新版でないことがある（規約に明示あり）ので画面にもそう書く。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "wagamachi.json")
    try:
        d = json.load(open(path, encoding="utf-8"))
        return d.get("muni", {}), d.get("_fetched", "")
    except Exception as e:  # noqa: BLE001
        print("わがまちハザードマップを読めません（公式リンクは出しません）:", e)
        return {}, ""


WAGAMACHI, WAGAMACHI_FETCHED = _load_wagamachi()

MUNI = _load_muni()
for _c in MUNI:
    SLUG_BY_CODE.setdefault(_c, _c)
    CODE_BY_SLUG.setdefault(SLUG_BY_CODE[_c], _c)
# 都道府県コード -> [市区町村（区域数の多い順）]
MUNI_BY_PREF = {}
for _d in sorted(MUNI.values(), key=lambda x: -x["zones"]):
    MUNI_BY_PREF.setdefault(_d["pref_code"], []).append(_d)
PREF_NAME = {c: v[0]["pref"] for c, v in MUNI_BY_PREF.items()}


def _muni_of(slug):
    """スラッグ（romaji でも団体コードでも）から市区町村を引く。"""
    code = CODE_BY_SLUG.get(slug) or (slug if slug in MUNI else None)
    if code is None:
        return None
    d = MUNI.get(code)
    if d is None and code in ZERO_MUNI:
        pref, muni = ZERO_MUNI[code]
        d = dict(code=code, pref_code=code[:2], pref=pref, muni=muni, zones=0, yellow=0,
                 red=0, planned=0, steep=0, debris=0, slide=0, first_on=None, last_on=None,
                 unknown_on=0, samples=[])
    return d


def _area_head(city, slug, desc, title=None, faq=None):
    url = "https://kurage.exbridge.jp/khazard.php/area/" + slug
    title = title or (city + "のハザードマップ｜土砂災害警戒区域を住所から調べる | Kurage")
    ga = ('<script async src="https://www.googletagmanager.com/gtag/js?id=G-BP0650KDFR"></script>'
          '<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}'
          "gtag('js',new Date());gtag('config','G-BP0650KDFR');</script>")
    bc = json.dumps({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Kurage 土砂災害ハザードマップ",
         "item": "https://kurage.exbridge.jp/khazard.php/"},
        {"@type": "ListItem", "position": 2, "name": city, "item": url}]}, ensure_ascii=False)
    faq = faq or [("%sのハザードマップ（土砂災害）はどこで調べられますか？" % city,
                   "このページで%sの住所を入れると、土砂災害警戒区域（イエロー／レッド）の内外が表示されます。"
                   "国土交通省のデータにもとづく参考情報で、最終確認は自治体の最新ハザードマップで行ってください。" % city)]
    faq_ld = json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q,
         "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}, ensure_ascii=False)
    return ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>" + title + "</title>"
            '<meta name="description" content="' + html_escape(desc) + '">'
            '<link rel="canonical" href="' + url + '">'
            '<meta property="og:type" content="website">'
            '<meta property="og:title" content="' + html_escape(title) + '">'
            '<meta property="og:description" content="' + html_escape(desc) + '">'
            '<meta property="og:url" content="' + url + '">'
            '<meta property="og:image" content="https://kurage.exbridge.jp/pv/khazard-pv-poster.jpg">'
            '<meta name="twitter:card" content="summary_large_image">'
            '<script type="application/ld+json">' + bc + '</script>'
            '<script type="application/ld+json">' + faq_ld + '</script>' + ga)


def html_escape(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _fmt_on(d):
    """9999年は『不明・未定』を表す欠測コード。日付として出さない。"""
    return "不明" if (d is None or d.year >= 9999) else d.strftime("%Y年%-m月%-d日")


def _card(k, v, cls=""):
    return '<div class="mcard %s"><div class="mk">%s</div><div class="mv">%s</div></div>' % (cls, k, v)


_AREA_CSS = ("<style>.mgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}"
             ".mcard{border:1px solid #dfe6ea;border-radius:10px;padding:10px 12px;background:#fff;min-width:0}"
             ".mcard.red{border-color:#e0b4b4;background:#fdf6f6}.mcard.amber{border-color:#e6d3a3;background:#fffdf5}"
             ".mk{font-size:12px;color:#5b6b76}.mv{font-size:19px;font-weight:700;color:#12202f;margin-top:3px}"
             ".mlist{font-size:14px;line-height:2;columns:2;column-gap:22px}"
             "@media(max-width:560px){.mlist{columns:1}}"
             ".mtbl{width:100%;border-collapse:collapse;font-size:14px}.mtbl th,.mtbl td{border:1px solid #e3e9ec;padding:6px 8px;text-align:left}"
             ".mtbl th{background:#f5f8f9;white-space:nowrap}.mwrap{overflow-x:auto}</style>")



# 検索する人の言い方と、法令・行政の用語はずれている。両方の語で拾えるように対応表を置く。
# 実例: 名古屋市は「内水ハザードマップ」を「雨水出水浸水想定区域」へ改称していた（2026-09-14 実測）。
TERMS = [("がけ崩れ・崖崩れ", "急傾斜地の崩壊"),
         ("土砂崩れ・山崩れ", "急傾斜地の崩壊／土石流／地すべり（3つに分かれます）"),
         ("イエローゾーン", "土砂災害警戒区域"),
         ("レッドゾーン", "土砂災害特別警戒区域"),
         ("土砂災害ハザードマップ", "土砂災害警戒区域等（水防法ではなく土砂災害防止法）"),
         ("がけ条例", "建築基準法や各自治体の条例による、がけ付近の建築制限")]


def _official_block(code, city):
    """市区町村の公式ハザードマップへの導線。担当課と電話も出す。"""
    w = WAGAMACHI.get(code) or {}
    if not w:
        return ""
    rows, contact = [], None
    for kind in ("土砂災害", "洪水"):
        it = w.get(kind)
        if not it:
            continue
        contact = contact or it
        # ポータル側のリンクも1割弱が切れている（実測）。死んだ先へ利用者を送らない。
        if not it.get("ok", True):
            continue
        tel = (" ／ " + html_escape(it["tel"])) if it.get("tel") else ""
        rows.append('<li><a href="%s" target="_blank" rel="noopener">%sの%sハザードマップ（%s公式）</a>'
                    '<br><span class="src">担当: %s%s</span></li>'
                    % (html_escape(it["url"]), html_escape(city), kind, html_escape(city),
                       html_escape(it.get("dept") or "—"), tel))
    if not rows:
        # リンクが全部切れていても、どこへ聞けばよいかは出せる
        if not contact:
            return ""
        tel = (" ／ " + html_escape(contact["tel"])) if contact.get("tel") else ""
        return ('<section class="doc"><h2>%sの公式ハザードマップ</h2>'
                '<p>このページの判定は国のデータにもとづく<strong>参考情報</strong>です。'
                '正式なものは市区町村が作るハザードマップです。'
                '公開ページのリンクが変わっているため、窓口をご案内します。</p>'
                '<p class="src">担当: %s%s</p>'
                '<p class="src">出典: わがまちハザードマップ（ハザードマップポータルサイト・国土交通省）'
                '%s。リンク先の著作権は各市区町村にあります。</p></section>'
                % (html_escape(city), html_escape(contact.get("dept") or "—"), tel,
                   ("・%s時点" % WAGAMACHI_FETCHED) if WAGAMACHI_FETCHED else ""))
    return ('<section class="doc"><h2>%sの公式ハザードマップ</h2>'
            '<p>このページの判定は国のデータにもとづく<strong>参考情報</strong>です。'
            '正式なものは市区町村が作るハザードマップなので、最終確認はこちらでお願いします。</p>'
            '<ul style="font-size:14.5px;line-height:1.8">%s</ul>'
            '<p class="src">出典: わがまちハザードマップ（ハザードマップポータルサイト・国土交通省）'
            '%s。リンク先の著作権は各市区町村にあり、最新版でない場合があります。</p></section>'
            % (html_escape(city), "".join(rows),
               ("・%s時点" % WAGAMACHI_FETCHED) if WAGAMACHI_FETCHED else ""))


def _howto_block(city):
    """市のハザードマップとの使い分け。どちらが要るかを先に示す。"""
    return ('<section class="doc"><h2>市のハザードマップとの使い分け</h2>'
            '<div class="mwrap"><table class="mtbl">'
            '<tr><th></th><th>%sの公式ハザードマップ</th><th>このページ</th></tr>'
            '<tr><th>調べ方</th><td>地図の中から自分の場所を目で探す</td><td>住所を入れると1件で判定</td></tr>'
            '<tr><th>形</th><td>地図（多くはPDF）</td><td>Webページ（文字で読める・読み上げできる）</td></tr>'
            '<tr><th>範囲</th><td>その市区町村</td><td>全国どこでも同じ形で引ける</td></tr>'
            '<tr><th>位置づけ</th><td><strong>正式</strong></td><td>参考情報（出典と時点を明記）</td></tr>'
            '</table></div>'
            '<p class="src">引っ越し先や実家など、別の市区町村も同じ使い方で調べられます。'
            '不動産取引の重要事項説明には、市区町村の正式なハザードマップを使ってください。</p></section>'
            % html_escape(city))


def _terms_block():
    rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (a, b) for a, b in TERMS)
    return ('<section class="doc"><h2>検索でよく使われる言い方と、行政の用語</h2>'
            '<div class="mwrap"><table class="mtbl"><tr><th>ふだんの言い方</th>'
            '<th>法令・行政の用語</th></tr>' + rows + '</table></div>'
            '<p class="src">自治体のページは法令の用語で書かれているので、'
            'ふだんの言い方で検索すると見つからないことがあります。'
            'このページはどちらの言い方でも同じ答えを返します。</p></section>')


@app.get("/area/pref/{pref_code}", response_class=HTMLResponse)
def area_pref(pref_code: str):
    """都道府県ごとの一覧。市区町村ページをクロールさせる内部リンクの束ね役。"""
    lst = MUNI_BY_PREF.get(pref_code)
    if not lst:
        raise HTTPException(404, "その都道府県のページはありません")
    pref = lst[0]["pref"]
    zones = sum(d["zones"] for d in lst)
    red = sum(d["red"] for d in lst)
    desc = ("%sの土砂災害警戒区域は%s市区町村で計%s区域（うち特別警戒区域＝レッドゾーン%s区域）。"
            "市区町村を選ぶか、住所を入れると区域の内外を判定します。"
            % (pref, f"{len(lst):,}", f"{zones:,}", f"{red:,}"))
    rows = "".join(
        '<tr><td><a href="/khazard.php/area/%s">%s</a></td><td>%s</td><td>%s</td><td>%s</td></tr>'
        % (SLUG_BY_CODE[d["code"]], d["muni"], f'{d["zones"]:,}', f'{d["yellow"]:,}', f'{d["red"]:,}')
        for d in lst)
    body = ('<h1><a href="/khazard.php/">%s</a>の土砂災害警戒区域（市区町村一覧）</h1>' % pref
            + '<p class="lead">%sでは<strong>%s市区町村</strong>に計<strong>%s区域</strong>が指定されています'
              '（特別警戒区域＝レッドゾーンは%s区域）。市区町村ごとの指定状況は下の表からどうぞ。</p>'
              % (pref, f"{len(lst):,}", f"{zones:,}", f"{red:,}")
            + '<div class="mwrap"><table class="mtbl"><tr><th>市区町村</th><th>区域数</th>'
              '<th>イエロー</th><th>レッド</th></tr>' + rows + '</table></div>'
            + '<p class="src" style="margin-top:14px">全国版は <a href="/khazard.php/">Kurage 土砂災害ハザードマップ</a>、'
              '地図で見るなら <a href="/khazard.php/map/">全国地図</a>、他県は <a href="/khazard.php/area/">地域一覧</a>。</p>'
            + '<p class="src">出典: 国土数値情報（土砂災害警戒区域データ A33）国土交通省 を加工して作成</p>')
    head = _area_head(pref, "pref/" + pref_code, desc,
                      title="%sの土砂災害警戒区域｜市区町村別の指定状況 | Kurage" % pref)
    return HTMLResponse(head + _STYLE + _AREA_CSS + '</head><body><div class="wrap">' + body + "</div></body></html>")


@app.get("/area/{slug}", response_class=HTMLResponse)
def area(slug: str):
    d = _muni_of(slug)
    if not d:
        raise HTTPException(404, "地域が見つかりません")
    city, pref, code = d["muni"], d["pref"], d["code"]
    full = pref + city
    canon = SLUG_BY_CODE.get(code, code)
    z, y, r, pl = d["zones"], d["yellow"], d["red"], d["planned"]
    samples = d["samples"] or []
    example = samples[0]["address"] if samples else full
    exq = requests.utils.quote(example)

    if z == 0:
        desc = ("%sには土砂災害警戒区域・特別警戒区域の指定が1件もありません（国土交通省 A33 実測）。"
                "隣接する市区町村では指定があります。住所を入れて確かめられます。" % full)
        lead = ('<p class="lead">国土交通省の土砂災害警戒区域データ（A33）を数えたところ、<strong>%sには指定された区域が1件もありません</strong>。'
                '平野部で急傾斜地・渓流・地すべり地形が無いためです。隣接する市区町村には指定があるので、'
                '職場や実家の住所も確かめてみてください。</p>' % full)
        stats = ""
    else:
        desc = ("%sの土砂災害警戒区域は%s区域（イエロー%s・レッド%s%s）。急傾斜地の崩壊%s・土石流%s・地すべり%s。"
                "住所を入れると、その地点が区域の内か外かを判定します。"
                % (full, f"{z:,}", f"{y:,}", f"{r:,}",
                   ("・指定予定%s" % f"{pl:,}") if pl else "",
                   f'{d["steep"]:,}', f'{d["debris"]:,}', f'{d["slide"]:,}'))
        lead = ('<p class="lead">%s には土砂災害警戒区域が<strong>%s区域</strong>あります。'
                'そのうち<strong>特別警戒区域（レッドゾーン）が%s区域</strong>で、建築物の構造規制がかかります。'
                '住所を入れると、その地点が区域の内か外か、現象と指定年月日まで返します。</p>'
                % (full, f"{z:,}", f"{r:,}"))
        cards = (_card("区域の合計", f"{z:,}")
                 + _card("土砂災害警戒区域（イエロー）", f"{y:,}", "amber")
                 + _card("特別警戒区域（レッド）", f"{r:,}", "red")
                 + (_card("指定予定", f"{pl:,}") if pl else ""))
        phen = "".join(_card(PHEN_LABEL[k], f'{d[k]:,}') for k in ("steep", "debris", "slide") if d[k])
        span = ('<p class="src">指定年月日は <strong>%s</strong> から <strong>%s</strong> まで。%s</p>'
                % (_fmt_on(d["first_on"]), _fmt_on(d["last_on"]),
                   ("指定年月日が不明・未定（9999年）の区域が%s件あります。" % f'{d["unknown_on"]:,}') if d["unknown_on"] else ""))
        ex = ""
        if samples:
            ex = ('<h2>%sで指定されている区域の例</h2><div class="mwrap"><table class="mtbl">'
                  '<tr><th>区域名</th><th>住所</th></tr>%s</table></div>'
                  % (city, "".join("<tr><td>%s</td><td>%s</td></tr>" % (html_escape(s["name"]), html_escape(s["address"]))
                                   for s in samples)))
        stats = ('<section class="doc"><h2>%sの指定状況（実データ）</h2><div class="mgrid">%s</div>'
                 '<h2>現象別の内訳</h2><div class="mgrid">%s</div>%s%s</section>'
                 % (city, cards, phen or '<p class="src">現象の内訳はデータに記載がありません。</p>', span, ex))

    # 同じ県の他の市区町村へ（内部リンク。サイトマップに載せるだけではクロールされない）
    sib = [x for x in MUNI_BY_PREF.get(d["pref_code"], []) if x["code"] != code][:40]
    sib_html = ""
    if sib:
        sib_html = ('<section class="doc"><h2>%sの他の市区町村</h2><div class="mlist">%s</div>'
                    '<p class="src" style="margin-top:8px"><a href="/khazard.php/area/pref/%s">%sの全市区町村一覧</a></p></section>'
                    % (pref, "".join('<a href="/khazard.php/area/%s">%s</a>（%s区域）<br>'
                                     % (SLUG_BY_CODE[x["code"]], x["muni"], f'{x["zones"]:,}') for x in sib),
                       d["pref_code"], pref))

    faq = [("%sで土砂災害警戒区域に指定されている場所はどれくらいありますか？" % full,
            ("%sには指定された区域がありません。" % full) if z == 0 else
            ("%sには%s区域あります。内訳は土砂災害警戒区域（イエローゾーン）%s区域、特別警戒区域（レッドゾーン）%s区域です。"
             % (full, f"{z:,}", f"{y:,}", f"{r:,}"))),
           ("イエローゾーンとレッドゾーンは何が違いますか？",
            "土砂災害警戒区域（イエロー）は警戒避難体制を整える区域で、特別警戒区域（レッド）は"
            "建築物に構造規制がかかり、開発行為に許可が要る区域です。不動産取引では重要事項説明の対象になります。")]

    body = ('<h1><a href="/khazard.php/">%sのハザードマップ（土砂災害）</a></h1>' % full + lead
            + '<div class="card"><form id="f"><input id="q" placeholder="例: %s" value="%s" autocomplete="off">'
              '<button id="b">判定する</button></form><div class="res" id="r"></div></div>'
              % (html_escape(example), html_escape(example))
            + stats
            + _official_block(code, full)
            + _howto_block(full)
            + _terms_block()
            + sib_html
            + '<section class="doc"><h2>あわせて確認したい方へ</h2><p>'
              '%sの津波浸水想定は <a href="/ktsunami.php/">津波浸水想定マップ</a>、'
              '使える避難所は <a href="/krefuge.php/?q=%s">避難所マップ</a>、'
              '盛土の規制区域は <a href="/kmorido.php/">盛土規制区域マップ</a>、'
              '条例の災害危険区域は <a href="/kriskarea.php/">災害危険区域マップ</a> で調べられます。'
              '地図で見るなら <a href="/khazard.php/map/">全国地図</a>、全国版は '
              '<a href="/khazard.php/">Kurage 土砂災害ハザードマップ</a> です。</p></section>' % (full, exq)
            + '<p class="src">出典: 国土数値情報（土砂災害警戒区域データ A33）国土交通省 を加工して作成'
              '／住所検索: 国土地理院 地名検索API。区域数は住所文字列から市区町村を判定して数えた実測値です'
              '（全国179万区域のうち0.33%は合併前の旧市町村名のため、どの市区町村にも計上していません）。'
              '最終確認は自治体の最新のハザードマップでお願いします。</p>')
    head = _area_head(full, canon, desc,
                      title="%sのハザードマップ（土砂災害）｜警戒区域%s区域を住所で判定 | Kurage"
                            % (full, f"{z:,}") if z else "%sのハザードマップ（土砂災害）｜指定区域なし | Kurage" % full,
                      faq=faq)
    return HTMLResponse(head + _STYLE + _AREA_CSS + '</head><body><div class="wrap">' + body + _SCRIPT + "</body></html>")


@app.get("/area", response_class=HTMLResponse)
@app.get("/area/", response_class=HTMLResponse)
def area_index():
    total = sum(d["zones"] for d in MUNI.values())
    prefs = sorted(MUNI_BY_PREF.items(), key=lambda x: x[0])
    rows = "".join(
        '<tr><td><a href="/khazard.php/area/pref/%s">%s</a></td><td>%s</td><td>%s</td><td>%s</td></tr>'
        % (pc, lst[0]["pref"], f"{len(lst):,}", f'{sum(x["zones"] for x in lst):,}',
           f'{sum(x["red"] for x in lst):,}')
        for pc, lst in prefs)
    desc = ("全国%s市区町村の土砂災害警戒区域（計%s区域）を、市区町村ごとの指定件数つきで一覧にしました。"
            "都道府県から市区町村を選ぶと、イエロー・レッドの内訳と指定年月日が分かります。"
            % (f"{len(MUNI):,}", f"{total:,}"))
    body = ('<h1><a href="/khazard.php/">地域から土砂災害の警戒区域を調べる</a></h1>'
            '<p class="lead">全国<strong>%s市区町村</strong>・計<strong>%s区域</strong>を収録しています。'
            '都道府県を選ぶと市区町村ごとの指定件数が出ます。住所で直接調べるなら '
            '<a href="/khazard.php/">全国版</a>、地図で見るなら <a href="/khazard.php/map/">全国地図</a> をどうぞ。</p>'
            % (f"{len(MUNI):,}", f"{total:,}")
            + '<div class="mwrap"><table class="mtbl"><tr><th>都道府県</th><th>市区町村</th>'
              '<th>区域数</th><th>うちレッド</th></tr>' + rows + '</table></div>'
            + '<p class="src">出典: 国土数値情報（土砂災害警戒区域データ A33）国土交通省 を加工して作成</p>')
    head = _area_head("地域一覧", "", desc, title="全国の土砂災害警戒区域｜都道府県・市区町村別の指定状況 | Kurage")
    return HTMLResponse(head + _STYLE + _AREA_CSS + '</head><body><div class="wrap">' + body + "</div></body></html>")


_LLMS_BODY = """# Kurage 土砂災害ハザードマップ

> 住所を入れると、土砂災害警戒区域（イエローゾーン）・特別警戒区域（レッドゾーン）の
> 内外を判定するサイト。区域区分・現象（急傾斜地の崩壊／土石流／地すべり）・指定年月日を返す。

## 収録
- 区域数: 1,793,171
- 都道府県: 47（全国）
- 出典: 国土交通省 国土数値情報（土砂災害警戒区域）を加工して作成

## 大事な区別
- **「区域外」は「安全」ではない。** 警戒区域は都道府県が調査して指定した範囲で、
  未指定でも危険がないとは限らない。
- 住所から求めた座標は町丁目の代表点。正確な区域は自治体の最新ハザードマップで確認すること。
- **「指定予定」は指定済みではない。** データには都道府県が今後指定する予定の区域が含まれる
  （15県・25,754件。指定年月日が 9999 で記録されている）。指定予定の区域には現時点で
  法律上の建築制限はかからない。本サービスは両者を分けて返す（in_hazard_zone / in_planned_zone）。

## 使い方
- 住所で調べる: https://kurage.exbridge.jp/khazard.php/?q=<住所>
- 地図で見る: https://kurage.exbridge.jp/khazard.php/map/ （区域を地図に重ねて表示。クリックした地点を判定）
- 座標で判定するAPI: https://kurage.exbridge.jp/khazard.php/api/at?lat=<緯度>&lon=<経度>
- API: https://kurage.exbridge.jp/khazard.php/api/check?q=<住所>

## 買い切り版
- 商品ページ: https://kappstore.exbridge.jp/app.php?id=02b945f9c87c9d86
- 税込55,000円。ソースコード（MIT）・データ取り込みスクリプト・設置手順書を同梱。自社サーバーで動かせる。

## 関連（同じ運営の防災ツール）
- 洪水・内水・高潮: https://kurage.exbridge.jp/kflood.php/
- 津波浸水想定: https://kurage.exbridge.jp/ktsunami.php/
- 地震の想定震度・液状化（名古屋版）: https://kurage.exbridge.jp/kjishin.php/

運営: 株式会社エクスブリッジ https://exbridge.jp/
"""

# ---- AEO/GEO の標準セット（llms.txt / robots.txt / sitemap.xml）----
# 他のKurage製品と同じ形にそろえる。AI検索に「何を答えるサイトか」を最初に渡す。
# ---- 地図（MapLibre + ベクタータイル） ---------------------------------------
# 区域は179万件あるので GeoJSON では配信できない。PostGIS の ST_AsMVT でタイル化し、
# 一度作ったタイルはディスクに残す（kflood と同じ作り）。
TILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tiles")
TILE_SQL = """SELECT ST_AsMVT(q, 'sediment', 4096, 'geom') FROM (
    SELECT zone_kind AS k, phenomenon AS p,
           ST_AsMVTGeom(ST_Transform(geom, 3857), ST_TileEnvelope(%(z)s, %(x)s, %(y)s), 4096, 64, true) AS geom
    FROM hazard_sediment
    WHERE geom && ST_Transform(ST_TileEnvelope(%(z)s, %(x)s, %(y)s), 6668)) q"""
TILE_MINZ, TILE_MAXZ = 11, 16


@app.get("/tiles/{z}/{x}/{y}.pbf")
def vector_tile(z: int, x: int, y: int):
    hdr = {"Cache-Control": "public, max-age=86400"}
    if z < TILE_MINZ or z > TILE_MAXZ or x < 0 or y < 0 or x >= 2 ** z or y >= 2 ** z:
        return Response(status_code=204, headers=hdr)
    path = os.path.join(TILE_DIR, str(z), str(x), f"{y}.pbf")
    if os.path.exists(path):
        data = open(path, "rb").read()
    else:
        with conn() as c, c.cursor() as cur:
            cur.execute(TILE_SQL, dict(z=z, x=x, y=y))
            row = cur.fetchone()
        data = bytes(row[0]) if row and row[0] else b""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    if not data:
        return Response(status_code=204, headers=hdr)
    return Response(content=data, media_type="application/vnd.mapbox-vector-tile", headers=hdr)


@app.get("/api/at")
def check_at(request: Request, lat: float, lon: float):
    """座標での判定（地図クリック用）。住所を介さないので町丁目代表点のズレが無い。"""
    ip = request.client.host if request.client else "?"
    if limited(ip, per_min=60):
        raise HTTPException(429, "アクセスが集中しています。1分ほど待って再度お試しください")
    with conn() as c, c.cursor() as cur:
        cur.execute("""SELECT zone_kind, phenomenon, zone_no, zone_name, address, designated_on
                       FROM hazard_sediment
                       WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s,%s),6668))
                       ORDER BY zone_kind DESC""", (lon, lat))
        rows = [dict(zone_kind=zk, zone_kind_label=ZONE_KIND.get(zk, "不明"),
                     phenomenon_label=PHENOMENON.get(ph, "不明"),
                     zone_no=zn, zone_name=nm, address=ad,
                     designated_on=fmt_designated(dt))
                for zk, ph, zn, nm, ad, dt in cur.fetchall()]
        hits = [r for r in rows if r["zone_kind"] not in PLANNED_KINDS]
        planned = [r for r in rows if r["zone_kind"] in PLANNED_KINDS]
        out = {"lat": lat, "lon": lon, "in_hazard_zone": bool(hits), "zones": hits,
               "in_planned_zone": bool(planned), "planned_zones": planned}
        if not rows:
            # 区域外と言い切る前に最寄りの区域までの距離を測る（住所判定と同じ方針）。
            cur.execute("""SELECT round(ST_Distance(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s,%s),6668)::geography)::numeric) AS m,
                             zone_kind, zone_name
                           FROM hazard_sediment WHERE zone_kind NOT IN (3,4)
                           ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s,%s),6668)
                           LIMIT 1""", (lon, lat, lon, lat))
            n = cur.fetchone()
            if n:
                out["nearest"] = {"distance_m": int(n[0]),
                                  "zone_kind_label": ZONE_KIND.get(n[1], "不明"), "zone_name": n[2]}
    return JSONResponse(out)


_MAP_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-BP0650KDFR"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}gtag('js',new Date());gtag('config','G-BP0650KDFR');</script>
<script>(function(){var s=document.createElement('script');s.src='https://kurage.exbridge.jp/simpletrack.php?url='+encodeURIComponent(location.href)+'&ref='+encodeURIComponent(document.referrer);s.async=true;document.head.appendChild(s)})();</script>
<title>地図で見る｜Kurage 土砂災害ハザードマップ</title>
<meta name="description" content="土砂災害警戒区域（イエローゾーン）と特別警戒区域（レッドゾーン）を地図に重ねて表示します。クリックするとその地点の区分・現象・指定年月日が出ます。全国47都道府県を収録。">
<link rel="canonical" href="https://kurage.exbridge.jp/khazard.php/map/">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="website"><meta property="og:site_name" content="Kurage">
<meta property="og:title" content="地図で見る｜Kurage 土砂災害ハザードマップ">
<meta property="og:description" content="イエローゾーン・レッドゾーンを地図で。クリックで区分と指定年月日を判定します。">
<meta property="og:url" content="https://kurage.exbridge.jp/khazard.php/map/">
<meta property="og:image" content="https://kurage.exbridge.jp/pv/khazard-pv-poster.jpg">
<meta name="twitter:card" content="summary_large_image">
<link href="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js"></script>
<style>
:root{--ink:#12202f;--muted:#5a6a7a;--line:#dce7ea;--teal:#0a9a8f;--deep:#0a726b;--paper:#f7fbfa}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);line-height:1.75;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans JP",sans-serif}
header{background:#fff;border-bottom:1px solid var(--line)}
.bar{max-width:1040px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.brand{font-weight:800;color:var(--ink);text-decoration:none;font-size:16px}
.brand small{display:block;font-weight:500;font-size:11.5px;color:var(--muted)}
.bar nav{margin-left:auto}.bar nav a{color:var(--deep);text-decoration:none;font-size:13.5px;margin-left:14px}
main{max-width:1040px;margin:0 auto;padding:22px 20px 60px}
h1{font-size:clamp(19px,3.2vw,25px);margin:0 0 8px}
.muted{color:var(--muted);font-size:13.5px}
.maprow{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,320px);gap:14px;margin-top:14px}
@media(max-width:820px){.maprow{grid-template-columns:minmax(0,1fr)}}
#map{height:min(70vh,620px);border-radius:12px;border:1px solid var(--line);min-width:0}
.side{min-width:0}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.legend{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:12.5px;margin-top:10px}
.legend i{display:inline-block;width:14px;height:14px;border-radius:3px;vertical-align:-2px;margin-right:6px;border:1px solid rgba(0,0,0,.18)}
.note{background:#fff8e8;border:1px solid #ecd8a7;border-radius:9px;padding:10px 12px;font-size:12.5px;margin:10px 0 0}
form.search{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
input[type=text]{flex:1;min-width:min(100%,220px);padding:11px 13px;border:1px solid var(--line);border-radius:9px;font-size:15px}
button.go{padding:11px 20px;border:0;border-radius:9px;background:linear-gradient(135deg,var(--teal),var(--deep));color:#fff;font-weight:700;cursor:pointer}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left}
th{background:#eef6f5;white-space:nowrap}
</style></head><body>
<header><div class="bar">
 <a class="brand" href="../">Kurage 土砂災害ハザードマップ<small>EXBRIDGE, INC.</small></a>
 <nav><a href="../">住所で調べる</a><a href="./">地図で見る</a></nav>
</div></header>
<main>
<h1>地図で見る</h1>
<p class="muted">全国の土砂災害警戒区域を地図に重ねています。<b>地図をクリック</b>すると、その地点の区分・現象・指定年月日が出ます。</p>

<div class="maprow">
 <div id="map"></div>
 <div class="side">
  <div class="card" id="result"><p class="muted" style="margin:0">地図をクリックすると、ここに判定が出ます。</p></div>
  <div class="legend">
   <b>凡例</b>
   <div><i style="background:#e8b84b"></i>土砂災害警戒区域（イエローゾーン）</div>
   <div><i style="background:#c0392b"></i>土砂災害特別警戒区域（レッドゾーン）</div>
   <div><i style="background:#9aa7b4"></i>指定予定（まだ指定されていません）</div>
  </div>
  <div class="note" id="hint" hidden>もう少し<b>拡大</b>すると区域を表示します。</div>
  <div class="note"><b>色が付いていない＝安全ではありません。</b>このデータは県内のすべての区域を網羅しているわけではなく、縮尺1/25,000相当の概略図です。</div>
 </div>
</div>

<form class="search" method="get" action="./">
 <input type="text" name="q" value="__Q__" placeholder="住所で移動（例: 広島県広島市安佐南区八木）">
 <button class="go" type="submit">移動</button>
</form>
<p class="muted" style="margin-top:16px">背景地図: 国土地理院 淡色地図。区域: 国土数値情報「土砂災害警戒区域データ(A33)」国土交通省を加工して作成。<br>
本サービスの判定は参考情報です。宅地建物取引業法の重要事項説明など、根拠を示す必要がある用途には使えません。</p>
<p class="muted" style="margin-top:14px">このシステムは買い切りで自社サーバーに設置できます → <a href="https://kappstore.exbridge.jp/app.php?id=02b945f9c87c9d86&amp;ref=khazard-map" target="_blank" rel="noopener" style="color:#0a726b">Kurage 土砂災害ハザードマップ（税込55,000円・ソースコード同梱）</a></p>
</main>
<script>
var BASE='../';
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
var map=new maplibregl.Map({container:'map',
 style:{version:8,sources:{
   gsi:{type:'raster',tiles:['https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png'],tileSize:256,
        attribution:'<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">地理院タイル</a>'},
   sed:{type:'vector',tiles:[location.origin+location.pathname.replace(/map\/$/,'')+'tiles/{z}/{x}/{y}.pbf'],minzoom:11,maxzoom:16}},
  layers:[{id:'gsi',type:'raster',source:'gsi'},
   {id:'sed-fill',type:'fill',source:'sed','source-layer':'sediment',
    paint:{'fill-color':['match',['get','k'],1,'#e8b84b',2,'#c0392b',3,'#9aa7b4',4,'#9aa7b4','#888'],
           'fill-opacity':['match',['get','k'],3,0.35,4,0.45,0.45]}},
   {id:'sed-line',type:'line',source:'sed','source-layer':'sediment',
    filter:['!',['in',['get','k'],['literal',[3,4]]]],
    paint:{'line-color':['match',['get','k'],1,'#b98c1e',2,'#8e2a1e','#666'],'line-width':0.7}},
   /* 指定予定は破線にする。塗りだけだと「指定済み」と見分けがつかない。 */
   {id:'sed-planned-line',type:'line',source:'sed','source-layer':'sediment',
    filter:['in',['get','k'],['literal',[3,4]]],
    paint:{'line-color':'#66727e','line-width':1.1,'line-dasharray':[2,2]}}]},
 center:[__LON__,__LAT__],zoom:__ZOOM__,minZoom:5,maxZoom:17});
map.addControl(new maplibregl.NavigationControl({showCompass:false}),'top-right');
map.addControl(new maplibregl.GeolocateControl({positionOptions:{enableHighAccuracy:true}}),'top-right');
function hint(){document.getElementById('hint').hidden = map.getZoom() >= 11;}
map.on('load',hint); map.on('zoomend',hint);
var marker=null;
function judge(lat,lon){
 document.getElementById('result').innerHTML='<p class="muted" style="margin:0">判定しています…</p>';
 fetch(BASE+'api/at?lat='+lat+'&lon='+lon).then(function(r){return r.json()}).then(function(j){
  var h='';
  if(j.in_hazard_zone){
   var red=j.zones.some(function(z){return z.zone_kind===2});
   h='<div style="font-weight:800;color:'+(red?'#c0392b':'#b98c1e')+';margin-bottom:6px">'
     +(red?'土砂災害特別警戒区域（レッドゾーン）の中です':'土砂災害警戒区域（イエローゾーン）の中です')+'</div>'
     +'<table><tr><th>区分</th><th>現象</th><th>指定年月日</th></tr>';
   for(var i=0;i<j.zones.length;i++){var z=j.zones[i];
    h+='<tr><td>'+esc(z.zone_kind_label)+'</td><td>'+esc(z.phenomenon_label)+'</td><td>'+esc(z.designated_on||'—')+'</td></tr>';}
   h+='</table>';
   if(j.zones[0].zone_name)h+='<div class="muted">区域名: '+esc(j.zones[0].zone_name)+'</div>';
   if(red)h+='<div class="note">特別警戒区域では、住宅の新築・増築に建築基準法の構造規制がかかり、宅地の造成に許可が要ります。</div>';
  }else if(j.in_planned_zone){
   h='<div style="font-weight:800;color:#5a6a7a;margin-bottom:6px">指定が予定されている区域です</div>'
     +'<div class="note"><b>まだ指定されていないため、現時点で法律上の制限はかかっていません。</b>'
     +'ただし危険性があると判断されている場所で、指定されると制限の対象になります。</div>'
     +'<table><tr><th>区分</th><th>現象</th></tr>';
   for(var i=0;i<j.planned_zones.length;i++){var z=j.planned_zones[i];
    h+='<tr><td>'+esc(z.zone_kind_label)+'</td><td>'+esc(z.phenomenon_label)+'</td></tr>';}
   h+='</table>';
  }else{
   h='<div style="font-weight:800;color:#0a726b;margin-bottom:6px">区域には入っていません</div>';
   if(j.nearest)h+='<div class="muted">最も近い区域まで約 '+esc(j.nearest.distance_m)+' m（'+esc(j.nearest.zone_kind_label)+'）</div>';
   h+='<div class="note">区域外は安全という意味ではありません。指定は随時行われ、このデータに反映されていない区域もあります。</div>';
  }
  h+='<p style="margin:10px 0 0"><a href="'+BASE+'" style="color:#0a726b">住所で詳しく調べる →</a></p>';
  document.getElementById('result').innerHTML=h;
  if(marker)marker.remove();
  marker=new maplibregl.Marker({color:'#0a9a8f'}).setLngLat([lon,lat]).addTo(map);
 }).catch(function(){document.getElementById('result').innerHTML='<p class="muted" style="margin:0">判定できませんでした。もう一度クリックしてください。</p>';});
}
map.on('click',function(e){judge(+e.lngLat.lat.toFixed(6),+e.lngLat.lng.toFixed(6))});
__AUTO__
</script></body></html>"""


@app.get("/map/", response_class=HTMLResponse)
def map_page(lat: float = None, lon: float = None, q: str = ""):
    q = (q or "").strip()[:100]
    if q and lat is None:
        try:
            g = geocode(q)
        except Exception:
            g = None
        if g:
            lat, lon = g["lat"], g["lon"]
    auto = "map.on('load',function(){judge(%r,%r)});" % (lat, lon) if lat is not None else ""
    return HTMLResponse(_MAP_HTML
                        .replace("__LAT__", str(lat if lat is not None else 34.39))
                        .replace("__LON__", str(lon if lon is not None else 132.46))
                        .replace("__ZOOM__", "14" if lat is not None else "12")
                        .replace("__Q__", q.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;"))
                        .replace("__AUTO__", auto))


@app.get("/robots.txt", response_class=PlainTextResponse)
def _robots():
    return "User-agent: *\nAllow: /\n\nSitemap: https://kurage.exbridge.jp/khazard.php/sitemap.xml\n"


@app.get("/sitemap.xml")
def _sitemap():
    base = "https://kurage.exbridge.jp/khazard.php"
    # 1,603市区町村＋47都道府県。枚数を出さないと検索の入口が増えない（2026-09-13 実測の結論）
    urls = (["/", "/map/", "/about", "/area/"]
            + ["/area/pref/" + pc for pc in sorted(MUNI_BY_PREF)]
            + ["/area/" + SLUG_BY_CODE[c] for c in sorted(MUNI)])
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(f'<url><loc>{base}{u}</loc><changefreq>monthly</changefreq></url>' for u in urls)
           + '</urlset>')
    return Response(content=xml, media_type="application/xml")


@app.get("/llms.txt", response_class=PlainTextResponse)
def _llms():
    return _LLMS_BODY
