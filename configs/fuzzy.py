"""Нечёткое сравнение строк (``ratio``, ``partial_ratio``) в шкале 0–100, как у ``fuzzywuzzy``.

``fuzzywuzzy`` не развивается (переименован в ``thefuzz``) и без ``python-Levenshtein`` считает медленно на чистом
Python. Здесь — ``rapidfuzz`` (C++, та же метрика: нормированное расстояние Indel), с запасными вариантами:

1. ``rapidfuzz`` — основной (в ``requirements.txt``);
2. ``thefuzz`` / ``fuzzywuzzy`` — если установлен только он;
3. ``difflib.SequenceMatcher`` — без сторонних пакетов (так же считает ``fuzzywuzzy`` без ``python-Levenshtein``).

Совместимость с ``fuzzywuzzy``: результат — целое число (округление как в ``fuzzywuzzy.utils.intr``), пустая
строка с любой другой даёт 0 (у ``rapidfuzz`` две пустые строки дают 100), предварительной обработки строк
(регистр, знаки) нет — как у ``fuzz.ratio`` / ``fuzz.partial_ratio`` в ``fuzzywuzzy``.

У ``partial_ratio`` ``rapidfuzz`` ищет лучшее выравнивание точно, а ``fuzzywuzzy`` — эвристикой по совпадающим
блокам, поэтому на тех же строках ``rapidfuzz`` может дать на несколько пунктов больше.
"""
from difflib import SequenceMatcher
from typing import Callable, Optional, Tuple

BACKEND = "difflib"


def _load() -> Tuple[Callable[[str, str], float], Callable[[str, str], float]]:
    """Выбирает реализацию: rapidfuzz → thefuzz/fuzzywuzzy → difflib."""
    global BACKEND
    try:
        from rapidfuzz import fuzz as rf

        BACKEND = "rapidfuzz"
        return rf.ratio, rf.partial_ratio
    except ImportError:
        pass
    for name in ("thefuzz", "fuzzywuzzy"):
        try:
            module = __import__(name, fromlist=["fuzz"])
            BACKEND = name
            return module.fuzz.ratio, module.fuzz.partial_ratio
        except ImportError:
            continue

    def ratio(a: str, b: str) -> float:
        """Сходство строк по ``SequenceMatcher`` (0–100)."""
        return 100.0 * SequenceMatcher(None, a, b).ratio()

    def partial_ratio(a: str, b: str) -> float:
        """Лучшее сходство короткой строки с подстрокой длинной той же длины (по совпадающим блокам)."""
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        best = 0.0
        for block in SequenceMatcher(None, short, long_).get_matching_blocks():
            start = max(0, block[1] - block[0])
            window = long_[start:start + len(short)]
            best = max(best, SequenceMatcher(None, short, window).ratio())
            if best >= 0.995:
                break
        return 100.0 * best

    return ratio, partial_ratio


_RATIO, _PARTIAL = _load()


def _score(fn: Callable[[str, str], float], a: Optional[str], b: Optional[str]) -> int:
    """Целое 0–100; пустая строка (или не строка) — 0."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return 0
    return int(round(fn(a, b)))


def ratio(a: Optional[str], b: Optional[str]) -> int:
    """Сходство двух строк целиком (0–100), как ``fuzzywuzzy.fuzz.ratio``."""
    return _score(_RATIO, a, b)


def partial_ratio(a: Optional[str], b: Optional[str]) -> int:
    """Сходство короткой строки с лучшим фрагментом длинной (0–100), как ``fuzzywuzzy.fuzz.partial_ratio``."""
    return _score(_PARTIAL, a, b)
