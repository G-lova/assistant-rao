import os
import shutil
import tempfile
import json
import logging

import rarfile
import zipfile
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Union, Optional
from pydantic import BaseModel

from configs.schemas import DocumentContentResponse, APIError, ProcurementCheckResponse, MissingDocument
from configs.utils import APIKeyMiddleware, read_file, check_procurement_completeness, get_required_documents
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from configs.working_with_db import save_document_content_to_db, ALLOWED_COLUMNS


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(APIKeyMiddleware)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@app.get("/test-auth")
async def test_auth():
    return {"message": "Authenticated successfully"}


@app.post(
    "/get-documents-content",
    response_model=DocumentContentResponse,
    responses={400: {"model": APIError}, 500: {"model": APIError}}
)
async def get_documents_content(
    procurement_id: str = Form(...),
    document_type: str = Form(...),  # Например: "Акт о приемке товара"
    file: UploadFile = File(...)
):
    """
    Принимает файл документа закупки, извлекает содержимое,
    определяет колонку в БД через DOCUMENT_TYPE_MAPPING,
    сохраняет в report_for_operator.
    Поддерживает ZIP и RAR архивы.
    """
    try:
        # Ищем соответствие в маппинге
        if document_type not in DOCUMENT_TYPE_MAPPING:
            available = ", ".join(DOCUMENT_TYPE_MAPPING.keys())
            raise HTTPException(
                status_code=400,
                detail=f"Неизвестный тип документа. Допустимые типы: {available}"
            )

        # Получаем техническое имя колонки
        db_column = DOCUMENT_TYPE_MAPPING[document_type]

        # Защита: убедимся, что колонка разрешена
        if db_column not in ALLOWED_COLUMNS:
            logger.error(f"Document type '{db_column}' not in ALLOWED_COLUMNS")
            raise HTTPException(status_code=500, detail="Внутренняя ошибка: недопустимая колонка БД")

        # Создаём временную директорию
        temp_dir = tempfile.mkdtemp()
        file_path = os.path.join(temp_dir, file.filename)

        try:
            # Сохраняем файл
            with open(file_path, "wb") as buffer:
                buffer.write(await file.read())

            content = ""

            # Обработка ZIP
            if file.filename.lower().endswith('.zip'):
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    if not zip_ref.namelist():
                        raise HTTPException(status_code=400, detail="ZIP архив пуст")
                    first_file = zip_ref.namelist()[0]
                    with zip_ref.open(first_file) as f:
                        raw_content = f.read()
                        content = raw_content.decode('utf-8', errors='ignore')

            # Обработка RAR
            elif file.filename.lower().endswith('.rar'):
                try:
                    with rarfile.RarFile(file_path, 'r') as rar_ref:
                        if not rar_ref.namelist():
                            raise HTTPException(status_code=400, detail="RAR архив пуст")
                        first_file = rar_ref.namelist()[0]
                        with rar_ref.open(first_file) as f:
                            raw_content = f.read()
                            content = raw_content.decode('utf-8', errors='ignore')
                except rarfile.Error as e:
                    raise HTTPException(status_code=400, detail=f"Ошибка чтения RAR: {str(e)}")

            # Обычные файлы
            else:
                content = read_file(file_path)

            # Сохраняем в БД по найденной колонке
            success = save_document_content_to_db(
                procurement_id=procurement_id,
                document_type=db_column,  # ← техническое имя
                content=content
            )

            if not success:
                raise HTTPException(
                    status_code=500,
                    detail="Не удалось сохранить данные в базу"
                )

            # Возвращаем ответ с исходным именем документа
            return DocumentContentResponse(
                procurement_id=procurement_id,
                document_type=document_type,  # ← человекочитаемое имя
                filename=file.filename,
                content_type=file.content_type or "unknown",
                content=content[:20],
                size=file.size,
                is_valid=True
            )

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing document: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка при обработке документа: {str(e)}"
        )


@app.post("/check-procurement-documents", response_model=ProcurementCheckResponse)
async def check_procurement_documents(
    procurement_id: str = Form(...),
    expertise_object: str = Form(...),
    legislation: str = Form(...),
    procurement_method: str = Form(...),
    expertise_details: str = Form(...),
    eis_link: Optional[str] = Form(None),
    documents: str = Form("[]"),  # JSON строка с документами
    files: List[UploadFile] = File([])  # Загруженные файлы
):
    try:
        # Парсим документы
        docs_list = json.loads(documents)

        # Получаем обязательные документы
        required_docs = get_required_documents(legislation, procurement_method, expertise_details)

        # Для ответа
        documents_content = {}
        provided_docs = []
        missing_docs = []

        # 1. Обрабатываем загруженные файлы
        for file in files:
            try:
                content_bytes = await file.read()
                filename = file.filename

                # Определяем тип документа по имени файла или маппингу
                # Попробуем найти в маппинге (например, по имени)
                # Или можно использовать другую логику — зависит от UX

                # Простой вариант: имя файла → ключ маппинга
                # Но лучше: ты передаёшь в `documents` соответствие

                # Пока просто сохраняем контент
                try:
                    text_content = content_bytes.decode('utf-8')[:50]
                except UnicodeDecodeError:
                    text_content = "Бинарный файл (не текст)"

                documents_content[filename] = text_content
                provided_docs.append(filename)

                # Попробуем сопоставить имя файла с колонкой
                # Упрощённо: если имя файла совпадает с ключом маппинга
                doc_type = None
                for human_name, col_name in DOCUMENT_TYPE_MAPPING.items():
                    if human_name.lower() in filename.lower():
                        doc_type = col_name
                        break

                # Если нашли — сохраняем в БД
                if doc_type and doc_type in ALLOWED_COLUMNS:
                    save_document_content_to_db(
                        procurement_id=procurement_id,
                        document_type=doc_type,
                        content=content_bytes.decode('utf-8', errors='ignore')  # весь текст
                    )
                else:
                    logger.info(f"Не удалось сопоставить файл {filename} с колонкой БД")

                await file.seek(0)

            except Exception as e:
                logger.error(f"Error processing file {file.filename}: {str(e)}")

        # 2. Обрабатываем документы из списка (уже загруженные, по file_path)
        for doc in docs_list:
            doc_name = doc.get("document_name")
            status = doc.get("status")

            if status == "missing":
                missing_docs.append(
                    MissingDocument(
                        document_name=doc_name,
                        explanation=doc.get("explanation", "Не указана причина")
                    )
                )
            elif status == "uploaded" and doc.get("file_path"):
                try:
                    with open(doc["file_path"], "r", encoding="utf-8") as f:
                        content = f.read()
                    short_content = content[:50]
                    documents_content[doc_name] = short_content
                    provided_docs.append(doc_name)

                    # Сохраняем в БД, если имя документа есть в маппинге
                    col_name = DOCUMENT_TYPE_MAPPING.get(doc_name)
                    if col_name and col_name in ALLOWED_COLUMNS:
                        save_document_content_to_db(
                            procurement_id=procurement_id,
                            document_type=col_name,
                            content=content
                        )
                    else:
                        logger.warning(f"Документ '{doc_name}' не сопоставлен с колонкой БД")

                except Exception as e:
                    logger.error(f"Error reading file {doc['file_path']}: {str(e)}")

        # 3. Проверяем отсутствующие обязательные документы
        for req_doc in required_docs:
            if (req_doc not in provided_docs and 
                not any(md.document_name == req_doc for md in missing_docs)):
                missing_docs.append(
                    MissingDocument(
                        document_name=req_doc,
                        explanation="Не предоставлен"
                    )
                )

        # 4. Сохраняем мета-поля (если они тоже должны быть в БД)
        meta_fields = {
            "expertise_object": expertise_object,
            "legal_regulation": legislation,
            "procurement_method": procurement_method,
            "expertise_request": expertise_details,
            "eis_link": eis_link
        }

        for field_name, value in meta_fields.items():
            if value and field_name in ALLOWED_COLUMNS:
                save_document_content_to_db(
                    procurement_id=procurement_id,
                    document_type=field_name,
                    content=str(value)
                )

        # 5. Формируем ответ
        return ProcurementCheckResponse(
            status="allow" if not missing_docs else "deny",
            required_documents=required_docs,
            provided_documents=provided_docs,
            missing_documents=missing_docs,
            documents_content=documents_content,
            error=None
        )

    except Exception as e:
        logger.error(f"Error in check_procurement_documents: {str(e)}", exc_info=True)
        return ProcurementCheckResponse(
            status="deny",
            required_documents=[],
            provided_documents=[],
            missing_documents=[],
            documents_content={},
            error=str(e)
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=20142)