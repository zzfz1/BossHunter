import unittest
import tempfile
from pathlib import Path

from bosshunter.collection.capabilities import PLATFORM_CAPABILITIES, platform_supports
from bosshunter.db import get_db, insert_job
from bosshunter.ai.greeter import generate_greetings


class PlatformCapabilitiesTests(unittest.TestCase):
    def test_boss_has_full_capabilities(self):
        self.assertTrue(platform_supports("boss", "collect"))
        self.assertTrue(platform_supports("boss", "score"))
        self.assertTrue(platform_supports("boss", "greet"))
        self.assertTrue(platform_supports("boss", "deliver"))
        self.assertTrue(platform_supports("boss", "monitor"))

    def test_new_platforms_are_read_only(self):
        for platform in ("zhilian", "51job", "liepin"):
            self.assertTrue(platform_supports(platform, "collect"))
            self.assertTrue(platform_supports(platform, "score"))
            self.assertTrue(platform_supports(platform, "greet"))
            self.assertFalse(platform_supports(platform, "deliver"))
            self.assertFalse(platform_supports(platform, "monitor"))

    def test_unknown_platform_supports_nothing(self):
        self.assertFalse(platform_supports("unknown", "collect"))
        self.assertFalse(platform_supports("nonexistent", "score"))

    def test_yingjiesheng_only_collects_and_scores(self):
        self.assertEqual(PLATFORM_CAPABILITIES["yingjiesheng"], frozenset({"collect", "score"}))

    def test_yingjiesheng_job_cannot_enter_greeting_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.db"
            db = get_db(path)
            insert_job(db, {
                "id": "yingjiesheng:1001", "source_platform": "yingjiesheng",
                "source_job_id": "1001", "title": "示例岗位", "company": "示例公司",
            })
            db.close()
            generated = generate_greetings(
                {"profile": {"ai_greeting_enabled": False, "fixed_greeting": "您好"}},
                job_ids=["yingjiesheng:1001"], db_path=path,
            )
            db = get_db(path)
            greeting = db.execute("SELECT greeting FROM jobs WHERE id = ?", ("yingjiesheng:1001",)).fetchone()["greeting"]
            db.close()
        self.assertEqual(generated, 0)
        self.assertFalse(greeting)

    def test_unknown_capability_returns_false(self):
        self.assertFalse(platform_supports("boss", "nonexistent"))

    def test_capability_map_keys(self):
        self.assertEqual(
            set(PLATFORM_CAPABILITIES),
            {"boss", "zhilian", "51job", "liepin", "yingjiesheng"},
        )


if __name__ == "__main__":
    unittest.main()
