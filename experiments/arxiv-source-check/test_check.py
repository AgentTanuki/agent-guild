import datetime as dt
import unittest
from check import inspect, unavailable

SOURCE = b'''<meta name="citation_arxiv_id" content="2606.08790">
<meta name="citation_title" content="One &amp; Two">
<meta name="citation_date" content="2026/09/15">
<span class="primary-subject">Artificial Intelligence (cs.AI)</span>
<div class="submission-history"><strong>[v2]</strong> Mon, 14 Sep 2026 00:00:00 UTC
<strong>[v1]</strong> Sun, 7 Jun 2026 19:12:55 UTC</div>'''
ITEM = {'record_id': 'one', 'arxiv_id': '2606.08790', 'expected_title': 'one & TWO'}

class Checks(unittest.TestCase):
    def run_check(self, source=SOURCE, item=ITEM, start='2026-03-01'):
        return inspect(source, item, dt.date.fromisoformat(start), dt.date(2026,9,15))

    def test_matching_metadata_and_first_version(self):
        r = self.run_check()
        self.assertTrue(r['title_matches_normalised'])
        self.assertEqual(r['first_submitted_at'], '2026-06-07T19:12:55+00:00')
        self.assertTrue(r['first_submission_in_window'])

    def test_revision_cannot_make_old_work_eligible(self):
        self.assertFalse(self.run_check(start='2026-07-01')['first_submission_in_window'])

    def test_wrong_paper_never_passes_other_checks(self):
        r = self.run_check(item={**ITEM,'arxiv_id':'2606.00001'})
        self.assertFalse(r['identifier_matches'])
        self.assertIsNone(r['title_matches_normalised'])
        self.assertIsNone(r['first_submission_in_window'])

    def test_missing_first_version_is_unknown(self):
        r = self.run_check(SOURCE.replace(b'[v1]', b'[v3]'))
        self.assertIsNone(r['first_submission_in_window'])
        self.assertTrue(r['source_fields_incomplete'])

    def test_missing_or_conflicting_identity_is_unknown(self):
        r = self.run_check(SOURCE + b'<meta name="citation_arxiv_id" content="2606.00001">')
        self.assertIsNone(r['identifier_matches'])
        self.assertTrue(r['source_fields_incomplete'])

    def test_title_difference_is_not_hidden(self):
        self.assertFalse(self.run_check(item={**ITEM,'expected_title':'Another paper'})['title_matches_normalised'])

    def test_absent_claim_is_not_a_match(self):
        self.assertIsNone(self.run_check(item={**ITEM,'expected_title':None})['title_matches_normalised'])

    def test_no_semantic_verdict(self):
        r = self.run_check()
        self.assertIn('semantic topic fit',r['not_checked'])
        self.assertIn('bounty acceptance',r['not_checked'])

    def test_access_failure_is_not_a_mismatch_or_pass(self):
        r = unavailable(ITEM, {'url':'https://arxiv.org/abs/2606.08790',
            'observed_at':'2026-09-15T00:00:00+00:00', 'http_status':406, 'fetch_error':'HTTPError'})
        self.assertEqual(r['http_status'],406)
        self.assertIsNone(r['identifier_matches'])
        self.assertIsNone(r['first_submission_in_window'])
        self.assertTrue(r['source_fields_incomplete'])

if __name__ == '__main__':
    unittest.main()
