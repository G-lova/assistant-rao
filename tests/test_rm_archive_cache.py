"""Кэш архивов ЕИС: открытыми держится не больше RM_MAX_OPEN_ARCHIVES архивов (13.11.2026, «Too many open files»)."""
import os
import tempfile
import unittest
import zipfile

from risk_monitoring.xml_event_analyzer import ArchiveCache


class ArchiveCacheTest(unittest.TestCase):
    """LRU открытых архивов."""

    def setUp(self):
        """Десять архивов по одному XML."""
        self.dir = tempfile.mkdtemp()
        self.paths = []
        for i in range(10):
            p = os.path.join(self.dir, f"a{i}.zip")
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr(f"dir/x{i}.xml", f"<x>{i}</x>")
            self.paths.append(p)

    def test_open_limited_and_reopen(self):
        """Открыто не больше трёх; вытесненный архив читается снова; close закрывает всё и удаляет файлы."""
        c = ArchiveCache(None)
        c.max_open = 3
        for i, p in enumerate(self.paths):
            self.assertEqual(c._read_cached(p, f"x{i}.xml"), f"<x>{i}</x>".encode())
            self.assertLessEqual(len(c.zips), 3)
        self.assertEqual(c._read_cached(self.paths[0], "dir/x0.xml"), b"<x>0</x>")
        self.assertIn(self.paths[0], c.zips)
        open_now = [zf for zf, _, _ in c.zips.values()]
        c.paths = {p: p for p in self.paths}
        c.close()
        self.assertTrue(all(zf.fp is None for zf in open_now))
        self.assertFalse(any(os.path.exists(p) for p in self.paths))

    def test_missing_entry(self):
        """Нет такого XML — FileNotFoundError."""
        c = ArchiveCache(None)
        with self.assertRaises(FileNotFoundError):
            c._read_cached(self.paths[0], "nope.xml")


if __name__ == "__main__":
    unittest.main()
