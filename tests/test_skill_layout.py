import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SkillLayoutTests(unittest.TestCase):
    def test_required_files_and_metadata_exist(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn("name: automation-anywhere-docs", skill)
        self.assertIn("description:", skill)
        self.assertNotIn("TODO", skill)
        self.assertTrue((ROOT / "agents" / "openai.yaml").is_file())
        self.assertTrue((ROOT / "scripts" / "search_docs.py").is_file())

    def test_ui_default_prompt_names_skill(self):
        metadata = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$automation-anywhere-docs", metadata)


if __name__ == "__main__":
    unittest.main()
