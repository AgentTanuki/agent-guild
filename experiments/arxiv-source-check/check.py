"""Bounded arXiv metadata lookup. No credentials, payments, or semantic verdicts."""
import argparse
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import unicodedata
import urllib.error
import urllib.request

ID = re.compile(r'\d{4}\.\d{4,5}(?:v[1-9]\d*)?\Z')
MONTHS = {m: i for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}

class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.history, self.subject = {}, [], []
        self.history_depth = 0
        self.subject_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and a.get('name', '').startswith('citation_'):
            self.meta.setdefault(a['name'], []).append(a.get('content', ''))
        if tag == 'div':
            if self.history_depth:
                self.history_depth += 1
            elif 'submission-history' in a.get('class', '').split():
                self.history_depth = 1
        if tag == 'span':
            if self.subject_depth:
                self.subject_depth += 1
            elif 'primary-subject' in a.get('class', '').split():
                self.subject_depth = 1

    def handle_endtag(self, tag):
        if tag == 'div' and self.history_depth:
            self.history_depth -= 1
        if tag == 'span' and self.subject_depth:
            self.subject_depth -= 1

    def handle_data(self, text):
        if self.history_depth:
            self.history.append(text)
        if self.subject_depth:
            self.subject.append(text)

def normalise(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())

def inspect(raw, item, start, end):
    """Return explicit unknowns when the required source fields are missing."""
    page = Page()
    page.feed(raw.decode('utf-8'))
    def one(key):
        values = page.meta.get(key, [])
        return values[0] if len(values) == 1 and values[0] else None
    source_id, title = one('citation_arxiv_id'), one('citation_title')
    requested = item['arxiv_id']
    id_match = (re.sub(r'v\d+$', '', source_id) == re.sub(r'v\d+$', '', requested)
                if source_id else None)
    match = re.search(r'\[v1\]\s*\w{3},\s*(\d{1,2}) (\w{3}) (\d{4}) '
                      r'(\d{2}:\d{2}:\d{2}) UTC', ' '.join(page.history))
    published = None
    if match:
        day, month, year, clock = match.groups()
        published = dt.datetime.fromisoformat(
            f'{year}-{MONTHS[month]:02d}-{int(day):02d}T{clock}+00:00')
    # Never let a revision's citation_date substitute for the first submission.
    trusted = id_match is True
    expected = item.get('expected_title')
    return {
        'record_id': item['record_id'], 'arxiv_id': requested,
        'source_url': 'https://arxiv.org/abs/' + requested,
        'source_sha256': hashlib.sha256(raw).hexdigest(),
        'identifier_matches': id_match,
        'source_title': title,
        'title_matches_normalised': (normalise(expected) == normalise(title)
                                     if trusted and expected and title else None),
        'first_submitted_at': published.isoformat() if published and trusted else None,
        'first_submission_in_window': (start <= published.date() <= end
                                       if published and trusted else None),
        'primary_subject': ' '.join(''.join(page.subject).split()) or None,
        'source_fields_incomplete': (not trusted or not title or not published),
        'not_checked': ['semantic topic fit', 'author affiliation', 'contact-route suitability',
                        'quotation accuracy', 'research quality', 'bounty acceptance'],
    }

def unavailable(item, source):
    return {
        'record_id': item['record_id'], 'arxiv_id': item['arxiv_id'],
        'source_url': source['url'], 'source_observed_at': source['observed_at'],
        'http_status': source.get('http_status'), 'fetch_error': source['fetch_error'],
        'identifier_matches': None, 'source_title': None,
        'title_matches_normalised': None, 'first_submitted_at': None,
        'first_submission_in_window': None, 'primary_subject': None,
        'source_fields_incomplete': True, 'not_checked': ['All source claims; source unavailable'],
    }

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('input', type=Path)
    ap.add_argument('--cache', type=Path, required=True)
    ap.add_argument('--offline', action='store_true')
    args = ap.parse_args()
    job = json.loads(args.input.read_text())
    items = job['records']
    assert 1 <= len(items) <= 20
    assert len({x['record_id'] for x in items}) == len(items)
    assert all(ID.fullmatch(x['arxiv_id']) for x in items)
    start, end = [dt.date.fromisoformat(job[x]) for x in ['window_start', 'window_end']]
    assert start <= end
    args.cache.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    rows = []
    fetched = 0
    for item in items:
        cache = args.cache / (item['arxiv_id'] + '.json')
        if not cache.exists():
            if args.offline:
                raise FileNotFoundError(f'Missing retained source: {cache.name}')
            if fetched:
                time.sleep(3)
            url = 'https://arxiv.org/abs/' + item['arxiv_id']
            req = urllib.request.Request(url, headers={
                'User-Agent': 'AgentGuild-public-metadata-prototype/1.0', 'Accept': 'text/html'})
            source = {'url': url}
            try:
                with opener.open(req, timeout=30) as response:
                    raw = response.read(1024*1024+1)
                    assert response.code == 200 and len(raw) <= 1024*1024
                source.update(http_status=200, sha256=hashlib.sha256(raw).hexdigest(), html=raw.decode('utf-8'))
            except (urllib.error.URLError, TimeoutError) as exc:
                source['fetch_error'] = type(exc).__name__
                if isinstance(exc, urllib.error.HTTPError):
                    source['http_status'] = exc.code
                    exc.close()
            source['observed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            cache.write_text(json.dumps(source) + '\n')
            fetched += 1
        source = json.loads(cache.read_text())
        assert source['url'] == 'https://arxiv.org/abs/' + item['arxiv_id']
        if source.get('fetch_error'):
            rows.append(unavailable(item, source))
            continue
        raw = source['html'].encode()
        assert source['sha256'] == hashlib.sha256(raw).hexdigest()
        row = inspect(raw, item, start, end)
        row['source_observed_at'] = source['observed_at']
        rows.append(row)
    print(json.dumps({'schema': 'ag-arxiv-source-check-prototype-v1',
        'scope': 'Source metadata only; no overall validity or acceptance verdict.',
        'window_start': str(start), 'window_end': str(end), 'records': rows}, indent=2))

if __name__ == '__main__':
    main()
