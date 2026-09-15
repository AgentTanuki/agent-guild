"""Real retained records plus counterexamples that must stop a clean result."""
import copy
from pathlib import Path
import unittest

from reconcile import load_snapshot, reconcile

HERE = Path(__file__).resolve().parent


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        _, self.listing, self.details = load_snapshot(HERE / 'sample')

    def test_real_worker_and_verifier_payments_remain_separate(self):
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'CONSISTENT_WITH_REGISTRY')
        self.assertEqual(out['totals']['worker']['paid_awards_atomic'], '1000000')
        self.assertEqual(out['totals']['verifier']['registry_receipted_atomic'], '100000')
        self.assertIsNone(out['totals']['verifier']['outstanding_liability_atomic'])
        unpaid = [x for x in out['routes'] if not x['receipt_joined']]
        self.assertEqual(len(unpaid), 4)
        self.assertTrue(all('award_id' not in x for x in unpaid))

    def test_verifier_receipt_cannot_pay_worker_award(self):
        self.listing['awards'][0]['receipt_id'] = 12
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'CONTRADICTED')
        self.assertIn('verifier payment', out['reason'])

    def test_truncated_transaction_hash_is_not_a_receipt(self):
        self.details[283]['receipt']['tx_hash'] = self.details[283]['receipt']['tx_hash'][:-1]
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'CONTRADICTED')
        self.assertIn('64 hex', out['reason'])

    def test_missing_receipt_record_is_unknown_not_paid(self):
        del self.details[283]
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'INDETERMINATE')
        self.assertNotIn('totals', out)

    def test_duplicate_transfer_cannot_create_a_second_payment(self):
        worker = self.details[283]['receipt']
        verifier = self.details[281]['receipt']
        worker['tx_hash'] = worker['payload']['tx_hash'] = verifier['tx_hash']
        worker['transfer_log_index'] = worker['payload']['transfer_log_index'] = verifier['transfer_log_index']
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'CONTRADICTED')
        self.assertIn('transfer reused', out['reason'])

    def test_reported_paid_total_must_match_role_scoped_joins(self):
        self.listing['economics']['amount_paid_atomic'] = '1100000'
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'CONTRADICTED')

    def test_wrong_asset_is_not_money_in_the_listing_asset(self):
        self.details[283]['receipt']['payload']['token'] = '0x' + '1' * 40
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'CONTRADICTED')

    def test_duplicate_worker_award_cannot_reuse_same_receipt(self):
        second = copy.deepcopy(self.listing['awards'][0])
        second['award_id'] = 999
        self.listing['awards'].append(second)
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'CONTRADICTED')
        self.assertIn('reused across worker awards', out['reason'])


if __name__ == '__main__':
    unittest.main()
