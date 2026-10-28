import tempfile
import unittest
from pathlib import Path

import collector
import desk
from schemas import brief_schema, validate


DATE = '2026-10-27'
OWNER = 'Research lead'
SOURCE = {
    'id': 1, 'title': 'Synthetic AI study', 'url': 'https://example.org/study',
    'publisher': 'Example', 'lane': 'research', 'published': '2026-10-26',
    'text': 'The study recruited 120 adults. Applications close November 10, 2026.',
    'text_truncated': False, 'text_scope': 'feed_text_not_verified_full_article',
}


def analysis(**changes):
    value = {
        'id': 1, 'headline': 'Review the evidence before changing instruction',
        'in_scope': True, 'tags': ['evidence'], 'primary': 'evidence',
        'source_grade': 'A', 'source_basis': 'primary_research',
        'source_note': 'Primary study excerpt.',
        'score_parts': {'fit': 4, 'usability': 3, 'rigor': 2, 'novelty': 0},
        'novelty_basis': None, 'evidence': ['The study recruited 120 adults.'],
        'implication': 'The excerpt does not establish an effect.',
        'recommendation': None, 'reported_dates': [], 'figures': [],
        'research': None, 'gaps': ['Effect measure unavailable'],
    }
    value.update(changes)
    return value


def empty_brief():
    return {key: [] for key, _, _, _ in desk.SECTIONS}


class PublicContractTests(unittest.TestCase):
    def test_feed_parse_and_source_url(self):
        body = (b'<rss><channel><item><title>Study</title>'
                b'<link>https://example.org/study</link>'
                b'<description>&lt;p&gt;An abstract&lt;/p&gt;</description>'
                b'</item></channel></rss>')
        rows, rejected = collector.parse_feed(body, {
            'id': 'example', 'publisher': 'Example', 'lane': 'research',
            'url': 'https://example.org/feed',
        })
        self.assertEqual((len(rows), rejected), (1, 0))
        self.assertEqual(rows[0]['excerpt'], 'An abstract')

    def test_duplicate_link_keeps_one_article(self):
        with tempfile.TemporaryDirectory() as folder:
            db = collector.connect(Path(folder) / 'articles.sqlite3')
            row = {
                'title': 'Study', 'url': 'https://example.org/study',
                'publisher': 'Example', 'lane': 'research', 'excerpt': 'Abstract',
                'published': '2026-10-26', 'guid': '', 'source_id': 'example',
                'feed_url': 'https://example.org/feed',
            }
            self.assertTrue(collector.store_article(db, row))
            self.assertFalse(collector.store_article(db, {
                **row, 'url': row['url'] + '?utm_source=rss',
                'source_id': 'example-topic',
            }))
            self.assertEqual(db.execute('SELECT count(*) FROM articles').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM sightings').fetchone()[0], 2)
            db.close()

    def test_evidence_must_appear_in_source(self):
        with self.assertRaises(ValueError):
            desk.assess(analysis(evidence=['The study proved a benefit.']),
                        SOURCE, DATE, '', '2026-11-03', OWNER)

    def test_reported_date_must_be_supported(self):
        event = {
            'date': '2026-12-10', 'event': 'Applications close',
            'quote': 'Applications close November 10, 2026.',
        }
        with self.assertRaises(ValueError):
            desk.assess(analysis(reported_dates=[event]),
                        SOURCE, DATE, '', '2026-11-03', OWNER)

    def test_promotional_source_grade_is_capped(self):
        row = desk.assess(analysis(source_basis='promotion'),
                          SOURCE, DATE, '', '2026-11-03', OWNER)
        self.assertEqual(row['source_grade'], 'D')
        self.assertLessEqual(row['relevance'], 4)

    def test_schema_rejects_unknown_brief_item(self):
        brief = empty_brief()
        brief['research'] = [{
            'headline': 'Study', 'summary': 'A reported finding.',
            'why_it_matters': 'Read the evidence.', 'item_ids': [999],
        }]
        with self.assertRaises(ValueError):
            validate({'brief': brief}, brief_schema([1]))

    def test_five_story_limit_keeps_complete_entries(self):
        rows = []
        brief = empty_brief()
        for number in range(1, 7):
            source = {**SOURCE, 'id': number,
                      'url': f'https://example.org/study/{number}'}
            rows.append(desk.assess(analysis(id=number), source,
                                    DATE, '', '2026-11-03', OWNER))
            brief['research'].append({
                'headline': f'Study {number}', 'summary': 'A reported finding.',
                'why_it_matters': 'Read the evidence.', 'item_ids': [number],
            })
        desk.assign_tiers(rows, DATE)
        edits = []
        final = desk.finalize_brief(brief, rows, '2026-11-03', OWNER, edits)
        self.assertEqual(len(final['research']), 5)
        self.assertEqual(final['research'][0], brief['research'][0])
        self.assertEqual(len(edits), 1)

    def test_dropped_item_cannot_enter_brief(self):
        row = desk.assess(analysis(), SOURCE, DATE, '', '2026-11-03', OWNER)
        row['tier'] = 'drop'
        brief = empty_brief()
        brief['research'] = [{
            'headline': 'Study', 'summary': 'A reported finding.',
            'why_it_matters': 'Read the evidence.', 'item_ids': [1],
        }]
        with self.assertRaises(ValueError):
            desk.finalize_brief(brief, [row], '2026-11-03', OWNER, [])

    def test_large_feed_shortlist_respects_limit_and_diversity(self):
        rows = [
            {**SOURCE, 'id': index, 'title': 'AI verification study',
             'publisher': 'High-volume'} for index in range(2400)
        ]
        rows.append({**SOURCE, 'id': 2400, 'title': 'AI verification study',
                     'publisher': 'Small outlet'})
        selected = desk.select_articles(rows, 120)
        self.assertEqual(len(selected), 120)
        self.assertIn(2400, [row['id'] for row in selected])


if __name__ == '__main__':
    unittest.main()
