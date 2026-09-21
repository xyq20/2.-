import json
import tempfile
import unittest
from pathlib import Path

from historical_readbacks import load_verified_attribute_history


class HistoricalReadbackTests(unittest.TestCase):
    def test_latest_verified_values_seed_only_the_same_product(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            old = runs / "20260901-100000"
            new = runs / "20260902-100000"
            other = runs / "20260903-100000"
            for run, style in ((old, "NGBL-1"), (new, "NGBL-1"), (other, "OTHER")):
                (run / "douyin").mkdir(parents=True)
                (run / "input-summary.json").write_text(
                    json.dumps({"style_code": style}), encoding="utf-8"
                )
            (old / "douyin" / "douyin-after-save-validation.json").write_text(
                json.dumps({"attributes": {"风格": ["休闲"]}}), encoding="utf-8"
            )
            (new / "douyin" / "douyin-after-save-validation.json").write_text(
                json.dumps(
                    {"attributes": {"风格": ["时尚都市"], "裤长": ["长裤"]}}
                ),
                encoding="utf-8",
            )
            (other / "douyin" / "douyin-after-save-validation.json").write_text(
                json.dumps({"attributes": {"风格": ["商务"]}}), encoding="utf-8"
            )

            history = load_verified_attribute_history(runs, "NGBL-1")

        self.assertEqual(history[("douyin", "风格")], ("时尚都市",))
        self.assertEqual(history[("douyin", "裤长")], ("长裤",))
        self.assertNotIn(("douyin", "商务"), history)


if __name__ == "__main__":
    unittest.main()
