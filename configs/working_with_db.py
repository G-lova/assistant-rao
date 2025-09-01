import os
import logging
import json

import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Dict, Any, Optional
from psycopg2 import sql


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
    mapping = {
        "Объект экспертизы": "expertise_object",
        "Законодательное регулирование": "legal_regulation",
        "Способ закупки": "procurement_method",
        "Заявка на проведение экспертизы": "expertise_request",
        "Акт о приемке товара": "acceptance_act",
        "Дата контракта": "contract_date",
        "Документ о приемке и/или акт сдачи-приемки работ (услуг)": "works_acceptance_doc",
        "Документ о приемке товара (УПД, Счет-фактура и др.)": "goods_acceptance_doc",
        "Документация, подтверждающая невозможность использования иных способов определения поставщика": "impossible_alternative_doc",
        "Документы по взысканию пени и штрафов": "penalty_recovery_docs",
        "Документы, подтверждающие гарантийные обязательства": "warranty_docs",
        "Документы, подтверждающие исполнение всех условий контракта": "contract_conditions_docs",
        "Документы, подтверждающие передачу авторских прав": "ip_rights_transfer_docs",
        "Документы, подтверждающие страну происхождения товара": "goods_origin_docs",
        "Дополнительные материалы": "additional_materials",
        "Дополнительные соглашения к контракту": "contract_amendments",
        "Извещение": "notice",
        "Контракт на выполнение НИР (или НИОКР)": "nir_contract",
        "Контракт на выполнение работ (оказание услуг)": "service_contract",
        "Контракт на поставку товара": "goods_contract",
        "Материалы, подтверждающие Обоснование н(м)цк": "price_justification_docs",
        "Обоснование н(м)цк": "price_justification",
        "Описание объекта закупки": "procurement_description",
        "Отчет о выполнении НИР": "nir_report",
        "Положение о закупках организации": "procurement_policy",
        "Порядок рассмотрения и оценки заявок на конкурс": "bid_evaluation_procedure",
        "Предмет контракта": "contract_subject",
        "Проект контракта": "contract_draft",
        "Реквизиты контракта": "contract_details",
        "Сертификаты соответствия": "compliance_certificates",
        "Ссылка на ЕИС": "eis_link",
        "Техническая документация, паспорт товара и пр.": "technical_documentation",
        "Товарная накладная": "goods_invoice",
        "Требования к содержанию заявки на конкурс": "bid_requirements",
        "Фото результатов выполнения работ": "work_results_photos",
        "Фото товара": "goods_photos",
        "Экспертиза результатов исполнения контракта": "contract_execution_expertise"
    }
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

        # Подготавливаем данные
        data_to_save = full_analysis
        json_data = json.dumps(data_to_save, ensure_ascii=False)

        # Проверяем существование записи
        check_query = "SELECT 1 FROM raw_document_data WHERE procurement_id = %s"
        cursor.execute(check_query, (procurement_id,))
        exists = cursor.fetchone()

        if exists:
            query = sql.SQL("""
                UPDATE raw_document_data
                SET {column} = %s, updated_at = NOW()
                WHERE procurement_id = %s
            """).format(column=sql.Identifier(column_name))
            cursor.execute(query, (json_data, procurement_id))
        else:
            query = sql.SQL("""
                INSERT INTO raw_document_data (procurement_id, {column})
                VALUES (%s, %s)
            """).format(column=sql.Identifier(column_name))
            cursor.execute(query, (procurement_id, json_data))

        conn.commit()
        logger.info(f"Полный анализ сохранён в raw_document_data: {document_type} для procurement_id {procurement_id}")
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

        # Проверяем, существует ли запись
        check_query = "SELECT 1 FROM clean_document_conclusions WHERE procurement_id = %s"
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
        logger.info(f"Итоговое заключение сохранено: {document_type} для procurement_id {procurement_id}")
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
        
        # Получаем данные из обеих таблиц
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
            
        return result
        
    except Exception as e:
        logger.error(f"Ошибка при получении отчета: {str(e)}", exc_info=True)
        return {}
    finally:
        if conn:
            conn.close()