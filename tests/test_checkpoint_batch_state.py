import json
import sqlite3
import unittest

from local_review.service import ApiError, _validate_checkpoint


class BatchCheckpointStateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE run_checkpoints (run_id,product_version,device_id,execution_mode,image_version,platform_order_json,current_index,status,pending_review_id,version)')
        self.db.execute('CREATE TABLE review_tasks (id,run_id,product_version,device_id,status,platform_id)')
        self.db.execute('INSERT INTO run_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('run','product','device','save_only','images',json.dumps(['xhs','jd']),0,'resume_pending','first',5))
        self.db.execute('INSERT INTO review_tasks VALUES (?,?,?,?,?,?)', ('second','run','product','device','resume_ready','xhs'))
        self.payload = dict(run_id='run', product_version='product', device_id='device',
                            execution_mode='save_only', image_version='images',
                            platform_order=['xhs','jd'], current_index=0,
                            status='waiting_review', pending_review_id='second', version=6)

    def tearDown(self):
        self.db.close()

    def test_next_review_in_same_platform_is_allowed(self):
        _validate_checkpoint(self.db, self.payload)

    def test_batch_transition_cannot_jump_to_another_platform(self):
        self.db.execute("UPDATE review_tasks SET platform_id='jd'")
        with self.assertRaises(ApiError):
            _validate_checkpoint(self.db, {**self.payload, 'current_index': 1})

    def test_batch_transition_cannot_reopen_the_same_review(self):
        self.db.execute("UPDATE review_tasks SET id='first'")
        with self.assertRaises(ApiError):
            _validate_checkpoint(self.db, {**self.payload, 'pending_review_id': 'first'})
