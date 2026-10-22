# -*- coding: utf-8 -*-
"""GitHub Actions 采集器 v2：在被墙的海外媒体与国内之间架一座桥。

v2 修复（2026-10-22）：
  * 不再因「0 条」退出 1（只要脚本跑完就成功），避免个别源失效导致整个 job 变红
  * 每个源独立 try/except + 15s 超时，失败只记 warning
  * 输出 data/latest.json 时保留上一次成功抓到的条目（合并去重），避免空文件覆盖好数据
  * 打印汇总表，便于在 Actions 日志里一眼看出哪些源失效

运行位置：GitHub Actions（海外网络），每 10 分钟后由 workflow 提交回仓库；
本地采集器通过 ghproxy.net 拉取 raw 地址。
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/144.0 Safari/537.36')

# (key, 显示名, RSS 地址, 基础分, 领域提示)
FEEDS = [
    # ---- 全球头条（国内被墙，海外可达）----
    ('gh_bbc',        'BBC World',          'https://feeds.bbci.co.uk/news/world/rss.xml', 48, 'policy'),
    ('gh_aljazeera',  'Al Jazeera',         'https://www.aljazeera.com/xml/rss/all.xml', 48, 'policy'),
    ('gh_dw',         'DW World',           'https://rss.dw.com/rdf/rss-en-world', 46, 'policy'),
    ('gh_scmp',       'SCMP',               'https://www.scmp.com/rss/91/feed', 46, 'policy'),
    ('gh_nyt',        'NYT World',          'https://rss.nytimes.com/services/xml/rss/nyt/World.xml', 50, 'policy'),
    ('gh_guardian',   'Guardian World',     'https://www.theguardian.com/world/rss', 46, 'policy'),
    ('gh_wsj',        'WSJ World',          'https://feeds.a.dj.com/rss/RSSWorldNews.xml', 50, 'policy'),
    ('gh_nikkei',     'Nikkei Asia',        'https://asia.nikkei.com/rss/feed/nar', 50, 'economy'),
    ('gh_politico',   'Politico',           'https://www.politico.com/rss/politicopicks.xml', 44, 'policy'),
    # ---- 财经 ----
    ('gh_ft',         'Financial Times',    'https://www.ft.com/rss/home', 52, 'economy'),
    ('gh_yahoo',      'Yahoo Finance',      'https://finance.yahoo.com/news/rssindex', 44, 'economy'),
    ('gh_investing',  'Investing.com',      'https://www.investing.com/rss/news.rss', 44, 'economy'),
    ('gh_oilprice',   'OilPrice',           'https://oilprice.com/rss/main', 50, 'economy'),
    ('gh_coindesk',   'CoinDesk',           'https://www.coindesk.com/arc/outboundfeeds/rss/', 44, 'economy'),
    # ---- 防务 ----
    ('gh_defenseone', 'Defense One',        'https://www.defenseone.com/rss/all/', 52, 'military'),
    ('gh_twz',        'The War Zone',       'https://www.twz.com/feed', 52, 'military'),
    ('gh_breakingdef', 'Breaking Defense',  'https://breakingdefense.com/feed/', 50, 'military'),
    # ---- 科技 / 生物 ----
    ('gh_techreview',  'MIT Tech Review',   'https://www.technologyreview.com/feed/', 46, 'other'),
    ('gh_verge',       'The Verge',         'https://www.theverge.com/rss/index.xml', 44, 'other'),
    ('gh_stat',        'STAT News',         'https://www.statnews.com/feed/', 46, 'other'),
    ('gh_sciam',       'Scientific American', 'https://www.scientificamerican.com/feed/', 44, 'other'),
    ('gh_eurekalert',  'EurekAlert',        'https://www.eurekalert.org/news-rss/all', 42, 'other'),
]

MIL_WORDS = ('strike', 'strikes', 'attack', 'killed', 'missile', 'drone', 'war', 'troops',
             'airstrike', 'shelling', 'casualt', 'offensive', 'ceasefire', 'military')
ECO_WORDS = ('oil', 'brent', 'opec', 'gas', 'price', 'market', 'stocks', 'dollar', 'yield',
             'inflation', 'tariff', 'sanction', 'trade', 'economy', 'gdp', 'rate', 'earnings')
TECH_WORDS = ('artificial intelligence', ' ai ', 'semiconductor', 'chip', 'data center',
              'nvidia', 'quantum', 'robot', 'model', 'openai')
BIO_WORDS = ('study', 'research', 'virus', 'vaccine', 'cancer', 'clinical', 'gene',
             'scientists', 'species', 'outbreak', 'fda', 'trial')

MAX_AGE_HOURS = 36


def _clean(s, limit=400):
    s = re.sub(r'<[^>]+>', ' ', str(s or ''))
    s = re.sub(r'&[a-z]+;', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:limit]


def _parse_date(txt):
    if not txt:
        return None
    txt = txt.strip()
    for fmt in ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S %Z',
                '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%d'):
        try:
            d = dt.datetime.strptime(txt, fmt)
            if d.tzinfo:
                d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return d
        except Exception:
            continue
    return None


def _text(el, names):
    for n in names:
        for child in el:
            tag = child.tag.split('}')[-1].lower()
            if tag == n and (child.text or '').strip():
                return (child.text or '').strip()
    return ''


def _link(el):
    for child in el:
        tag = child.tag.split('}')[-1].lower()
        if tag == 'link':
            href = child.get('href')
            if href:
                return href.strip()
            if (child.text or '').strip():
                return child.text.strip()
    return ''


def fetch_one(key, name, url, base, hint):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'application/rss+xml, application/xml, text/xml, */*',
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    now = dt.datetime.utcnow()
    out = []
    for el in root.iter():
        tag = el.tag.split('}')[-1].lower()
        if tag not in ('item', 'entry'):
            continue
        title = _clean(_text(el, ('title',)), 200)
        if not title or len(title) < 6:
            continue
        d = _parse_date(_text(el, ('pubdate', 'published', 'updated', 'date')))
        if d and (now - d).total_seconds() > MAX_AGE_HOURS * 3600:
            continue
        low = title.lower()
        score = base
        cat = hint
        if any(w in low for w in MIL_WORDS):
            score += 12
            cat = 'military'
        if any(w in low for w in ECO_WORDS):
            score += 6
            cat = cat if cat == 'military' else 'economy'
        if any(w in low for w in TECH_WORDS):
            score += 4
        if any(w in low for w in BIO_WORDS):
            score += 2
        out.append({
            'source': key,
            'source_name': name,
            'title': title,
            'url': _link(el),
            'summary': _clean(_text(el, ('description', 'summary', 'content', 'encoded')), 360),
            'published_at': d.strftime('%Y-%m-%dT%H:%M:%SZ') if d else '',
            'category_hint': cat,
            'score_hint': min(95, score),
        })
        if len(out) >= 40:
            break
    return out


def _load_previous(path):
    try:
        with open(path, encoding='utf-8') as f:
            return (json.load(f).get('items') or [])
    except Exception:
        return []


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(base_dir, 'data')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, 'latest.json')

    prev = _load_previous(path)
    items, errors, ok_sources = [], [], []
    for key, name, url, base, hint in FEEDS:
        t0 = time.time()
        try:
            got = fetch_one(key, name, url, base, hint)
            items.extend(got)
            ok_sources.append((name, len(got)))
            print('%-20s %3d 条  %.1fs' % (name, len(got), time.time() - t0))
        except Exception as e:
            errors.append('%s: %s' % (name, str(e)[:100]))
            print('%-20s 失败      %.1fs  %s' % (name, time.time() - t0, str(e)[:80]))

    # 与上一次结果合并（本次抓到的优先），跨源按标题去重
    merged, seen = [], set()
    for it in items + prev:
        k = re.sub(r'\s+', '', (it.get('title') or '').lower())[:60]
        if not k or k in seen:
            continue
        seen.add(k)
        merged.append(it)
        if len(merged) >= 1200:
            break

    payload = {
        'generated_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'count': len(merged),
        'fresh': len(items),
        'ok_sources': len(ok_sources),
        'errors': errors,
        'items': merged,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print('=' * 50)
    print('本次抓取 %d 条，合并后 %d 条；成功源 %d/%d，失败 %d' % (
        len(items), len(merged), len(ok_sources), len(FEEDS), len(errors)))
    print('写入 %s' % path)
    # v2：始终成功退出（部分源失效不应让工作流变红）
    return 0


if __name__ == '__main__':
    sys.exit(main())
