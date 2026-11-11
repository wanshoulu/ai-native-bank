from __future__ import annotations

import unittest

from app.product_reference_resolution import (
    ProductReferenceResolutionError,
    resolve_product_reference,
)


def product(
    product_id: str,
    name: str,
    *,
    code: str,
    status: str = "ON_SALE",
) -> dict[str, object]:
    return {
        "id": product_id,
        "name": name,
        "productCode": code,
        "status": status,
        "riskLevel": "R2",
        "category": "FIXED_INCOME_WEALTH",
    }


def non_wealth_product() -> dict[str, object]:
    return {
        "id": "prod_loan_kuaidai",
        "name": "信用快贷",
        "productCode": "LOAN-KUAIDAI",
        "status": "ON_SALE",
        "riskLevel": "NA",
        "category": "LOAN",
    }


class ProductReferenceResolutionTest(unittest.TestCase):
    def test_resolves_exact_catalogue_name_to_provider_owned_id(self):
        resolution = resolve_product_reference(
            "申购理财产品：乾元-安鑫（按日）开放式固定收益类理财产品 10000元",
            [
                product("prod_w_longbao", "乾元-建行龙宝（按日）开放式净值型理财产品", code="LB001"),
                product("prod_w_anxin", "乾元-安鑫（按日）开放式固定收益类理财产品", code="AX001"),
            ],
        )

        self.assertEqual(resolution.state, "selected")
        self.assertEqual(resolution.product_id, "prod_w_anxin")
        self.assertEqual(resolution.semantic_reference, "乾元-安鑫(按日)开放式固定收益类理财产品")

    def test_returns_safe_choices_when_reference_is_missing(self):
        resolution = resolve_product_reference(
            "我想申购理财",
            [
                product("prod_w_longbao", "乾元-建行龙宝", code="LB001"),
                product("prod_w_anxin", "乾元-安鑫", code="AX001"),
            ],
        )

        self.assertEqual(resolution.state, "ambiguous")
        self.assertEqual(
            [item.reply_message for item in resolution.options],
            ["选择理财产品：乾元-建行龙宝", "选择理财产品：乾元-安鑫"],
        )

    def test_excludes_products_that_are_not_on_sale(self):
        resolution = resolve_product_reference(
            "申购乾元-安鑫",
            [product("prod_w_anxin", "乾元-安鑫", code="AX001", status="SUSPENDED")],
        )

        self.assertEqual(resolution.state, "empty")

    def test_filters_mixed_financial_catalogue_before_validating_wealth_candidates(self):
        resolution = resolve_product_reference(
            "申购乾元-安鑫",
            [
                non_wealth_product(),
                product("prod_w_anxin", "乾元-安鑫", code="AX001"),
            ],
        )

        self.assertEqual(resolution.state, "selected")
        self.assertEqual(resolution.product_id, "prod_w_anxin")

    def test_limited_wealth_product_remains_a_valid_purchase_candidate(self):
        resolution = resolve_product_reference(
            "申购个人大额存单3个月",
            [
                {
                    **product(
                        "prod_cd_3m",
                        "个人大额存单3个月",
                        code="CD-2026-3M",
                        status="LIMITED",
                    ),
                    "category": "CERTIFICATE_OF_DEPOSIT",
                    "riskLevel": "R1",
                }
            ],
        )

        self.assertEqual(resolution.state, "selected")
        self.assertEqual(resolution.product_id, "prod_cd_3m")

    def test_rejects_untrusted_catalogue_identifiers(self):
        with self.assertRaises(ProductReferenceResolutionError):
            resolve_product_reference(
                "申购乾元-安鑫",
                [product("../../secret", "乾元-安鑫", code="AX001")],
            )


if __name__ == "__main__":
    unittest.main()
