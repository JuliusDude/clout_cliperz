import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from clipper import publisher, worker
from clipper.captions import Word, highlights, write_captions
from clipper.storage import ClipperError, read_json, save_json


def response(status, headers=None, body=None):
    return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=body or {}))


class UploadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.state = self.folder / ".state"
        self.details = {"upload_key": "k", "title": "Clip", "description": "#Shorts"}
        (self.folder / "short.mp4").write_bytes(b"0123456789")
        self.client = Mock()
        self.client.post.return_value = response(200, {"Location": publisher.INSERT + "?upload_id=test"})
        self.complete = response(201, body={"id": "Abc123_4567", "status": {"privacyStatus": "private"}})

    def upload(self):
        return publisher.publish(self.client, self.folder, self.details, self.state, "private", "general")

    @patch.object(publisher, "CHUNK_SIZE", 4)
    def test_process_restart_resumes_from_acknowledged_byte(self):
        self.client.put.side_effect = [response(308, {"Range": "bytes=0-3"}), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.upload()
        self.client.put.side_effect = [response(308, {"Range": "bytes=0-3"}),
                                       response(308, {"Range": "bytes=0-7"}), self.complete]
        self.assertEqual(self.upload(), "Abc123_4567")
        calls = self.client.put.call_args_list
        self.assertEqual(calls[-3].kwargs["headers"]["Content-Range"], "bytes */10")
        self.assertEqual(calls[-2].kwargs["data"], b"4567")
        self.assertEqual(calls[-1].kwargs["data"], b"89")
        self.assertEqual(self.client.post.call_count, 1)
        self.assertEqual(self.client.post.call_args.kwargs["json"]["status"],
                         {"privacyStatus": "private", "selfDeclaredMadeForKids": False})

    @patch.object(publisher.time, "sleep")
    def test_lost_final_response_and_subsequent_rerun_do_not_repost(self, _sleep):
        self.client.put.side_effect = [requests.Timeout(), self.complete]
        self.upload()
        self.upload()
        self.assertEqual(self.client.put.call_count, 2)
        self.assertEqual(self.client.put.call_args.kwargs["headers"]["Content-Range"], "bytes */10")
        self.assertEqual(self.client.post.call_count, 1)
        receipt = read_json(self.state / "uploads/k.json")
        self.assertEqual(receipt["privacy"], "private")
        self.assertNotIn("session", receipt)

    @patch.object(publisher.time, "sleep")
    def test_expired_uncertain_final_upload_is_held_for_reconciliation(self, _sleep):
        self.client.put.side_effect = [requests.Timeout(), response(410)]
        with self.assertRaisesRegex(ClipperError, "final upload receipt expired"):
            self.upload()
        self.assertEqual(self.client.post.call_count, 1)
        self.assertTrue(read_json(self.state / "uploads/k.json")["final_sent"])


class ChannelTests(unittest.TestCase):
    def test_feed_reader_and_handle_urls(self):
        source = (b'<feed xmlns="http://www.w3.org/2005/Atom" '
                  b'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
                  b'<entry><yt:videoId>0123456789A</yt:videoId></entry>'
                  b'<entry><yt:videoId>0123456789A</yt:videoId></entry>'
                  b'<entry><yt:videoId>0123456789B</yt:videoId></entry></feed>')
        with patch("urllib.request.urlopen", return_value=io.BytesIO(source)):
            self.assertEqual(worker.channel_feed("UC" + "a" * 22), ["0123456789A", "0123456789B"])
        self.assertEqual(worker.channel_handle("https://www.youtube.com/@mychannel/videos"), "@mychannel")
        self.assertIsNone(worker.channel_handle("https://example.com/@mychannel"))

    def test_first_scan_backlog_pending_retry_and_own_upload(self):
        old, fresh, own = "abcdefghijk", "lmnopqrstuv", "123456789AB"
        channel = "UC" + "a" * 22
        args = SimpleNamespace(include_latest=False)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            save_json(state / "uploads/one.json", {"video_id": own})
            with (patch.object(worker, "channel_feed", side_effect=[[old], [own, fresh, old], [own]]),
                  patch.object(worker, "try_source", side_effect=[False, True]) as process):
                self.assertTrue(worker.scan_channel(channel, args, None, state))
                process.assert_not_called()
                self.assertFalse(worker.scan_channel(channel, args, None, state))
                self.assertTrue(worker.scan_channel(channel, args, None, state))
                self.assertEqual([c.args[0] for c in process.call_args_list], [worker.youtube_url(fresh)] * 2)
                self.assertEqual(read_json(state / "channels" / (channel + ".json"))["pending"], [])


class ClipTests(unittest.TestCase):
    def test_nonoverlap_cap_and_subtitles_relative_to_nonzero_start(self):
        words = [Word(n * 620 + 2500, n * 620 + 3040, "Finished." if n % 9 == 8 else "word")
                 for n in range(70)]
        clips = highlights(words, 50000, seconds=12, count=3)
        self.assertEqual(len(clips), 3)
        for i, clip in enumerate(clips):
            self.assertLessEqual(clip.end - clip.start, 12000)
            self.assertGreaterEqual(clip.words[0].start, clip.start)
            self.assertLessEqual(clip.words[-1].end, clip.end)
            if i:
                self.assertGreaterEqual(clip.start, clips[i - 1].end)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_captions(clips[-1], folder, True)
            timing = (folder / "captions.srt").read_text().splitlines()[1]
            self.assertTrue(timing.startswith("00:00:00,"))
            self.assertIn(r"{\c&H69E8FF&}", (folder / "captions.ass").read_text())


if __name__ == "__main__":
    unittest.main()
