import asyncio
import json
import numpy as np
import pandas as pd

from json_repair import repair_json

from configs.llm_resilience import call_llm
from configs.logger import get_logger
from typing import Any, Dict

from configs.utils import extract_json_objects, split_large_text
from evaluate_documents.final_digest import compact_input, count_tokens, package_facts
from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING

# Предел сводки комплекта для итоговой проверки (токенов); прежний предел обрезки входа был 14 500
DIGEST_MAX_TOKENS = 14000


logger = get_logger(__name__)


class ConsistencyChecker:
    """
    Выполняет финальную комплексную проверку согласованности, полноты и соответствия типов
    для пакета документов по закупке.

    Класс анализирует агрегированные данные по всем документам (включая тип, читаемость,
    извлечённые метаданные и локальную согласованность) и формирует:
        - итоговое заключение по всей закупке (`overall_status`, `overall_summary`),
        - детализированный отчёт по каждому документу с кодом, статусом и описанием проблем.

    Использует LLM с guided JSON для генерации структурированного финального решения,
    а также обеспечивает отказоустойчивость при ошибках парсинга.
    """
    def __init__(self, llm_client, model):
        """
        Инициализирует проверяющий модуль с клиентом LLM и названием модели.

        Загружает:
            - системный промпт из файла `prompts/consistency_prompt.txt`,
            - JSON-схему (в виде строки) из `schemas/consistency_schema.json`
              для использования с параметром `guided_json`.

        Args:
            llm_client: Экземпляр клиента LLM (совместимого с OpenAI API),
                        поддерживающего метод `chat.completions.create`.
            model (str): Название модели LLM.
        """
        self.client = llm_client
        self.model = model

        with open("prompts/consistency_prompt.txt") as f:
            self.consistency_prompt = f.read()

        with open("schemas/consistency_schema.json") as f:
            self.CONSISTENCY_SCHEMA = f.read()


    async def check_consistency(self, content: str) -> Dict[str, Any]:
        """
        Проводит финальный анализ согласованности данных по закупке и возвращает итоговое заключение.

        Метод передаёт агрегированные данные (например, JSON с извлечёнными полями из всех документов)
        в LLM, которая оценивает их на предмет:
            - внутренней логической непротиворечивости,
            - соответствия между разными документами (например, сумма в контракте = сумма в акте),
            - наличия критически важных пропусков.

        Ожидаемый формат ответа модели:
            {
                "overall_status": "allow" | "deny",
                "overall_summary": "Текстовое обоснование решения"
            }

        В случае ошибок парсинга реализованы два уровня резерва:
            1. Поиск JSON-структур в неформатированном ответе с помощью `extract_json_objects`.
            2. Ручной парсинг через разбиение строки по кавычкам (экстренная мера).

        Args:
            content (str): Строка с агрегированными данными по закупке (обычно JSON или
                           структурированный текст для анализа).

        Returns:
            Dict[str, Any]: Словарь с итоговым заключением, содержащий:
                - "overall_status" (str): "allow" — данные согласованы, "deny" — обнаружены проблемы,
                - "overall_summary" (str): Текстовое обоснование решения.
                В случае полного сбоя возвращается резервный словарь с пометкой об ошибке.

        Raises:
            Исключения не пробрасываются — все ошибки обрабатываются внутри метода,
            гарантируя возврат валидного словаря.
        """
        logger.info(f"Финальный анализ документов")

        cleaned_content = self.remove_empty(content)
        if "documents" not in cleaned_content:
            cleaned_content["documents"] = []
        logger.info(f"cleaned_content: {cleaned_content}")

        # Модели — компактная сводка и факты, посчитанные кодом (раньше полный JSON обрезался до первых 14 500
        # токенов, и в большом комплекте модель не видела половину документов)
        digest, level = compact_input(cleaned_content, max_tokens=DIGEST_MAX_TOKENS)
        facts = package_facts(cleaned_content)
        user_content = (
            "ФАКТЫ (посчитаны программно по данным, проверенным по текстам документов; при расхождении с отдельными "
            "значениями из документов опирайся на факты):\n"
            f"{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n"
            f"ДАННЫЕ ПО ДОКУМЕНТАМ:\n{json.dumps(digest, ensure_ascii=False, default=str)}"
        )
        tokens = count_tokens(user_content)
        if level:
            logger.info(f"Сводка для итоговой проверки сокращена до уровня {level} ({tokens} токенов)")
        if tokens > DIGEST_MAX_TOKENS + 2000:
            logger.warning(f"Сводка для итоговой проверки велика даже в краткой форме: {tokens} токенов")

        try:
            # Устойчивый вызов: поток (шлюз не обрывает долгий ответ), паузы 5/15/45 с вместо повторов SDK
            # через секунду, предохранитель при серии 504. Сбой модели не роняет весь конвейер экспертизы.
            raw_response = await call_llm(
                self.client,
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self.consistency_prompt
                    },
                    {
                        "role": "user",
                        "content": f"Проанализируй:\n\n{user_content}"
                    }
                ],
                max_tokens=800,   # 3–5 предложений на русском; обрыв ответа ломает JSON
                temperature=0.1,
                extra_body={"guided_json": self.CONSISTENCY_SCHEMA}
            )
        except Exception as e:  # noqa: BLE001 — 504 / таймаут после пауз: резервный ответ, как при таймауте
            logger.error(f"Финальный анализ документов не выполнен: {type(e).__name__}: {str(e)[:200]}")
            fallback = {
                "overall_summary": "Ошибка анализа данных по закупке",
                "overall_status": "deny"
            }
            return fallback

        raw_response = (raw_response or "").strip()
        logger.info(f"Сырой ответ модели: {raw_response}")
        
        # Парсим JSON
        try:
            result = json.loads(raw_response)
            logger.info(f"Удалось распарсить JSON: {result}")
            return self.prepare_docs_info_for_final_insertion(result, cleaned_content)
        
        except json.JSONDecodeError as e:
            logger.warning(f"Первая попытка парсинга JSON не удалась: {e}")

            # Поиск JSON структур вручную
            try:
                result = json.loads(repair_json(raw_response))

                if not result:
                    logger.error("JSON структуры не найдены.")
                    fallback = {
                        "overall_summary": "Ошибка анализа данных по закупке",
                        "overall_status": "deny"
                    }
                    return self.prepare_docs_info_for_final_insertion(fallback, cleaned_content)
                
                logger.info(f"Удалось распарсить JSON из извлечённого фрагмента вручную: {result}")
                return self.prepare_docs_info_for_final_insertion(result, cleaned_content)
            
            except json.JSONDecodeError:
                logger.error("Не удалось распарсить ни одну JSON структуру.")
                fallback = {
                    "overall_summary": "Ошибка анализа данных по закупке",
                    "overall_status": "deny"
                }
                return self.prepare_docs_info_for_final_insertion(fallback, cleaned_content)


    def remove_empty(self, obj: Any) -> Any:
        """
        Рекурсивно удаляет пустые значения из вложенных структур.

        Удаляются:
        - None
        - ''
        - строки из пробелов
        - []
        - {}
        - pd.NA
        - np.nan

        Возвращает очищенную структуру.
        """

        # --- Нормализация numpy/pandas ---
        if isinstance(obj, (np.generic,)):
            obj = obj.item()

        if obj is pd.NA:
            return None

        if isinstance(obj, float) and np.isnan(obj):
            return None

        # --- Словари ---
        if isinstance(obj, dict):
            cleaned = {
                k: self.remove_empty(v)
                for k, v in obj.items()
            }

            # удаляем пустые значения
            cleaned = {
                k: v
                for k, v in cleaned.items()
                if v not in (None, "", [], {})
            }

            return cleaned or None  # ← ключевой момент

        # --- Списки ---
        if isinstance(obj, list):
            cleaned = [self.remove_empty(x) for x in obj]
            cleaned = [x for x in cleaned if x not in (None, "", [], {})]
            return cleaned or None

        # --- Строки ---
        if isinstance(obj, str):
            stripped = obj.strip()
            return stripped if stripped else None

        # --- Остальное ---
        return obj
    
    
    def prepare_docs_info_for_final_insertion(self, result: Dict[str, Any], content: Dict[str, Any]) -> str:
        
        """
        Обогащает итоговое заключение LLM детализированной информацией по каждому документу.

        Формирует список `documents` с унифицированным форматом для последующего отображения
        или сохранения в БД. Для каждого документа:
            - определяется человекочитаемое имя на основе `DOCUMENT_TYPE_MAPPING`,
            - формируются описания для трёх проверок: соответствия типу, читаемости, полноты,
            - вычисляется общий статус документа ("deny", если хотя бы одна проверка провалена).

        Игнорирует служебные документы с кодами: "contract", "dateContract", "subjectContract", "linkDocs".

        Args:
            result (Dict[str, Any]): Итоговый ответ от LLM (с `overall_status`, `overall_summary`).
            content (Dict[str, Any]): Исходные данные по документам (как передавались на вход).

        Returns:
            Dict[str, Any]: Расширенный результат, содержащий дополнительный ключ `'documents'`
                            с детализацией по каждому анализируемому документу.
        """
        documents_data = []

        for item in content['documents']:
            
            # if item.get('document_code') not in {"contract", "dateContract", "subjectContract"}:

            doc_description = {}

            doc_description['document_code'] = item.get('doc_code')
            doc_description['document_name'] = item.get('doc_type')

            # статус
            doc_description['status'] = 'allow'

            # соответствие типу, читабельность, полнота
            for key in ['type_compliance', 'readability', 'completeness']:
                indicator = item.get(key, {})
                if indicator:
                    status = indicator.get('status', 'allow' if (key == 'readability' and indicator.get('readability_score', 0) >= 0.7) else 'deny')
                    description = indicator.get('description', '' if status == 'allow' else 'Ошибка анализа данных в документе')

                    doc_description[key] = {
                        'status': status,
                        'description': description
                    }

                    if status == 'deny':
                        doc_description['status'] = 'deny'

                else:
                    doc_description[key] = {
                        'status': 'deny',
                        'description': 'Ошибка анализа данных в документе'
                    }
                    doc_description['status'] = 'deny'
            
            # извлеченные данные
            doc_description['raw_data'] = item.get('raw_data', [])


            documents_data.append(doc_description)

        result['documents'] = documents_data
        return result