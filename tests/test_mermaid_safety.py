import unittest

from services.mermaid_safety import remove_invalid_mermaid_blocks, validate_mermaid_blocks


class MermaidSafetyTests(unittest.TestCase):
    def test_valid_pie_and_bar_are_accepted(self):
        text = """Status summary.

```mermaid
pie showData
  "Delivered" : 4
  "Pending" : 2
```

```mermaid
xychart-beta
  x-axis ["ABC123", "XYZ789"]
  y-axis "SOs" 0 --> 5
  bar [3, 2]
```
"""
        checks = validate_mermaid_blocks(text)
        self.assertEqual([c.diagram_type for c in checks], ["pie", "xychart-beta"])
        self.assertTrue(all(c.valid for c in checks))


    def test_invalid_diagram_is_removed_with_note(self):
        text = """Bad visual.

```mermaid
graph TD
  A
```
"""
        checks = validate_mermaid_blocks(text)
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0].valid)
        cleaned = remove_invalid_mermaid_blocks(text, checks)
        self.assertNotIn("```mermaid", cleaned)
        self.assertIn("Diagram omitted", cleaned)


    def test_html_labels_are_rejected(self):
        text = """```mermaid
flowchart LR
  A["<b>SO</b>"] --> B["Done"]
```"""
        [check] = validate_mermaid_blocks(text)
        self.assertFalse(check.valid)
        self.assertEqual(check.reason, "html is not allowed")


if __name__ == "__main__":
    unittest.main()
