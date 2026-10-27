import itertools
import re
from difflib import SequenceMatcher

from fuzzywuzzy import fuzz


# Организационно-правовая форма (ОПФ) в полной записи. Слова ОПФ согласуются с «учреждение / предприятие /
# организация» и стоят в среднем роде («государственное бюджетное образовательное учреждение»), тогда как
# собственное имя организации — в мужском или женском («государственный университет», «государственная академия»).
# Поэтому снимаются только формы среднего рода: «государственный» из названия вуза остаётся и различает,
# например, «Воронежский государственный университет» и «Воронежский государственный университет инженерных технологий».
_OPF_PHRASES_RE = re.compile(
    r"\b(?:"
    r"высшего\s+(?:профессионального\s+)?образования"
    r"|среднего\s+профессионального\s+образования"
    r"|дополнительного\s+(?:профессионального\s+)?образования"
    r"|учреждение\s+науки"
    r"|общество\s+с\s+ограниченной\s+ответственностью"
    r"|(?:публичное\s+|закрытое\s+|открытое\s+)?акционерное\s+общество"
    r")\b"
)
_OPF_WORDS = {
    "федеральное", "государственное", "муниципальное", "бюджетное", "автономное", "казенное", "унитарное",
    "некоммерческое", "частное", "негосударственное", "образовательное", "общеобразовательное", "научное",
    "профессиональное", "учреждение", "предприятие",
}
# Сокращённые ОПФ (в т. ч. частые опечатки вроде «фбгоу»)
_OPF_ABBR = {
    "фгбоу", "фгаоу", "фгоу", "фбгоу", "фгбу", "фгбун", "фгау", "фгао", "фгб", "фгуп", "фку", "фгку", "фгбну",
    "гбоу", "гаоу", "гоу", "гбу", "гау", "гбпоу", "гапоу", "гбоуво", "мбоу", "мау", "мбу", "ано", "оано", "нчоу",
    "чоу", "оу", "во", "впо", "спо", "дпо", "ооо", "оао", "зао", "пао", "ао", "нко",
}
# Служебные слова, не различающие организации
_NOISE_TOKENS = {"и", "г", "город"}
# Маркеры обособленного подразделения: «X — филиал Y», «центр при Y» — это тот же работодатель, что и Y
_BRANCH_MARKERS = {"филиал", "при", "подразделение"}


def _org_tokens(text):
    """
    Приводит название организации к списку значимых слов («ядру») для сравнения.

    Нижний регистр, «ё» → «е», удаление кавычек и пунктуации, снятие полной и сокращённой ОПФ,
    «имени» → «им», отбрасывание однобуквенных инициалов и служебных слов.

    Args:
        text (str | Any): Название организации в произвольной записи; None/NaN — пустой список.

    Returns:
        list[str]: Слова ядра названия в исходном порядке.
    """
    if not isinstance(text, str):
        return []
    t = text.lower().replace("ё", "е")
    t = _OPF_PHRASES_RE.sub(" ", t)
    tokens = []
    for tok in re.split(r"[^\w]+", t):
        if not tok or tok in _OPF_WORDS or tok in _OPF_ABBR or tok in _NOISE_TOKENS or len(tok) == 1:
            continue
        tokens.append("им" if tok == "имени" else tok)
    return tokens


def _same_token(a, b):
    """
    Сравнивает два слова с допуском на опечатку.

    Слова равны, если совпадают полностью или (оба длиной от 5 символов) совпадают первые 3 буквы
    и сходство не ниже 0.85. Требование общего начала отсекает пары вроде «забайкальский» / «байкальский»,
    «кубанский» / «курганский», а опечатки («унивесритет», «эконимический») пропускает.

    Args:
        a (str): Первое слово.
        b (str): Второе слово.

    Returns:
        bool: True, если слова считаются одинаковыми.
    """
    if a == b:
        return True
    if len(a) < 5 or len(b) < 5 or a[:3] != b[:3]:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.85


class ConflictDetector:
    """
    Утилита для обнаружения потенциальных конфликтов интересов между экспертами и участниками закупки.

    Класс предоставляет методы для сравнения наименований экспертов, организаций и контрагентов
    с использованием нечёткого поиска (fuzzy matching). Позволяет выявлять случаи, когда эксперт
    может быть связан с исполнителем или заказчиком, что может повлиять на объективность экспертизы.
    """

    @staticmethod
    def same_organization(org_a, org_b):
        """
        Определяет, обозначают ли две записи названия одну и ту же организацию (или её филиал).

        В отличие от `fuzz.ratio` по целым строкам, не зависит от записи ОПФ: «ФГБОУ ВО "Тульский
        государственный университет"» и «ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ
        ВЫСШЕГО ОБРАЗОВАНИЯ «ТУЛЬСКИЙ ГОСУДАРСТВЕННЫЙ УНИВЕРСИТЕТ»» дают одно ядро.

        Правило: однословные ядра должны совпасть точно; иначе все слова более короткого ядра должны по порядку найтись в более длинном
        (с допуском на опечатку). Лишние слова длинного ядра допустимы, только если это:
            - хвост «им …» (название без почётного имени),
            - до трёх коротких (≤ 5 символов) слов в конце — аббревиатура («ринх», «ниу белгу», «фиц иу ран»),
            - всё совпавшее стоит после маркера «филиал» / «при» / «подразделение» — подразделение той же организации.
        Любое другое различающее слово («инженерных технологий», «омский» / «брянский») — разные организации.

        Args:
            org_a (str | Any): Первое название (например, организация эксперта).
            org_b (str | Any): Второе название (например, организация-заказчик или исполнитель).

        Returns:
            bool: True, если организации признаны совпадающими.
        """
        ta, tb = _org_tokens(org_a), _org_tokens(org_b)
        short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if len(short) < 2:
            # однословное ядро («ООО "Ромашка"») — только точное совпадение, иначе слишком много ложных пар
            return len(short) == 1 and short == long_ and len(short[0]) >= 4

        matched, pos = [], 0
        for tok in short:
            while pos < len(long_) and not _same_token(tok, long_[pos]):
                pos += 1
            if pos == len(long_):
                return False
            matched.append(pos)
            pos += 1

        extra = [i for i in range(len(long_)) if i not in set(matched)]
        if not extra:
            return True

        # хвост «им …»: всё лишнее стоит после «им», которого нет в коротком ядре
        if "им" in long_ and "им" not in short and min(extra) >= long_.index("им"):
            return True
        # аббревиатура в конце названия
        if extra == list(range(len(long_) - len(extra), len(long_))) and len(extra) <= 3 \
                and all(len(long_[i]) <= 5 for i in extra):
            return True
        # филиал / подразделение: совпавшая часть целиком после маркера
        for i, tok in enumerate(long_):
            if tok in _BRANCH_MARKERS and min(matched) > i:
                return True
        return False

    @staticmethod
    def find_organization_conflicts(group):
        """
        Выявляет экспертов, работающих в организации-заказчике экспертизы или у исполнителя контракта.

        Сравнивает организацию эксперта (`expert_organization`) с организацией заказчика
        (`expertise_organization`) и с исполнителем контракта (`exucutorContract`) методом `same_organization`.

        Args:
            group (pandas.DataFrame): Строки одной экспертизы со столбцами
                expert_id, expert_organization, expertise_organization, exucutorContract.

        Returns:
            set: Идентификаторы экспертов с конфликтом по месту работы; пустое множество, если таких нет.
        """
        to_exclude = set()
        for row in group[["expert_id", "expert_organization", "expertise_organization", "exucutorContract"]].itertuples(index=False):
            if ConflictDetector.same_organization(row.expert_organization, row.expertise_organization) \
                    or ConflictDetector.same_organization(row.expert_organization, row.exucutorContract):
                to_exclude.add(row.expert_id)
        return to_exclude

    @staticmethod
    def fuzzy_match(row):
        """
        Выполняет нечёткое сравнение строк для определения степени совпадения между сущностями.

        Метод рассчитывает коэффициенты схожести (от 0 до 100) между различными парами полей,
        такими как имя эксперта и название организации-участника, и возвращает максимальное значение.
        Используется библиотека `fuzzywuzzy` для расчёта расстояния Левенштейна.

        Args:
            row (dict): Строка данных (например, pandas.Series), содержащая поля:
                - expert_name: ФИО или название экспертной организации.
                - expertise_name: Наименование организации, проводящей экспертизу.
                - expert_organization: Организация, к которой относится эксперт.
                - expertise_organization: Организация, заказавшая экспертизу.
                - exucutorContract: Название исполнителя по контракту.
                - expert_diplom: Данные из диплома или сертификата эксперта.

        Returns:
            int: Максимальный процент совпадения среди всех проверяемых пар (от 0 до 100).
        """
        name_matches = [
            fuzz.ratio(row['expert_name'], row['expertise_name']),
            fuzz.ratio(row['expert_name'], row['expertise_organization']),
            fuzz.ratio(row['expert_name'], row['exucutorContract']),
            fuzz.ratio(row['expertise_name'], row['expert_organization']),
            fuzz.ratio(row['expertise_name'], row['expert_organization']),
            fuzz.ratio(row['expertise_name'], row['expert_diplom']),
            fuzz.ratio(row['expert_organization'], row['expertise_organization']),
            fuzz.ratio(row['expert_organization'], row['exucutorContract']),
        ]    
        return max(name_matches)

    @staticmethod
    def find_family_conflicts(group):
        """
        Выявляет возможные конфликты интересов на основе схожести фамилий экспертов и заказчика экспертизы.

        Функция сравнивает фамилию заказчика экспертизы (expertise_surname) с фамилиями экспертов в группе.
        Конфликт интересов считается возможным, если фамилия эксперта:
            - полностью совпадает с фамилией заказчика, ИЛИ
            - отличается от неё ровно на один символ по длине (например, на одну букву больше или меньше)
            и при этом одна фамилия содержится в другой как подстрока (например, "Иванов" и "Иванова").

        Такой подход позволяет выявлять потенциальные семейные связи, даже при наличии различий в окончаниях 
        (например, мужская/женская форма фамилии).

        Args:
            group (pandas.DataFrame): Группа строк, относящихся к одной экспертизе, содержащая следующие столбцы:
                - expertise_surname (str): Фамилия заказчика (или представителя организации-заказчика) экспертизы.
                - expert_id (int): Уникальный идентификатор эксперта.
                - expert_surname (str): Фамилия эксперта.

        Returns:
            set: Множество идентификаторов экспертов (expert_id), чьи фамилии признаны конфликтующими
                с фамилией заказчика экспертизы. Возвращается пустое множество, если конфликты не обнаружены.

        """
        expertise_surname = group['expertise_surname'].iloc[0]
        expertise_director = group['expertise_director'].iloc[0]
        surnames = group[["expert_id", "expert_surname"]].dropna()
        to_exclude = set()
    
        for i, s in zip(surnames["expert_id"], surnames["expert_surname"]):
            if (
                (s == expertise_surname
                or (abs(len(s) - len(expertise_surname)) == 1 and (expertise_surname in s or s in expertise_surname)))
                or (s == expertise_director
                    or (abs(len(s) - len(expertise_director)) == 1 and (expertise_director in s or s in expertise_director)))
            ):
                to_exclude.add(i)

        return to_exclude
    

    @staticmethod
    def find_nepotism(group):
        """
        Выявляет потенциальные случаи родственных связей между экспертами в рамках одной экспертизы
        на основе схожести их фамилий.

        Функция анализирует все возможные пары экспертов в переданной группе и определяет, имеют ли они
        «похожие» фамилии по следующим критериям:
            - фамилии полностью совпадают, ИЛИ
            - одна фамилия длиннее другой ровно на один символ и при этом короткая фамилия является подстрокой длинной
            (например, «Смирнов» и «Смирнова»).

        При обнаружении такой пары из двух экспертов исключается тот, у кого значение в поле `similarity_embeddings`
        ниже — предполагается, что это менее «релевантный» или менее квалифицированный эксперт
        (по результатам эмбеддинг-сравнения с требуемым профилем эксперта).

        Args:
            group (pandas.DataFrame): Группа строк, соответствующая одной экспертизе, содержащая столбцы:
                - expert_id: Уникальный идентификатор эксперта.
                - expert_surname: Фамилия эксперта (строка).
                - similarity_embeddings: Числовая метрика (например, косинусное сходство), отражающая степень
                соответствия эксперта требованиям экспертизы (чем выше — тем лучше).

        Returns:
            set: Множество идентификаторов экспертов (`expert_id`), подлежащих исключению из-за подозрения на непотизм.
                Возвращается пустое множество, если подозрительных пар не обнаружено.
        """
        surnames = group[["expert_id", "expert_surname", "similarity_embeddings"]].dropna()
        to_exclude = set()

        for (i1, s1, sim1), (i2, s2, sim2) in itertools.combinations(surnames.itertuples(index=False), 2):
            if (s1 == s2) or (len(s1) - len(s2) == 1 and (s2 in s1)) or (len(s2) - len(s1) == 1 and (s1 in s2)):
                if sim1 < sim2:
                    to_exclude.add(i1)
                else:
                    to_exclude.add(i2)

        return to_exclude