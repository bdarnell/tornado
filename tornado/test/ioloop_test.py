#!/usr/bin/env python

import os
import unittest
import time

from tornado.testing import AsyncTestCase, LogTrapTestCase

class TestIOLoop(AsyncTestCase, LogTrapTestCase):
    def test_add_callback_wakeup(self):
        # Make sure that add_callback from inside a running IOLoop
        # wakes up the IOLoop immediately instead of waiting for a timeout.
        def callback():
            self.called = True
            self.stop()

        def schedule_callback():
            self.called = False
            self.io_loop.add_callback(callback)
            # Store away the time so we can check if we woke up immediately
            self.start_time = time.time()
        self.io_loop.add_timeout(time.time(), schedule_callback)
        self.wait()
        # jython is pretty slow, at least if the jit hasn't warmed up.
        places = 1 if os.name == 'java' else 2
        self.assertAlmostEqual(time.time(), self.start_time, places=places)
        self.assertTrue(self.called)

if __name__ == "__main__":
    unittest.main()
