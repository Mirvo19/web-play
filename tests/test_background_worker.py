import contextlib
import unittest
from unittest.mock import patch

from app.worker import background


class DummyJob:
    def __init__(self, job_id, job_type='transcode_and_upload'):
        self.id = job_id
        self.job_type = job_type
        self.status = 'queued'
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


if __name__ == '__main__':
    unittest.main()
