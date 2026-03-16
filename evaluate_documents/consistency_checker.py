import asyncio
import json
import numpy as np
import pandas as pd

from json_repair import repair_json

from configs.logger import get_logger
from typing import Any, Dict

from configs.utils import extract_json_objects
from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING


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

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self.consistency_prompt
                    },
                    {
                        "role": "user",
                        "content": f"""Проанализируй:\n\n{cleaned_content}"""
                    }
                ],
                max_tokens=3000,
                temperature=0.1,
                extra_body={"guided_json": self.CONSISTENCY_SCHEMA}
            )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при анализе документов")
            fallback = {
                "overall_summary": "Ошибка анализа данных по закупке",
                "overall_status": "deny"
            }
            return fallback

        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Сырой ответ модели: {raw_response}")
        
        # Парсим JSON
        try:
            result = json.loads(raw_response)
            logger.info("Удалось распарсить JSON.")
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
                
                logger.info("Удалось распарсить JSON из извлечённого фрагмента вручную.")
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
            
            if item.get('document_code') not in {"contract", "dateContract", "subjectContract"}:

                doc_description = {}

                doc_description['document_code'] = item.get('document_code')
                doc_description['document_name'] = 'Ссылка на ЕИС' if item.get('document_code') == 'linkDocs' else DOCUMENT_TYPE_MAPPING.get(doc_description['document_code'])

                # соответствие типу
                type_compliance = item.get('type_compliance', {})
                if type_compliance:
                    doc_description['type_compliance'] = {
                        'status': type_compliance['status'], 
                        'description': '\n'.join(i for i in type_compliance.get('issues', []) if i) or ''
                    }


                # читаемость
                readability = item.get('readability', {})
                if readability:
                    if readability['status'] == 'allow':
                        readability['description'] = readability.get('image_description', '')
                    else:
                        readability['description'] = '\n'.join(i for i in readability.get('issues', []) if i) or ''
                        
                    doc_description['readability'] = {
                        'status': readability['status'], 
                        'description': readability['description']
                    }
                
                # полнота и согласованность
                completeness = item.get('completeness', {})
                if completeness:
                    doc_description['completeness'] = {
                        'status': completeness.get('status', 'deny'), 
                        'description': completeness.get('description', 'Ошибка обработки документа')
                    }
                
                # извлеченные данные
                doc_description['raw_data'] = item.get('raw_data', {})

                # статус
                doc_description['status'] = 'deny' if 'deny' in {type_compliance.get('status', ""), readability.get('status', ""), completeness.get('status', "")} else 'allow'


                documents_data.append(doc_description)

        result['documents'] = documents_data
        return result