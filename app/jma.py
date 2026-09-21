# -*- coding: utf-8 -*-
"""気象庁の警報・注意報（市区町村単位）。

出典: 気象庁 防災情報（bosai）の JSON。鍵は要らない。
  区域表   https://www.jma.go.jp/bosai/common/const/area.json
  警報等   https://www.jma.go.jp/bosai/warning/data/r8/<府県予報区コード>.json

**2026-05-29 に配信が令和8年体系（r8）へ移った。**旧 `/warning/data/warning/` は 5/28 で凍結。
旧パスを読み続けて「気象庁の配信が止まっている」と誤診し、記事と議員メールにそう書いた（2026-09-22）。
r8 は **1府県＝電文のリスト**（VPWW55 大雨／56 土砂／57 高潮／58 暴風／59 波浪／60 大雪／61 その他）で、
各電文の warning.class20Items[].areaCode（市区町村）に kinds[]{code,status} が並ぶ。
大雨・土砂・高潮は レベル2注意報／3警報／4危険警報／5特別警報 の4段階になった。

**kflood と同じモジュールを複製している（直すときは両方）。なぜ足したか（2026-09-21）**: 港区の防災ポータルは「警戒レベル相当情報」「避難情報」の枠が
常にあって、出ていないときは「ありません」と書く。名古屋市は発令が無いと枠ごと消えるので、
住民からは「出ていない」のか「取れていない」のか分からない。kflood は後者を区別する作りなので、
同じ考え方で気象警報・注意報の枠を足す。

**取得できないときは「発表なし」と言わない。** 市の避難情報（alerts.py）と同じ原則。

住所→市区町村コードは area.json の class20s（1,805件）を住所文字列に対する最長一致で引く。
政令市は区ではなく市の単位で発表されるので（名古屋市=2310000）、これで正しく当たる。
"""
import json
import os
import threading
import time
from datetime import datetime

import requests

AREA_URL = 'https://www.jma.go.jp/bosai/common/const/area.json'
WARN_URL = 'https://www.jma.go.jp/bosai/warning/data/r8/{office}.json'
SOURCE_NAME = '気象庁 防災情報'
SOURCE_URL = 'https://www.jma.go.jp/bosai/warning/'
UA = {'User-Agent': 'khazard/1.0 (kurage.exbridge.jp; jma)'}
CACHE_SEC = 180
AREA_MAX_AGE = 7 * 24 * 3600
# 発表中の項目があるのに、この時間より長く更新が無い報は「いま」と呼ばない。
# **注意報も警報も無い状態なら、最後の解除報が何か月前でも正しい**ので、古いこと自体は問題にしない。
# 問題は「発表中のまま更新が止まっている」ほう（2026-09-22 実測: 気象庁の warning JSON が
# 全都道府県 5月末の Last-Modified のまま止まっていて、5/28の濃霧注意報を『いま出ている』と
# 表示していた。予報 JSON は当日で生きているので、こちらの配信だけが止まっている）。
REPORT_MAX_AGE_H = 24
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AREA_PATH = os.path.join(ROOT, 'data', 'jma_area.json')

# 警報・注意報の種別コード（令和8年体系）。2026-09-22 に実際の電文（防災情報XML VPWW55〜61）から
# <Kind><Name>/<Code> を集めて作った。大雨・土砂・高潮は 0x=警報(L3)/1x,2x=注意報(L2)/3x=特別警報(L5)/4x=危険警報(L4)。
# 高潮の 08/19/38/48 は同じ規則からの当て推量で、電文で未確認（※印）。
CODES = {
    '00': '解除', '02': '暴風雪警報', '03': 'レベル3大雨警報', '04': '洪水警報',
    '05': '暴風警報', '06': '大雪警報', '07': '波浪警報', '08': 'レベル3高潮警報※',
    '09': 'レベル3土砂災害警報',
    '10': 'レベル2大雨注意報', '12': '大雪注意報', '13': '風雪注意報', '14': '雷注意報',
    '15': '強風注意報', '16': '波浪注意報', '17': '融雪注意報', '18': '洪水注意報',
    '19': 'レベル2高潮注意報※', '20': '濃霧注意報', '21': '乾燥注意報', '22': 'なだれ注意報',
    '23': '低温注意報', '24': '霜注意報', '25': '着氷注意報', '26': '着雪注意報',
    '27': 'その他の注意報', '29': 'レベル2土砂災害注意報',
    '32': '暴風雪特別警報', '33': 'レベル5大雨特別警報', '35': '暴風特別警報', '36': '大雪特別警報',
    '37': '波浪特別警報', '38': 'レベル5高潮特別警報※', '39': 'レベル5土砂災害特別警報',
    '43': 'レベル4大雨危険警報', '48': 'レベル4高潮危険警報※', '49': 'レベル4土砂災害危険警報',
}
# 画面の色と並び: 特別警報(L5) > 危険警報(L4) > 警報(L3) > 注意報(L2)
SPECIAL = {'32', '33', '35', '36', '37', '38', '39'}
DANGER = {'43', '48', '49'}
WARNINGS = {'02', '03', '04', '05', '06', '07', '08', '09'}
# 「出ていない」を表す status（発表中と区別する）
INACTIVE_STATUS = ('解除', '発表警報・注意報はなし', '警報・注意報はなし')

_lock = threading.Lock()
_cache = {}          # office -> {'at': float, 'data': dict}
_area = {'at': 0.0, 'd': None}


def _load_area():
    """区域表を読む。手元に無ければ取りに行き、data/jma_area.json に置く（週1で取り直す)。"""
    with _lock:
        if _area['d'] is not None and time.time() - _area['at'] < AREA_MAX_AGE:
            return _area['d']
        d = None
        try:
            st = os.path.getmtime(AREA_PATH)
            if time.time() - st < AREA_MAX_AGE:
                d = json.load(open(AREA_PATH, encoding='utf-8'))
        except (OSError, ValueError):
            d = None
        if d is None:
            try:
                r = requests.get(AREA_URL, headers=UA, timeout=20)
                r.raise_for_status()
                d = r.json()
                os.makedirs(os.path.dirname(AREA_PATH), exist_ok=True)
                with open(AREA_PATH, 'w', encoding='utf-8') as f:
                    json.dump(d, f, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                # 取りに行けなかったときは、古くても手元のものを使う（無ければ諦める）
                try:
                    d = json.load(open(AREA_PATH, encoding='utf-8'))
                except (OSError, ValueError):
                    return None
        _area.update(at=time.time(), d=d)
        return d


def office_for(muni_code: str):
    """市区町村コード（class20）から府県予報区コード（office）へ、親をたどる。"""
    d = _load_area()
    if not d:
        return None
    code = muni_code
    for key in ('class20s', 'class15s', 'class10s'):
        it = (d.get(key) or {}).get(code)
        if not it:
            return None
        code = it.get('parent')
    return code if code in (d.get('offices') or {}) else None


def muni_for_address(address: str):
    """住所の文字列から市区町村を引く（最長一致）。当たらなければ None。

    「愛知県名古屋市瑞穂区内浜町」→ 2310000（名古屋市）。政令市は市の単位で発表されるので、
    区名まで一致させない。同名の町村があるため、長い名前から先に見る。
    """
    d = _load_area()
    if not d or not address:
        return None
    best = None
    for code, it in (d.get('class20s') or {}).items():
        name = it.get('name') or ''
        if name and name in address and (best is None or len(name) > len(best[1])):
            best = (code, name)
    return dict(code=best[0], name=best[1]) if best else None


def fetch(office: str):
    """府県予報区の警報・注意報（r8）。取得できなければ前回値を stale で返す。

    r8 は電文のリスト。report_at はいちばん新しい電文の reportDatetime。
    headline は市区町村ごとに違うので、ここでは持たず status_for で選ぶ。
    """
    now = time.time()
    with _lock:
        c = _cache.get(office)
        if c and now - c['at'] < CACHE_SEC:
            return c['data']
    try:
        r = requests.get(WARN_URL.format(office=office), headers=UA, timeout=12)
        r.raise_for_status()
        raw = r.json()
        if isinstance(raw, dict):      # 万一 旧形式が返っても落ちないように
            raw = [raw]
        rep = max((x.get('reportDatetime') or '' for x in raw), default=None) or None
        data = dict(status='ok', raw=raw, fetched_at=datetime.now().strftime('%Y-%m-%d %H:%M'),
                    report_at=rep, headline='',
                    office_name=(raw[0].get('publishingOffice') if raw else None))
    except Exception as e:  # noqa: BLE001
        with _lock:
            c = _cache.get(office)
        if c:
            data = dict(c['data'])
            data['status'] = 'stale'
            data['error'] = type(e).__name__
            return data
        return dict(status='unavailable', error=type(e).__name__, raw=None,
                    fetched_at=datetime.now().strftime('%Y-%m-%d %H:%M'), headline='', report_at=None)
    with _lock:
        _cache[office] = dict(at=now, data=data)
    return data


def _kind(code: str) -> str:
    if code in SPECIAL:
        return 'special'
    if code in DANGER:
        return 'danger'
    if code in WARNINGS:
        return 'warning'
    return 'advisory'


def _items_for(raw, muni_code):
    """その市区町村に発表中の警報・注意報だけを、名前にして返す。解除・なしは落とす。

    r8: 電文ごとに warning.class20Items[].areaCode を見る。同じ code が複数電文に出たら1つにする。
    戻り値には、その市区町村に発表中の項目を含む電文のうち最新の headline も添える（_headline）。
    """
    out, head, head_at = [], '', ''
    for bul in (raw or []):
        w = (bul or {}).get('warning') or {}
        for a in w.get('class20Items', []):
            if a.get('areaCode') != muni_code:
                continue
            for k in a.get('kinds', []):
                st = k.get('status') or ''
                code = k.get('code')
                if not code or any(st.startswith(x) for x in INACTIVE_STATUS):
                    continue
                name = CODES.get(code, f'コード{code}')
                out.append(dict(code=code, name=name, kind=_kind(code), status=st))
                if (bul.get('reportDatetime') or '') > head_at:
                    head_at = bul.get('reportDatetime') or ''
                    head = (bul.get('headlineText') or '').strip()
    order = {'special': 0, 'danger': 1, 'warning': 2, 'advisory': 3}
    seen, uniq = set(), []
    for it in sorted(out, key=lambda x: (order[x['kind']], x['code'])):
        if it['code'] in seen:
            continue
        seen.add(it['code'])
        uniq.append(it)
    if uniq:
        uniq[0]['_headline'] = head
    return uniq


def status_for(address: str):
    """住所ひとつぶんの「いまの気象警報・注意報」。**枠は常に返す**（空でも返す）。

    status: ok（発表あり/なしを言い切れる）／stale（前回値）／unavailable（取得できない）／
            uncovered（市区町村を特定できない）
    """
    base = dict(status='uncovered', muni=None, muni_name=None, items=[], headline='',
                fetched_at=None, report_at=None, source=SOURCE_NAME, source_url=SOURCE_URL, office=None,
                report_age_h=None, source_outdated=False)
    m = muni_for_address(address or '')
    if not m:
        return base
    office = office_for(m['code'])
    if not office:
        return base
    d = fetch(office)
    base.update(muni=m['code'], muni_name=m['name'], office=office, status=d.get('status'),
                fetched_at=d.get('fetched_at'), report_at=d.get('report_at'),
                headline=d.get('headline') or '', office_name=d.get('office_name'))
    if d.get('status') in ('ok', 'stale'):
        base['items'] = _items_for(d.get('raw'), m['code'])
        if base['items']:
            base['headline'] = base['items'][0].pop('_headline', '') or ''
    base['report_age_h'] = _report_age_h(base.get('report_at'))
    # 発表中の項目があるのに更新が止まっている報は「いま」と言わない。
    # 本文（headline）は「28日夜遅くまで」のような期限つきの文なので、古いときは出さない。
    if base['items'] and base['report_age_h'] is not None and base['report_age_h'] > REPORT_MAX_AGE_H:
        base['source_outdated'] = True
        base['headline'] = ''
    return base


def _report_age_h(report_at):
    """発表時刻から何時間たったか。読めなければ None（古いと決めつけない）。"""
    if not report_at:
        return None
    try:
        t = datetime.fromisoformat(report_at)
    except ValueError:
        return None
    now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
    return max(0.0, (now - t).total_seconds() / 3600.0)


def max_kind(items):
    """いちばん重いものを返す（色を決めるのに使う）。"""
    for k in ('special', 'danger', 'warning', 'advisory'):
        if any(i['kind'] == k for i in items or []):
            return k
    return None
