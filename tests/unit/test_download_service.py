import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from video_downloader.application.download_service import _create_progress_callback
from video_downloader.domain.download_job import DownloadJob

class DownloadServiceTests(unittest.TestCase):
    def test_progress_callback_coalescing(self):
        job = DownloadJob(url="dummy", quality="best", output_dir=Path("out"))
        job.update_progress = MagicMock()
        
        callback = _create_progress_callback(job)
        
        with patch("video_downloader.application.download_service.time.monotonic") as mock_time:
            # Emit first callback, should be registered (if time allows, wait, we mock it)
            mock_time.return_value = 0.0
            callback(10, 100)
            
            # Since should_emit checks for now - last_progress_emit_time >= 0.15,
            # for the first call, now (0.0) - last (0.0) >= 0.15 is False!
            # Wait, the initial last_progress_emit_time is 0.0, so 0.0 - 0.0 = 0 < 0.15.
            # So the FIRST call won't emit unless we advance time past 0.15!
            job.update_progress.assert_not_called()
            
            # Advance time by 0.2s
            mock_time.return_value = 0.2
            callback(20, 100)
            job.update_progress.assert_called_once_with(20, 100)
            job.update_progress.reset_mock()
            
            # Fast emissions before 0.15 seconds has passed
            mock_time.return_value = 0.25
            callback(30, 100)
            mock_time.return_value = 0.30
            callback(40, 100)
            job.update_progress.assert_not_called()
            
            # Time advanced by 0.15 from 0.2 (last emit) -> 0.35
            mock_time.return_value = 0.36
            callback(50, 100)
            job.update_progress.assert_called_once_with(50, 100)
            job.update_progress.reset_mock()
            
            # Final emit must always go through even if time hasn't advanced 0.15s
            mock_time.return_value = 0.38
            callback(100, 100)
            job.update_progress.assert_called_once_with(100, 100)
