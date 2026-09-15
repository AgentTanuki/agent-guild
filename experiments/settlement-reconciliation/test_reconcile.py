"""Real retained records plus counterexamples that must stop a clean result."""
import copy
import json
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

    def test_real_anchor_note_change_does_not_disappear_behind_equal_hashes(self):
        _, listing, details = load_snapshot(HERE / 'sample-anchor-change')
        out = reconcile(listing, details)
        self.assertEqual(out['status'], 'CONSISTENT_WITH_REGISTRY')
        route = next(x for x in out['routes'] if x['binding_id'] == 154)
        self.assertEqual(route['registry_receipted_atomic'], '1000000')
        history = route['anchor_history']
        self.assertEqual(history['status'], 'CHANGED')
        self.assertEqual(history['changed_fields'], ['clocks_note'])
        self.assertFalse(history['anchor_payload_hash_changed'])
        self.assertEqual(history['binding_time_anchor_pointer'], '/anchor_at_binding')
        self.assertEqual(history['binding_payload_hash'], details[154]['payload_hash'])

    def test_current_anchor_cannot_replace_binding_time_payload(self):
        _, listing, details = load_snapshot(HERE / 'sample-anchor-change')
        details[154]['anchor_at_binding'] = copy.deepcopy(details[154]['anchor_current'])
        out = reconcile(listing, details)
        self.assertEqual(out['status'], 'CONTRADICTED')
        self.assertIn('retained payout payload', out['reason'])

    def test_missing_historical_anchor_is_indeterminate(self):
        self.details[283]['anchor_at_binding'] = None
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'INDETERMINATE')
        self.assertNotIn('totals', out)

    def test_equal_hashes_do_not_make_a_changed_flag_disagreement_safe(self):
        _, listing, details = load_snapshot(HERE / 'sample-anchor-change')
        details[154]['anchor_changed_since_binding'] = False
        out = reconcile(listing, details)
        self.assertEqual(out['status'], 'INDETERMINATE')
        self.assertIn('flag disagrees', out['reason'])

    def test_changed_listing_between_reads_is_indeterminate(self):
        self.listing['payload_hash'] = '0' * 64
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'INDETERMINATE')
        self.assertIn('between source reads', out['reason'])

    def test_historical_anchor_cannot_switch_roles(self):
        binding = self.details[283]
        binding['anchor_at_binding']['role'] = 'verifier'
        binding['docket_at_binding']['role'] = 'verifier'
        binding['payload']['docket_snapshot'] = json.dumps(binding['anchor_at_binding'])
        out = reconcile(self.listing, self.details)
        self.assertEqual(out['status'], 'CONTRADICTED')
        self.assertIn('historical anchor identity/role', out['reason'])

    def test_unreceipted_anchor_history_remains_explicitly_unknown(self):
        out = reconcile(self.listing, self.details)
        for route in out['routes']:
            if route['receipt_joined']:
                self.assertEqual(route['anchor_history']['status'], 'UNCHANGED')
            else:
                self.assertIsNone(route['anchor_history'])


if __name__ == '__main__':
    unittest.main()
