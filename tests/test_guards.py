"""Fast offline tests (no LLM, no papers needed):  .venv\Scripts\python -W ignore -m unittest discover -s tests -v"""
import unittest

from app.agent import parse_tool_call
from app.index import chunk_page
from app.secrets_guard import check_command, is_secret_file, mask_text

FAKE_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"


class SecretGuard(unittest.TestCase):
    def test_masks_known_key_formats(self):
        for raw in [FAKE_KEY, "AKIA" + "ABCDEFGHIJKLMNOP", "ghp_" + "a" * 36, "hf_" + "b" * 34]:
            self.assertNotIn(raw, mask_text(f"key is {raw} ok"))

    def test_masks_hardcoded_assignment(self):
        out = mask_text(f'OPENAI_API_KEY = "{FAKE_KEY}"')
        self.assertNotIn(FAKE_KEY, out)
        self.assertIn("REDACTED", out)

    def test_leaves_placeholders_alone(self):
        t = 'api_key = "your-api-key-here"'
        self.assertEqual(mask_text(t), t)

    def test_secret_files(self):
        for f in [".env", ".env.local", "api.key", "id_rsa", "credentials.json", "auth.json"]:
            self.assertTrue(is_secret_file(f), f)
        for f in [".env.example", "train.py", "README.md", "config.yaml"]:
            self.assertFalse(is_secret_file(f), f)

    def test_dangerous_commands_refused(self):
        for c in ["git push origin main", "git commit -am x", "git reset --hard", "type .env", "cat config/.env",
                  "printenv", "echo %OPENAI_API_KEY%", "echo $env:OPENAI_API_KEY", "Get-Content api.key"]:
            self.assertIsNotNone(check_command(c), c)

    def test_normal_commands_allowed(self):
        for c in ["python train.py --epochs 1", "pip install -r requirements.txt", "git status", "git log -3", "dir"]:
            self.assertIsNone(check_command(c), c)


class ToolCallParsing(unittest.TestCase):
    def test_fenced_tool(self):
        c = parse_tool_call('```tool\n{"name": "search_papers", "args": {"query": "lr"}}\n```')
        self.assertEqual(c, {"name": "search_papers", "args": {"query": "lr"}})

    def test_fenced_json_and_bare(self):
        self.assertEqual(parse_tool_call('```json\n{"name": "list_papers"}\n```')["name"], "list_papers")
        self.assertEqual(parse_tool_call('{"name": "list_papers", "args": {}}')["name"], "list_papers")

    def test_plain_answer_is_not_a_call(self):
        self.assertIsNone(parse_tool_call("The learning rate is 3e-4 [Paper p.5]."))

    def test_malformed_json_is_not_a_call(self):
        self.assertIsNone(parse_tool_call('```tool\n{"name": "x", \n```'))


class Chunking(unittest.TestCase):
    def test_chunks_cover_text_and_respect_size(self):
        text = " ".join(f"word{i}" for i in range(1500))
        chunks = chunk_page(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1400 for c in chunks))
        self.assertIn("word0", chunks[0])
        self.assertIn("word1499", chunks[-1])


if __name__ == "__main__":
    unittest.main()
