import logging
import json

import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Dict, Any
from psycopg2 import sql

from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from configs.retry_utils import sync_retry, DATABASE_RETRY_CONFIG
from configs.config import Config


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
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        dbname=Config.DB_NAME,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
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


@sync_retry(DATABASE_RETRY_CONFIG)
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


@sync_retry(DATABASE_RETRY_CONFIG)
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

        # Для итогового заключения используем специальное поле
        if document_type == "consistency_check":
            column_name = "consistency_check"
        elif document_type == "completeness_check":
            column_name = "consistency_check"
        else:
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


@sync_retry(DATABASE_RETRY_CONFIG)
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
        row_dict = dict(row)
        for col_name, value in row_dict.items():
            # Пропускаем служебные колонки
            if col_name in ['id', 'procurement_id', 'created_at', 'updated_at']:
                continue
            if value is None:
                continue

            # Для JSONB колонок пытаемся распарсить JSON
            if col_name in ['eis_data', 'summary_report'] and isinstance(value, (str, dict)):
                try:
                    if isinstance(value, str):
                        parsed_value = json.loads(value)
                    else:
                        parsed_value = value
                    data[col_name] = parsed_value
                    continue
                except (json.JSONDecodeError, TypeError):
                    pass

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


def get_contract_info_from_db(procurement_id: str) -> Dict[str, str]:
    """
    Извлекает основные реквизиты контракта из базы данных по идентификатору закупки.

    Функция обращается к таблице `raw_document_data`, получает данные из поля `summary_report`
    и извлекает оттуда номер контракта, сумму (НМЦК или сумма контракта) и дату контракта.
    Если данные отсутствуют или произошла ошибка — возвращает значения по умолчанию ("0").

    Args:
        procurement_id (str): Уникальный идентификатор закупки.

    Returns:
        Dict[str, str]: Словарь с ключами:
            - "contract_number" (str): Номер контракта или "0", если не найден.
            - "amount" (str): Сумма контракта в виде строки или "0", если не найдена.
            - "date" (str): Дата контракта в текстовом формате или "0", если не найдена.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        query = """
            SELECT summary_report 
            FROM raw_document_data 
            WHERE procurement_id = %s
        """
        cursor.execute(query, (procurement_id,))
        result = cursor.fetchone()

        if not result or not result["summary_report"]:
            logger.warning(f"Данные summary_report не найдены для procurement_id={procurement_id}")
            return {"contract_number": "0", "amount": "0", "date": "0"}

        data = result["summary_report"]

        # Извлечение данных из сводного отчета
        contract_numbers = []
        amounts = []
        dates = []

        # Собираем все возможные значения из всех документов
        for doc_name, doc_data in data.get("documents_summary", {}).items():
            raw_data = doc_data.get("raw_data", {})
            
            # Номера контрактов
            if raw_data.get("contract_number") and raw_data["contract_number"] != "0":
                contract_numbers.append(raw_data["contract_number"])
            
            # Суммы
            for amt in raw_data.get("amounts", []):
                if isinstance(amt, dict) and amt.get("value") and amt.get("value") != "0":
                    field = amt.get("field", "").lower()
                    if any(keyword in field for keyword in ["нмцк", "сумма контракта", "цена", "стоимость"]):
                        amounts.append(amt["value"])
            
            # Даты
            for dt in raw_data.get("dates", []):
                if isinstance(dt, dict) and dt.get("value") and dt.get("value") != "0":
                    field = dt.get("field", "").lower()
                    if any(keyword in field for keyword in ["дата контракта", "дата заключения", "дата подписания"]):
                        dates.append(dt["value"])

        # Выбираем наиболее вероятные значения
        contract_number = contract_numbers[0] if contract_numbers else "0"
        amount = amounts[0] if amounts else "0"
        date = dates[0] if dates else "0"

        return {
            "contract_number": str(contract_number),
            "amount": str(amount),
            "date": str(date)
        }

    except Exception as e:
        logger.error(f"Ошибка при получении данных о контракте: {str(e)}", exc_info=True)
        return {"contract_number": "0", "amount": "0", "date": "0"}
    finally:
        if conn:
            conn.close()


def save_summary_report(procurement_id: int, summary_data: Dict[str, Any]):
    """
    Сохраняет сводный отчёт по закупке в базу данных.

    Функция сериализует переданный словарь с результатами анализа в JSON
    и сохраняет его в таблицу `raw_document_data` по уникальному идентификатору закупки.
    Если запись с таким `procurement_id` уже существует — обновляется поле `summary_report`,
    иначе создаётся новая запись. Время обновления автоматически устанавливается в NOW().

    Args:
        procurement_id (int): Уникальный идентификатор закупки.
        summary_data (Dict[str, Any]): Словарь с данными сводного отчёта (например, статусы документов, ошибки, рекомендации).

    Returns:
        None: Функция ничего не возвращает, но логирует успешное сохранение или ошибку.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Преобразуем данные в JSON
        json_data = json.dumps(summary_data, ensure_ascii=False, indent=2)

        # Логируем ключевую информацию для отладки
        logger.info(f"Сохранение summary_report для {procurement_id}:")
        logger.info(f"- Всего документов: {len(summary_data.get('documents_summary', {}))}")
        logger.info(f"- Document codes: {[doc.get('document_code') for doc in summary_data.get('documents_summary', {}).values()]}")
        logger.info(f"- Document labels: {[doc.get('document_label') for doc in summary_data.get('documents_summary', {}).values()]}")

        # Проверяем, существует ли уже запись с таким procurement_id
        check_query = "SELECT id FROM raw_document_data WHERE procurement_id = %s"
        cursor.execute(check_query, (procurement_id,))
        exists = cursor.fetchone()

        if exists:
            # Обновляем поле summary_report
            query = sql.SQL("""
                UPDATE raw_document_data 
                SET summary_report = %s, updated_at = NOW() 
                WHERE procurement_id = %s
            """)
            cursor.execute(query, (json_data, procurement_id))
        else:
            # Вставляем новую запись
            query = sql.SQL("""
                INSERT INTO raw_document_data (procurement_id, summary_report) 
                VALUES (%s, %s)
            """)
            cursor.execute(query, (procurement_id, json_data))

        conn.commit()
        logger.info(f"Сводный отчет сохранен для procurement_id={procurement_id}")

    except Exception as e:
        logger.error(f"Ошибка при сохранении summary_report: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


@sync_retry(DATABASE_RETRY_CONFIG)
def delete_procurement_data(procurement_id: int):
    """
    Удаляет все данные, связанные с закупкой, из таблиц БД с поддержкой повторных попыток.

    Выполняет транзакционное удаление записей из таблиц raw_document_data и
    clean_document_conclusions по указанному идентификатору закупки. В случае ошибки
    откатывает транзакцию и повторяет операцию в соответствии с настройками
    DATABASE_RETRY_CONFIG. Гарантирует согласованность данных при временных сбоях БД.

    Args:
        procurement_id (int): Числовой идентификатор закупки, все данные которой подлежат удалению.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Удаляем из обеих таблиц
        cursor.execute("DELETE FROM raw_document_data WHERE procurement_id = %s", (procurement_id,))
        cursor.execute("DELETE FROM clean_document_conclusions WHERE procurement_id = %s", (procurement_id,))

        conn.commit()
        logger.info(f"Все данные для procurement_id={procurement_id} успешно удалены.")
    except Exception as e:
        logger.error(f"Ошибка при удалении данных для procurement_id={procurement_id}: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()