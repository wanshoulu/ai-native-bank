import hashlib
import json
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path("/app") if Path("/app/data/ai-native-bank").exists() else Path(__file__).resolve().parents[4]
SKILL_ROOT = PROJECT_ROOT / "data" / "ai-native-bank" / "skills"
EXPECTED = {
    "transfer-remittance": "bank.transfer-remittance",
    "account-inquiry": "bank.account-inquiry",
    "credit-card-management": "bank.credit-card-management",
    "wealth-management": "bank.wealth-management",
    "wealth-advisory": "bank.wealth-advisory",
    "cashflow-planning": "bank.cashflow-planning",
    "spending-analysis": "bank.spending-analysis",
    "obligation-management": "bank.obligation-management",
    "customer-profile": "bank.customer-profile",
}
REQUIRED_EVAL_CATEGORIES = {
    "single_task", "missing_slot", "multi_instance", "multi_intent",
    "resume", "unauthorized",
}


def load_skill(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise AssertionError("SKILL.md must have YAML frontmatter")
    _, frontmatter, body = text.split("---", 2)
    return yaml.safe_load(frontmatter), body.strip()


def content_digest(skill_dir: Path) -> str:
    paths = [skill_dir / "SKILL.md"]
    references = skill_dir / "references"
    if references.exists():
        paths.extend(sorted(references.rglob("*.md")))
    hasher = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(skill_dir).as_posix().encode("utf-8")
        hasher.update(relative + b"\0" + path.read_bytes() + b"\0")
    return "sha256:" + hasher.hexdigest()


class BusinessSkillTests(unittest.TestCase):
    def test_skills_are_openjiuwen_loadable_and_do_not_embed_provider_or_secret_details(self):
        for directory, skill_id in EXPECTED.items():
            with self.subTest(skill=skill_id):
                skill_dir = SKILL_ROOT / directory
                metadata, body = load_skill(skill_dir / "SKILL.md")
                manifest = json.loads((skill_dir / "manifest.json").read_text(encoding="utf-8"))

                self.assertEqual(metadata["name"], directory)
                self.assertTrue(metadata["description"])
                self.assertEqual(manifest["id"], skill_id)
                self.assertEqual(manifest["digest"], content_digest(skill_dir))
                self.assertNotIn("http://", body)
                self.assertNotIn("https://", body)
                self.assertNotIn("confirmationToken", body)
                self.assertNotIn("bank.transfer.execute", manifest["requiredCapabilityIds"])

    def test_each_skill_has_a_six_category_evaluation_set(self):
        for directory in EXPECTED:
            with self.subTest(skill=directory):
                payload = json.loads(
                    (SKILL_ROOT / directory / "evals" / "evals.json").read_text(encoding="utf-8")
                )
                categories = {item["category"] for item in payload["evals"]}
                self.assertTrue(REQUIRED_EVAL_CATEGORIES.issubset(categories))
                for item in payload["evals"]:
                    self.assertTrue(item["prompt"])
                    self.assertTrue(item["assertions"])

    def test_account_inquiry_documents_mixed_multi_intent_task_isolation(self):
        _, body = load_skill(SKILL_ROOT / "account-inquiry" / "SKILL.md")

        self.assertIn("Mixed multi-intent participation", body)
        self.assertIn("Never propagate an account reference to another Todo", body)
        self.assertIn("emit the completed result before observing the next Todo", body)

    def test_wealth_advisory_documents_suitability_and_source_boundaries(self):
        _, body = load_skill(SKILL_ROOT / "wealth-advisory" / "SKILL.md")

        self.assertIn("authoritative risk assessment", body)
        self.assertIn("Product catalog is not personalized advice", body)
        self.assertIn("Memory never replaces current core data", body)
        self.assertIn("explicit current-turn values always override those defaults", body)
        self.assertIn("Do not invent or add a product", body)
        self.assertIn("primary plan", body)
        self.assertIn("allocation amounts must equal", body)
        self.assertIn("one customer action per product", body)
        self.assertIn("productScope", body)


if __name__ == "__main__":
    unittest.main()
