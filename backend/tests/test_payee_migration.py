import copy
import importlib.util
import json
from pathlib import Path
import unittest

from backend.database import empty_state

spec = importlib.util.spec_from_file_location('payee_migration', Path(__file__).resolve().parents[2] / 'scripts/migrate-payee-roles.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class PayeeMigrationTest(unittest.TestCase):
    def state(self):
        state = empty_state()
        for key, name, category, role in (
                ('staff', 'RAJU EEDGI', 'STAFF', 'SUPERVISOR'),
                ('gd', 'GD Saw Mill', 'Wood/Runner supplier', 'Vendor - GD saw mill'),
                ('sri', 'Sri Karthikeya', 'Shuttering Sub Contractor', 'Centring'),
                ('sub', 'Raziuddin', 'Steel Sub Contractor', 'Steel')):
            state['vendors'][key] = {'id': key, 'name': name, 'category': category, 'role': role}
            state['transactions'][key] = {'id': key, 'vendor': key, 'category': category,
                                          'type': 'EXPENSE', 'amount_paise': 12345,
                                          'date': '2026-10-02', 'version': 1, 'history': '[]',
                                          'reversed_at': None, 'request_hash': 'unchanged'}
        return state

    def test_migration_preserves_financials_and_unknown_salary_months(self):
        before = self.state()
        original = copy.deepcopy(before)
        after, ids = migration.migrate_state(before, 'now', corrections=True)
        self.assertEqual(before, original)
        self.assertEqual(set(ids), {'staff', 'gd', 'sri'})
        self.assertEqual(after['vendors']['staff']['role'], 'SUPERVISOR')
        self.assertEqual(after['vendors']['staff']['roles'], ['STAFF'])
        self.assertEqual(after['vendors']['sub']['roles'], ['SUBCONTRACTOR'])
        self.assertEqual(after['vendors']['sri']['roles'], ['SUPPLIER'])
        self.assertEqual(after['transactions']['gd']['category'], 'Wood Supplier')
        self.assertEqual(after['transactions']['staff']['category'], 'Staff Salaries')
        self.assertNotIn('salary_month', after['transactions']['staff'])
        self.assertEqual(json.loads(after['transactions']['staff']['history'])[0]['before']['category'], 'STAFF')
        for key in before['transactions']:
            self.assertEqual(after['transactions'][key]['amount_paise'], 12345)
        again, ids = migration.migrate_state(after, 'later', corrections=True)
        self.assertEqual(again, after)
        self.assertEqual(ids, [])

    def test_only_confirmed_month_is_added(self):
        after, _ = migration.migrate_state(self.state(), 'now', salary_months={'staff': '2026-09'})
        self.assertEqual(after['transactions']['staff']['salary_month'], '2026-09')
        for months in ({'gd': '2026-10'}, {'staff': '2026-13'}, {'missing': '2026-10'}):
            with self.assertRaises(ValueError):
                migration.migrate_state(self.state(), 'now', salary_months=months)

    def test_reversed_payment_is_preserved(self):
        before = self.state()
        before['transactions']['staff']['reversed_at'] = 'earlier'
        after, _ = migration.migrate_state(before, 'now')
        self.assertEqual(after['transactions']['staff'], before['transactions']['staff'])

    def test_wood_payments_with_older_expense_category_are_corrected(self):
        before = self.state()
        before['transactions']['gd']['category'] = 'Other Expense'
        after, _ = migration.migrate_state(before, 'now', corrections=True)
        self.assertEqual(after['transactions']['gd']['category'], 'Wood Supplier')
