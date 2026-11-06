"""Запуск LibreOffice (``configs.file_reader.run_soffice``): таймаут завершает всю группу процессов, профиль
удаляется (06.11.2026: конвертация большого .docx дольше 60 с)."""
import asyncio
import glob
import os
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tests.stubs import stubbed_import


class RunSofficeTest(unittest.TestCase):
    """Подменяем ``soffice`` скриптом, который запускает дочерний процесс (как настоящий soffice.bin)."""

    def setUp(self):
        """Каталог с поддельным ``soffice`` в PATH и функция модуля."""
        self.dir = tempfile.mkdtemp()
        self.marker = os.path.join(self.dir, "child_alive")
        script = os.path.join(self.dir, "soffice")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n"
                    f'case "$*" in *slow*) (sleep 3; touch {self.marker}) & sleep 30;; *fail*) exit 3;; esac\n'
                    "echo done\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"PATH": self.dir + os.pathsep + os.environ["PATH"]})
        self.env.start()
        self.addCleanup(self.env.stop)
        with stubbed_import("configs.file_reader") as module:
            self.run = module.run_soffice

    def profiles(self):
        """Профили LibreOffice во временном каталоге."""
        return set(glob.glob(os.path.join(tempfile.gettempdir(), "lo_profile_*")))

    def test_ok_and_profile_removed(self):
        """Успешный запуск — stdout, профиль удалён."""
        before = self.profiles()
        res = asyncio.run(self.run(["--convert-to", "pdf", "x.docx"]))
        self.assertIn("done", res.stdout)
        self.assertEqual(self.profiles(), before)

    def test_timeout_kills_group(self):
        """По таймауту завершается и дочерний процесс: метка через 3 с не появляется; профиль удалён."""
        before = self.profiles()
        t = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            asyncio.run(self.run(["slow"], timeout=1))
        self.assertLess(time.monotonic() - t, 10)
        time.sleep(3.5)
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(self.profiles(), before)

    def test_error_code(self):
        """Ненулевой код возврата — CalledProcessError."""
        with self.assertRaises(subprocess.CalledProcessError):
            asyncio.run(self.run(["fail"]))


if __name__ == "__main__":
    unittest.main()
