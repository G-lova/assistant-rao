import asyncio
import os
import asyncpg
import json
import logging
import psycopg2
from contextlib import asynccontextmanager
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from typing import Any, Dict, List, Optional

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
        "linkDocs": "eis_link",

        "consistency_check": "consistency_check",
        "summary_report": "summary_report",
        "unknown": "unknown"
}


# Колонки таблицы raw_document_data по типам документов (init.sql). consistency_check и summary_report — не типы
# документов: consistency_check в таблице нет, summary_report пишется отдельно.
RAW_DOC_COLUMNS = sorted({c for c in DOCUMENT_CODE_TO_COLUMN.values() if c not in ("consistency_check", "summary_report")})


def _env(environment: Optional[str]) -> str:
    """Окружение строки ``raw_document_data`` (``X-API-Database``), нормализованное как в хранилище знаний.

    Если окружение не передано (вызов не из API), берётся ``PE_LEGACY_ENV`` (по умолчанию ``dev``) с предупреждением.

    Args:
        environment: Окружение.

    Returns:
        str: Нормализованное окружение.

    Raises:
        ValueError: Недопустимое имя окружения.
    """
    from knowledge_store.scope import normalize_env

    if not environment:
        fallback = os.getenv("PE_LEGACY_ENV", "dev")
        logger.warning(f"raw_document_data: окружение не передано, используется {fallback}")
        environment = fallback
    return normalize_env(environment)


def _json(value: Any) -> str:
    """JSON для колонки JSONB (без нулевых символов: Postgres их не принимает)."""
    from knowledge_store.documents import strip_nul
    return json.dumps(strip_nul(value), ensure_ascii=False, indent=2, default=str)


def document_columns(analysis: Any) -> Dict[str, Optional[str]]:
    """Значения колонок по типам документов: найденные в анализе — JSON, остальные — ``NULL``.

    Все колонки перечисляются явно, поэтому тип документа, которого в новом прогоне нет, не остаётся от прошлого.

    Args:
        analysis: Список результатов по типам документов (элементы с ``doc_code``).

    Returns:
        Dict[str, Optional[str]]: Колонка → JSON или ``None``.
    """
    values: Dict[str, Optional[str]] = {c: None for c in RAW_DOC_COLUMNS}
    for item in analysis or []:
        column = DOCUMENT_CODE_TO_COLUMN.get((item or {}).get("doc_code"))
        if column in values:
            values[column] = _json(item)
    return values


@async_retry(DATABASE_RETRY_CONFIG)
async def save_evaluation_result(procurement_id: int, environment: Optional[str], analysis: Any,
                                 summary_data: Dict[str, Any]) -> None:
    """Атомарно сохраняет результат ``/evaluate-documents``: колонки по типам документов и ``summary_report``.

    Одна запись ``INSERT … ON CONFLICT (env, procurement_id) DO UPDATE`` после успешного прогона. Прежний результат
    не удаляется заранее: если прогон упал, он остаётся.

    Args:
        procurement_id: ID экспертизы.
        environment: Окружение (``X-API-Database``).
        analysis: Результаты по типам документов (``data_for_final_evaluation["documents"]``).
        summary_data: Итоговый отчёт ``ConsistencyChecker``.
    """
    env, procurement_id = _env(environment), int(procurement_id)
    columns = document_columns(analysis)
    names = list(columns) + ["summary_report"]
    values = list(columns.values()) + [_json(summary_data)]
    placeholders = ", ".join(f"${i}" for i in range(3, len(names) + 3))
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in names)
    sql = f"""
        INSERT INTO raw_document_data (env, procurement_id, {", ".join(f'"{c}"' for c in names)})
        VALUES ($1, $2, {placeholders})
        ON CONFLICT (env, procurement_id) DO UPDATE SET {updates}, updated_at = NOW()
    """
    async with get_async_db_connection() as conn:
        await conn.execute(sql, env, procurement_id, *values)
    found = [c for c, v in columns.items() if v is not None]
    logger.info(f"raw_document_data {env}:{procurement_id}: сохранены summary_report и колонки {found}")


@async_retry(DATABASE_RETRY_CONFIG)
async def save_raw_data(procurement_id: int, analysis: Dict[str, Any], environment: Optional[str] = None):
    """Сохраняет только колонки по типам документов (строка создаётся, если её нет).

    Пайплайн ``/evaluate-documents`` использует :func:`save_evaluation_result`; функция оставлена для
    совместимости.

    Args:
        procurement_id: ID экспертизы.
        analysis: Результаты по типам документов (элементы с ``doc_code``).
        environment: Окружение (``X-API-Database``).
    """
    env, procurement_id = _env(environment), int(procurement_id)
    columns = document_columns(analysis)
    names = list(columns)
    placeholders = ", ".join(f"${i}" for i in range(3, len(names) + 3))
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in names)
    async with get_async_db_connection() as conn:
        await conn.execute(f"""
            INSERT INTO raw_document_data (env, procurement_id, {", ".join(f'"{c}"' for c in names)})
            VALUES ($1, $2, {placeholders})
            ON CONFLICT (env, procurement_id) DO UPDATE SET {updates}, updated_at = NOW()
        """, env, procurement_id, *columns.values())


async def get_async_summary_report_from_db(procurement_id: str, environment: Optional[str] = None):
    """Асинхронно извлекает ``summary_report`` по экспертизе.

    С окружением читается строка этого окружения. Без окружения (вызывающая система не передала
    ``X-API-Database``) — самая свежая строка с этим ``procurement_id`` в любом окружении, как до миграции 008;
    если таких окружений несколько, пишется предупреждение.

    Args:
        procurement_id: ID экспертизы.
        environment: Окружение или ``None``.

    Returns:
        Optional[str]: JSON отчёта или ``None``.
    """
    try:
        procurement_id_int = int(procurement_id)
        async with get_async_db_connection() as conn:
            if environment:
                row = await conn.fetchrow(
                    "SELECT summary_report FROM raw_document_data WHERE env = $1 AND procurement_id = $2",
                    _env(environment), procurement_id_int)
            else:
                rows = await conn.fetch(
                    "SELECT env, summary_report FROM raw_document_data WHERE procurement_id = $1 "
                    "ORDER BY updated_at DESC NULLS LAST, id DESC", procurement_id_int)
                envs = {r["env"] for r in rows}
                if len(envs) > 1:
                    logger.warning(f"raw_document_data: для {procurement_id} нет X-API-Database, есть строки окружений "
                                   f"{sorted(envs)} — взята самая свежая ({rows[0]['env']})")
                row = rows[0] if rows else None
            if not row or not row["summary_report"]:
                logger.warning(f"Данные summary_report не найдены для procurement_id={procurement_id}")
                return None
            return row["summary_report"]

    except Exception as e:
        logger.error(f"Ошибка при получении данных из БД: {str(e)}", exc_info=True)
        return None


async def get_contract_info_from_db(procurement_id: str, environment: Optional[str] = None) -> Dict[str, str]:
    """
    Извлекает основные реквизиты контракта из базы данных по идентификатору закупки.

    Функция обращается к таблице `raw_document_data`, получает данные из поля `summary_report`
    и извлекает оттуда номер контракта, сумму (НМЦК или сумма контракта) и дату контракта.
    Если данные отсутствуют или произошла ошибка — возвращает значения по умолчанию ("0").

    Args:
        procurement_id (str): Уникальный идентификатор закупки.
        environment (Optional[str]): Окружение (``X-API-Database``); ``None`` — см.
            :func:`get_async_summary_report_from_db`.

    Returns:
        Dict[str, str]: Словарь с ключами:
            - "contract_number" (str): Номер контракта или "0", если не найден.
            - "amount" (str): Сумма контракта в виде строки или "0", если не найдена.
            - "date" (str): Дата контракта в текстовом формате или "0", если не найдена.
    """
    try:
        data = await get_async_summary_report_from_db(procurement_id, environment)
        if not data:
            return {"contract_number": "0", "amount": "0", "date": "0"}
        data = json.loads(data) if isinstance(data, (str, bytes)) else data   # asyncpg отдаёт JSONB строкой
        logger.info(data)
        
        # Извлечение данных из сводного отчета
        for item in data.get('documents', []):
            raw_data = item.get('raw_data', {})
            for r in raw_data:
                if ('контракт' in r.get('document_name', '').lower()) or ('договор' in r.get('document_name', '').lower()):
                    contract_info = {
                        'contract_number': str(r.get('contract_number', '0'))
                    }
                    finances = r.get('finances', [])
                    dates = r.get('dates', [])
                    contract_info['amount'] = str(finances[0].get('value', '0'))
                    contract_info['date'] = str(dates[0].get('value', '0'))
                    
                    return contract_info
        logger.warning(f"Данные контракта не найдены для procurement_id={procurement_id}")    
        return {"contract_number": "0", "amount": "0", "date": "0"}

    except Exception as e:
        logger.error(f"Ошибка при получении данных о контракте: {str(e)}", exc_info=True)
        return {"contract_number": "0", "amount": "0", "date": "0"}


@async_retry(DATABASE_RETRY_CONFIG)
async def save_summary_report(procurement_id: int, summary_data: Dict[str, Any], environment: Optional[str] = None):
    """Сохраняет только ``summary_report`` (строка создаётся, если её нет).

    Пайплайн ``/evaluate-documents`` использует :func:`save_evaluation_result`; функция оставлена для
    совместимости.

    Args:
        procurement_id: ID экспертизы.
        summary_data: Сводный отчёт.
        environment: Окружение (``X-API-Database``).
    """
    env, procurement_id = _env(environment), int(procurement_id)
    async with get_async_db_connection() as conn:
        await conn.execute("""
            INSERT INTO raw_document_data (env, procurement_id, summary_report) VALUES ($1, $2, $3)
            ON CONFLICT (env, procurement_id) DO UPDATE SET summary_report = EXCLUDED.summary_report, updated_at = NOW()
        """, env, procurement_id, _json(summary_data))
    logger.info(f"Сводный отчет сохранен для {env}:{procurement_id}")


@async_retry(DATABASE_RETRY_CONFIG)
async def delete_procurement_data(procurement_id: int, environment: Optional[str] = None):
    """Удаляет результат ``/evaluate-documents`` экспертизы в окружении.

    Пайплайн больше не удаляет строку перед прогоном (запись атомарная, :func:`save_evaluation_result`).

    Args:
        procurement_id: ID экспертизы.
        environment: Окружение (``X-API-Database``).
    """
    env, procurement_id = _env(environment), int(procurement_id)
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM raw_document_data WHERE env = $1 AND procurement_id = $2", env, procurement_id)
    logger.info(f"Данные {env}:{procurement_id} удалены из raw_document_data")
