import os
import logging
from typing import List

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


# Настройка логгера
logger = logging.getLogger(__name__)


# Список допустимых колонок в таблице report_for_operator
ALLOWED_COLUMNS = {
    "expertise_object",
    "legal_regulation",
    "procurement_method",
    "expertise_request",
    "acceptance_act",
    "contract_date",
    "works_acceptance_doc",
    "goods_acceptance_doc",
    "impossible_alternative_doc",
    "penalty_recovery_docs",
    "warranty_docs",
    "contract_conditions_docs",
    "ip_rights_transfer_docs",
    "goods_origin_docs",
    "additional_materials",
    "contract_amendments",
    "notice",
    "nir_contract",
    "service_contract",
    "goods_contract",
    "price_justification_docs",
    "price_justification",
    "procurement_description",
    "nir_report",
    "procurement_policy",
    "bid_evaluation_procedure",
    "contract_subject",
    "contract_draft",
    "contract_details",
    "compliance_certificates",
    "eis_link",
    "technical_documentation",
    "goods_invoice",
    "bid_requirements",
    "work_results_photos",
    "goods_photos",
    "contract_execution_expertise",
}


def save_document_content_to_db(procurement_id: str, document_type: str, content: str) -> bool:
    if document_type.lower() not in ALLOWED_COLUMNS:
        logger.error(f"Invalid document_type: {document_type}")
        return False

    conn = None
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432")
        )

        with conn.cursor() as cursor:
            # Проверяем, существует ли запись
            cursor.execute("SELECT 1 FROM report_for_operator WHERE procurement_id = %s", (procurement_id,))
            if cursor.fetchone() is None:
                # Создаём новую запись
                cursor.execute(
                    "INSERT INTO report_for_operator (procurement_id) VALUES (%s)",
                    (procurement_id,)
                )
                logger.info(f"Created new record for procurement_id={procurement_id}")

            # Обновляем нужную колонку
            query = sql.SQL("""
                UPDATE report_for_operator 
                SET {} = %s 
                WHERE procurement_id = %s
            """).format(sql.Identifier(document_type.lower()))

            cursor.execute(query, (content, procurement_id))
            conn.commit()

            return True  # Мы либо вставили, либо обновили — успех

    except Exception as e:
        logger.error(f"Error saving to DB: {str(e)}", exc_info=True)
        return False

    finally:
        if conn:
            try:
                conn.close()
            except Exception as ex:
                logger.error(f"Error closing DB connection: {ex}")