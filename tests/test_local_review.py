import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from local_review.app import create_app
from local_review.config import Settings
from local_review.database import connect, migrate, transaction
from local_review.security import create_user
from local_review.service import analyze_product


class LocalReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self.temporary.name),
            device_token="device-secret",
        )
        migrate(self.settings)
        with transaction(self.settings) as connection:
            create_user(connection, "operator", "1234", role="operator")
        self.client_context = TestClient(
            create_app(self.settings), base_url="http://testserver"
        )
        self.client = self.client_context.__enter__()
        self.device_headers = {"Authorization": "Device device-secret"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def event(self, key, event_type, payload, expected=201):
        response = self.client.post(
            "/api/device/events",
            headers=self.device_headers,
            json={
                "idempotency_key": key,
                "event_type": event_type,
                "payload": payload,
            },
        )
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def product_and_snapshot(self):
        self.event(
            "product-1",
            "product.upsert",
            {
                "product_version": "pv-1",
                "style_code": "NGBL-10588",
                "title": "军绿色休闲直筒裤",
                "category_json": {"family": "pants"},
            },
        )
        self.event(
            "snapshot-1",
            "snapshot.created",
            {
                "snapshot_version": "sv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "field_label": "裤长",
                "canonical_field": "pant_length",
                "schema_version": "schema-1",
                "control_type": "select",
                "custom_allowed": False,
                "options": [
                    {"value_id": "short", "label": "短裤", "position": 0},
                    {"value_id": "long", "label": "长裤", "position": 1},
                ],
            },
        )

    def login(self):
        response = self.client.post(
            "/api/login",
            headers={"Origin": "http://testserver"},
            json={"username": "operator", "password": "1234"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_health_login_and_wrong_short_password_are_stable(self):
        self.assertEqual(self.client.get("/health").json()["storage"], "local")
        wrong = self.client.post(
            "/api/login",
            headers={"Origin": "http://testserver"},
            json={"username": "missing", "password": "0000"},
        )
        self.assertEqual(wrong.status_code, 401)
        self.login()
        self.assertIn("商品属性审核", self.client.get("/").text)

    def test_review_page_contains_candidate_search(self):
        self.login()
        page = self.client.get("/").text
        script = self.client.get("/app.js").text

        self.assertIn('id="option-search"', page)
        self.assertIn('id="option-count"', page)
        self.assertIn('id="custom-value"', page)
        self.assertIn("function filterOptions()", script)
        self.assertIn("$('option-search').oninput=filterOptions", script)
        self.assertIn("$('custom-value').oninput=", script)

    def test_failed_manual_input_review_requires_existing_option(self):
        self.product_and_snapshot()
        self.event('restricted-review', 'review.created', {
            'id': 'restricted-review', 'run_id': 'restricted-run', 'device_id': 'mac-1',
            'product_version': 'pv-1', 'platform_id': 'jd',
            'category_leaf_id': 'straight-pants', 'field_id': 'length',
            'field_label': '裤长', 'canonical_field': 'pant_length',
            'snapshot_version': 'sv-1', 'reason_code': 'manual_input_rejected',
            'evidence_json': {'selection_only': True, 'summary': '无法手填，请选择候选'},
        })
        self.login()
        task = self.client.get('/api/reviews').json()['tasks'][0]
        self.assertFalse(task['custom_allowed'])
        headers = {'Origin': 'http://testserver'}
        claim = self.client.post('/api/reviews/restricted-review/claim',
                                 headers=headers, json={'version': task['version']}).json()
        rejected = self.client.post('/api/reviews/restricted-review/confirm', headers=headers,
                                    json={'version': claim['version'], 'final_value_id': '自定义'})
        self.assertEqual(rejected.status_code, 422)
        # 多选确认：逗号分隔多个候选 valueId 全部唯一命中时接受，
        # 混入不存在候选的值时拒绝（selection_only 字段禁止自定义）。
        multi = self.client.post('/api/reviews/restricted-review/confirm', headers=headers,
                                 json={'version': claim['version'], 'final_value_id': 'short,长裤'})
        self.assertEqual(multi.status_code, 200, multi.text)
        self.event('restricted-review-2', 'review.created', {
            'id': 'restricted-review-2', 'run_id': 'restricted-run', 'device_id': 'mac-1',
            'product_version': 'pv-1', 'platform_id': 'jd',
            'category_leaf_id': 'straight-pants', 'field_id': 'length',
            'field_label': '裤长', 'canonical_field': 'pant_length',
            'snapshot_version': 'sv-1', 'reason_code': 'manual_input_rejected',
            'evidence_json': {'selection_only': True, 'summary': '无法手填，请选择候选'},
        })
        task = self.client.get('/api/reviews').json()['tasks'][0]
        claim = self.client.post('/api/reviews/restricted-review-2/claim',
                                 headers=headers, json={'version': task['version']}).json()
        mixed = self.client.post('/api/reviews/restricted-review-2/confirm', headers=headers,
                                 json={'version': claim['version'], 'final_value_id': 'short,不存在值'})
        self.assertEqual(mixed.status_code, 422)
        duplicate = self.client.post('/api/reviews/restricted-review-2/confirm', headers=headers,
                                     json={'version': claim['version'], 'final_value_id': 'short,short'})
        self.assertEqual(duplicate.status_code, 400)
        accepted = self.client.post('/api/reviews/restricted-review-2/confirm', headers=headers,
                                    json={'version': claim['version'], 'final_value_id': 'long'})
        self.assertEqual(accepted.status_code, 200, accepted.text)

    def test_custom_review_value_is_saved_and_returned_to_device(self):
        self.product_and_snapshot()
        # Legacy snapshots deny custom platform options; operators must still
        # be able to submit a value for the writer to try and verify.
        self.event(
            "review-custom-value",
            "review.created",
            {
                "id": "review-custom-value",
                "run_id": "run-custom-value",
                "device_id": "mac-1",
                "product_version": "pv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "field_label": "裤长",
                "canonical_field": "pant_length",
                "snapshot_version": "sv-1",
                "reason_code": "candidate_missing",
                "evidence_json": {"summary": "Excel 值不在当前候选列表中"},
            },
        )
        self.login()
        task = self.client.get("/api/reviews").json()["tasks"][0]
        self.assertTrue(task["custom_allowed"])
        claimed = self.client.post(
            "/api/reviews/review-custom-value/claim",
            headers={"Origin": "http://testserver"},
            json={"version": task["version"]},
        ).json()
        confirmed = self.client.post(
            "/api/reviews/review-custom-value/confirm",
            headers={"Origin": "http://testserver"},
            json={"version": claimed["version"], "final_value_id": "加长裤"},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

        decision = self.client.post(
            "/api/device/decide",
            headers=self.device_headers,
            json={
                "product_version": "pv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "canonical_field": "pant_length",
                "snapshot_version": "sv-1",
            },
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertEqual(decision.json()["status"], "auto_fill_ready")
        self.assertEqual(decision.json()["source"], "human_override")
        self.assertEqual(decision.json()["value_id"], "加长裤")
        self.assertEqual(decision.json()["value_label"], "加长裤")

    def test_empty_options_are_allowed_only_for_custom_input_reviews(self):
        self.event(
            "product-empty-options",
            "product.upsert",
            {
                "product_version": "pv-empty-options",
                "style_code": "NGBL-20644",
                "title": "外套尺码表",
                "category_json": {"family": "outerwear"},
            },
        )
        closed = {
            "snapshot_version": "sv-empty-closed",
            "platform_id": "tb",
            "category_leaf_id": "jacket",
            "field_id": "size_s_height",
            "field_label": "尺码表 S 身高（cm）",
            "canonical_field": "size_chart_s_height",
            "schema_version": "schema-1",
            "control_type": "input",
            "custom_allowed": False,
            "options": [],
        }
        error = self.event(
            "snapshot-empty-closed",
            "snapshot.created",
            closed,
            expected=400,
        )
        self.assertEqual(error["error"], "invalid_options")

        custom = {
            **closed,
            "snapshot_version": "sv-empty-custom",
            "custom_allowed": True,
        }
        self.event("snapshot-empty-custom", "snapshot.created", custom)
        self.event(
            "review-empty-custom",
            "review.created",
            {
                "id": "review-empty-custom",
                "run_id": "run-empty-custom",
                "device_id": "mac-1",
                "product_version": "pv-empty-options",
                "platform_id": "tb",
                "category_leaf_id": "jacket",
                "field_id": "size_s_height",
                "field_label": "尺码表 S 身高（cm）",
                "canonical_field": "size_chart_s_height",
                "snapshot_version": "sv-empty-custom",
                "reason_code": "insufficient_evidence",
                "evidence_json": {"summary": "尺码表必填值为空"},
            },
        )
        self.login()
        task = self.client.get("/api/reviews").json()["tasks"][0]
        self.assertEqual(task["options"], [])
        self.assertTrue(task["custom_allowed"])
        claimed = self.client.post(
            "/api/reviews/review-empty-custom/claim",
            headers={"Origin": "http://testserver"},
            json={"version": task["version"]},
        ).json()
        confirmed = self.client.post(
            "/api/reviews/review-empty-custom/confirm",
            headers={"Origin": "http://testserver"},
            json={"version": claimed["version"], "final_value_id": "170"},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_failed_run_reviews_are_hidden_from_operator_queue(self):
        self.product_and_snapshot()
        self.event(
            "review-failed-run",
            "review.created",
            {
                "id": "review-failed-run",
                "run_id": "run-failed-review",
                "device_id": "mac-1",
                "product_version": "pv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "field_label": "裤长",
                "canonical_field": "pant_length",
                "snapshot_version": "sv-1",
                "reason_code": "insufficient_evidence",
                "evidence_json": {},
            },
        )
        checkpoint = {
            "run_id": "run-failed-review",
            "product_version": "pv-1",
            "device_id": "mac-1",
            "execution_mode": "save_only",
            "platform_order": ["jd"],
            "current_index": 0,
            "status": "running",
            "pending_review_id": None,
            "version": 1,
            "image_version": "images-1",
        }
        self.event("checkpoint-failed-review-1", "checkpoint.updated", checkpoint)
        self.event(
            "checkpoint-failed-review-2",
            "checkpoint.updated",
            {**checkpoint, "status": "failed", "version": 2},
        )
        self.login()
        self.assertEqual(self.client.get("/api/reviews").json()["tasks"], [])

    def test_tunnel_forwarded_https_origin_gets_secure_session_cookie(self):
        response = self.client.post(
            "/api/login",
            headers={
                "Host": "review.example.com",
                "Origin": "https://review.example.com",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "operator", "password": "1234"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)

    def test_four_digit_pin_is_enforced_and_failed_logins_are_rate_limited(self):
        invalid = self.client.post(
            "/api/login",
            headers={"Origin": "http://testserver"},
            json={"username": "operator", "password": "12ab"},
        )
        self.assertEqual(invalid.status_code, 400)
        for _ in range(5):
            failed = self.client.post(
                "/api/login",
                headers={"Origin": "http://testserver"},
                json={"username": "operator", "password": "0000"},
            )
            self.assertEqual(failed.status_code, 401)
        locked = self.client.post(
            "/api/login",
            headers={"Origin": "http://testserver"},
            json={"username": "operator", "password": "1234"},
        )
        self.assertEqual(locked.status_code, 429)

    def test_device_event_is_idempotent_and_text_fact_autofills(self):
        self.product_and_snapshot()
        duplicate = self.event(
            "product-1",
            "product.upsert",
            {
                "product_version": "pv-1",
                "style_code": "NGBL-10588",
                "title": "军绿色休闲直筒裤",
                "category_json": {"family": "pants"},
            },
            expected=409,
        )
        self.assertTrue(duplicate["event_id"])
        self.event(
            "facts-1",
            "text_facts.created",
            {
                "product_version": "pv-1",
                "source": "excel",
                "payload_json": {
                    "values": {"pant_length": {"value_id": "long"}},
                    "text_tokens": ["休闲裤", "长裤"],
                },
            },
        )
        response = self.client.post(
            "/api/device/decide",
            headers=self.device_headers,
            json={
                "product_version": "pv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "canonical_field": "pant_length",
                "snapshot_version": "sv-1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["value_id"], "long")
        self.assertEqual(response.json()["source"], "explicit_text")

    def test_legacy_checkpoint_id_is_normalized_before_idempotency_check(self):
        self.event(
            "product-legacy-checkpoint",
            "product.upsert",
            {
                "product_version": "pv-legacy",
                "style_code": "NGBL-LEGACY",
                "title": "旧版检查点兼容测试",
                "category_json": {},
            },
        )
        checkpoint = {
            "run_id": "run-legacy",
            "checkpoint_id": "run-legacy",
            "product_version": "pv-legacy",
            "device_id": "mac-legacy",
            "execution_mode": "save_only",
            "platform_order": ["douyin", "taobao"],
            "current_index": 0,
            "status": "running",
            "pending_review_id": None,
            "version": 1,
            "image_version": "images-legacy",
        }
        self.event("checkpoint-legacy", "checkpoint.updated", checkpoint)
        with connect(self.settings) as connection:
            stored = json.loads(
                connection.execute(
                    "SELECT payload_json FROM device_events WHERE idempotency_key=?",
                    ("checkpoint-legacy",),
                ).fetchone()["payload_json"]
            )
        self.assertNotIn("checkpoint_id", stored)
        normalized = dict(checkpoint)
        normalized.pop("checkpoint_id")
        duplicate = self.event(
            "checkpoint-legacy",
            "checkpoint.updated",
            normalized,
            expected=409,
        )
        self.assertTrue(duplicate["event_id"])

        mismatch = dict(checkpoint)
        mismatch["run_id"] = "different-run"
        response = self.client.post(
            "/api/device/events",
            headers=self.device_headers,
            json={
                "idempotency_key": "checkpoint-mismatch",
                "event_type": "checkpoint.updated",
                "payload": mismatch,
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"], "checkpoint_id_mismatch")

    def test_checkpoint_accepts_platform_registry_alias_for_review(self):
        self.event(
            "product-platform-alias",
            "product.upsert",
            {
                "product_version": "pv-alias",
                "style_code": "NGBL-ALIAS",
                "title": "平台别名检查",
                "category_json": {},
            },
        )
        self.event(
            "snapshot-platform-alias",
            "snapshot.created",
            {
                "snapshot_version": "sv-alias",
                "platform_id": "tb",
                "category_leaf_id": "casual-pants",
                "field_id": "style",
                "field_label": "风格",
                "canonical_field": None,
                "schema_version": "schema-alias",
                "control_type": "select",
                "custom_allowed": False,
                "options": [
                    {"value_id": "casual", "label": "休闲", "position": 0},
                    {"value_id": "urban", "label": "时尚都市", "position": 1},
                ],
            },
        )
        self.event(
            "checkpoint-platform-alias-running",
            "checkpoint.updated",
            {
                "run_id": "run-alias",
                "product_version": "pv-alias",
                "device_id": "mac-alias",
                "execution_mode": "save_only",
                "platform_order": ["base", "taobao", "tmall"],
                "current_index": 1,
                "status": "running",
                "pending_review_id": None,
                "version": 1,
                "image_version": "images-alias",
            },
        )
        self.event(
            "review-platform-alias",
            "review.created",
            {
                "id": "review-platform-alias",
                "run_id": "run-alias",
                "device_id": "mac-alias",
                "product_version": "pv-alias",
                "platform_id": "tb",
                "category_leaf_id": "casual-pants",
                "field_id": "style",
                "field_label": "风格",
                "canonical_field": None,
                "snapshot_version": "sv-alias",
                "suggested_value_id": "casual",
                "reason_code": "field_mapping_required",
                "evidence_json": {"excel": True},
            },
        )

        self.event(
            "checkpoint-platform-alias-waiting",
            "checkpoint.updated",
            {
                "run_id": "run-alias",
                "product_version": "pv-alias",
                "device_id": "mac-alias",
                "execution_mode": "save_only",
                "platform_order": ["base", "taobao", "tmall"],
                "current_index": 1,
                "status": "waiting_review",
                "pending_review_id": "review-platform-alias",
                "version": 2,
                "image_version": "images-alias",
            },
        )

    def test_asset_review_resume_and_verified_rule_outcome(self):
        self.product_and_snapshot()
        image = b"\x89PNG\r\n\x1a\nlocal-test-image"
        digest = hashlib.sha256(image).hexdigest()
        uploaded = self.client.put(
            f"/api/device/assets/{digest}",
            headers={
                **self.device_headers,
                "X-Product-Version": "pv-1",
                "X-Asset-Kind": "learning_thumbnail",
                "Content-Type": "image/png",
            },
            content=image,
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        review = self.event(
            "review-1",
            "review.created",
            {
                "id": "review-1",
                "run_id": "run-1",
                "device_id": "mac-1",
                "product_version": "pv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "field_label": "裤长",
                "canonical_field": "pant_length",
                "snapshot_version": "sv-1",
                "suggested_value_id": "long",
                "reason_code": "insufficient_evidence",
                "evidence_json": {"summary": "图片显示裤脚到脚踝"},
            },
        )
        self.assertTrue(review["event_id"])
        self.event(
            "checkpoint-1",
            "checkpoint.updated",
            {
                "run_id": "run-1",
                "product_version": "pv-1",
                "device_id": "mac-1",
                "execution_mode": "save_only",
                "platform_order": ["jd"],
                "current_index": 0,
                "status": "waiting_review",
                "pending_review_id": "review-1",
                "version": 1,
                "image_version": "images-1",
            },
        )
        self.login()
        task = self.client.get("/api/reviews").json()["tasks"][0]
        self.assertEqual(task["field_label"], "裤长")
        self.assertEqual(task["asset_ids"], [uploaded.json()["asset_id"]])
        claimed = self.client.post(
            "/api/reviews/review-1/claim",
            headers={"Origin": "http://testserver"},
            json={"version": task["version"]},
        ).json()
        confirmed = self.client.post(
            "/api/reviews/review-1/confirm",
            headers={"Origin": "http://testserver"},
            json={"version": claimed["version"], "final_value_id": "long"},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.event(
            "checkpoint-2",
            "checkpoint.updated",
            {
                "run_id": "run-1",
                "product_version": "pv-1",
                "device_id": "mac-1",
                "execution_mode": "save_only",
                "platform_order": ["jd"],
                "current_index": 0,
                "status": "resume_pending",
                "pending_review_id": "review-1",
                "version": 2,
                "image_version": "images-1",
            },
        )
        resume = self.client.get(
            "/api/device/resume?device_id=mac-1",
            headers=self.device_headers,
        ).json()["events"][0]
        acknowledged = self.client.post(
            f"/api/device/resume/{resume['event_id']}/ack",
            headers=self.device_headers,
            json={
                "checkpoint_persisted": True,
                "checkpoint_id": "run-1",
                "device_id": "mac-1",
            },
        )
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
        self.event(
            "readback-1",
            "readback.recorded",
            {
                "run_id": "run-1",
                "product_version": "pv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "snapshot_version": "sv-1",
                "actual_value_id": "long",
                "actual_label": "长裤",
                "verified": True,
                "payload_json": {"source": "save_readback"},
            },
        )
        with connect(self.settings) as connection:
            rule = connection.execute("SELECT * FROM conditional_rules").fetchone()
            self.assertIsNotNone(rule)
            self.assertEqual(rule["consecutive_confirmations"], 1)
            self.assertEqual(rule["status"], "observing")
            self.assertEqual(
                connection.execute("SELECT count(*) FROM rule_outcomes").fetchone()[0],
                1,
            )

    def test_multi_choice_readback_requires_every_unique_snapshot_candidate(self):
        self.product_and_snapshot()
        checkpoint = {
            "run_id": "run-multi-readback",
            "product_version": "pv-1",
            "device_id": "mac-1",
            "execution_mode": "save_only",
            "platform_order": ["jd"],
            "current_index": 0,
            "status": "running",
            "pending_review_id": None,
            "version": 1,
            "image_version": "images-1",
        }
        self.event(
            "checkpoint-multi-readback", "checkpoint.updated", checkpoint
        )
        payload = {
            "run_id": "run-multi-readback",
            "product_version": "pv-1",
            "platform_id": "jd",
            "category_leaf_id": "straight-pants",
            "field_id": "length",
            "snapshot_version": "sv-1",
            "actual_value_id": "short,long",
            "actual_label": "短裤,长裤",
            "verified": True,
            "payload_json": {"source": "save_readback"},
        }

        self.event("readback-multi", "readback.recorded", payload)
        invalid = self.client.post(
            "/api/device/events",
            headers=self.device_headers,
            json={
                "idempotency_key": "readback-multi-invalid",
                "event_type": "readback.recorded",
                "payload": {
                    **payload,
                    "actual_value_id": "short,missing",
                },
            },
        )

        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(invalid.json()["error"], "actual_candidate_not_unique")
        with connect(self.settings) as connection:
            stored = connection.execute(
                "SELECT actual_value_id,actual_label FROM persisted_readbacks "
                "WHERE idempotency_key='readback-multi'"
            ).fetchone()
        self.assertEqual(
            (stored["actual_value_id"], stored["actual_label"]),
            ("short,long", "短裤,长裤"),
        )

    def test_review_images_prefer_oldest_uploaded_thumbnails(self):
        self.product_and_snapshot()
        uploaded_ids = []
        for body in (b"a", b"z"):
            digest = hashlib.sha256(body).hexdigest()
            response = self.client.put(
                f"/api/device/assets/{digest}",
                headers={
                    **self.device_headers,
                    "X-Product-Version": "pv-1",
                    "X-Asset-Kind": "learning_thumbnail",
                    "Content-Type": "image/png",
                },
                content=body,
            )
            self.assertEqual(response.status_code, 201, response.text)
            uploaded_ids.append(response.json()["asset_id"])
        self.event(
            "review-image-order",
            "review.created",
            {
                "id": "review-image-order",
                "run_id": "run-image-order",
                "device_id": "mac-1",
                "product_version": "pv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "field_label": "裤长",
                "canonical_field": "pant_length",
                "snapshot_version": "sv-1",
                "suggested_value_id": "long",
                "reason_code": "insufficient_evidence",
                "evidence_json": {},
            },
        )
        self.login()
        task = self.client.get("/api/reviews").json()["tasks"][0]
        self.assertEqual(task["asset_ids"][:2], uploaded_ids)

    def test_configured_model_analysis_is_validated_and_cached_locally(self):
        self.product_and_snapshot()
        image = b"\x89PNG\r\n\x1a\nmodel-test-image"
        digest = hashlib.sha256(image).hexdigest()
        uploaded = self.client.put(
            f"/api/device/assets/{digest}",
            headers={
                **self.device_headers,
                "X-Product-Version": "pv-1",
                "X-Asset-Kind": "learning_thumbnail",
                "Content-Type": "image/png",
            },
            content=image,
        ).json()
        facts = {
            "garment_type": "pants",
            "visible_colors": ["green"],
            "length_landmark": "ankle",
            "silhouette": "straight",
            "thickness_evidence": "regular",
            "image_consistency": {uploaded["asset_id"]: "same product"},
            "evidence_asset_ids": [uploaded["asset_id"]],
        }
        configured = Settings(
            data_dir=self.settings.data_dir,
            device_token="device-secret",
            model_api_url="https://model.example/responses",
            model_api_key="model-secret",
            ai_model="vision-test",
        )
        response = Mock(is_success=True, status_code=200)
        response.json.return_value = {"output_text": json.dumps(facts)}
        with patch("local_review.service.httpx.post", return_value=response) as post:
            first = analyze_product(configured, "pv-1")
            second = analyze_product(configured, "pv-1")
        self.assertEqual(first["status"], "ready")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        post.assert_called_once()
        request_json = post.call_args.kwargs["json"]
        self.assertEqual(request_json["model"], "vision-test")
        self.assertNotIn("model-secret", str(request_json))

    def test_review_without_ai_suggestion_does_not_require_correction_reason(self):
        self.product_and_snapshot()
        self.event(
            "review-no-suggestion",
            "review.created",
            {
                "id": "review-no-suggestion",
                "run_id": "run-no-suggestion",
                "device_id": "mac-1",
                "product_version": "pv-1",
                "platform_id": "jd",
                "category_leaf_id": "straight-pants",
                "field_id": "length",
                "field_label": "裤长",
                "canonical_field": "pant_length",
                "snapshot_version": "sv-1",
                "reason_code": "insufficient_evidence",
                "evidence_json": {},
            },
        )
        self.login()
        task = self.client.get("/api/reviews").json()["tasks"][0]
        claimed = self.client.post(
            "/api/reviews/review-no-suggestion/claim",
            headers={"Origin": "http://testserver"},
            json={"version": task["version"]},
        ).json()
        confirmed = self.client.post(
            "/api/reviews/review-no-suggestion/confirm",
            headers={"Origin": "http://testserver"},
            json={"version": claimed["version"], "final_value_id": "long"},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)


if __name__ == "__main__":
    unittest.main()
