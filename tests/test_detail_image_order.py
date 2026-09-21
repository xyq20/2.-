import unittest
from pathlib import Path

from kuaimai_erp import natural_key


class DetailImageOrderTests(unittest.TestCase):
    def test_black_langmu_groups_sort_before_group_two(self):
        names = [
            "未标题 2_02.jpg",
            "未标题-1-恢复的_02.jpg",
            "未标题 2_01.jpg",
            "未标题-1-恢复的_01.jpg",
            "未标题-1-恢复的_10.jpg",
        ]

        ordered = [name for name in sorted(map(Path, names), key=natural_key)]

        self.assertEqual(
            ordered,
            [
                "未标题-1-恢复的_01.jpg",
                "未标题-1-恢复的_02.jpg",
                "未标题-1-恢复的_10.jpg",
                "未标题 2_01.jpg",
                "未标题 2_02.jpg",
            ],
        )


if __name__ == "__main__":
    unittest.main()
