import json
import tempfile
import unittest
from pathlib import Path

from app.resource_registry import BusinessResourceRegistry, ResourceRegistryError


PROJECT_ROOT = Path("/app") if Path("/app/data/ai-native-bank").exists() else Path(__file__).resolve().parents[4]
REGISTRY_ROOT = PROJECT_ROOT / "data" / "ai-native-bank" / "registries"


class BusinessResourceRegistryTests(unittest.TestCase):
    def test_guided_skills_have_exact_capability_boundaries(self):
        registry = BusinessResourceRegistry.load(REGISTRY_ROOT)
        self.assertEqual(registry.get_skill("bank.cashflow-planning").required_capability_ids, (
            "bank.cashflow.salary-plan.get",
        ))
        self.assertEqual(registry.get_skill("bank.spending-analysis").required_capability_ids, (
            "bank.cashflow.spending-analysis.get",
        ))
        self.assertEqual(registry.get_skill("bank.obligation-management").required_capability_ids, (
            "bank.obligation.list", "bank.loan.list", "bank.loan.detail.get",
            "bank.loan.repayment.list", "bank.loan.precalculate.create", "bank.utility.bill.list",
            "bank.credit-card.repayment.create", "bank.loan.repayment.create", "bank.utility.payment.create",
        ))

    def test_python_runtime_resolves_the_same_versioned_execution_view(self):
        registry = BusinessResourceRegistry.load(REGISTRY_ROOT)

        view = registry.resolve_execution_view(
            intent_ids=("ccb.account.balance.query",),
            actor_scopes=frozenset({"account:read"}),
        )

        self.assertEqual(view.skill_ids, ("bank.account-inquiry",))
        self.assertEqual(
            view.capability_ids,
            ("bank.account.list", "bank.account.balance.get", "bank.account.detail.get"),
        )
        self.assertTrue(view.digest.startswith("sha256:"))

    def test_python_runtime_fails_closed_when_scope_is_missing(self):
        registry = BusinessResourceRegistry.load(REGISTRY_ROOT)

        with self.assertRaises(ResourceRegistryError) as raised:
            registry.resolve_execution_view(
                intent_ids=("ccb.credit.card.loss-report",),
                actor_scopes=frozenset({"credit-card:read"}),
            )

        self.assertEqual(raised.exception.code, "provider_scope_denied")

    def test_python_registry_rejects_type_confusion_and_endpoint_injection(self):
        skills = (REGISTRY_ROOT / "skills.v1.json").read_text(encoding="utf-8")
        providers = json.loads(
            (REGISTRY_ROOT / "providers.v1.json").read_text(encoding="utf-8")
        )
        providers["providers"][0]["endpointBinding"]["command"] = "powershell"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "skills.v1.json").write_text(skills, encoding="utf-8")
            (root / "providers.v1.json").write_text(
                json.dumps(providers, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(ResourceRegistryError) as raised:
                BusinessResourceRegistry.load(root)
        self.assertEqual(raised.exception.code, "registry_contract_invalid")


if __name__ == "__main__":
    unittest.main()
