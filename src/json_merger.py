import json
from collections import OrderedDict
from copy import deepcopy
from typing import Any, Dict, Optional

class JSONMerger:
    """Сервис для слияния JSON-документов с сохранением порядка полей"""
    
    # Разрешенные позитивные фразы ДЛЯ КОНТЕКСТА field
    POSITIVE_IN_FIELD_CONTEXT = {
        "нет", "no", "замечаний нет", "претензий нет", "ошибок нет",
        "соответствует", "да", "ok", "принято", "исполнено", "требований нет",
        "претензий не имеется", "все в порядке", ""
    }

    @staticmethod
    def is_field_context(field_path: str) -> bool:
        """
        Определяет, находится ли поле в контексте field-структур:
        - Любой путь содержащий 'field' (field3_2, field6_0_0)
        - Вложенные поля q1/q2 в таких структурах
        """
        if not field_path:
            return False
        return 'field' in field_path.lower()

    @staticmethod
    def is_negative(value: Any, field_path: str = "") -> bool:
        """
        Универсальная проверка отрицательных значений с учетом контекста field
        """
        in_field = JSONMerger.is_field_context(field_path)
        
        # Специальная обработка для boolean в контексте field
        if in_field and isinstance(value, bool):
            return value  # True = есть замечание (отрицательное), False = нет замечаний
        
        # Специальная обработка для чисел в контексте field
        if in_field and isinstance(value, (int, float)):
            return value == 0  # 1 = отрицательное, 0 = положительное
        
        # Обработка строк
        if isinstance(value, str):
            cleaned = value.strip().lower().replace('"', '').replace('\n', ' ')
            
            # Для контекста field: проверяем на разрешенные позитивные фразы
            if in_field:
                if cleaned in JSONMerger.POSITIVE_IN_FIELD_CONTEXT:
                    return False  # Это положительное значение
                return bool(cleaned)  # Любая непустая строка НЕ из списка = отрицательное
            
            # Для остальных полей: стандартная логика
            return cleaned in {"0", "false", "no", "нет", "", "null"}
        
        # Пустые коллекции всегда отрицательные
        if isinstance(value, (list, OrderedDict)) and not value:
            return True
        
        # Стандартные отрицательные значения
        return value in (False, 0, None, "null")

    @staticmethod
    def is_special_q_object(obj1: OrderedDict, obj2: OrderedDict, field_path: str) -> bool:
        """Проверяет, является ли объект специальным (q1/q2) в контексте field"""
        in_field = JSONMerger.is_field_context(field_path)
        has_q1_q2 = (
            isinstance(obj1, OrderedDict) and isinstance(obj2, OrderedDict) and
            "q1" in obj1 and "q2" in obj1 and "q1" in obj2 and "q2" in obj2
        )
        return in_field and has_q1_q2

    @staticmethod
    def merge_q_objects(obj1: OrderedDict, obj2: OrderedDict, field_path: str) -> OrderedDict:
        """
        Слияние объектов вида:
        {
            "q1": true,  // true = есть замечание (отрицательное)
            "q2": "Текст замечания"
        }
        Объединяет комментарии в ЛЮБОМ ПОРЯДКЕ через " или "
        """
        # Определяем отрицательные значения для q1
        neg1 = JSONMerger.is_negative(obj1["q1"], f"{field_path}.q1")
        neg2 = JSONMerger.is_negative(obj2["q1"], f"{field_path}.q1")
        
        # Правило (в): Оба отрицательные - объединяем ВСЕ валидные комментарии
        if neg1 and neg2:
            def clean_for_check(text: str) -> str:
                """Очистка текста для проверки на позитивные фразы"""
                if not isinstance(text, str):
                    return ""
                cleaned = text.strip().lower()
                # Удаляем знаки препинания в конце
                cleaned = cleaned.rstrip('.,;:!?()[]{}"\'')
                # Удаляем спецсимволы, оставляя буквы, цифры и пробелы
                cleaned = ''.join(ch for ch in cleaned if ch.isalnum() or ch.isspace())
                return cleaned.strip()
            
            # Собираем ВСЕ непустые комментарии из обоих объектов
            all_comments = []
            for src_obj in [obj1, obj2]:
                raw_comment = str(src_obj.get("q2", "") or "").strip()
                if raw_comment and clean_for_check(raw_comment) not in JSONMerger.POSITIVE_IN_FIELD_CONTEXT:
                    all_comments.append(raw_comment)
            
            # Убираем дубликаты (сохраняя порядок первого вхождения)
            unique_comments = []
            for comment in all_comments:
                if comment not in unique_comments:
                    unique_comments.append(comment)
            
            # Формируем объединенный комментарий
            merged_comment = " или ".join(unique_comments) if unique_comments else ""
            
            # Гарантируем правильный порядок полей q1 -> q2
            return OrderedDict([
                ("q1", True),  # Всегда отрицательное при объединении
                ("q2", merged_comment)
            ])
        
        # Правило (б): Разные значения - берем отрицательное
        if neg1 != neg2:
            source = obj1 if neg1 else obj2
            return OrderedDict(source)
        
        # Правило (а): Одинаковые значения - сохраняем первый объект
        return OrderedDict(obj1)

    @staticmethod
    def merge_primitives(val1: Any, val2: Any, field_path: str) -> Any:
        """Слияние примитивов с учетом контекста field"""
        
        # === СПЕЦОБРАБОТКА ДЛЯ ПОЛЯ inn ===
        if field_path.split('.')[-1] == 'inn':
            def to_inn_string(val):
                if isinstance(val, (int, float)):
                    if isinstance(val, float) and val.is_integer():
                        return str(int(val))
                    else:
                        try:
                            return str(int(val))
                        except (ValueError, OverflowError):
                            return format(val, 'f').rstrip('0').rstrip('.')
                elif isinstance(val, str):
                    return val
                else:
                    return str(val)
    
            str_val1 = to_inn_string(val1)
            str_val2 = to_inn_string(val2)
    
            in_field = JSONMerger.is_field_context(field_path)
            neg1 = JSONMerger.is_negative(str_val1, field_path)
            neg2 = JSONMerger.is_negative(str_val2, field_path)
    
            if in_field and neg1 and neg2:
                c1 = str_val1.strip()
                c2 = str_val2.strip()
                comments = []
                if c1 and c1.lower() not in JSONMerger.POSITIVE_IN_FIELD_CONTEXT:
                    comments.append(c1)
                if c2 and c2.lower() not in JSONMerger.POSITIVE_IN_FIELD_CONTEXT:
                    comments.append(c2)
                
                # Удаление дубликатов
                unique_comments = []
                for cmnt in comments:
                    if cmnt not in unique_comments:
                        unique_comments.append(cmnt)
                
                if unique_comments:
                    return unique_comments[0] if len(unique_comments) == 1 else " или ".join(unique_comments)
    
            if neg1 != neg2:
                return str_val1 if neg1 else str_val2
    
            return str_val1
    
        # === ОБЫЧНАЯ ЛОГИКА ДЛЯ ДРУГИХ ПОЛЕЙ ===
        in_field = JSONMerger.is_field_context(field_path)
        neg1 = JSONMerger.is_negative(val1, field_path)
        neg2 = JSONMerger.is_negative(val2, field_path)
        
        # Правило (в): Оба отрицательные и есть комментарии (только для контекста field)
        if in_field and neg1 and neg2 and isinstance(val1, str) and isinstance(val2, str):
            c1 = val1.strip()
            c2 = val2.strip()
            
            raw_comments = []
            if c1 and c1.lower() not in JSONMerger.POSITIVE_IN_FIELD_CONTEXT:
                raw_comments.append(c1)
            if c2 and c2.lower() not in JSONMerger.POSITIVE_IN_FIELD_CONTEXT:
                raw_comments.append(c2)
            
            # Удаление точных дубликатов (с учётом strip)
            unique_comments = []
            for cmnt in raw_comments:
                if cmnt not in unique_comments:
                    unique_comments.append(cmnt)
            
            if not unique_comments:
                return ""
            elif len(unique_comments) == 1:
                return unique_comments[0]
            else:
                return " или ".join(unique_comments)
        
        # Правило (б): Разные значения - берем отрицательное
        if neg1 != neg2:
            return val1 if neg1 else val2
        
        # Правило (а): Одинаковые или оба положительные - берем из первого JSON
        return val1
    
    
    @staticmethod
    def merge_root_objects(obj1: OrderedDict, obj2: OrderedDict, field_path: str) -> OrderedDict:
        """Слияние корневого уровня с сохранением порядка"""
        merged = OrderedDict()
        
        # 1. Поля из первого JSON в исходном порядке
        for key in obj1:
            full_path = f"{field_path}.{key}" if field_path else key
            if key in obj2:
                merged[key] = JSONMerger.merge_values(obj1[key], obj2[key], full_path)
            else:
                merged[key] = JSONMerger.deepcopy_as_ordered(obj1[key])
        
        # 2. Новые поля из второго JSON
        for key in obj2:
            if key not in obj1:
                merged[key] = JSONMerger.deepcopy_as_ordered(obj2[key])
        
        return merged

    @staticmethod
    def merge_ordered_dicts(dict1: OrderedDict, dict2: OrderedDict, field_path: str) -> OrderedDict:
        """Слияние OrderedDict с сохранением порядка ключей"""
        merged = OrderedDict()
        
        # 1. Поля из первого словаря
        for key in dict1:
            full_path = f"{field_path}.{key}" if field_path else key
            if key in dict2:
                merged[key] = JSONMerger.merge_values(dict1[key], dict2[key], full_path)
            else:
                merged[key] = JSONMerger.deepcopy_as_ordered(dict1[key])
        
        # 2. Новые поля из второго словаря
        for key in dict2:
            if key not in dict1:
                merged[key] = JSONMerger.deepcopy_as_ordered(dict2[key])
        
        return merged

    @staticmethod
    def merge_arrays(arr1: list, arr2: list, field_path: str) -> list:
        """Слияние массивов с сохранением порядка"""
        merged = []
        min_len = min(len(arr1), len(arr2))
        
        # Сливаем пересекающиеся элементы
        for i in range(min_len):
            full_path = f"{field_path}[{i}]"
            merged.append(JSONMerger.merge_values(arr1[i], arr2[i], full_path))
        
        # Добавляем остатки из первого массива
        for i in range(min_len, len(arr1)):
            merged.append(JSONMerger.deepcopy_as_ordered(arr1[i]))
        
        # Добавляем остатки из второго массива
        for i in range(min_len, len(arr2)):
            merged.append(JSONMerger.deepcopy_as_ordered(arr2[i]))
        
        return merged

    @staticmethod
    def deepcopy_as_ordered(value: Any) -> Any:
        """Глубокое копирование с преобразованием в OrderedDict"""
        if isinstance(value, OrderedDict):
            return OrderedDict((k, JSONMerger.deepcopy_as_ordered(v)) for k, v in value.items())
        if isinstance(value, dict):
            return OrderedDict((k, JSONMerger.deepcopy_as_ordered(v)) for k, v in value.items())
        if isinstance(value, list):
            return [JSONMerger.deepcopy_as_ordered(item) for item in value]
        return deepcopy(value)

    @staticmethod
    def merge_values(val1: Any, val2: Any, field_path: str = "", is_root: bool = False) -> Any:
        """Рекурсивное слияние с сохранением порядка и учетом контекста field"""
        # Обработка отсутствующих значений
        if val1 is None and val2 is None:
            return None
        if val1 is None:
            return JSONMerger.deepcopy_as_ordered(val2)
        if val2 is None:
            return JSONMerger.deepcopy_as_ordered(val1)
        
        # Разные типы
        if type(val1) != type(val2):
            neg1 = JSONMerger.is_negative(val1, field_path)
            neg2 = JSONMerger.is_negative(val2, field_path)
            if neg1 and not neg2:
                return JSONMerger.deepcopy_as_ordered(val1)
            if neg2 and not neg1:
                return JSONMerger.deepcopy_as_ordered(val2)
            return JSONMerger.deepcopy_as_ordered(val1)  # Приоритет первого при конфликте
        
        # Обработка корневого уровня
        if is_root and isinstance(val1, OrderedDict):
            return JSONMerger.merge_root_objects(val1, val2, field_path)
        
        # Обработка объектов
        if isinstance(val1, OrderedDict):
            # Специальная обработка для объектов с q1/q2 в контексте field
            if JSONMerger.is_special_q_object(val1, val2, field_path):
                return JSONMerger.merge_q_objects(val1, val2, field_path)
            return JSONMerger.merge_ordered_dicts(val1, val2, field_path)
        
        # Обработка массивов
        if isinstance(val1, list):
            return JSONMerger.merge_arrays(val1, val2, field_path)
        
        # Обработка примитивов
        return JSONMerger.merge_primitives(val1, val2, field_path)

    @classmethod
    def merge_jsons(cls, json1: OrderedDict, json2: OrderedDict) -> dict:
        """Основная функция слияния - возвращает обычный dict для сериализации"""
        merged_ordered = cls.merge_values(json1, json2, is_root=True)
        # Конвертируем в обычный словарь для JSON-сериализации
        return json.loads(json.dumps(merged_ordered))
