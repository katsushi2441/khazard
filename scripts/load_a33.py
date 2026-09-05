#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国土数値情報の土砂災害警戒区域(A33)を PostGIS に取り込む。

GDAL の GML ドライバはこのスキーマのレイヤを認識しないので自前で解析する
（2026-09-05 実測: ogrinfo はファイルを開けるがレイヤ0件）。

GMLの構造:
  ファイル前半に gml:Surface（gml:id="sf12345"）が並び、
  後半の ksj:SedimentRelatedDisasterWarningAreasPolygon が
  <ksj:bounds xlink:href="#sf12345"/> で参照する。
  座標は JGD2011 (B, L) = 緯度 経度 の順（EPSG:6668）。

  取り込むと同時に datasets 表へ「どこから・いつ時点のデータか」を記録する。
  この製品は判定結果に必ずデータ時点を添えるので、ここが欠けると出力できない。

使い方:
  python3 scripts/load_a33.py 23            # 愛知県だけ
  python3 scripts/load_a33.py all           # 全都道府県
  python3 scripts/load_a33.py 23 --year 25  # 版を指定（既定は最新を自動探索）
"""
import argparse
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'data', 'raw')
BASE = 'https://nlftp.mlit.go.jp/ksj/gml/data/A33'
UA = {'User-Agent': 'khazard/1.0 (kurage.exbridge.jp; +https://exbridge.jp/)'}
SRID = 6668          # JGD2011 地理座標系
GML = '{http://schemas.opengis.net/gml/3.2.1}'
KSJ = '{http://nlftp.mlit.go.jp/ksj/schemas/ksj-app}'
XLINK = '{http://www.w3.org/1999/xlink}'

# 仕様書（KsjTmplt-A33.html）で確認したコードの意味。
# 誤ると「レッドゾーンなのに警戒区域と表示する」事故になるので定数で持つ。
ZONE_KIND = {1: '土砂災害警戒区域（イエローゾーン）',
             2: '土砂災害特別警戒区域（レッドゾーン）'}
PHENOMENON = {1: '急傾斜地の崩壊', 2: '土石流', 3: '地すべり'}

DB = dict(host='127.0.0.1', port=55433, dbname='khazard', user='postgres',
          password=os.environ.get('KHAZARD_DB_PASS', 'khazard_local'))

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;

-- どのデータを、どこから、いつ時点のものとして取り込んだか。
-- 判定結果に必ず添えるため、この表が空だと API は判定を返さない。
CREATE TABLE IF NOT EXISTS datasets (
  key           text PRIMARY KEY,
  name          text NOT NULL,
  source_url    text NOT NULL,
  data_vintage  text NOT NULL,   -- 提供元メタデータの作成日
  loaded_at     timestamptz NOT NULL,
  attribution   text NOT NULL,   -- 出典表記（PDL1.0 の要件）
  note          text
);

CREATE TABLE IF NOT EXISTS hazard_sediment (
  id            bigserial PRIMARY KEY,
  dataset_key   text NOT NULL REFERENCES datasets(key) ON DELETE CASCADE,
  pref_code     text,
  zone_kind     smallint,        -- 1=警戒区域(イエロー) 2=特別警戒区域(レッド)
  phenomenon    smallint,        -- 1=急傾斜地の崩壊 2=土石流 3=地すべり
  zone_no       text,
  zone_name     text,
  address       text,
  designated_on date,            -- 区域ごとの指定年月日
  geom          geometry(MultiPolygon, 6668) NOT NULL
);
CREATE INDEX IF NOT EXISTS hazard_sediment_geom_idx ON hazard_sediment USING GIST (geom);
CREATE INDEX IF NOT EXISTS hazard_sediment_pref_idx ON hazard_sediment (pref_code);
"""


def psql(sql, args=None):
    import psycopg2
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            try:
                return cur.fetchall()
            except Exception:
                return None


def latest_year(pref):
    """配布されている最新の版を実地で探す。存在しない年を決め打ちしない。"""
    for yy in range(28, 12, -1):
        url = f'{BASE}/A33-{yy:02d}/A33-{yy:02d}_{pref}_GML.zip'
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA, method='HEAD'), timeout=25)
            if r.getcode() == 200:
                return yy
        except Exception:
            continue
    return None


def fetch(pref, yy):
    os.makedirs(RAW, exist_ok=True)
    url = f'{BASE}/A33-{yy:02d}/A33-{yy:02d}_{pref}_GML.zip'
    zpath = os.path.join(RAW, f'A33-{yy:02d}_{pref}.zip')
    if not os.path.exists(zpath):
        print(f'  取得中: {url}')
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=300) as r, open(zpath, 'wb') as f:
            f.write(r.read())
    d = os.path.join(RAW, f'A33-{yy:02d}_{pref}')
    if not os.path.isdir(d):
        with zipfile.ZipFile(zpath) as z:
            z.extractall(d)
    # 配布zipはパス区切りが「\」なので、Linuxではファイル名に「\」が入ったまま
    # 1ファイルとして展開される（ディレクトリにならない）。basename で判定しない。
    xml = meta = None
    for root, _, files in os.walk(d):
        for fn in files:
            p = os.path.join(root, fn)
            if not fn.endswith('.xml'):
                continue
            if 'KS-META' in fn:
                meta = p
            else:
                xml = p
    if not xml:
        sys.exit(f'GMLが見つかりません: {d}')
    return url, xml, meta


def vintage(meta_path):
    """提供元メタデータの作成日。取れなければ判定に使わせない（空を返さない）。"""
    if not meta_path or not os.path.exists(meta_path):
        sys.exit('メタデータが無く、データ時点を確定できません。取り込みを中止します')
    t = open(meta_path, encoding='utf-8', errors='ignore').read()
    m = re.search(r'<[^>]*date[^>]*>\s*(\d{4}-\d{2}-\d{2})', t, re.I)
    if not m:
        sys.exit('メタデータから作成日を読めません。取り込みを中止します')
    return m.group(1)


def poslist_to_ring(text):
    """JGD2011 (B, L) = 緯度 経度 の順で並ぶ。WKT は経度 緯度 の順なので入れ替える。"""
    v = text.split()
    pts = [f'{v[i+1]} {v[i]}' for i in range(0, len(v) - 1, 2)]
    if pts and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def parse(xml_path):
    """3段階の参照を辿る。

    このGMLは実体を直接持たず、すべて xlink で参照する（2026-09-05 実測）:
      gml:Curve(id=cv1_0) が posList を持つ
      gml:Surface(id=sf1) の exterior/Ring/curveMember が #cv1_0 を指す
      フィーチャの ksj:bounds が #sf1 を指す
    ファイル内の並びは Curve → Surface → フィーチャ なので1パスで解ける。
    """
    curves = {}
    surfaces = {}
    feats = []
    ctx = ET.iterparse(xml_path, events=('end',))
    for _, el in ctx:
        tag = el.tag
        if tag == GML + 'Curve':
            cid = el.get(GML + 'id')
            pts = []
            for pl in el.iter(GML + 'posList'):
                pts += poslist_to_ring(pl.text or '')
            if cid and len(pts) >= 4:
                curves[cid] = pts
            el.clear()
        elif tag == GML + 'Surface':
            sid = el.get(GML + 'id')
            rings = []
            for patch in el.iter(GML + 'PolygonPatch'):
                ext, ints = None, []
                for side in ('exterior', 'interior'):
                    for node in patch.findall(GML + side):
                        pts = []
                        for cm in node.iter(GML + 'curveMember'):
                            ref = (cm.get(XLINK + 'href') or '').lstrip('#')
                            pts += curves.get(ref, [])
                        # 参照ではなく直接書かれている場合にも備える
                        if not pts:
                            for pl in node.iter(GML + 'posList'):
                                pts += poslist_to_ring(pl.text or '')
                        if len(pts) >= 4:
                            if side == 'exterior':
                                ext = pts
                            else:
                                ints.append(pts)
                if ext:
                    body = '(' + ','.join(ext) + ')'
                    for r in ints:
                        body += ',(' + ','.join(r) + ')'
                    rings.append('(' + body + ')')
            if sid and rings:
                surfaces[sid] = 'MULTIPOLYGON(' + ','.join(rings) + ')'
            el.clear()
        elif tag.endswith('SedimentRelatedDisasterWarningAreasPolygon'):
            def txt(name):
                n = el.find(KSJ + name)
                return (n.text or '').strip() if n is not None and n.text else None
            b = el.find(KSJ + 'bounds')
            href = (b.get(XLINK + 'href') if b is not None else '') or ''
            pad = None
            for tp in el.iter(GML + 'timePosition'):
                if tp.text and re.fullmatch(r'\d{8}', tp.text.strip()):
                    s = tp.text.strip()
                    pad = f'{s[:4]}-{s[4:6]}-{s[6:]}'
                break
            feats.append(dict(sid=href.lstrip('#'),
                              cop=txt('cop'), coz=txt('coz'), prc=txt('prc'),
                              znn=txt('znn'), znm=txt('znm'), ads=txt('ads'),
                              pad=pad))
            el.clear()
    return surfaces, feats


def load(pref, yy):
    import psycopg2
    from psycopg2.extras import execute_batch
    url, xml, meta = fetch(pref, yy)
    v = vintage(meta)
    key = f'A33-{yy:02d}_{pref}'
    print(f'  解析中: {os.path.basename(xml)}（データ時点 {v}）')
    surfaces, feats = parse(xml)
    rows = []
    miss = 0
    for f in feats:
        wkt = surfaces.get(f['sid'])
        if not wkt:
            miss += 1
            continue
        rows.append((key, f['prc'],
                     int(f['coz']) if f['coz'] and f['coz'].isdigit() else None,
                     int(f['cop']) if f['cop'] and f['cop'].isdigit() else None,
                     f['znn'], f['znm'], f['ads'], f['pad'], wkt))
    print(f"  曲線 {len(surfaces):,} / 区域 {len(feats):,} / 突合できず {miss}")
    if not rows:
        sys.exit('取り込む行がありません')

    attribution = ('出典: 国土数値情報（土砂災害警戒区域データ）国土交通省 を加工して作成。'
                   'この地図の作成にあたっては、国土地理院長の承認を得て、'
                   '同院発行の基盤地図情報を使用した（承認番号 平27情使、第585号）。')
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("""INSERT INTO datasets(key,name,source_url,data_vintage,loaded_at,attribution,note)
                           VALUES(%s,%s,%s,%s,now(),%s,%s)
                           ON CONFLICT (key) DO UPDATE SET
                             source_url=EXCLUDED.source_url, data_vintage=EXCLUDED.data_vintage,
                             loaded_at=EXCLUDED.loaded_at, attribution=EXCLUDED.attribution""",
                        (key, '国土数値情報（土砂災害警戒区域）A33', url, v, attribution,
                         '区域ごとの指定年月日は designated_on 列に保持する'))
            cur.execute('DELETE FROM hazard_sediment WHERE dataset_key=%s', (key,))
            execute_batch(cur, """INSERT INTO hazard_sediment
                (dataset_key,pref_code,zone_kind,phenomenon,zone_no,zone_name,address,designated_on,geom)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s, ST_Multi(ST_GeomFromText(%s,6668)))""",
                rows, page_size=500)
    n = psql('SELECT count(*) FROM hazard_sediment WHERE dataset_key=%s', (key,))[0][0]
    print(f'  取り込み完了: {n:,} 区域（{key}）')
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pref', help='都道府県コード2桁 または all')
    ap.add_argument('--year', type=int, default=None, help='版（既定は最新を自動探索）')
    a = ap.parse_args()
    prefs = [f'{i:02d}' for i in range(1, 48)] if a.pref == 'all' else [a.pref.zfill(2)]
    total = 0
    for p in prefs:
        yy = a.year or latest_year(p)
        if not yy:
            print(f'  {p}: 配布データが見つかりません'); continue
        print(f'== 都道府県 {p} / A33-{yy:02d}')
        try:
            total += load(p, yy)
        except SystemExit:
            raise
        except Exception as e:
            print(f'  {p}: 失敗 {e}')
    print(f'\n合計 {total:,} 区域')


if __name__ == '__main__':
    main()
