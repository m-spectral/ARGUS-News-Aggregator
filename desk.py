import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from schemas import brief_schema, item_schema, validate
from settings import load_env
ROOT = Path(__file__).resolve().parent

def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)

def gemini(system, payload, schema, model, api_key):
    import random
    import time
    from email.utils import parsedate_to_datetime

    if not re.fullmatch('[a-zA-Z0-9._-]+', model):
        raise ValueError('Invalid Gemini model id')
    body = {
        'system_instruction': {'parts': [{'text': system}]},
        'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': 32768,
            'responseFormat': {'text': {'mimeType': 'APPLICATION_JSON', 'schema': schema}},
        },
    }
    request = Request(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'x-goog-api-key': api_key},
        method='POST',
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=180) as response:
                raw = response.read(8000001)
                if len(raw) > 8000000:
                    raise ValueError('Gemini response exceeded 8 MB')
                data = json.loads(raw)
            break
        except HTTPError as error:
            message = f'Gemini HTTP {error.code}'
            try:
                detail = json.loads(error.read(8192)).get('error', {}).get('message', '')
                detail = str(detail).replace(api_key, '[REDACTED]')
                detail = re.sub(r'AIza[0-9A-Za-z_-]{35}', '[REDACTED]', detail)
                if detail:
                    message += ': ' + ' '.join(detail.split())[:300]
            except (ValueError, OSError, AttributeError):
                pass
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(message + '; request stopped') from None
            delay = 5 * (2 ** attempt) + random.uniform(0, 1)
            retry_after = error.headers.get('Retry-After', '') if error.headers else ''
            if retry_after:
                try:
                    required = float(retry_after) if retry_after.isdigit() else parsedate_to_datetime(retry_after).timestamp() - time.time()
                    delay = max(delay, required)
                except (ValueError, TypeError, OverflowError):
                    pass
            if delay > 60:
                raise RuntimeError(message + '; server requested a longer wait, rerun later') from None
            print(f'Gemini HTTP {error.code}; retry {attempt + 1}/2 in {delay:.1f}s', flush=True)
            time.sleep(delay)
        except (URLError, TimeoutError, OSError):
            raise RuntimeError('Gemini response unconfirmed; no automatic retry for ambiguous network failures') from None
    candidates = data.get('candidates', [])
    if not candidates or candidates[0].get('finishReason') != 'STOP':
        raise ValueError('Gemini response blocked or incomplete; reduce batch size')
    parts = candidates[0].get('content', {}).get('parts', [])
    output = json.loads(''.join(part.get('text', '') for part in parts if not part.get('thought')))
    validate(output, schema)
    return {
        'output': output,
        'usage': data.get('usageMetadata', {}),
        'model_version': data.get('modelVersion', model),
        'http_attempts': attempt + 1,
    }

def cached_call(system, payload, schema, model, key, directory, validator=None):
    signature = json.dumps([system, payload, schema, model], ensure_ascii=False, sort_keys=True)
    path = directory / (hashlib.sha256(signature.encode()).hexdigest() + '.json')
    if path.exists():
        result = json.loads(path.read_text(encoding='utf-8'))
        validate(result['output'], schema)
        if validator:
            validator(result['output'])
        return (result, True)
    result = gemini(system, payload, schema, model, key)
    if validator:
        validator(result['output'])
    dump(path, result)
    return (result, False)

def articles(path, current_date, days, characters):
    if not path.exists():
        raise ValueError('Collect articles first; the database does not exist')
    cutoff = (date.fromisoformat(current_date) - timedelta(days=days)).isoformat()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT * FROM articles WHERE substr(COALESCE(published,collected),1,10) BETWEEN ? AND ? ORDER BY id', (cutoff, current_date)).fetchall()
    result = []
    for row in rows:
        excerpt = row['excerpt'] or ''
        result.append({
            'id': row['id'],
            'title': row['title'],
            'url': row['url'],
            'publisher': row['publisher'],
            'lane': row['lane'],
            'published': row['published'],
            'text': excerpt[:characters],
            'text_truncated': len(excerpt) > characters,
            'text_scope': 'feed_text_not_verified_full_article',
        })
    return result

def select_articles(sources, limit):
    from collections import defaultdict, deque
    if len(sources) <= limit:
        return list(sources)
    pattern = re.compile(
        r'\b(?:ai|artificial intelligence|machine learning|deep learning|'
        r'generative|llms?|language models?|agentic|agents?|automation|'
        r'algorithmic|neural|chatgpt|openai|anthropic|claude|gemini|copilot|'
        r'deepmind|hugging face|human judgment|human agency|decision support)\b', re.I)
    lanes = defaultdict(lambda: defaultdict(list))
    for source in sources:
        if source['text'].strip() and pattern.search(source['title'] + ' ' + source['text']):
            lanes[source['lane']][source['publisher']].append(source)
    queues = {}
    for lane, publishers in sorted(lanes.items()):
        queues[lane] = deque()
        publisher_queues = [deque(sorted(rows, key=lambda r: (r['published'] or '', r['id']), reverse=True))
                            for _, rows in sorted(publishers.items())]
        while publisher_queues:
            for queue in publisher_queues:
                queues[lane].append(queue.popleft())
            publisher_queues = [queue for queue in publisher_queues if queue]
    selected = []
    while queues and len(selected) < limit:
        for lane in list(queues):
            if len(selected) == limit:
                break
            selected.append(queues[lane].popleft())
            if not queues[lane]:
                del queues[lane]
    return selected

def batches(items, maximum, characters=60000):
    (result, batch, size) = ([], [], 0)
    for item in items:
        length = len(json.dumps(item, ensure_ascii=False))
        if length > characters:
            raise ValueError('One article exceeds the batch input bound')
        if batch and (len(batch) >= maximum or size + length > characters):
            result.append(batch)
            (batch, size) = ([], 0)
        batch.append(item)
        size += length
    if batch:
        result.append(batch)
    return result

def date_in_quote(value, quote):
    day = date.fromisoformat(value)
    forms = {day.isoformat(), day.strftime('%m/%d/%Y'), f'{day.month}/{day.day}/{day.year}'}
    for month in (day.strftime('%B'), day.strftime('%b')):
        forms.update((f'{month} {day.day} {day.year}', f'{day.day} {month} {day.year}'))
    return any((form.casefold() in quote.replace(',', '').casefold() for form in forms))

def assess(raw, source, current_date, prior_coverage, planning_date, owner):
    if not source['text'].strip():
        raise ValueError(f"Item {source['id']}: insufficient content; retrieve source text before assessing relevance")
    item = json.loads(json.dumps(raw))
    parts = item['score_parts']
    evidence_text = ' '.join([source['title'], source['text'], source['published'] or ''])
    quotes = item['evidence'] + [event['quote'] for event in item['reported_dates']] + [figure['quote'] for figure in item['figures']]
    quoted_words = sum(len(quote.split()) for quote in set(quotes))
    if quoted_words > 25:
        item.setdefault('editorial_notes', []).append(
            f'Evidence contains {quoted_words} quoted words; review quotation length.'
        )
    for quote in quotes:
        if not quote.strip() or quote not in evidence_text:
            raise ValueError(f"Item {item['id']}: unsupported evidence excerpt")
    allowed = item_schema()['properties']['items']['items']['properties']['primary']['enum']
    if item['primary'] not in allowed or any(tag not in allowed for tag in item['tags']):
        raise ValueError(f"Item {item['id']}: invalid primary tag")
    if item['primary'] not in item['tags']:
        item['normalization_notes'] = ['Included the declared primary tag in tags.']
    item['tags'] = list(dict.fromkeys([item['primary'], *item['tags']]))
    if not item['evidence'] and (parts['fit'] or item['figures'] or item['reported_dates']):
        raise ValueError(f"Item {item['id']}: factual analysis needs evidence")
    for figure in item['figures']:
        if figure['value'] not in figure['quote']:
            raise ValueError(f"Item {item['id']}: numeric value not present in evidence")
    for event in item['reported_dates']:
        if not date_in_quote(event['date'], event['quote']):
            raise ValueError(f"Item {item['id']}: unsupported reported date")
    if not prior_coverage or not item['novelty_basis']:
        parts['novelty'] = 0
    source_grade = {'commentary': 'C', 'promotion': 'D', 'rumor': 'D', 'unknown': 'D'}.get(item['source_basis'])
    if source_grade:
        item['source_grade'] = source_grade
    grade = item['source_grade']
    research = item['research']
    rigor = 2 if grade == 'A' and research and (research['design_supports_claim'] is True) or (grade == 'B' and item['source_basis'] == 'documented_reporting') else 1 if grade == 'B' else 0
    parts['rigor'] = min(parts['rigor'], rigor)
    if not item['in_scope']:
        parts.update({key: 0 for key in parts})
    recommendation = item['recommendation']
    if recommendation:
        recommendation['owner'] = owner
        if recommendation['date_type'] == 'reported':
            supported = {event['date'] for event in item['reported_dates']}
            if recommendation['date'] not in supported or recommendation['date'] < current_date:
                raise ValueError(f"Item {item['id']}: unsupported recommendation deadline")
        else:
            recommendation['date'] = planning_date
            recommendation['owner'] = owner
    raw_score = sum(parts.values())
    cap = {'A': 10, 'B': 10, 'C': 7, 'D': 4}[item['source_grade']]
    item.update(raw_score=raw_score, source_cap=cap, relevance=min(raw_score, cap), title=source['title'], url=source['url'], publisher=source['publisher'], lane=source['lane'], published=source['published'], text_truncated=source['text_truncated'], text_scope=source['text_scope'])
    return item

def assign_tiers(items, current_date):
    (high, middle) = ('priority', 'watch')
    for item in items:
        score = item['relevance']
        item['tier'] = high if score >= 8 else middle if score >= 5 else 'archive' if score >= 3 else 'drop'
        item['tier_reason'] = 'score'
    candidates = [item for item in items if item['tier'] == high]
    candidates.sort(key=lambda item: (-item['relevance'], item['source_grade'], item['id']))
    for item in candidates[3:]:
        item.update(tier=middle, tier_reason='global_priority_limit')
    return items

MAX_PER_SECTION = 5
MAX_BRIEF_WORDS = 1800
SECTIONS = (
    ('market_moving', '1. Market-Moving Headlines', 'market_impact', 'Market impact'),
    ('business', '2. AI + Business', 'operational_takeaway', 'Operational takeaway'),
    ('missteps', '3. AI + Missteps', 'lesson', 'The lesson'),
    ('research', '4. AI + New Research', 'why_it_matters', 'Why it matters'),
)

def validate_brief(brief, items, planning_date, owner):
    validate({'brief': brief}, brief_schema())
    by_id = {item['id']: item for item in items}
    used = set()
    total_words = 0
    for section, _, detail, _ in SECTIONS:
        if len(brief[section]) > MAX_PER_SECTION:
            raise ValueError('Brief exceeds five stories per section')
        for entry in brief[section]:
            ids = entry['item_ids']
            if not ids or len(ids) != len(set(ids)) or any(i not in by_id for i in ids):
                raise ValueError('Brief cites missing, duplicate, or unknown source items')
            if used.intersection(ids):
                raise ValueError('Brief repeats a source story across sections')
            if any(not by_id[i]['in_scope'] or by_id[i]['tier'] == 'drop' for i in ids):
                raise ValueError('Brief includes an ineligible item')
            used.update(ids)
            for key in ('headline', 'summary', detail):
                if not entry[key].strip():
                    raise ValueError('Brief contains an empty story field')
                total_words += len(entry[key].split())
    if total_words > MAX_BRIEF_WORDS:
        raise ValueError('Brief exceeds the 1800-word editorial bound')
    return brief

def finalize_brief(raw, items, planning_date, owner, edits):
    validate({'brief': raw}, brief_schema())
    brief = json.loads(json.dumps(raw))
    used = set()
    allowed = {item['id'] for item in items if item['in_scope'] and item['tier'] != 'drop'}
    for section, _, detail, _ in SECTIONS:
        selected = []
        for entry in brief[section]:
            entry['item_ids'] = list(dict.fromkeys(entry['item_ids']))
            ids = entry['item_ids']
            if not ids or any(item_id not in allowed for item_id in ids):
                raise ValueError('Brief cites missing or ineligible source items')
            reason = None
            if used.intersection(ids):
                reason = 'Repeated source; kept its earlier section.'
            elif len(selected) >= MAX_PER_SECTION:
                reason = 'Section limited to five stories.'
            elif any(not entry[key].strip() for key in ('headline', 'summary', detail)):
                reason = 'Empty story field.'
            if reason:
                edits.append({'section': section, 'item_ids': ids, 'reason': reason})
                continue
            selected.append(entry)
            used.update(ids)
        brief[section] = selected
    def word_count():
        return sum(len(entry[key].split()) for section, _, detail, _ in SECTIONS
                   for entry in brief[section] for key in ('headline', 'summary', detail))
    while word_count() > MAX_BRIEF_WORDS:
        for section, _, _, _ in reversed(SECTIONS):
            if brief[section]:
                removed = brief[section].pop()
                edits.append({'section': section, 'item_ids': removed['item_ids'], 'reason': 'Omitted whole story to meet the 1800-word limit.'})
                break
    return validate_brief(brief, items, planning_date, owner)

def render(result, slack=False):
    from html import escape
    brief = result['brief']
    by_id = {item['id']: item for item in result['items']}
    def clean(value):
        value = ' '.join(value.split())
        if slack:
            return escape(value, quote=False).replace('*', '').replace('`', '')
        return value
    def bold(value):
        marker = '*' if slack else '**'
        return marker + clean(value) + marker
    def links(ids):
        sources = []
        for item_id in ids:
            item = by_id[item_id]
            url = item['url']
            if not re.fullmatch(r'https?://[^\s<>|]+', url):
                raise ValueError('Invalid source URL')
            label = clean(item['publisher'])
            link = f'<{escape(url, quote=False)}|{label.replace("|", "")}>' if slack else f'[{label}]({url})'
            if link not in sources:
                sources.append(link)
        return ' · '.join(sources)
    title = 'Standard News Desk: ' + result['run']['current_date']
    lines = [bold(title) if slack else '# ' + title, '', 'Your daily AI briefing.', '']
    for section, heading, detail, label in SECTIONS:
        lines.extend(['---', '', bold(heading) if slack else '## ' + heading, ''])
        if not brief[section]:
            lines.extend(['No sufficiently supported item selected this period.', ''])
        for entry in brief[section]:
            lines.extend([
                '- ' + bold(entry['headline'] + ':') + ' ' + clean(entry['summary']),
                ('  • ' if slack else '  - ') + bold(label + ':') + ' ' + clean(entry[detail]),
                '  ' + links(entry['item_ids']), '',
            ])
    if result.get('synthetic'):
        lines = ['SYNTHETIC EXAMPLE: fictional stories for demonstrating the format.', '', *lines]
    return '\n'.join(lines) + '\n'

def render_analysis(result):
    coverage = result['run']['coverage']
    lines = ['# Standard analysis record', '',
        f"Coverage: {coverage['accepted_items']} of {coverage['input_items']} passed automated checks; {coverage['review_items']} require review.",
        'Automated checks do not independently verify factual interpretations.', '']
    for item in sorted(result['items'], key=lambda row: -row['relevance']):
        lines.extend([f"## {item['id']} | {item['tier']} | {item['relevance']}/10 | {item['source_grade']}",
            '', item['headline'], '', item['implication'], '', f"[{item['publisher']}]({item['url']})", '',
            'Score parts: ' + json.dumps(item['score_parts']), ''])
    lines.extend(['## Review queue', '', json.dumps(result.get('review_queue', []), ensure_ascii=False, indent=2)])
    if 'selection' in result['run']:
        selection = result['run']['selection']
        lines.extend(['', f"Selection: {selection['selected_items']} of {selection['eligible_items']} eligible items selected; {selection['deferred_items']} deferred without LLM review."])
    return '\n'.join(lines)

def send_brief(directory):
    from collector import slack_chunks
    path = directory / 'latest.json'
    if not path.exists():
        raise ValueError('Generate and review a brief before sending')
    result = json.loads(path.read_text(encoding='utf-8'))
    signature = hashlib.sha256(json.dumps([result['run']['current_date'], result['brief'], [(i['id'], i['url']) for i in result['items']]], sort_keys=True).encode()).hexdigest()
    ledger = directory / 'sent_briefs.json'
    sent = json.loads(ledger.read_text()) if ledger.exists() else []
    if signature in sent:
        print('This exact brief was already delivered')
        return
    webhook = os.environ.get('SLACK_WEBHOOK_URL')
    if not webhook:
        raise ValueError('Set SLACK_WEBHOOK_URL before sending')
    import time
    for chunk in slack_chunks(render(result, slack=True)):
        request = Request(webhook, data=json.dumps({'text': chunk, 'mrkdwn': True, 'unfurl_links': False, 'unfurl_media': False}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=30) as response:
                if response.status != 200 or response.read().strip() != b'ok':
                    raise ValueError('Slack did not acknowledge the brief')
        except (URLError, TimeoutError, OSError):
            raise RuntimeError('Brief delivery unconfirmed; no automatic retry') from None
        time.sleep(1.1)
    dump(ledger, sent + [signature])
    print('Brief delivered')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, default=ROOT / 'data/news.sqlite3')
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=4)
    parser.add_argument('--characters', type=int, default=3000)
    parser.add_argument('--max-calls', type=int, default=50)
    parser.add_argument('--max-articles', type=int, default=120)
    parser.add_argument('--model', default=None)
    parser.add_argument('--current-date', default=datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat())
    parser.add_argument('--prior-coverage', type=Path)
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--send-brief', action='store_true')
    args = parser.parse_args()
    if min(args.days, args.batch_size, args.characters, args.max_calls, args.max_articles) < 1 or args.batch_size > 20:
        parser.error('Use positive bounds and batch size at most 20')
    load_env(ROOT / '.env')
    if args.send_brief:
        if args.dry_run:
            parser.error('--send-brief and --dry-run cannot be combined')
        send_brief(args.out / 'standard')
        return
    model = args.model or os.environ.get('GEMINI_MODEL') or 'gemini-3.5-flash-lite'
    current = date.fromisoformat(args.current_date)
    planning = (current + timedelta(days=7)).isoformat()
    owner = 'Research lead'
    prior = args.prior_coverage.read_text(encoding='utf-8') if args.prior_coverage else ''
    system = (ROOT / 'instructions.md').read_text(encoding='utf-8')
    eligible = articles(args.db, args.current_date, args.days, args.characters)
    if eligible and args.max_calls < 2:
        raise ValueError('At least two calls are needed: item analysis and synthesis')
    limit = min(args.max_articles, max(0, (args.max_calls - 1) * args.batch_size))
    sources = select_articles(eligible, limit)
    groups = batches(sources, args.batch_size)
    while sources and len(groups) + 1 > args.max_calls:
        sources.pop()
        groups = batches(sources, args.batch_size)
    selected_ids = {item['id'] for item in sources}
    selection = {
        'eligible_items': len(eligible),
        'selected_items': len(sources),
        'deferred_items': len(eligible) - len(sources),
        'deferred_ids': [item['id'] for item in eligible if item['id'] not in selected_ids],
        'method': 'When over budget: keyword shortlist, then round-robin across lanes and publishers; newest first within each publisher. Deferred items are unreviewed, not irrelevant.',
    }
    calls = len(groups) + 1 if groups else 0
    plan = {
        'profile': 'standard',
        'model': model,
        'eligible_articles': len(eligible),
        'articles': len(sources),
        'deferred_articles': selection['deferred_items'],
        'maximum_calls': calls,
        'maximum_http_attempts': calls * 3,
        'truncated_articles': sum((item['text_truncated'] for item in sources)),
        'characters_per_article': args.characters,
    }
    print(json.dumps(plan, indent=2))
    if args.dry_run:
        return
    if not sources:
        dump(args.out / 'standard' / 'selection.json', selection)
        print('No articles selected; no Gemini calls made.')
        return
    if calls > args.max_calls:
        raise ValueError('Call plan exceeds --max-calls; narrow the date window or explicitly raise the bound')
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise ValueError('Set GEMINI_API_KEY in .env or the environment')
    directory = args.out / 'standard'
    dump(directory / 'selection.json', selection)
    common = {
        'current_date': args.current_date,
        'planning_date': planning,
        'proposed_owner': owner,
        'prior_coverage': prior,
    }
    (items, review_queue, usage, cached) = ([], [], [], 0)
    for group in groups:
        payload = {**common, 'task': 'assess_items', 'items': group}
        by_id = {item['id']: item for item in group}

        def check_group(output):
            if sorted((item['id'] for item in output['items'])) != sorted(by_id):
                raise ValueError('Gemini omitted, duplicated, or added item ids')
        try:
            (response, hit) = cached_call(system, payload, item_schema(), model, key, directory / 'cache', check_group)
        except ValueError as error:
            for source in group:
                review_queue.append({'id': source['id'], 'title': source['title'], 'url': source['url'], 'reason': 'Batch rejected: ' + str(error)})
            dump(directory / 'review_queue.json', review_queue)
            print(f'Batch requires review: {error}', flush=True)
            continue
        raw = response['output']['items']
        for item in raw:
            try:
                items.append(assess(item, by_id[item['id']], args.current_date, prior, planning, owner))
            except ValueError as error:
                source = by_id[item['id']]
                review_queue.append({'id': source['id'], 'title': source['title'], 'url': source['url'], 'reason': str(error)})
                print(f'Review required: {error}', flush=True)
        dump(directory / 'review_queue.json', review_queue)
        usage.append({
            'stage': 'items',
            'cached': hit,
            'http_attempts': 0 if hit else response.get('http_attempts', 1),
            'usage': response['usage'],
            'model_version': response['model_version'],
        })
        cached += hit
    coverage = {'input_items': len(sources), 'accepted_items': len(items), 'review_items': len(review_queue)}
    dump(directory / 'coverage.json', coverage)
    print('Analysis coverage: ' + json.dumps(coverage), flush=True)
    if not items:
        raise ValueError('No items passed evidence validation; inspect the review queue. No brief was published.')
    assign_tiers(items, args.current_date)
    synthesis_items = [item for item in items if item['in_scope'] and item['tier'] != 'drop']
    dump(directory / 'validated_items.json', {'items': items, 'coverage': coverage,
        'review_queue': review_queue, 'synthesis_ids': [item['id'] for item in synthesis_items]})
    print(f'Brief candidates: {len(synthesis_items)}; excluded from synthesis: {len(items) - len(synthesis_items)}', flush=True)
    source_settings = json.loads((ROOT / 'sources.json').read_text(encoding='utf-8'))
    payload = {**common, 'task': 'synthesize_brief', 'items': synthesis_items, 'coverage': coverage,
        'preferred_sources': source_settings.get('editorial_sources', {})}
    if len(json.dumps(payload, ensure_ascii=False)) > 500000:
        raise ValueError('Synthesis exceeds the 500,000-character bound; narrow the date window')
    hit = False
    editorial_edits = []
    if synthesis_items:
        schema = brief_schema([item['id'] for item in synthesis_items])
        def finalize_output(output):
            output['brief'] = finalize_brief(output['brief'], synthesis_items, planning, owner, editorial_edits)
        (response, hit) = cached_call(system, payload, schema, model, key, directory / 'cache', finalize_output)
        brief = validate_brief(response['output']['brief'], synthesis_items, planning, owner)
        usage.append({
            'stage': 'brief',
            'cached': hit,
            'http_attempts': 0 if hit else response.get('http_attempts', 1),
            'usage': response['usage'],
            'model_version': response['model_version'],
        })
    else:
        brief = {section: [] for section, _, _, _ in SECTIONS}
        validate_brief(brief, [], planning, owner)
    result = {
        'brief': brief,
        'items': items,
        'profile': 'standard',
        'run': {
            **plan,
            'selection': selection,
            'current_date': args.current_date,
            'planning_date': planning,
            'input_ids': [item['id'] for item in sources],
            'cache_hits': cached + hit,
            'usage': usage,
            'instructions_sha256': hashlib.sha256(system.encode()).hexdigest(),
            'text_scope': 'Feed metadata and bounded feed text; not verified full articles',
        },
    }
    result['editorial_edits'] = editorial_edits
    dump(directory / 'brief_edits.json', editorial_edits)
    if editorial_edits:
        print(f'Brief formatting edits: {len(editorial_edits)}; no extra model calls.', flush=True)
    result['review_queue'] = review_queue
    result['run']['coverage'] = coverage
    dump(directory / 'latest.json', result)
    (directory / 'brief.md').write_text(render(result), encoding='utf-8')
    (directory / 'full_analysis.md').write_text(render_analysis(result), encoding='utf-8')
    print(directory / 'brief.md')
if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        raise SystemExit(f'Stopped: {error}')
