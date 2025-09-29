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
