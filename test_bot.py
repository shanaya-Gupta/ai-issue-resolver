import unittest
import os
import shutil
from pathlib import Path
from bot import validate_changes, select_relevant_files, is_safe_to_modify, get_modified_files
import unittest.mock

class TestIsSafeToModify(unittest.TestCase):
    def test_jsx_is_safe(self):
        self.assertTrue(is_safe_to_modify("src/component.jsx"))

class TestGetModifiedFiles(unittest.TestCase):
    def test_get_modified_files(self):
        final_implementations = {
            "file1.py": {"content": "...", "change_type": "REWRITE"},
            "file2.js": {"content": "...", "change_type": "APPEND"}
        }
        modified_files = get_modified_files(final_implementations)
        self.assertEqual(sorted(modified_files), ["file1.py", "file2.js"])

class TestValidateChanges(unittest.TestCase):
    def setUp(self):
        self.temp_dir = "test_temp_dir"
        os.makedirs(self.temp_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_validate_changes_with_syntax_error(self):
        invalid_python_code = "def invalid_syntax("
        invalid_file_path = Path(self.temp_dir) / "invalid_file.py"
        with open(invalid_file_path, "w") as f:
            f.write(invalid_python_code)

        implementations = {"invalid_file.py": {}}
        self.assertFalse(validate_changes(self.temp_dir, implementations))

class TestSelectRelevantFiles(unittest.TestCase):
    @unittest.mock.patch('bot.call_gemini_with_limits')
    def test_select_relevant_files_valid_json(self, mock_call_gemini):
        mock_call_gemini.return_value = '["file1.py", "file2.js"]'

        issue = {"title": "Test Issue", "body": "Test Body"}
        file_structure = "file1.py\nfile2.js\nfile3.txt"

        selected_files = select_relevant_files(issue, file_structure)
        self.assertEqual(selected_files, ["file1.py", "file2.js"])

    @unittest.mock.patch('bot.call_gemini_with_limits')
    def test_select_relevant_files_invalid_json(self, mock_call_gemini):
        mock_call_gemini.return_value = 'this is not json'

        issue = {"title": "Test Issue", "body": "Test Body"}
        file_structure = "file1.py\nfile2.js\nfile3.txt"

        selected_files = select_relevant_files(issue, file_structure)
        self.assertEqual(selected_files, [])

if __name__ == "__main__":
    unittest.main()
