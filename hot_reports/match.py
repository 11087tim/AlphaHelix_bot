"""英文標題 → nash-ai 報告 id 匹配：多候選關鍵字搜尋 + 機構/日期/頁數交叉計分。"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from . import config
from .nash import NashClient

logger = logging.getLogger(__name__)

INST_MAP = {
    '摩根士丹利': ['morgan stanley'],
    '大摩': ['morgan stanley'],
    '高盛': ['goldman'],
    '摩根大通': ['jpmorgan', 'jp morgan', 'j.p. morgan'],
    '瑞银': ['ubs'],
    '花旗': ['citi'],
    '美银': ['bofa', 'bank of america', 'baml'],
    '野村': ['nomura'],
    '杰富瑞': ['jefferies'],
    '汇丰': ['hsbc'],
    '巴克莱': ['barclays'],
    '德银': ['deutsche'],
    '瑞信': ['credit suisse'],
}


def _tokens(s: str) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', s.lower()))


def candidate_keywords(title_en: str) -> list[str]:
    """由長到短產生候選關鍵字（去括號、拆冒號/分號段、去系列名前綴）。"""
    kws = [title_en]
    no_paren = re.sub(r'\s+', ' ', re.sub(r'\([^)]*\)', ' ', title_en)).strip()
    if no_paren != title_en:
        kws.append(no_paren)
    segs = sorted((s.strip() for s in re.split(r'[:;]', no_paren) if len(s.strip()) >= 8),
                  key=len, reverse=True)
    kws += segs
    for s in list(segs):
        kws += [p.strip() for p in s.split('-') if len(p.strip()) >= 12]
    words = no_paren.split()
    if len(words) >= 5:
        for drop in (1, 2, 3):
            cand = ' '.join(words[drop:])
            if len(cand) >= 10:
                kws.append(cand)
    seen: set[str] = set()
    return [k for k in kws if not (k.lower() in seen or seen.add(k.lower()))]


def score(rec: dict, item: dict) -> float:
    """token 重疊(0~1) + 日期吻合(0.3) + 頁數吻合(0.3) + 機構吻合(0.2)。"""
    q, t = _tokens(item['title_en']), _tokens(rec.get('title') or '')
    if not q or not t:
        return 0.0
    s = len(q & t) / len(q)
    if item.get('date') and rec.get('reDate'):
        try:
            d1 = datetime.strptime(item['date'], '%Y%m%d')
            d2 = datetime.strptime(rec['reDate'][:10], '%Y-%m-%d')
            if abs((d1 - d2).days) <= 5:
                s += 0.3
        except ValueError:
            pass
    if item.get('pages') and rec.get('page'):
        if abs(int(item['pages']) - int(rec['page'])) <= 1:
            s += 0.3
    aliases = INST_MAP.get(item['institution'], [])
    if aliases and any(a in (rec.get('securities') or '').lower() for a in aliases):
        s += 0.2
    return s


def match_one(client: NashClient, item: dict) -> dict:
    """回傳 {status: match|weak|none, score, nash_id, nash_title, ...}。"""
    best, best_score, used_kw = None, 0.0, ''
    for kw in candidate_keywords(item['title_en']):
        try:
            recs = client.search(kw)
        except Exception as exc:
            logger.warning("搜尋『%s』失敗：%s", kw[:40], exc)
            raise
        for r in recs:
            sc = score(r, item)
            if sc > best_score:
                best, best_score, used_kw = r, sc, kw
        time.sleep(config.SEARCH_DELAY_SEC)
        if best_score >= 1.0:   # 標題高度吻合 + 至少一項交叉驗證，提前收工
            break
    status = 'match' if best_score >= config.MATCH_THRESHOLD else ('weak' if best else 'none')
    return {
        'status': status, 'score': round(best_score, 2), 'keyword_used': used_kw,
        'nash_id': best['id'] if best else None,
        'nash_title': best.get('title') if best else None,
        'nash_securities': best.get('securities') if best else None,
        'nash_date': best.get('reDate') if best else None,
        'nash_pages': best.get('page') if best else None,
    }
