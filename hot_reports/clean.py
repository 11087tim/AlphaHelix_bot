"""清洗 valuelist 標題：拆機構/中文/英文/日期/頁數，抽英文標題給 nash-ai 搜尋。

標題格式: 機構-中文標題-English Title-YYYYMMDD【N页】
英文標題內部可能含 '-'，所以從尾端逐段回收「CJK 比例低」的段落再拼回。
"""
from __future__ import annotations

import re

CJK = re.compile(r'[一-鿿　-〿！-／：-＠“”‘’]')

PUNCT_MAP = {
    '：': ':', '，': ',', '（': '(', '）': ')', '；': ';',
    '’': "'", '‘': "'", '“': '"', '”': '"',
    '–': '-', '—': '-', '＆': '&', '？': '?', '！': '!',
}


def _cjk_ratio(s: str) -> float:
    s = s.strip()
    if not s:
        return 1.0
    return len(CJK.findall(s)) / len(s)


def _normalize(s: str) -> str:
    for a, b in PUNCT_MAP.items():
        s = s.replace(a, b)
    return re.sub(r'\s+', ' ', s).strip(' -')


def parse_title(title: str) -> dict:
    date = pages = None
    m = re.search(r'-(\d{8})(?:【(\d+)页】)?$', title)
    if m:
        date, pages = m.group(1), m.group(2)
        title = title[:m.start()]

    parts = title.split('-')
    institution = parts[0]
    rest = parts[1:]

    en_parts: list[str] = []
    while rest:
        seg = rest[-1]
        if re.search(r'[A-Za-z]', seg) and _cjk_ratio(seg) < 0.15:
            en_parts.insert(0, rest.pop())
        else:
            break

    return {
        'institution': institution,
        'title_cn': '-'.join(rest),
        'title_en': _normalize('-'.join(en_parts)),
        'date': date,
        'pages': pages,
    }


def clean_rows(rows: list[dict]) -> list[dict]:
    """去重（同文跨區合併 sections）+ 解析標題。"""
    seen: dict[str, dict] = {}
    for r in rows:
        views = re.search(r'(\d+)\s*次', r.get('meta', ''))
        if r['url'] in seen:
            seen[r['url']]['sections'] += '|' + r['section']
            continue
        rec = parse_title(r['title'])
        rec.update({
            'sections': r['section'],
            'views': int(views.group(1)) if views else None,
            'url': r['url'],
            'title_raw': r['title'],
        })
        seen[r['url']] = rec
    return list(seen.values())
