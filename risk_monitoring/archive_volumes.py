"""Многотомные архивы: «Проектная документация.part01.rar» … «.part13.rar» — разные файлы внешней БД.

Каждый том по отдельности не читается («Need to start from first volume»), а первый — без остальных («Failed the
read enough data»): прогон 21.10.2026, файлы 5278–5285. Поэтому первый том читается вместе со всеми томами в одной
папке, а остальные тома получают паспорт-ссылку на первый без анализа.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

# «имя.part01.rar» (RAR 3+) и «имя.zip.001» / «имя.7z.001» (разбитые архивы)
VOLUME_RE = re.compile(r"^(?P<base>.+?)\.part(?P<n>\d{1,4})\.rar$|^(?P<base2>.+?\.(?:zip|7z|rar))\.(?P<n2>\d{3})$",
                       re.IGNORECASE)


def volume_info(file_name: Optional[str]) -> Optional[Tuple[str, int, str]]:
    """Том многотомного архива по имени файла.

    Args:
        file_name: Имя файла.

    Returns:
        Optional[Tuple[str, int, str]]: ``(основа имени, номер тома с 1, шаблон LIKE для поиска томов)`` или ``None``.
    """
    m = VOLUME_RE.match((file_name or "").strip())
    if not m:
        return None
    if m.group("base") is not None:
        base = m.group("base")
        return base, int(m.group("n")), _like(base) + ".part%.rar"
    base = m.group("base2")
    return base, int(m.group("n2")), _like(base) + ".%"


def _like(s: str) -> str:
    """Экранирование для LIKE."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def siblings(rows: List[Dict[str, Any]], base: str) -> List[Dict[str, Any]]:
    """Тома того же архива (по основе имени), по номеру тома, без повторов номера (берётся меньший id)."""
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        v = volume_info(r.get("file_name"))
        if v and v[0] == base and (v[1] not in out or int(r["id"]) < int(out[v[1]]["id"])):
            out[v[1]] = r
    return [out[k] for k in sorted(out)]


def complete(volumes: List[Dict[str, Any]]) -> bool:
    """Все тома с 1 по последний на месте (без пропусков)."""
    nums = [volume_info(v.get("file_name"))[1] for v in volumes]
    return bool(nums) and nums == list(range(1, len(nums) + 1))
