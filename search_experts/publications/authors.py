"""Проверка авторства: есть ли фамилия эксперта среди авторов публикации.

Нужна, чтобы чужая статья, по ошибке загруженная в анкету (или найденная по ссылке), не формировала профиль эксперта.
Фамилия сравнивается с первым словом каждого автора («Жанаева С.Я.», «Жанаева Светлана Яковлевна»,
«S.Ya. Zhanaeva» — для латиницы фамилия эксперта транслитерируется) с допуском на одну-две буквы
(«Жанаева» / «Жанаевой», «Zhanaeva» / «Janaeva»).
"""
import re
from difflib import SequenceMatcher
from typing import Iterable, Optional

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def surname_of(name: Optional[str]) -> Optional[str]:
    """Фамилия из ФИО анкеты (первое слово), в нижнем регистре, «ё» → «е».

    Args:
        name: ``users.name`` («Гоголева Елена Николаевна»).

    Returns:
        Optional[str]: Фамилия или ``None``, если её не выделить (меньше 2 букв).
    """
    if not name:
        return None
    first = re.split(r"[\s,]+", str(name).strip())[0].lower().replace("ё", "е")
    first = re.sub(r"[^\w-]", "", first)
    return first if len(first) >= 2 else None


def translit(text: str) -> str:
    """Транслитерация кириллицы в латиницу (упрощённая BGN/PCGN: ж → zh, х → kh, ц → ts).

    Args:
        text: Строка в нижнем регистре.

    Returns:
        str: Латиница.
    """
    return "".join(_TRANSLIT.get(ch, ch) for ch in text)


def _author_tokens(author: str) -> list:
    """Слова автора длиной от 2 букв без инициалов («S.Ya. Zhanaeva» → ``["zhanaeva"]``)."""
    words = re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)?", str(author).lower().replace("ё", "е"))
    return [w for w in words if len(w) >= 3]


def _close(a: str, b: str) -> bool:
    """Одна фамилия в разных формах: совпадение, общий корень с разницей окончания или сходство ≥ 0.85."""
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4] and abs(len(a) - len(b)) <= 2 and (a.startswith(b[:-1]) or b.startswith(a[:-1])):
        return True
    return len(a) >= 5 and len(b) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.85


def author_matches(expert_name: Optional[str], authors: Optional[Iterable[str]]) -> Optional[bool]:
    """Есть ли эксперт среди авторов.

    Args:
        expert_name: ФИО эксперта из анкеты.
        authors: Авторы публикации (строки) или ``None``.

    Returns:
        Optional[bool]: ``True`` — найден; ``False`` — авторы известны, эксперта среди них нет; ``None`` — авторы
        не указаны (список трудов без авторов) или фамилию эксперта не выделить.
    """
    surname = surname_of(expert_name)
    authors = [a for a in (authors or []) if a and str(a).strip()]
    if not surname or not authors:
        return None
    variants = {surname, translit(surname)}
    for author in authors:
        for token in _author_tokens(author):
            if any(_close(token, v) for v in variants):
                return True
    return False
