import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Optional

import feedparser

RSS_URL  = "https://rss.arxiv.org/rss/{subject}"
API_URL  = "http://export.arxiv.org/api/query?search_query=cat:{subject}&sortBy=submittedDate&sortOrder=descending&max_results=50"
ATOM_NS  = {'a': 'http://www.w3.org/2005/Atom'}


def fetch_new_papers(subject_code: str, target_date: Optional[str] = None) -> list[dict]:
    """
    Fetch papers for a subject. If target_date is provided (YYYY-MM-DD),
    fetch papers submitted on that specific date using the API.
    Otherwise use RSS with API fallback.
    """
    if target_date:
        print(f"  Fetching papers for {target_date} via API...")
        return _fetch_api(subject_code, target_date)

    papers = _fetch_rss(subject_code)
    if not papers:
        print(f"  RSS empty, falling back to arXiv API...")
        papers = _fetch_api(subject_code)
    return papers


def _fetch_rss(subject_code: str) -> list[dict]:
    try:
        feed = feedparser.parse(RSS_URL.format(subject=subject_code))
        if not feed.entries:
            return []
        return [p for p in (_parse_rss_entry(e, subject_code) for e in feed.entries) if p]
    except Exception as e:
        print(f"  [WARN] RSS error: {e}")
        return []


def _fetch_api(subject_code: str, target_date: Optional[str] = None) -> list[dict]:
    """
    Fetch papers from arXiv API with retry logic for rate limiting.
    Retries up to 3 times on 429 or 5xx errors with exponential backoff.
    """
    if target_date:
        # Convert YYYY-MM-DD to YYYYMMDD format for arXiv API
        dt = datetime.strptime(target_date, '%Y-%m-%d')
        start_date = dt.strftime('%Y%m%d')
        end_date = (dt + timedelta(days=1)).strftime('%Y%m%d')
        date_filter = f"+AND+submittedDate:[{start_date}+TO+{end_date}]"
        url = f"http://export.arxiv.org/api/query?search_query=cat:{subject_code}{date_filter}&sortBy=submittedDate&sortOrder=descending&max_results=100"
    else:
        url = API_URL.format(subject=subject_code)

    req = urllib.request.Request(url, headers={'User-Agent': 'TheBrief/1.0 (mailto:contact@example.com)'})

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Respect arXiv rate limits: 5s base delay (3s minimum + headroom)
            time.sleep(5)

            with urllib.request.urlopen(req) as r:
                tree = ET.parse(r)
            papers = []
            for e in tree.findall('a:entry', ATOM_NS):
                p = _parse_api_entry(e, subject_code)
                if p:
                    papers.append(p)
            return papers

        except urllib.error.HTTPError as e:
            # Retry on rate limiting (429) or server errors (5xx)
            if e.code == 429 or e.code >= 500:
                if attempt < max_retries - 1:
                    wait = 30 * (2 ** attempt)  # 30s, 60s, 120s
                    print(f"  [WAIT] HTTP {e.code} — retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue
                else:
                    print(f"  [ERROR] API error after {max_retries} attempts: HTTP Error {e.code}: {e.reason}")
                    return []
            else:
                # Don't retry on other 4xx errors (bad request, not found, etc.)
                print(f"  [ERROR] API error: HTTP Error {e.code}: {e.reason}")
                return []

        except Exception as e:
            print(f"  [ERROR] API error: {e}")
            return []

    return []


def _parse_rss_entry(entry, subject_code: str) -> Optional[dict]:
    link = getattr(entry, 'link', '') or getattr(entry, 'id', '')
    arxiv_id = _extract_id(link)
    if not arxiv_id:
        return None
    title = re.sub(r'\s*\(arXiv:[^\)]+\)', '', getattr(entry, 'title', '')).strip()
    abstract = _clean(getattr(entry, 'summary', '') or getattr(entry, 'description', ''))
    authors = ', '.join(a.get('name','') for a in getattr(entry, 'authors', [])) or getattr(entry, 'author', '')
    tags = [t.get('term','') for t in getattr(entry, 'tags', [])]
    if subject_code not in tags:
        tags.insert(0, subject_code)
    announced = date.today().isoformat()
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        announced = date(*entry.published_parsed[:3]).isoformat()
    return {'id': arxiv_id, 'arxiv_url': f'https://arxiv.org/abs/{arxiv_id}',
            'announced_date': announced, 'subject': subject_code,
            'subjects_all': tags, 'title_original': title,
            'authors': authors, 'abstract': abstract}


def _parse_api_entry(e, subject_code: str) -> Optional[dict]:
    ns = ATOM_NS
    id_text = e.find('a:id', ns)
    if id_text is None:
        return None
    arxiv_id = _extract_id(id_text.text)
    if not arxiv_id:
        return None
    title = e.find('a:title', ns).text.strip().replace('\n', ' ')
    abstract = _clean(e.find('a:summary', ns).text or '')
    authors = ', '.join(
        a.find('a:name', ns).text
        for a in e.findall('a:author', ns)
        if a.find('a:name', ns) is not None
    )
    tags = [c.get('term','') for c in e.findall('a:category', ns)]
    if subject_code not in tags:
        tags.insert(0, subject_code)
    pub = e.find('a:published', ns)
    announced = pub.text[:10] if pub is not None else date.today().isoformat()
    return {'id': arxiv_id, 'arxiv_url': f'https://arxiv.org/abs/{arxiv_id}',
            'announced_date': announced, 'subject': subject_code,
            'subjects_all': tags, 'title_original': title,
            'authors': authors, 'abstract': abstract}


def _extract_id(text: str) -> Optional[str]:
    m = re.search(r'(\d{4}\.\d{4,5})', text or '')
    return m.group(1) if m else None


def _clean(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    return ' '.join(text.split()).strip()
