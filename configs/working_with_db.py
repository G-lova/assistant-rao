import os
import logging
import json

import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Dict, Any, Optional
from psycopg2 import sql

from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING


logger = logging.getLogger(__name__)


def get_db_connection():
    """
    Устанавливает и возвращает соединение с базой данных PostgreSQL.

    Функция считывает параметры подключения из переменных окружения
    и устанавливает соединение с использованием библиотеки psycopg2.
    Кодировка клиента устанавливается в UTF-8 для корректной работы с кириллицей.

    Returns:
        psycopg2.extensions.connection: Объект соединения с базой данных PostgreSQL.
    """
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        client_encoding='UTF8'
    )


def map_document_type_to_column(document_type: str) -> str:
    """
    Сопоставляет тип документа с именем колонки в таблице.
    
    Args:
        document_type (str): Тип документа (например, "Извещение")
        
    Returns:
        str: Имя колонки в таблице БД
    """
    mapping = DOCUMENT_TYPE_MAPPING
    return mapping.get(document_type, "additional_materials")  # fallback


def save_raw_data(procurement_id: int, document_type: str, full_analysis: Dict[str, Any]):
    """
    Сохраняет полный анализ документа в таблицу сырых данных.

    Функция сериализует переданный словарь анализа в JSON и сохраняет его
    в соответствующий столбец таблицы `raw_document_data`, определяемый типом документа.
    Если запись с указанным procurement_id существует, данные обновляются;
    в противном случае создаётся новая запись.

    Args:
        procurement_id (int): Уникальный идентификатор закупки.
        document_type (str): Тип документа (например, 'contract', 'act'), используется для определения целевого столбца.
        full_analysis (Dict[str, Any]): Словарь с полным результатом анализа документа, включая извлечённые данные и метаинформацию.

    Raises:
        Исключения логируются, транзакция откатывается при ошибке.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        column_name = map_document_type_to_column(document_type)
        if not column_name:
            raise ValueError(f"Неизвестный тип документа: {document_type}")

        # Преобразуем данные в JSON
        json_data = json.dumps(full_analysis, ensure_ascii=False, indent=2)

        # Проверяем, существует ли уже запись с таким procurement_id
        check_query = "SELECT id FROM raw_document_data WHERE procurement_id = %s"
        cursor.execute(check_query, (procurement_id,))
        exists = cursor.fetchone()

        if exists:
            # Обновляем конкретное поле
            query = sql.SQL("""
                UPDATE raw_document_data 
                SET {column} = %s, updated_at = NOW() 
                WHERE procurement_id = %s
            """).format(column=sql.Identifier(column_name))
            cursor.execute(query, (json_data, procurement_id))
        else:
            # Вставляем новую запись, только с одним заполненным полем
            query = sql.SQL("""
                INSERT INTO raw_document_data (procurement_id, {column}) 
                VALUES (%s, %s)
            """).format(column=sql.Identifier(column_name))
            cursor.execute(query, (procurement_id, json_data))

        conn.commit()
        logger.info(f"Сохранено в raw_document_data: {document_type} для procurement_id={procurement_id}")

    except Exception as e:
        logger.error(f"Ошибка при сохранении raw_data: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def save_clean_conclusion(procurement_id: int, document_type: str, conclusion: str):
    """
    Сохраняет очищенное текстовое заключение по документу в таблицу выводов.

    Функция сохраняет строку с итоговым заключением в столбец таблицы `clean_document_conclusions`,
    соответствующий типу документа. Если запись с таким procurement_id уже существует — обновляет её,
    иначе создает новую запись.

    Args:
        procurement_id (int): Уникальный идентификатор закупки.
        document_type (str): Тип документа, определяющий целевой столбец через маппинг.
        conclusion (str): Текстовое заключение по результатам анализа документа.

    Raises:
        Исключения логируются, транзакция откатывается при ошибке.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        column_name = map_document_type_to_column(document_type)
        if not column_name:
            raise ValueError(f"Неизвестный тип документа: {document_type}")

        # Проверяем существование записи
        check_query = "SELECT id FROM clean_document_conclusions WHERE procurement_id = %s"
        cursor.execute(check_query, (procurement_id,))
        exists = cursor.fetchone()

        if exists:
            query = sql.SQL("""
                UPDATE clean_document_conclusions 
                SET {column} = %s, updated_at = NOW() 
                WHERE procurement_id = %s
            """).format(column=sql.Identifier(column_name))
            cursor.execute(query, (conclusion, procurement_id))
        else:
            query = sql.SQL("""
                INSERT INTO clean_document_conclusions (procurement_id, {column}) 
                VALUES (%s, %s)
            """).format(column=sql.Identifier(column_name))
            cursor.execute(query, (procurement_id, conclusion))

        conn.commit()
        logger.info(f"Заключение сохранено: {document_type} для procurement_id={procurement_id}")

    except Exception as e:
        logger.error(f"Ошибка при сохранении clean_conclusion: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def get_procurement_report(procurement_id: int) -> Dict[str, Any]:
    """
    Получает полный отчет по закупке из обеих таблиц.
    
    Args:
        procurement_id (int): ID закупки
        
    Returns:
        Dict[str, Any]: Объединенные данные из raw_document_data и clean_document_conclusions
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        raw_query = "SELECT * FROM raw_document_data WHERE procurement_id = %s"
        clean_query = "SELECT * FROM clean_document_conclusions WHERE procurement_id = %s"

        cursor.execute(raw_query, (procurement_id,))
        raw_data = cursor.fetchone()

        cursor.execute(clean_query, (procurement_id,))
        clean_data = cursor.fetchone()

        result = {}
        if raw_data:
            result["raw_data"] = dict(raw_data)
        if clean_data:
            result["clean_data"] = dict(clean_data)

        return result if result else None

    except Exception as e:
        logger.error(f"Ошибка при получении отчёта: {str(e)}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()


def get_raw_data_by_procurement_id(procurement_id: str) -> Dict[str, Any]:
    """
    Получает сырые извлечённые данные по идентификатору закупки из базы данных.

    Функция выполняет запрос к таблице `raw_document_data`, извлекает строку с указанным `procurement_id`
    и преобразует содержимое столбцов с документами в словарь, где ключ — нормализованный тип документа,
    а значение — соответствующие извлечённые данные в формате JSON. Служебные поля (id, даты и т.п.) игнорируются.

    Args:
        procurement_id (str): Уникальный идентификатор закупки, для которой запрашиваются данные.

    Returns:
        Dict[str, Any]: Словарь с данными, где:
            - ключ: строковое название типа документа (например, "Техническое задание", "Извещение"),
            - значение: структурированные данные документа (вложенные словари/списки), извлечённые ранее.
            Если запись не найдена или произошла ошибка — возвращается пустой словарь.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        query = "SELECT * FROM raw_document_data WHERE procurement_id = %s;"
        cursor.execute(query, (procurement_id,))
        row = cursor.fetchone()

        if not row:
            return {}

        data = {}
        row_dict = dict(row)  # Убедимся, что работаем с dict
        for col_name, value in row_dict.items():
            # Пропускаем служебные колонки
            if col_name in ['id', 'procurement_id', 'created_at', 'updated_at']:
                continue
            if value is None:
                continue

            # Находим соответствующий тип документа по маппингу
            doc_type = next(
                (k for k, v in DOCUMENT_TYPE_MAPPING.items() if v == col_name),
                col_name
            )
            data[doc_type] = value
        return data

    except Exception as e:
        logger.error(f"Ошибка чтения данных из БД: {str(e)}", exc_info=True)
        return {}
    finally:
        if conn:
            conn.close()