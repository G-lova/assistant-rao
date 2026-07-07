import asyncio
import asyncpg
import json
import logging
import psycopg2
from contextlib import asynccontextmanager
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from typing import Dict, Any, List

from configs.retry_utils import async_retry, sync_retry, DATABASE_RETRY_CONFIG
from configs.config import Config


logger = logging.getLogger(__name__)


@asynccontextmanager
async def get_async_db_connection():
    """Создаёт временный пул для каждой операции"""
    pool = await asyncpg.create_pool(
        host=Config.DB_HOST,
        port=int(Config.DB_PORT),
        database=Config.DB_NAME,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        min_size=1,
        max_size=4,
        max_inactive_connection_lifetime=300,
    )
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        await pool.close()

# _db_pool = None

# async def get_db_pool():
#     global _db_pool
#     if _db_pool is None:
#         _db_pool = await asyncpg.create_pool(
#             host=Config.DB_HOST,
#             port=int(Config.DB_PORT),
#             database=Config.DB_NAME,
#             user=Config.DB_USER,
#             password=Config.DB_PASSWORD,
#             min_size=2,
#             max_size=10,
#             max_inactive_connection_lifetime=300,
#         )
#     return _db_pool

# @asynccontextmanager
# async def get_async_db_connection():
#     pool = await get_db_pool()
#     async with pool.acquire() as conn:
#         yield conn

DOCUMENT_CODE_TO_COLUMN = {
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


@async_retry(DATABASE_RETRY_CONFIG)
async def save_raw_data(procurement_id: int, analysis: Dict[str, Any]):
    """
    Сохраняет полный анализ документа в таблицу сырых данных.

    Функция сериализует переданный словарь анализа в JSON и сохраняет его
    в соответствующий столбец таблицы `raw_document_data`, определяемый типом документа.
    Если запись с указанным procurement_id существует, данные обновляются;
    в противном случае создаётся новая запись.

    Args:
        procurement_id (int): Уникальный идентификатор закупки.
        document_code (str): Тип документа (например, 'contract', 'act'), используется для определения целевого столбца.
        analysis (Dict[str, Any]): Словарь с полным результатом анализа документа, включая извлечённые данные и метаинформацию.

    Raises:
        Исключения логируются, транзакция откатывается при ошибке.
    """
    procurement_id = int(procurement_id)
    try:
        async with get_async_db_connection() as conn:
            
            updates = {}
            codes = []

            for item in analysis:
                column = DOCUMENT_CODE_TO_COLUMN.get(item["doc_code"])
                if column is None:
                    continue

                codes.append(item["doc_code"])

                updates[column] = json.dumps(
                    item,
                    ensure_ascii=False,
                    indent=2
                )

            set_parts = []
            values = []

            for i, (column, value) in enumerate(updates.items(), start=1):
                set_parts.append(f'"{column}" = ${i}')
                values.append(value)

            # updated_at
            set_parts.append("updated_at = NOW()")

            sql = f"""
                UPDATE raw_document_data
                SET {", ".join(set_parts)}
                WHERE procurement_id = ${len(values)+1}
            """

            values.append(procurement_id)

            await conn.execute(sql, *values)

            for code in codes:
                logger.info(f"Сохранено в raw_document_data: {code} для procurement_id={procurement_id}")

    except Exception as e:
        logger.error(f"Ошибка при сохранении raw_data: {str(e)}", exc_info=True)
        raise

async def get_async_summary_report_from_db(procurement_id: str):
    """
    Асинхронно извлекает summary_report из БД по procurement_id.
    """
    try:
        procurement_id_int = int(procurement_id)
        async with get_async_db_connection() as conn:
            row = await conn.fetchrow(
                "SELECT summary_report FROM raw_document_data WHERE procurement_id = $1",
                procurement_id_int
            )
            if not row or not row["summary_report"]:
                logger.warning(f"Данные summary_report не найдены для procurement_id={procurement_id}")
                return None
            return row["summary_report"]

    except Exception as e:
        logger.error(f"Ошибка при получении данных из БД: {str(e)}", exc_info=True)
        return None

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
        logger.info(data)
        
        # logger.info(f'data.get("documents", [])={data.get("documents", [])}')
        # Извлечение данных из сводного отчета
        for item in data.get('documents', []):
            raw_data = item.get('raw_data', {})
            for r in raw_data:
                if ('контракт' in r.get('document_name', '').lower()) or ('договор' in r.get('document_name', '').lower()):
                    contract_info = {
                        'contract_number': str(r.get('contract_number', '0'))
                    }
                    amounts = r.get('amounts', [])
                    dates = r.get('dates', [])
                    contract_info['amount'] = str(amounts[0].get('value', '0'))
                    contract_info['date'] = str(dates[0].get('value', '0'))
                    
                    return contract_info
        logger.warning(f"Данные контракта не найдены для procurement_id={procurement_id}")    
        return {"contract_number": "0", "amount": "0", "date": "0"}

    except Exception as e:
        logger.error(f"Ошибка при получении данных о контракте: {str(e)}", exc_info=True)
        return {"contract_number": "0", "amount": "0", "date": "0"}


async def save_summary_report(procurement_id: int, summary_data: Dict[str, Any]):
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
        async with get_async_db_connection() as conn:
            # Преобразуем данные в JSON
            json_data = json.dumps(summary_data, ensure_ascii=False, indent=2)

            # Логируем ключевую информацию для отладки
            logger.info(f"Сохранение summary_report для {procurement_id}")
            logger.info(f"- Всего документов: {len(summary_data.get('documents', {}))}")
            codes = set([f"{doc.get('document_code')}: {doc.get('document_name')}" for doc in summary_data.get('documents', {})])
            logger.info(f"- Document codes:")
            for code in codes:
                logger.info(f"  -- {code}")

            # Проверка существования записи (asyncpg: $1 вместо %s)
            check_result = await conn.fetchrow(
                "SELECT id FROM raw_document_data WHERE procurement_id = $1",
                procurement_id
            )

            if check_result:
                # UPDATE
                await conn.execute("""
                    UPDATE raw_document_data 
                    SET summary_report = $1, updated_at = NOW() 
                    WHERE procurement_id = $2
                """, json_data, procurement_id)
            else:
                # INSERT
                await conn.execute("""
                    INSERT INTO raw_document_data (procurement_id, summary_report) 
                    VALUES ($1, $2)
                """, procurement_id, json_data)

            logger.info(f"Сводный отчет сохранен для procurement_id={procurement_id}")

    except Exception as e:
        logger.error(f"Ошибка при сохранении summary_report: {str(e)}", exc_info=True)
        raise


@async_retry(DATABASE_RETRY_CONFIG)
async def delete_procurement_data(procurement_id: int):
    """_summary_

    Args:
        procurement_id (int): _description_
    """
    procurement_id = int(procurement_id)
    try:
        async with get_async_db_connection() as conn:
            # Выполняем удаление в рамках одной транзакции
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM raw_document_data WHERE procurement_id = $1",
                    procurement_id
                )
                # При необходимости раскомментируйте удаление из второй таблицы:
                # await conn.execute(
                #     "DELETE FROM clean_document_conclusions WHERE procurement_id = $1",
                #     procurement_id
                # )
            
            logger.info(f"Все данные для procurement_id={procurement_id} успешно удалены.")

    except Exception as e:
        logger.error(f"Ошибка при удалении данных для procurement_id={procurement_id}: {str(e)}", exc_info=True)
        raise