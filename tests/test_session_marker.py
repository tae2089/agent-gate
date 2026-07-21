"""Contract tests for shared, project-local session marker storage."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from session_marker import marker_path, read_marker, write_marker  # noqa: E402


class TestSessionMarker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.namespace = ".sample-sessions"
        self.session_id = "session-1"

    def tearDown(self):
        self.temp.cleanup()

    def test_json_object_round_trip(self):
        self.assertEqual(
            read_marker(self.root, self.namespace, self.session_id),
            ("absent", None),
        )

        self.assertTrue(
            write_marker(
                self.root,
                self.namespace,
                self.session_id,
                {"path": "_workspace/task/handoff.md"},
            )
        )

        self.assertEqual(
            read_marker(self.root, self.namespace, self.session_id),
            ("valid", {"path": "_workspace/task/handoff.md"}),
        )

    def test_symlink_marker_is_unsafe_and_never_overwritten(self):
        marker = marker_path(self.root, self.namespace, self.session_id)
        marker.parent.mkdir(parents=True)
        outside = Path(tempfile.mkdtemp()) / "outside.json"
        outside.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
        marker.symlink_to(outside)

        self.assertEqual(
            read_marker(self.root, self.namespace, self.session_id),
            ("unsafe", None),
        )
        self.assertFalse(
            write_marker(
                self.root, self.namespace, self.session_id, {"path": "replacement"}
            )
        )
        self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_OVERWRITE")

    def test_failed_atomic_replace_preserves_existing_marker(self):
        marker = marker_path(self.root, self.namespace, self.session_id)
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"path": "original"}), encoding="utf-8")

        with patch("session_marker.os.replace", side_effect=OSError("replace failed")):
            self.assertFalse(
                write_marker(
                    self.root,
                    self.namespace,
                    self.session_id,
                    {"path": "replacement"},
                )
            )

        self.assertEqual(
            read_marker(self.root, self.namespace, self.session_id),
            ("valid", {"path": "original"}),
        )
        self.assertEqual(list(marker.parent.glob(".session-marker-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
