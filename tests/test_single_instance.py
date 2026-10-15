import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import main


class TestSingleInstance(unittest.TestCase):

    def test_single_instance_mutex(self):
        if sys.platform != "win32":
            return

        test_mutex_id = "Local\\JARVIS_Test_Instance_Mutex_Test"

        # First acquisition should succeed
        res1 = main.acquire_single_instance_lock(app_id=test_mutex_id)
        self.assertTrue(res1)
        h1 = main._INSTANCE_MUTEX
        self.assertIsNotNone(h1)

        # Second acquisition should detect existing instance and return False
        res2 = main.acquire_single_instance_lock(app_id=test_mutex_id)
        self.assertFalse(res2)

        # Close first handle
        import ctypes
        ctypes.windll.kernel32.CloseHandle(h1)
        main._INSTANCE_MUTEX = None

        # Third acquisition should succeed again
        res3 = main.acquire_single_instance_lock(app_id=test_mutex_id)
        self.assertTrue(res3)
        h3 = main._INSTANCE_MUTEX
        ctypes.windll.kernel32.CloseHandle(h3)
        main._INSTANCE_MUTEX = None


if __name__ == "__main__":
    unittest.main()
