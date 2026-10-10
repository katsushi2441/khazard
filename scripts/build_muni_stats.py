#!/usr/bin/env python3
"""市区町村ごとの土砂災害警戒区域の統計を作る（地域ページの中身）。

A33 に市区町村コードが無いので、scripts/muni.py で address から市区町村を決めて
`muni_stats` に集計を保存する。**地域ページに載せる数字はすべてここで実測した値**で、
市名を差し替えただけの薄いページを作らないための土台。

  cd /home/kojima/work/khazard && /usr/bin/python3 scripts/build_muni_stats.py
"""
import os, re, sys, json, collections
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from muni import load_canon, extract, PREFS, load_wards, extract_ward

DB = dict(host="127.0.0.1", port=55433, dbname="khazard", user="postgres",
          password=os.environ.get("KHAZARD_DB_PASS", "khazard_local"))
PREF_BY_CODE = {f"{i+1:02d}": p for i, p in enumerate(PREFS)}

DDL = """
CREATE TABLE IF NOT EXISTS muni_stats (
  muni_code   text PRIMARY KEY,
  pref_code   text NOT NULL,
  pref        text NOT NULL,
  muni        text NOT NULL,
  zones       integer NOT NULL,
  yellow      integer NOT NULL,   -- 区域区分1: 土砂災害警戒区域
  red         integer NOT NULL,   -- 区域区分2: 特別警戒区域
  planned     integer NOT NULL,   -- 区域区分3/4: 指定予定
  steep       integer NOT NULL,   -- 現象1: 急傾斜地の崩壊
  debris      integer NOT NULL,   -- 現象2: 土石流
  slide       integer NOT NULL,   -- 現象3: 地すべり
  first_on    date,
  last_on     date,
  unknown_on  integer NOT NULL,   -- 指定年月日が9999年（不明・未定）
  samples     jsonb NOT NULL,     -- 代表的な区域名と住所
  computed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS muni_stats_pref_idx ON muni_stats(pref_code);
-- 政令指定都市の区（住所の区名で振り分け。列の意味は muni_stats と同じ）
CREATE TABLE IF NOT EXISTS ward_stats (
  muni_code   text PRIMARY KEY,   -- 区の団体コード（例 14101）
  city_code   text NOT NULL,      -- 市の団体コード（例 14100）
  pref_code   text NOT NULL,
  pref        text NOT NULL,
  city        text NOT NULL,      -- 横浜市
  ward        text NOT NULL,      -- 鶴見区
  zones       integer NOT NULL,
  yellow      integer NOT NULL,
  red         integer NOT NULL,
  planned     integer NOT NULL,
  steep       integer NOT NULL,
  debris      integer NOT NULL,
  slide       integer NOT NULL,
  first_on    date,
  last_on     date,
  unknown_on  integer NOT NULL,
  samples     jsonb NOT NULL,
  computed_at timestamptz NOT NULL DEFAULT now()
);
-- 住所に区名が無い・旧区名などで区に振り分けられなかった区域（市のページに注記する）
CREATE TABLE IF NOT EXISTS ward_unassigned (
  city_code   text PRIMARY KEY,
  zones       integer NOT NULL,
  examples    jsonb NOT NULL,     -- 振り分けられなかった住所の書き出し（多い順）
  computed_at timestamptz NOT NULL DEFAULT now()
);
"""


def _new(**kw):
    return dict(zones=0, yellow=0, red=0, planned=0, steep=0, debris=0, slide=0,
                first_on=None, last_on=None, unknown_on=0, samples=[], **kw)


def _add(a, kind, phen, zname, addr, don):
    a["zones"] += 1
    if kind == 1: a["yellow"] += 1
    elif kind == 2: a["red"] += 1
    elif kind in (3, 4): a["planned"] += 1
    if phen == 1: a["steep"] += 1
    elif phen == 2: a["debris"] += 1
    elif phen == 3: a["slide"] += 1
    # 9999年は「不明・未定」を表す欠測コードなので、期間の最大値に混ぜない
    if don is not None:
        if don.year >= 9999:
            a["unknown_on"] += 1
        else:
            if a["first_on"] is None or don < a["first_on"]: a["first_on"] = don
            if a["last_on"] is None or don > a["last_on"]: a["last_on"] = don
    if len(a["samples"]) < 6 and zname and addr:
        a["samples"].append({"name": zname, "address": addr})


def main():
    pref_canon, muni_canon = load_canon()
    cn = psycopg2.connect(**DB)
    cur = cn.cursor()
    cur.execute(DDL)

    wcities, wards, wards_by_city = load_wards()
    agg, wagg = {}, {}
    wmiss = collections.defaultdict(collections.Counter)
    unmatched = collections.Counter()
    cur.execute("SELECT pref_code, zone_kind, phenomenon, zone_name, address, designated_on "
                "FROM hazard_sediment")
    n = 0
    for pc, kind, phen, zname, addr, don in cur:
        n += 1
        hit = extract(pc, addr, muni_canon)
        if not hit:
            unmatched[pc] += 1
            continue
        name, code = hit
        a = agg.get(code)
        if a is None:
            a = agg[code] = _new(pref_code=pc, pref=PREF_BY_CODE.get(pc, ""), muni=name)
        _add(a, kind, phen, zname, addr, don)
        if code in wcities:
            wc, rest = extract_ward(code, addr, wcities, wards_by_city)
            if wc:
                w = wagg.get(wc)
                if w is None:
                    wi = wards[wc]
                    w = wagg[wc] = _new(city_code=code, pref_code=pc, pref=wi["pref"],
                                        city=wi["city"], ward=wi["ward"])
                _add(w, kind, phen, zname, addr, don)
            else:
                # 例として見せる書き出し。旧区名（浜松市「北区」）はその区名でまとめる
                m = re.match(r"^[^0-9０-９]{1,3}?区", rest or "")
                wmiss[code][m.group(0) if m else ((rest or "")[:4] or "（市名のみ）")] += 1

    cur.execute("TRUNCATE muni_stats")
    ins = ("INSERT INTO muni_stats (muni_code,pref_code,pref,muni,zones,yellow,red,planned,"
           "steep,debris,slide,first_on,last_on,unknown_on,samples) VALUES "
           "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
    for code, a in agg.items():
        cur.execute(ins, (code, a["pref_code"], a["pref"], a["muni"], a["zones"], a["yellow"],
                          a["red"], a["planned"], a["steep"], a["debris"], a["slide"],
                          a["first_on"], a["last_on"], a["unknown_on"], json.dumps(a["samples"], ensure_ascii=False)))
    cur.execute("TRUNCATE ward_stats")
    wins = ("INSERT INTO ward_stats (muni_code,city_code,pref_code,pref,city,ward,zones,yellow,red,"
            "planned,steep,debris,slide,first_on,last_on,unknown_on,samples) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
    for code, w in wagg.items():
        cur.execute(wins, (code, w["city_code"], w["pref_code"], w["pref"], w["city"], w["ward"],
                           w["zones"], w["yellow"], w["red"], w["planned"], w["steep"], w["debris"],
                           w["slide"], w["first_on"], w["last_on"], w["unknown_on"],
                           json.dumps(w["samples"], ensure_ascii=False)))
    cur.execute("TRUNCATE ward_unassigned")
    for cc in wcities:
        if cc in agg:
            m = wmiss.get(cc, collections.Counter())
            cur.execute("INSERT INTO ward_unassigned (city_code,zones,examples) VALUES (%s,%s,%s)",
                        (cc, sum(m.values()), json.dumps(m.most_common(8), ensure_ascii=False)))
    cn.commit()
    # 検算: 市の区域数 ＝ 区の合計 ＋ 振り分けられない数
    for cc in sorted(wcities):
        if cc not in agg:
            continue
        ws_ = sum(w["zones"] for w in wagg.values() if w["city_code"] == cc)
        um = sum(wmiss.get(cc, {}).values())
        assert ws_ + um == agg[cc]["zones"], (cc, ws_, um, agg[cc]["zones"])
        print(f"   {wcities[cc]}: 市{agg[cc]['zones']:,} = 区の合計{ws_:,} + 振り分けられない{um:,}")
    miss = sum(unmatched.values())
    print(f"区域 {n:,} / 市区町村 {len(agg):,} / 住所から市区町村を特定できず {miss:,} ({miss*100/max(n,1):.2f}%)")
    cur.execute("SELECT pref, muni, zones, yellow, red, planned FROM muni_stats ORDER BY zones DESC LIMIT 5")
    for r in cur.fetchall():
        print(f"   {r[0]}{r[1]}: 区域{r[2]:,} イエロー{r[3]:,} レッド{r[4]:,} 指定予定{r[5]:,}")
    cn.close()


if __name__ == "__main__":
    main()
