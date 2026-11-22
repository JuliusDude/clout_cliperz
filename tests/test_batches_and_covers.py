import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from clipper import batch, publisher
from clipper.captions import Word
from clipper.storage import ClipperError, read_json, save_json


class BatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.ids = ["abcdefghijk", "lmnopqrstuv"]
        self.args = SimpleNamespace(clips=3, per_video=False, seconds=10, model="base", language=None)
        self.selections = {}

    def source(self, url, *_):
        ident = batch.video_id(url)
        words = [Word(i * 700, i * 700 + 550, "End." if i % 6 == 5 else "word") for i in range(65)]
        return SimpleNamespace(words=words, duration=46000, media_id=ident)

    def finish(self, source, clips, *_):
        self.selections[source.media_id] = [(c.start, c.end) for c in clips]
        return len(clips), len(clips)

    def test_pasted_aliases_and_invalid_input(self):
        ids = batch.parse_links("https://youtu.be/abcdefghijk,\nhttps://youtube.com/watch?v=abcdefghijk "
                                "https://youtube.com/shorts/lmnopqrstuv")
        self.assertEqual(ids, self.ids)
        for bad in ("", "https://example.com/abcdefghijk", "https://youtu.be/bad"):
            with self.assertRaises(ClipperError):
                batch.parse_links(bad)

    def test_batch_total_is_shared_and_plan_survives_retry(self):
        with (patch.object(batch, "prepare_source", side_effect=self.source),
              patch.object(batch, "finish_clips", side_effect=self.finish)):
            self.assertEqual(batch.run_batch(self.ids, self.args, None, self.state), 0)
            self.assertEqual([len(self.selections[i]) for i in self.ids], [2, 1])
            first = self.selections.copy()
            with patch.object(batch, "highlights", side_effect=AssertionError("should reuse plan")):
                self.assertEqual(batch.run_batch(self.ids, self.args, None, self.state), 0)
            self.assertEqual(first, self.selections)
            self.args.per_video = True
            self.assertEqual(batch.run_batch(self.ids, self.args, None, self.state), 0)
            self.assertEqual([len(self.selections[i]) for i in self.ids], [3, 3])

    def test_recovered_source_fills_only_the_remaining_slots(self):
        first = self.source(batch.youtube_url(self.ids[0]))
        first.words, first.duration = first.words[:4], 3000
        with patch.object(batch, "finish_clips", side_effect=self.finish):
            with patch.object(batch, "prepare_source", side_effect=[first, ClipperError("unavailable")]):
                self.assertEqual(batch.run_batch(self.ids, self.args, None, self.state), 1)
            original = self.selections[self.ids[0]]
            with patch.object(batch, "prepare_source", side_effect=lambda url, *_:
                              first if batch.video_id(url) == self.ids[0] else self.source(url)):
                self.assertEqual(batch.run_batch(self.ids, self.args, None, self.state), 0)
            self.assertEqual(self.selections[self.ids[0]], original)
            self.assertEqual(len(self.selections[self.ids[1]]), 2)


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.state = self.folder / ".state"
        self.receipt = self.state / "uploads/k.json"
        self.ident = "abcdefghijk"
        save_json(self.receipt, {"video_id": self.ident, "requested_privacy": "public", "title": "A clip"})
        self.cover = b"\xff\xd8\xff" + b"test image"
        (self.folder / "thumbnail.jpg").write_bytes(self.cover)
        self.client = Mock()

    def status(self, value):
        return Mock(status_code=200, json=lambda: {"items": [{"processingDetails": {"processingStatus": value}}]})

    def apply(self):
        return publisher.publish_with_thumbnail(self.client, self.folder, {"upload_key": "k"}, self.state, "public", None)

    @patch.object(publisher.time, "sleep")
    def test_waits_for_processing_then_uploads_jpeg_exactly_once(self, _sleep):
        self.client.get.side_effect = [self.status("processing"), self.status("succeeded")]
        def upload(url, *, params, data, headers, timeout):
            self.assertEqual(self.client.get.call_count, 2)
            self.assertEqual(url, publisher.SET_THUMBNAIL)
            self.assertEqual(params["videoId"], self.ident)
            self.assertEqual(headers["Content-Type"], "image/jpeg")
            self.assertEqual(data.read(), self.cover)
            return Mock(status_code=200)
        self.client.post.side_effect = upload
        self.assertEqual(self.apply(), self.ident)
        self.assertEqual(self.apply(), self.ident)
        self.assertEqual(self.client.post.call_count, 1)
        self.client.put.assert_not_called()
        self.assertIn("thumbnail_sha256", read_json(self.receipt))

    @patch.object(publisher.time, "sleep")
    def test_thumbnail_failure_retries_without_another_video_upload(self, _sleep):
        self.client.get.return_value = self.status("succeeded")
        self.client.post.side_effect = [Mock(status_code=403, json=lambda: {"error": {"message": "not enabled"}}),
                                        Mock(status_code=200)]
        with self.assertRaisesRegex(ClipperError, "403"):
            self.apply()
        self.assertNotIn("thumbnail_sha256", read_json(self.receipt))
        self.assertEqual(self.apply(), self.ident)
        self.assertTrue(all(call.args[0] == publisher.SET_THUMBNAIL for call in self.client.post.call_args_list))

    @patch.object(publisher.time, "sleep")
    def test_media_processing_timeout_preserves_video(self, _sleep):
        self.client.get.return_value = self.status("processing")
        with self.assertRaisesRegex(ClipperError, "thumbnail is pending"):
            self.apply()
        self.client.post.assert_not_called()
        self.assertEqual(read_json(self.receipt)["video_id"], self.ident)
