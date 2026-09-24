import unittest

from field_policies import (
    FREIGHT_TEMPLATE_SHOPS,
    clear_category_field,
    freight_alternatives,
    is_single_material_expression,
    match_option_candidates,
)


class FieldPolicyTests(unittest.TestCase):
    def test_freight_scope_is_explicit_not_all_publish_shops(self):
        self.assertEqual(FREIGHT_TEMPLATE_SHOPS['wxsph'], ('NEIGBORL钊叔制鞋服',))
        self.assertEqual(FREIGHT_TEMPLATE_SHOPS['xhs'],
                         ('钊叔的店', '啊亮製NEIGBORL的店', '钊叔制NEIGBORL的店'))

    def test_freight_or_preserves_template_punctuation(self):
        self.assertEqual(freight_alternatives({"运费设置/运费模板": "新疆，西藏不包邮-鞋子，皮衣，外套/外套模板"}),
                         ("新疆，西藏不包邮-鞋子，皮衣，外套", "外套模板"))

    def test_conflicting_freight_sources_are_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "冲突"):
            freight_alternatives({"运费设置": "裤子", "运费模板": "外套"})

    def test_missing_source_and_scope(self):
        self.assertEqual(freight_alternatives({}), ())
        self.assertTrue(clear_category_field("yz", "尺码"))
        self.assertFalse(clear_category_field("jd", "尺码"))
        self.assertFalse(clear_category_field("yz", "尺码表"))

    def test_single_material_or_not_mixture(self):
        self.assertTrue(is_single_material_expression("棉100%/棉"))
        self.assertTrue(is_single_material_expression("羊毛100%/羊毛"))
        self.assertFalse(is_single_material_expression("棉60%,涤纶40%"))
        self.assertFalse(is_single_material_expression("棉60%+涤纶40%"))
        self.assertFalse(is_single_material_expression("棉60% 涤纶40%"))

    def test_option_match_prefers_annotated_candidate_over_substring_option(self):
        # 2026-09-21 京东真实候选：期望“氨纶/聚氨酯弹性纤维(氨纶)”时，
        # 独立候选“弹性纤维”只是别名的子串，必须让位给“氨纶(聚氨酯弹性纤维)”。
        candidates = ("棉", "氨纶(聚氨酯弹性纤维)", "弹性纤维", "聚烯烃弹性纤维")
        self.assertEqual(
            match_option_candidates(("氨纶", "聚氨酯弹性纤维(氨纶)"), candidates),
            (1,),
        )

    def test_option_match_exact_and_annotated_forms(self):
        candidates = ("棉", "山羊绒", "氨纶(聚氨酯弹性纤维)")
        self.assertEqual(match_option_candidates(("棉",), candidates), (0,))
        # Excel“羊绒”包含于京东候选“山羊绒”。
        self.assertEqual(match_option_candidates(("羊绒",), candidates), (1,))
        # 括号语序相反（天猫别名“化学名(俗名)” vs 京东候选“俗名(化学名)”）
        # 拆出的名字集合相同，就是同一个材质，必须等价。
        self.assertEqual(
            match_option_candidates(("聚氨酯弹性纤维(氨纶)",), candidates), (2,)
        )

    def test_option_match_exact_beats_lookalike_substring(self):
        # 2026-09-21 17:05 线上失败：Excel“棉”精确存在时，
        # 子串沾边的“木棉”不得参与竞争导致非唯一转审核。
        candidates = ("木棉", "棉")
        self.assertEqual(match_option_candidates(("棉",), candidates), (1,))
        # 平台只有“木棉”时，“棉 ⊂ 木棉”走 Tier-2 弱匹配，
        # 与“羊绒 ⊂ 山羊绒”同一条既有兼容路径。
        self.assertEqual(match_option_candidates(("棉",), ("木棉",)), (0,))

    def test_cotton_cloth_alias_beats_other_cotton_fabrics(self):
        # JD's real dictionary has eight substring matches for Excel 棉.
        # The known spelling 棉布 must win without choosing a cotton blend.
        candidates = (
            "棉麻", "珠地棉", "棉布", "棉毛布", "美棉斜纹布",
            "棉绸", "水柔棉", "全棉牛仔布", "其他",
        )
        self.assertEqual(match_option_candidates(("棉",), candidates), (2,))
        self.assertEqual(
            match_option_candidates(("棉",), candidates + ("棉",)), (9,)
        )
        self.assertEqual(
            match_option_candidates(("棉布",), ("木棉", "棉", "棉麻")), (1,)
        )

    def test_option_match_falls_back_to_contained_candidate(self):
        # 没有标注形候选时，才允许“候选 ⊂ 期望别名”的弱匹配。
        self.assertEqual(
            match_option_candidates(("聚氨酯弹性纤维",), ("弹性纤维", "棉")),
            (0,),
        )

    def test_simple_material_does_not_fuzzy_match_compound_blend(self):
        self.assertEqual(
            match_option_candidates(("涤纶",), ("羊毛与涤纶混纺", "棉麻混纺")),
            (),
        )

    def test_option_match_ambiguous_stays_ambiguous(self):
        # Excel 有精确名时直接命中，子串近邻（獭兔毛、兔毛皮）不构成竞争。
        self.assertEqual(
            match_option_candidates(("兔毛",), ("兔毛", "獭兔毛", "兔毛皮")),
            (0,),
        )
        # 只有多个子串沾边候选、无精确与标注形时，原样返回由调用方处理。
        self.assertEqual(
            match_option_candidates(("兔毛",), ("獭兔毛", "兔毛皮")), (0, 1)
        )
        self.assertEqual(match_option_candidates(("羊毛",), ("竹纤维",)), ())
