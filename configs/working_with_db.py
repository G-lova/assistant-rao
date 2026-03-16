import asyncio
import asyncpg
import json
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Dict, Any, List
from psycopg2 import sql

from configs.retry_utils import sync_retry, DATABASE_RETRY_CONFIG
from configs.config import Config


logger = logging.getLogger(__name__)

DOCUMENT_TYPE_MAPPING = {
        "docAcceptInafPostavFiles": "impossible_alternative_doc",
        "docActPriemTovFiles": "acceptance_act",
        "docAssetSelOrgFiles": "procurement_policy",
        "docCargoTaxFiles": "goods_invoice",
        "docCertValidFiles": "compliance_certificates",
        "docContractDoWorkFiles": "service_contract",
        "docContractNIRFiles": "nir_contract",
        "docContractPostTovarFiles": "goods_contract",
        "docDocPriemActSdachFiles": "works_acceptance_doc",
        "docDopConsentContractFiles": "contract_amendments",
        "docDopMaterialsFiles": "additional_materials",
        "docExpertReportFiles": "contract_execution_expertise",
        "docIzvejenieFiles": "notice",
        "docMaterialValidNMCKFiles": "price_justification_docs",
        "docObosnNMCKFiles": "price_justification",
        "docOpusObjectZacupFiles": "description_purchase_object",
        "docPhotoCargoFiles": "goods_photos",
        "docPhotoFinishWorkFiles": "work_results_photos",
        "docPorViewOcenkFiles": "bid_evaluation_procedure",
        "docPriemTovSchetFiles": "goods_acceptance_doc",
        "docProjContractFiles": "contract_draft",
        "docReportDoNIRFiles": "nir_report",
        "docTechDocFiles": "technical_documentation",
        "docTrebContentRequestFiles": "bid_requirements",
        "docValidAllIfFiles": "contract_conditions_docs",
        "docValidCopyriteFiles": "ip_rights_transfer_docs",
        "docValidCountyFiles": "goods_origin_docs",
        "docValidGarantFiles": "warranty_docs",
        "docVziskPenyFiles": "penalty_recovery_docs",

        "consistency_check": "consistency_check",
        "summary_report": "summary_report",
        "unknown": "unknown"
}


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


async def get_async_db_connection():
    """
    Устанавливает и возвращает соединение с базой данных PostgreSQL.

    Функция считывает параметры подключения из переменных окружения
    и устанавливает соединение с использованием библиотеки psycopg2.
    Кодировка клиента устанавливается в UTF-8 для корректной работы с кириллицей.

    Returns:
        psycopg2.extensions.connection: Объект соединения с базой данных PostgreSQL.
    """
    return await asyncpg.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        database=Config.DB_NAME,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD
    )


@sync_retry(DATABASE_RETRY_CONFIG)
def save_raw_data(procurement_id: int, document_code: str, analysis: List):
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
    procurement_id = int(procurement_id)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        column_name = DOCUMENT_TYPE_MAPPING.get(document_code, "unknown")
        if not column_name:
            raise ValueError(f"Неизвестный тип документа: {document_code}")

        # Преобразуем данные в JSON
        json_data = json.dumps(analysis, ensure_ascii=False, indent=2)

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
        logger.info(f"Сохранено в raw_document_data: {document_code} для procurement_id={procurement_id}")

    except Exception as e:
        logger.error(f"Ошибка при сохранении raw_data: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def get_summary_report_from_db(procurement_id: str):
    """
    Извлекает основные реквизиты контракта из базы данных по идентификатору закупки.

    Функция обращается к таблице `raw_document_data`, получает данные из поля `summary_report`.

    Args:
        procurement_id (str): Уникальный идентификатор закупки.

    Returns:
        Dict[str, str]: 
    """
    conn = None
    procurement_id = int(procurement_id)
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
            return None

        return result["summary_report"]

    except Exception as e:
        logger.error(f"Ошибка при получении данных о контракте: {str(e)}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()

async def get_async_summary_report_from_db(procurement_id: str):
    """
    Асинхронно извлекает summary_report из БД по procurement_id.
    """
    conn = None
    try:
        procurement_id_int = int(procurement_id)
        conn = await get_async_db_connection()

        row = await conn.fetchrow(
            "SELECT summary_report FROM raw_document_data WHERE procurement_id = $1",
            procurement_id_int
        )

        if not row or not row["summary_report"]:
            logger.warning(f"Данные summary_report не найдены для procurement_id={procurement_id}")
            return None

        # logger.info(f"summary_report: {row['summary_report']}")

        # asyncpg автоматически парсит JSONB → dict
        return row["summary_report"]

    except Exception as e:
        logger.error(f"Ошибка при получении данных из БД: {str(e)}", exc_info=True)
        return None
    finally:
        if conn:
            await conn.close()

async def get_contract_info_from_db(procurement_id: str) -> Dict[str, str]:
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
    try:
        data = await get_async_summary_report_from_db(procurement_id)
        if not data:
            return {"contract_number": "0", "amount": "0", "date": "0"}
        data = json.loads(data)
        
        # logger.info(f'data.get("documents", [])={data.get("documents", [])}')
        # Извлечение данных из сводного отчета
        for item in data.get('documents', []):
            raw_data = item.get('raw_data', {})
            if ('контракт' in raw_data.get('document_name', '').lower()) or ('договор' in raw_data.get('document_name', '').lower()):
                contract_info = {
                    'contract_number': str(raw_data.get('contract_number', '0'))
                }
                amounts = raw_data.get('amounts', [])
                dates = raw_data.get('dates', [])
                contract_info['amount'] = str(amounts[0].get('value', '0'))
                contract_info['date'] = str(dates[0].get('value', '0'))
                
                return contract_info
        logger.warning(f"Данные контракта не найдены для procurement_id={procurement_id}")    
        return {"contract_number": "0", "amount": "0", "date": "0"}

    except Exception as e:
        logger.error(f"Ошибка при получении данных о контракте: {str(e)}", exc_info=True)
        return {"contract_number": "0", "amount": "0", "date": "0"}


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
    procurement_id = int(procurement_id)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Преобразуем данные в JSON
        json_data = json.dumps(summary_data, ensure_ascii=False, indent=2)

        # Логируем ключевую информацию для отладки
        logger.info(f"Сохранение summary_report для {procurement_id}")
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
    """_summary_

    Args:
        procurement_id (int): _description_
    """
    conn = None
    procurement_id = int(procurement_id)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Удаляем из обеих таблиц
        cursor.execute("DELETE FROM raw_document_data WHERE procurement_id = %s", (procurement_id,))
        # cursor.execute("DELETE FROM clean_document_conclusions WHERE procurement_id = %s", (procurement_id,))

        conn.commit()
        logger.info(f"Все данные для procurement_id={procurement_id} успешно удалены.")
    except Exception as e:
        logger.error(f"Ошибка при удалении данных для procurement_id={procurement_id}: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()