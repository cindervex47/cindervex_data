# Copy of cindervex-workbench scripts/delisted/build_delisted_list.py (lab126). Edit there, test there, copy here.
"""lab126 (D-160 step a, Kai OK 24 Nov): build the weekly list of US stocks delisted since 2016.

Source: the US regulator's delisting filings (Form 25-NSE, filed by the exchange). Free to reuse; at most
10 requests a second with a named contact, so this script stays at 5 a second and sends support@getcindervex.com.

What it keeps (Kai's checks, step a):
  * common stock only (bonds, notes, preferred-only, warrants, rights, units and depositary shares are dropped);
  * mergers and buyouts are kept, with a best-effort reason taken from the filing's rule box;
  * moves to another big exchange are marked "moved", not "delisted" (the same company registered on a new
    exchange near the filing date, is listed on a big exchange today, or has a later delisting filing);
    a drop to the over-the-counter market counts as delisted;
  * the filing has no ticker: tickers come from the regulator's records or the filing's own note when they
    give one; each user's app confirms against Alpaca's stock list (name + dates) and sets the end date from
    Alpaca's last candle (decisions 1-2, 24 Nov). This list never contains Alpaca data.

Runs on GitHub once a week in the public project cindervex47/cindervex_data (free minutes; moved there 24 Nov
after the private website project ran out of minutes); a copy of this script lives there. The first run backfills 2016 onward; it stops itself
before GitHub's time limit, saves its progress, and the next run picks up where it stopped.
Standard library only.
"""
import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CONTACT = 'CinderVex support@getcindervex.com'
SINCE_YEAR = 2016
RATE_PER_SEC = 5.0
WWW = 'https://www.sec.gov'
DATA = 'https://data.sec.gov'
LIST_VERSION = 1
MOVE_WINDOW_DAYS = 30
MONTHS = 'January|February|March|April|May|June|July|August|September|October|November|December'

REASONS = {
    'merger_or_buyout': 'Merger, buyout or shares retired',
    'removed_by_exchange': 'Removed by the exchange (did not meet its listing rules)',
    'company_chose': 'Company chose to leave the exchange',
    'matured_or_redeemed': 'Matured or redeemed',
    'other': 'Other or not stated',
}


class Fetcher:
    """One polite client: a contact on every request, never faster than RATE_PER_SEC, a few retries."""

    def __init__(self, rate=RATE_PER_SEC, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.gap = 1.0 / rate
        self.opener = opener or self._open
        self.sleep, self.clock = sleep, clock
        self.last = None
        self.count = 0

    @staticmethod
    def _open(url):
        req = urllib.request.Request(url, headers={'User-Agent': CONTACT, 'Accept-Encoding': 'gzip'})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            if r.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            return raw

    def get(self, url):
        for attempt in range(4):
            if self.last is not None:
                wait = self.gap - (self.clock() - self.last)
                if wait > 0:
                    self.sleep(wait)
            self.last = self.clock()
            self.count += 1
            try:
                return self.opener(url)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if attempt == 3:
                    raise
                self.sleep(10 * (attempt + 1))          # 403/429/5xx: back off, then try again
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise
                self.sleep(10 * (attempt + 1))
        return None


# ---------------------------------------------------------------- parsing (pure, tested offline)

IDX_LINE = re.compile(r'^(\S+)\s{2,}(.+?)\s{2,}(\d+)\s{2,}(\d{4}-\d{2}-\d{2})\s{2,}(\S+)\s*$')


def parse_form_index(text, forms=('25-NSE',)):
    """Quarterly form.idx -> one row per filing (amendments such as 25-NSE/A are not included)."""
    out, seen = [], set()
    for line in text.splitlines():
        m = IDX_LINE.match(line)
        if not m or m.group(1) not in forms:
            continue
        path = m.group(5)
        acc = path.rsplit('/', 1)[-1].replace('.txt', '')
        if acc in seen:                                  # the same filing can be listed under exchange and issuer
            continue
        seen.add(acc)
        out.append({'form': m.group(1), 'name': m.group(2).strip(), 'cik': m.group(3), 'date': m.group(4), 'path': path, 'accession': acc})
    return out


def _strip_ns(el):
    for e in el.iter():
        if isinstance(e.tag, str) and '}' in e.tag:
            e.tag = e.tag.split('}', 1)[1]
    return el


def parse_submission(text):
    """Full submission .txt -> the filing's fields, or None when it can't be read."""
    m = re.search(r'<XML>(.*?)</XML>', text, re.S | re.I)
    if not m:
        return None
    try:
        root = _strip_ns(ET.fromstring(m.group(1).strip()))
    except ET.ParseError:
        return None

    def find(path):
        el = root.find(path)
        return (el.text or '').strip() if el is not None and el.text else ''

    issuer_cik = find('issuer/cik')
    if not issuer_cik:
        return None
    note = ''
    for doc in re.findall(r'<DOCUMENT>(.*?)</DOCUMENT>', text, re.S | re.I):
        if re.search(r'<TYPE>\s*EX-', doc, re.I):
            body = re.search(r'<TEXT>(.*?)</TEXT>', doc, re.S | re.I)
            note += ' ' + re.sub(r'<[^>]+>', ' ', body.group(1) if body else doc)
    note = re.sub(r'\s+', ' ', note).strip()
    return {
        'issuer_cik': issuer_cik.lstrip('0') or '0',
        'company': find('issuer/entityName'),
        'exchange': find('exchange/entityName'),
        'security': find('descriptionClassSecurity'),
        'rule': find('ruleProvision'),
        'signed': find('signatureData/signatureDate'),
        'suspended_date': note_suspended_date(note),
        'note_tickers': note_tickers(note),
    }


NOT_COMMON = [
    re.compile(r'\bdepositary\b', re.I),
    re.compile(r'\b(warrants?|rights?|units?)\b[^;]*\b(each|consisting|exercisable|entitl\w*|to purchase|to acquire|to receive)\b', re.I),
    re.compile(r'^\W*(\d[\d.,/]*\s*%|\d[\d.,/]*\s*percent|floating|fixed|senior|subordinated|junior|notes?\b|debentures?|bonds?|exchangeable|convertible)', re.I),
]
COMMON = re.compile(r'\bcommon\b|\bordinary shares?\b', re.I)


def is_common_stock(description):
    d = re.sub(r'\([^)]*\)', ' ', description or '')
    if not COMMON.search(d):
        return False
    return not any(p.search(d) for p in NOT_COMMON)


def reason_from_rule(rule):
    r = (rule or '').replace(' ', '').lower()
    if '12d2-2(a)(3)' in r:
        return 'merger_or_buyout'
    if '12d2-2(a)(1)' in r or '12d2-2(a)(2)' in r:
        return 'matured_or_redeemed'
    if '12d2-2(b)' in r:
        return 'removed_by_exchange'
    if '12d2-2(c)' in r:
        return 'company_chose'
    return 'other'


def note_suspended_date(note):
    m = re.search(r'suspen\w*[^.]{0,120}?\b((?:%s)\s+\d{1,2},?\s+\d{4})' % MONTHS, note or '', re.I)
    if not m:
        return None
    try:
        return datetime.strptime(re.sub(r',', '', m.group(1)), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


TICKER_PATTERNS = [
    re.compile(r'\(\s*(?:NASDAQ|Nasdaq|NYSE(?:\s+(?:American|Arca|MKT))?|CBOE|Cboe)\s*:\s*([A-Z][A-Z.\-]{0,5})\s*\)'),
    re.compile(r'(?:ticker\s+)?symbol\s*[:\-]?\s*["“]?([A-Z][A-Z.\-]{0,5})\b["”]?'),
]
TICKER_STOP = {'THE', 'A', 'AN', 'OF', 'AND', 'FOR', 'ON', 'IS', 'WAS'}


def note_tickers(note):
    found = []
    for p in TICKER_PATTERNS:
        for t in p.findall(note or ''):
            t = t.strip('.-')
            if t and t not in TICKER_STOP and t not in found:
                found.append(t)
    return found


def ticker_candidates(tickers):
    """Regulator records often show a bankrupt company's over-the-counter ticker (e.g. SIVBQ). Add the old form too."""
    out = []
    for t in tickers:
        if t and t not in out:
            out.append(t)
        if len(t) == 5 and t.endswith('Q') and t[:-1] not in out:
            out.append(t[:-1])
    return out


def big_exchange(name):
    return bool(name) and name.strip().upper() not in ('OTC', '')


def _days(a, b):
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def moved_reason(filing_date, company, current_big, later_common_filing):
    """Return why this looks like a move to another big exchange, or None (it really left the market)."""
    for d in company.get('eight_a', []):
        if _days(d, filing_date) <= MOVE_WINDOW_DAYS:
            return 'registered on another exchange near the filing date'
    if later_common_filing:
        return 'has a later delisting filing, so it was listed again'
    if current_big:
        return 'listed on a big exchange today'
    return None


def company_from_submissions(doc):
    """data.sec.gov submissions JSON -> the few fields we keep."""
    rec = (doc.get('filings') or {}).get('recent') or {}
    return {
        'tickers': [t for t in doc.get('tickers') or [] if t],
        'exchanges': [e for e in doc.get('exchanges') or [] if e],
        'eight_a': sorted({d for f, d in zip(rec.get('form', []), rec.get('filingDate', [])) if str(f).startswith('8-A12B')}),
        'older_files': [f.get('name') for f in (doc.get('filings') or {}).get('files', []) if f.get('name')],
        'oldest_recent': min(rec.get('filingDate') or ['9999-12-31']),
    }


def company_tickers_exchange(doc):
    """sec.gov/files/company_tickers_exchange.json -> {cik: [(ticker, exchange)]} for companies listed today."""
    fields = doc.get('fields') or []
    try:
        ci, ti, ei = fields.index('cik'), fields.index('ticker'), fields.index('exchange')
    except ValueError:
        return {}
    out = {}
    for row in doc.get('data') or []:
        out.setdefault(str(row[ci]), []).append((row[ti], row[ei] or ''))
    return out


# ---------------------------------------------------------------- the run

def quarters(since=SINCE_YEAR, today=None):
    today = today or date.today()
    for y in range(since, today.year + 1):
        for q in range(1, 5):
            if date(y, 3 * q - 2, 1) <= today:
                yield y, q


def load_state(path):
    p = Path(path)
    if p.exists():
        raw = p.read_bytes()
        return json.loads(gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw)
    return {'version': 1, 'quarters_done': [], 'filings': {}, 'companies': {}}


def save_state(state, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + '.tmp')
    tmp.write_bytes(gzip.compress(json.dumps(state, sort_keys=True, separators=(',', ':')).encode(), mtime=0))
    tmp.replace(path)


def run(state, fetch, today=None, deadline=None, save=None, log=print):
    """Advance the state as far as time allows. Returns True when every quarter since 2016 is read."""
    today = today or date.today()
    recent_cut = (today - timedelta(days=200))
    done = set(state['quarters_done'])
    complete = True
    since_save = 0
    for y, q in quarters(today=today):
        key = '%dQ%d' % (y, q)
        refresh = date(y, 3 * q - 2, 1) >= recent_cut           # the newest quarters are read again for new filings
        if key in done and not refresh:
            continue
        if deadline and time.monotonic() > deadline:
            complete = False
            break
        raw = fetch('%s/Archives/edgar/full-index/%d/QTR%d/form.idx' % (WWW, y, q))
        if raw is None:
            continue
        rows = parse_form_index(raw.decode('latin-1'))
        log('%s: %d delisting filings' % (key, len(rows)))
        finished = True
        for row in rows:
            if row['accession'] in state['filings']:
                continue
            if deadline and time.monotonic() > deadline:
                finished = complete = False
                break
            sub = fetch('%s/Archives/%s' % (WWW, row['path']))
            parsed = parse_submission(sub.decode('latin-1')) if sub else None
            if parsed is None:
                state['filings'][row['accession']] = {'skip': 'unreadable', 'date': row['date']}
            elif not is_common_stock(parsed['security']):
                state['filings'][row['accession']] = {'skip': 'not common stock', 'date': row['date']}
            else:
                parsed['filing_date'] = row['date']
                state['filings'][row['accession']] = parsed
            since_save += 1
            if save and since_save >= 200:
                save(state)
                since_save = 0
        if not finished:
            break
        if key not in done:
            state['quarters_done'].append(key)
            done.add(key)
    if not complete:
        return False
    # companies: one record per company with a common-stock filing
    ciks = sorted({f['issuer_cik'] for f in state['filings'].values() if 'issuer_cik' in f})
    for cik in ciks:
        have = state['companies'].get(cik)
        newest = max(f['filing_date'] for f in state['filings'].values() if f.get('issuer_cik') == cik)
        week_ago = (today - timedelta(days=6)).isoformat()
        stale = have is None or (date.fromisoformat(newest) >= recent_cut and have.get('fetched', '') < week_ago)
        if not stale:
            continue
        if deadline and time.monotonic() > deadline:
            return False
        raw = fetch('%s/submissions/CIK%010d.json' % (DATA, int(cik)))
        if raw is None:
            state['companies'][cik] = {'tickers': [], 'exchanges': [], 'eight_a': [], 'fetched': today.isoformat()}
            continue
        comp = company_from_submissions(json.loads(raw))
        # 8-A registrations older than the "recent" block live in extra files: read only those that matter
        dates = sorted(f['filing_date'] for f in state['filings'].values() if f.get('issuer_cik') == cik)
        if comp['older_files'] and dates[0] < comp['oldest_recent']:
            for name in comp['older_files']:
                extra = fetch('%s/submissions/%s' % (DATA, name))
                if extra:
                    ex = json.loads(extra)
                    comp['eight_a'] = sorted(set(comp['eight_a']) | {d for f, d in zip(ex.get('form', []), ex.get('filingDate', [])) if str(f).startswith('8-A12B')})
        comp.pop('older_files', None)
        comp.pop('oldest_recent', None)
        comp['fetched'] = today.isoformat()
        state['companies'][cik] = comp
        since_save += 1
        if save and since_save >= 200:
            save(state)
            since_save = 0
    return True


def build_list(state, listed_today, today=None, complete=True):
    """State -> the public list file (regulator information only)."""
    today = today or date.today()
    by_cik = {}
    for acc, f in state['filings'].items():
        if 'issuer_cik' in f:
            by_cik.setdefault(f['issuer_cik'], []).append((f['filing_date'], acc, f))
    stocks = []
    for cik, items in by_cik.items():
        items.sort()
        comp = state['companies'].get(cik, {})
        today_rows = listed_today.get(cik, [])
        current_big = any(big_exchange(ex) for _t, ex in today_rows)
        for i, (fdate, acc, f) in enumerate(items):
            later = i + 1 < len(items)
            why = moved_reason(fdate, comp, current_big, later)
            tickers, source = [], None
            if f.get('note_tickers'):
                tickers, source = list(f['note_tickers']), 'filing note'
            elif comp.get('tickers'):
                tickers, source = ticker_candidates(comp['tickers']), 'regulator records'
            elif today_rows:
                tickers, source = ticker_candidates([t for t, _e in today_rows]), 'regulator records'
            reason = reason_from_rule(f.get('rule'))
            stocks.append({
                'cik': cik, 'company': f.get('company'), 'security': f.get('security'), 'exchange': f.get('exchange'),
                'filing_date': fdate, 'suspended_date': f.get('suspended_date'), 'accession': acc,
                'tickers': tickers, 'ticker_source': source,
                'reason': reason, 'reason_text': REASONS[reason],
                'status': 'moved' if why else 'delisted', 'moved_why': why,
            })
    stocks.sort(key=lambda s: (s['filing_date'], s['cik']))
    skipped = {}
    for f in state['filings'].values():
        if 'skip' in f:
            skipped[f['skip']] = skipped.get(f['skip'], 0) + 1
    dead = [s for s in stocks if s['status'] == 'delisted']
    return {
        'version': LIST_VERSION,
        'built': today.isoformat(),
        'since': '%d-01-01' % SINCE_YEAR,
        'complete': bool(complete),
        'source': 'US regulator delisting filings (Form 25-NSE), common stock only. Best effort.',
        'end_dates': 'Not in this list: each app sets the end date from the last price on or before the filing date.',
        'counts': {
            'delisted': len(dead), 'moved': len(stocks) - len(dead),
            'with_ticker': sum(1 for s in dead if s['tickers']), 'without_ticker': sum(1 for s in dead if not s['tickers']),
            'filings_read': len(state['filings']), 'skipped': skipped,
        },
        'stocks': stocks,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--state', default='tools/delisted/state.json.gz')
    ap.add_argument('--out', default='data/delisted-stocks.json')
    ap.add_argument('--max-minutes', type=float, default=320, help='stop and save before the host time limit')
    a = ap.parse_args(argv)
    fetcher = Fetcher()
    state = load_state(a.state)
    deadline = time.monotonic() + a.max_minutes * 60
    try:
        complete = run(state, fetcher.get, deadline=deadline, save=lambda s: save_state(s, a.state))
    finally:
        save_state(state, a.state)                     # progress is never lost, even if a request keeps failing
    listed = company_tickers_exchange(json.loads(fetcher.get(WWW + '/files/company_tickers_exchange.json') or b'{}'))
    out = build_list(state, listed, complete=complete)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    c = out['counts']
    print('requests %d; complete %s; delisted %d (with ticker %d, without %d); moved %d; skipped %s' % (
        fetcher.count, complete, c['delisted'], c['with_ticker'], c['without_ticker'], c['moved'], c['skipped']))
    sivb = [s for s in out['stocks'] if s['cik'] == '719739']
    print('SIVB check:', json.dumps(sivb))
    if 'GITHUB_OUTPUT' in __import__('os').environ:
        with open(__import__('os').environ['GITHUB_OUTPUT'], 'a') as fh:
            fh.write('complete=%s\n' % ('true' if complete else 'false'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
