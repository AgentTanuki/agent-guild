"""Offline, bounded reconciliation of a retained 1F916 Settlement V2 snapshot.

This checks consistency of registry records, not signatures or the Base chain.
No network access, wallet, credentials, dependencies, or payment side effects.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re


class Incomplete(ValueError):
    pass


class Contradiction(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Contradiction(message)


def amount(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'0|[1-9][0-9]*', value)),
            'amount is not a canonical nonnegative atomic-unit string')
    return int(value)


def address(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'0x[0-9a-fA-F]{40}', value)),
            'invalid EVM address')
    return value.lower()


def tx_hash(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'0x[0-9a-fA-F]{64}', value)),
            'transaction hash must contain exactly 64 hex digits')
    return value.lower()


def reconcile(listing, details):
    try:
        return _reconcile(listing, details)
    except Contradiction as exc:
        return {'status': 'CONTRADICTED', 'reason': str(exc)}
    except (Incomplete, KeyError, TypeError) as exc:
        return {'status': 'INDETERMINATE', 'reason': str(exc)}


def _reconcile(listing, details):
    if listing['settlement_version'] != 2:
        raise Incomplete('This prototype covers Settlement V2 only')
    if listing['bindings_has_more'] or listing['submissions_has_more']:
        raise Incomplete('Listing response is paginated; full input required')
    require(len(listing['bindings']) == listing['bindings_total'], 'binding coverage mismatch')
    docket = listing['id']
    require(bool(re.fullmatch(r'listing-[1-9][0-9]*', docket)), 'invalid listing identifier')
    asset = (listing['chain_id'], address(listing['token']))
    routes, used_transfers, used_bindings, receipt_routes = [], set(), set(), {}
    totals = {role: {'authorization_ceiling_sum_atomic': 0, 'registry_receipted_atomic': 0}
              for role in ('worker', 'verifier')}
    for row in sorted(listing['bindings'], key=lambda x: x['id']):
        bid = row['id']
        require(type(bid) is int and bid > 0 and bid not in used_bindings, 'duplicate/invalid binding id')
        used_bindings.add(bid)
        role = row['role']
        require(role in totals, 'unknown binding role')
        expected_row = docket + ('-verifier' if role == 'verifier' else '')
        require(row['row'] == expected_row, 'role does not match payout docket')
        require((row['chain_id'], address(row['token'])) == asset, 'binding asset differs from listing')
        value = amount(row['amount_atomic'])
        totals[role]['authorization_ceiling_sum_atomic'] += value
        route = {'binding_id': bid, 'role': role, 'payee': row['handle'],
                 'authorized_amount_atomic': str(value), 'receipt_id': row['receipt_id'],
                 'receipt_joined': False, 'registry_receipted_atomic': '0',
                 'tx_hash': None, 'transfer_log_index': None,
                 'observed_transfers': row['observed_payments']}
        if row['receipt_id'] is not None:
            if bid not in details:
                raise Incomplete(f'Missing detailed receipt for binding {bid}')
            binding = details[bid]
            require(binding['id'] == bid and binding['row'] == expected_row,
                    'detailed binding identity/docket mismatch')
            require(binding['anchor_role'] == role, 'detailed binding role mismatch')
            require(binding['amount_atomic'] == row['amount_atomic'], 'detailed authorization amount mismatch')
            require(address(binding['address']) == address(row['payout_address']), 'payout address mismatch')
            require((binding['chain_id'], address(binding['token'])) == asset, 'detailed binding asset mismatch')
            receipt = binding['receipt']
            if not receipt:
                raise Incomplete(f'Receipt disappeared from binding {bid} between reads')
            payload = receipt['payload']
            rid = receipt['id']
            require(type(rid) is int and rid > 0 and rid == row['receipt_id'], 'receipt id mismatch')
            require(rid not in receipt_routes, 'receipt reused across payout routes')
            require(payload['binding_payload_hash'] == binding['payload_hash'], 'receipt binds another authorization')
            require(payload['docket_id'] == expected_row, 'receipt pays another role/docket')
            require((payload['chain_id'], address(payload['token'])) == asset, 'receipt asset mismatch')
            require(address(payload['address']) == address(binding['address']), 'receipt recipient mismatch')
            require(amount(payload['amount_atomic']) == value, 'receipt amount differs from binding')
            transfer = (asset[0], tx_hash(receipt['tx_hash']), receipt['transfer_log_index'])
            require(type(transfer[2]) is int and transfer[2] >= 0, 'invalid transfer log index')
            require(transfer not in used_transfers, 'transfer reused across payout routes')
            require(tx_hash(row['tx_hash']) == transfer[1], 'listing transaction differs from receipt')
            require(tx_hash(payload['tx_hash']) == transfer[1] and payload['transfer_log_index'] == transfer[2],
                    'receipt payload transfer mismatch')
            require(address(receipt['source_address']) == address(row['receipt_source']), 'receipt source mismatch')
            require(address(payload['source_address']) == address(receipt['source_address']), 'payload source mismatch')
            require(receipt['finalized_block_number'] >= receipt['block_number'], 'registry does not report finalized inclusion')
            used_transfers.add(transfer)
            route.update(receipt_joined=True, registry_receipted_atomic=str(value),
                         tx_hash=transfer[1], transfer_log_index=transfer[2])
            totals[role]['registry_receipted_atomic'] += value
            receipt_routes[rid] = (row, route)
        routes.append(route)
    award_amount = paid_awards = 0
    seen_awards = set()
    for award in listing['awards']:
        require(award['award_id'] not in seen_awards, 'duplicate award id')
        seen_awards.add(award['award_id'])
        value = amount(award['amount_atomic'])
        award_amount += value
        if award['state'] == 'paid':
            joined = receipt_routes.get(award['receipt_id'])
            if not joined:
                raise Incomplete('Paid worker award lacks a joined receipt')
            row, route = joined
            require(row['role'] == 'worker', 'verifier payment cannot satisfy worker award')
            require(address(row['payout_address']) == address(award['ready_payout_address']), 'award recipient mismatch')
            require(value == amount(route['registry_receipted_atomic']), 'award/receipt amount mismatch')
            require('award_id' not in route, 'receipt reused across worker awards')
            route['award_id'] = award['award_id']
            paid_awards += value
    economics = listing['economics']
    require(paid_awards == amount(economics['amount_paid_atomic']), 'worker paid total disagrees with award joins')
    for role, cells in totals.items():
        totals[role] = {key: str(value) for key, value in cells.items()}
    totals['worker'].update(awarded_atomic=str(award_amount), paid_awards_atomic=str(paid_awards),
                           registry_reported_outstanding_awarded_atomic=str(amount(economics['outstanding_awarded_atomic'])))
    totals['verifier']['outstanding_liability_atomic'] = None
    return {'schema': 'ag-reconciliation-prototype-v0', 'status': 'CONSISTENT_WITH_REGISTRY',
            'listing': docket, 'asset': {'chain_id': asset[0], 'token': asset[1]},
            'routes': routes, 'totals': totals,
            'limits': ['Registry consistency only; no independent chain or signature verification.',
                       'A payout authorization is not an award or debt. Unreceipted bindings create no inferred liability.',
                       'Verifier outstanding liability is unknown: V2 worker economics do not establish verifier obligations.',
                       'Observed transfers are retained separately and never counted as registry receipts.',
                       'Payment and an award do not independently establish work quality or acceptance.',
                       'Sources were read at separate times; this is not an atomic database snapshot.']}


def load_snapshot(folder):
    manifest = json.loads((folder / 'manifest.json').read_text())
    data = {}
    for source in manifest['sources']:
        name = source['file']
        require(bool(re.fullmatch(r'(listing-33|binding-[1-9][0-9]*)\.json', name)), 'invalid source filename')
        raw = (folder / name).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == source['sha256'], 'source bytes differ from manifest')
        data[name] = json.loads(raw)
    return manifest, data['listing-33.json'], {int(k[8:-5]): v for k,v in data.items() if k.startswith('binding-')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        manifest, listing, details = load_snapshot(args.snapshot)
        report = reconcile(listing, details)
        report['source_manifest'] = manifest
    except Contradiction as exc:
        report = {'status': 'CONTRADICTED', 'reason': str(exc)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report = {'status': 'INDETERMINATE', 'reason': str(exc)}
    text = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'report.json').write_text(text)
        fields = ['binding_id', 'role', 'payee', 'authorized_amount_atomic', 'receipt_id',
                  'receipt_joined', 'registry_receipted_atomic', 'tx_hash', 'transfer_log_index', 'award_id']
        with (args.output / 'routes.csv').open('w', newline='') as fp:
            writer = csv.DictWriter(fp, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
            writer.writeheader()
            writer.writerows(report.get('routes', []))
    else:
        print(text, end='')
    raise SystemExit(0 if report['status'] == 'CONSISTENT_WITH_REGISTRY' else 1)


if __name__ == '__main__':
    main()
