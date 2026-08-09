import contextlib
import unittest
from unittest.mock import patch

from app.worker import background


class DummyJob:
    def __init__(self, job_id, job_type='transcode_and_upload', video_id='video-1', status='queued'):
        self.id = job_id
        self.job_type = job_type
        self.video_id = video_id
        self.status = status
        self.current_step = 'Queued'
        self.current_message = 'Queued'


class BackgroundWorkerTests(unittest.TestCase):
    def test_start_job_thread_dispatches_job_in_background(self):
        class DummyApp:
            def app_context(self):
                return contextlib.nullcontext()

        app = DummyApp()
        job = DummyJob('job-1')

        with patch.object(background, '_execute_job', return_value=None) as mock_execute:
            thread = background.start_job_thread(app, job)
            thread.join(timeout=1)

            self.assertFalse(thread.is_alive())
            self.assertTrue(mock_execute.called)

    def test_has_active_job_for_video_detects_duplicate_upload_jobs(self):
        queued_job = DummyJob('job-2', status='queued')
        active_jobs = [DummyJob('job-1', status='processing')]

        self.assertTrue(background.has_active_job_for_video(queued_job, active_jobs))


if __name__ == '__main__':
    unittest.main()
