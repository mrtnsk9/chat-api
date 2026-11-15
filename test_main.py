import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import create_app


class ChatDownloaderApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temporary_directory.name)
        self.client = TestClient(create_app({
            "output_dir": self.output_dir,
            "api_token": "test-token",
            "downloader_command": ["chat_downloader"],
        }))
        self.authorization = {"Authorization": "Bearer test-token"}

    def tearDown(self):
        self.temporary_directory.cleanup()

    @patch("main.subprocess.Popen")
    def test_starts_download_with_safe_argument_list(self, popen):
        response = self.client.post(
            "/chat-api",
            headers=self.authorization,
            json={"video_id": "jfKfPfyJRdk", "only_missing": False},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "started")
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "chat_downloader")
        self.assertEqual(
            command[1:3],
            ["https://www.youtube.com/watch?v=jfKfPfyJRdk", "--output"],
        )
        self.assertEqual(
            popen.call_args.kwargs["stdout"],
            subprocess.DEVNULL,
        )
        self.assertEqual(
            popen.call_args.kwargs["stderr"],
            subprocess.DEVNULL,
        )
        self.assertNotIn("log_path", response.json())
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertEqual(len(popen.call_args.kwargs["pass_fds"]), 1)
        self.assertNotIn("CHAT_API_TOKEN", popen.call_args.kwargs["env"])

    def test_rejects_missing_authentication(self):
        response = self.client.post(
            "/chat-api", json={"video_id": "jfKfPfyJRdk"}
        )

        self.assertEqual(response.status_code, 401)

    def test_rejects_get_requests(self):
        response = self.client.get(
            "/chat-api",
            headers=self.authorization,
            params={"video_id": "jfKfPfyJRdk"},
        )

        self.assertEqual(response.status_code, 405)

    @patch("main.subprocess.Popen")
    def test_reports_running_job_and_rejects_early_result(self, popen):
        popen.return_value.poll.return_value = None
        response = self.client.post(
            "/chat-api",
            headers=self.authorization,
            json={"video_id": "jfKfPfyJRdk"},
        )
        status_url = response.json()["status_url"]
        result_url = response.json()["result_url"]

        status = self.client.get(status_url, headers=self.authorization)
        result = self.client.get(result_url, headers=self.authorization)

        self.assertEqual(status.json()["status"], "running")
        self.assertEqual(result.status_code, 409)

    @patch("main.subprocess.Popen")
    def test_returns_result_after_job_completes(self, popen):
        popen.return_value.poll.return_value = 0
        response = self.client.post(
            "/chat-api",
            headers=self.authorization,
            json={"video_id": "jfKfPfyJRdk"},
        )
        output_path = self.output_dir / "jfKfPfyJRdk.json"
        output_path.write_text('[{"message":"hello"}]', encoding="utf-8")

        status = self.client.get(
            response.json()["status_url"], headers=self.authorization)
        result = self.client.get(
            response.json()["result_url"], headers=self.authorization)

        self.assertEqual(status.json()["status"], "completed")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), [{"message": "hello"}])
        self.assertEqual(
            result.headers["content-type"],
            "application/json",
        )

    @patch("main.subprocess.Popen")
    def test_reports_failed_job(self, popen):
        popen.return_value.poll.return_value = 1
        response = self.client.post(
            "/chat-api",
            headers=self.authorization,
            json={"video_id": "jfKfPfyJRdk"},
        )

        status = self.client.get(
            response.json()["status_url"], headers=self.authorization)
        result = self.client.get(
            response.json()["result_url"], headers=self.authorization)

        self.assertEqual(status.json()["status"], "failed")
        self.assertEqual(result.status_code, 502)

    def test_rejects_unknown_job(self):
        response = self.client.get(
            "/chat-api/unknown-job", headers=self.authorization)

        self.assertEqual(response.status_code, 404)

    @patch("main.subprocess.Popen")
    def test_prevents_duplicate_active_downloads_per_video(self, popen):
        held_fds = []
        popen.return_value.poll.return_value = None

        def retain_lock_fd(*args, **kwargs):
            held_fds.append(os.dup(kwargs["pass_fds"][0]))
            return popen.return_value

        popen.side_effect = retain_lock_fd
        try:
            first = self.client.post(
                "/chat-api",
                headers=self.authorization,
                json={"video_id": "jfKfPfyJRdk"},
            )
            duplicate = self.client.post(
                "/chat-api",
                headers=self.authorization,
                json={"video_id": "jfKfPfyJRdk", "only_missing": True},
            )
            os.close(held_fds.pop(0))
            after_completion = self.client.post(
                "/chat-api",
                headers=self.authorization,
                json={"video_id": "jfKfPfyJRdk"},
            )
            different_video = self.client.post(
                "/chat-api",
                headers=self.authorization,
                json={"video_id": "abcdefghijk"},
            )
        finally:
            for fd in held_fds:
                os.close(fd)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.json()["detail"],
            "A chat download for this video is already in progress",
        )
        self.assertEqual(after_completion.status_code, 202)
        self.assertEqual(different_video.status_code, 202)
        self.assertEqual(popen.call_count, 3)

    @patch("main.subprocess.Popen")
    def test_only_missing_skips_existing_chat(self, popen):
        (self.output_dir / "jfKfPfyJRdk.json").write_text(
            "{}", encoding="utf-8")

        response = self.client.post(
            "/chat-api",
            headers=self.authorization,
            data={"video_id": "jfKfPfyJRdk", "only_missing": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "skipped")
        status = self.client.get(
            response.json()["status_url"], headers=self.authorization)
        result = self.client.get(
            response.json()["result_url"], headers=self.authorization)
        self.assertEqual(status.json()["status"], "completed")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {})
        popen.assert_not_called()

    @patch("main.subprocess.Popen")
    def test_existing_chat_uses_unique_output_name(self, popen):
        (self.output_dir / "jfKfPfyJRdk.json").write_text(
            "{}", encoding="utf-8")

        response = self.client.post(
            "/chat-api",
            headers=self.authorization,
            json={"video_id": "jfKfPfyJRdk"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertNotEqual(
            popen.call_args.args[0][-1],
            str(self.output_dir / "jfKfPfyJRdk.json"),
        )

    def test_rejects_invalid_video_id(self):
        response = self.client.post(
            "/chat-api",
            headers=self.authorization,
            json={"video_id": "../../etc/passwd"},
        )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
