import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clipper.publisher import token_path, verify_clout_channel
from clipper.storage import ClipperError, download_workspace, temporary_downloads
from clipper.workflows import CLOUT_CUTS, MOVIE_RECAP, workflow_output
from web import command


class WorkflowTests(unittest.TestCase):
    def test_publish_checks_the_selected_workflows_token(self):
        job = {"mode": "links", "source": "https://www.youtube.com/watch?v=TLSYwkWCIzA",
               "visibility": "private"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clout_token = root / "clout.json"
            recap_token = root / "recap.json"
            clout_token.touch()
            with patch.dict("os.environ", {"SHORTS_CLIPPER_TOKEN": str(clout_token),
                                          "SHORTS_CLIPPER_MOVIE_RECAP_TOKEN": str(recap_token)}):
                self.assertEqual(token_path(CLOUT_CUTS), clout_token.resolve())
                self.assertIn(CLOUT_CUTS, command(job, root))
                with self.assertRaisesRegex(ValueError, "Connect YouTube for Movie-Recap"):
                    command({**job, "workflow": MOVIE_RECAP}, root)
                recap_token.touch()
                self.assertIn(MOVIE_RECAP, command({**job, "workflow": MOVIE_RECAP}, root))

    def test_dynamic_modes_share_channel_and_set_distinct_defaults(self):
        root = Path("output")
        for mode, seconds, style in (("streamers", "60", "stream-pop"), ("podcast", "120", "classic")):
            args = command({"mode": "follow", "source": "@creator", "workflow": mode}, root)
            self.assertEqual(args[args.index("--seconds") + 1], seconds)
            self.assertEqual(args[args.index("--caption-style") + 1], style)
            self.assertEqual(workflow_output(root, mode), workflow_output(root, CLOUT_CUTS))
            self.assertEqual(token_path(mode), token_path(CLOUT_CUTS))

    def test_wrong_channel_cannot_publish(self):
        client = Mock()
        expected = Mock(status_code=200)
        expected.json.return_value = {"items": [{"id": "expected"}]}
        other = Mock(status_code=200)
        other.json.return_value = {"items": [{"id": "other"}]}
        client.get.side_effect = [expected, other]
        with self.assertRaisesRegex(ClipperError, "@Clout-CutsTV"):
            verify_clout_channel(client)

    def test_failed_job_removes_only_managed_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            state = workflow_output(Path(directory).resolve(), CLOUT_CUTS) / ".state"
            downloads = download_workspace(state)
            downloads.mkdir(parents=True)
            (downloads / "interrupted.part").touch()
            supplied = downloads.parent / "user-video.mp4"
            supplied.touch()

            with self.assertRaisesRegex(RuntimeError, "job failed"), temporary_downloads(state):
                self.assertFalse(downloads.exists())
                downloads.mkdir()
                (downloads / "current-job.mkv").touch()
                raise RuntimeError("job failed")

            self.assertFalse(downloads.exists())
            self.assertTrue(supplied.is_file())
