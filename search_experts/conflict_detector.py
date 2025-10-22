import itertools
from fuzzywuzzy import fuzz


class ConflictDetector:
    """
    Утилита для обнаружения потенциальных конфликтов интересов между экспертами и участниками закупки.

    Класс предоставляет методы для сравнения наименований экспертов, организаций и контрагентов
    с использованием нечёткого поиска (fuzzy matching). Позволяет выявлять случаи, когда эксперт
    может быть связан с исполнителем или заказчиком, что может повлиять на объективность экспертизы.
    """
    
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
        surnames = group[["expert_id", "expert_surname"]].dropna()
        to_exclude = set()

        for i, s in zip(surnames["expert_id"], surnames["expert_surname"]):
            if (
                s == expertise_surname
                or (abs(len(s) - len(expertise_surname)) == 1 and (expertise_surname in s or s in expertise_surname))
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