import asyncio
import aiofiles
import os
import re
import shutil
import subprocess
import tempfile

import pandas as pd
from configs.rate_limiter import TokenBucket
import rarfile
import zipfile
from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from docx import Document
from pathlib import Path
from pptx import Presentation
from pdf2image import convert_from_path

from configs.logger import get_logger
from evaluate_documents.ocr import OCRProcessor


logger = get_logger(__name__)


class FileReader:
    """
    """
    def __init__(self, llm_client, model):
        self.ocr_processor = OCRProcessor(llm_client, model)

        self.semaphore = asyncio.Semaphore(3)
        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду


    async def read_txt_file(self, file_path: str) -> str:
        """
        Читает и возвращает содержимое текстового файла в кодировке UTF-8.

        Args:
            file_path (str): Путь к .txt файлу, который необходимо прочитать.

        Returns:
            str: Содержимое файла в виде строки. Возвращает пустую строку, если файл пустой.
        """
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            return await f.read()


    async def read_doc_file(self, file_path: str, ext: str, original_filename:str) -> str:
        """
        Читает файл формата .doc или .docx и извлекает из него текстовое содержимое.

        Для .docx дополнительно извлекается текст с изображений с помощью OCR.
        Проверяет существование файла, его размер и поддержку формата.

        Args:
            file_path (str): Путь к файлу .doc или .docx.

        Raises:
            ValueError: Если файл не существует.
            ValueError: Если файл пустой.
            ValueError: Если формат файла не поддерживается (не .doc и не .docx).
            ValueError: Если возникает ошибка при чтении файла .doc.
            ValueError: Если происходит ошибка на этапе обработки.

        Returns:
            str: Извлечённый текст из документа. В случае ошибки — исключение.
        """
        try:
            if not os.path.exists(file_path):
                raise ValueError("Файл не существует")
            if os.path.getsize(file_path) == 0:
                raise ValueError("Файл пустой")
            if ext == '.docx':
                return await self.extract_text_and_images_from_docx(file_path, original_filename, ocr_func=self.ocr_processor.ocr_image_with_qwen_vl)
            elif ext == '.doc':
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmpdir = Path(tmpdir)

                    # Пробуем конвертацию DOC → DOCX через LibreOffice
                    try:
                        await asyncio.to_thread(
                            subprocess.run,
                            [
                                "soffice",
                                "--headless",
                                "--convert-to", "docx",
                                file_path,
                                "--outdir", str(tmpdir)
                            ],
                            check=True,
                            capture_output=True
                        )

                        docx_path = tmpdir / (Path(file_path).stem + ".docx")
                        if docx_path.exists():
                            return await self.extract_text_and_images_from_docx(
                                str(docx_path),
                                original_filename,
                                ocr_func=self.ocr_processor.ocr_image_with_qwen_vl
                            )

                    except Exception as e:
                        logger.warning(f"LibreOffice не сработал: {e}. Пробуем antiword...")
                        
                        try:
                            # Пробуем antiword
                            text = await asyncio.to_thread(
                                subprocess.run,
                                ["antiword", file_path],
                                capture_output=True,
                                text=True
                            ).stdout.strip()

                            if text:
                                return text
                            else:
                                raise ValueError("antiword не вернул текст")

                        except Exception as e:
                            logger.warning(f"antiword не сработал: {e}. Пробуем catdoc...")

                            # Используем catdoc
                            try:
                                result = await asyncio.to_thread(
                                    subprocess.run,
                                    ["catdoc", file_path],
                                    capture_output=True,
                                    text=True
                                )
                                return result.stdout.strip()
                            except Exception as e2:
                                raise ValueError(f"Ошибка чтения DOC: {e2}")
                
            else:
                raise ValueError("Неподдерживаемый формат файла")
            
        except Exception as e:
            logger.error(f"Ошибка обработки документа: {str(e)}")
            raise ValueError(f"Ошибка обработки файла: {str(e)}")


    async def read_pptx_file(self, file_path: str) -> str:
        """
        Извлекает текстовое содержимое из презентации PowerPoint (.pptx).

        Проходит по всем слайдам и извлекает текст из всех доступных фигур (shape),
        включая заголовки, пункты списка и другие текстовые блоки.

        Args:
            file_path (str): Путь к файлу .pptx.

        Returns:
            str: Объединённый текст всех слайдов, разделённый переносами строк.
                Возвращает пустую строку, если текст не найден.
        """
        presentation = await asyncio.to_thread(Presentation, file_path)
        text_content = []
        for slide in presentation.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    text_content.append(shape.text)
        return "\n".join(text_content)





    async def read_pdf_file(self, file_path: str, original_filename:str) -> str:
        """
        Извлекает текст из PDF-файла ТОЛЬКО с помощью OCR для каждой страницы.

        Все страницы PDF конвертируются в изображения и обрабатываются через OCR (Qwen-VL),
        независимо от наличия встроенного текста. Это гарантирует единообразную обработку:
        сканы, защищённые PDF, многослойные документы — всё проходит через распознавание образа.

        Args:
            file_path (str): Путь к PDF-файлу.

        Raises:
            ValueError: Если произошла ошибка при чтении или обработке файла.

        Returns:
            str: Объединённый текст всех страниц с пометкой "OCR".
                Если текст не распознан, возвращает "[Нет читаемого текста]".
        """
        async def process_page(i, image, temp_dir):
            async with self.semaphore:
                img_path = os.path.join(temp_dir, f"page_{i}.jpg")

                # Сохранение изображения в отдельном потоке
                await asyncio.to_thread(image.save, img_path, "JPEG")

                # OCR
                ocr_text = await self.ocr_processor.ocr_image_with_qwen_vl(img_path, original_filename)

                return f"Страница {i+1} (OCR): {ocr_text}"

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # PDF → изображения (в отдельном потоке)
                images = await asyncio.to_thread(
                    convert_from_path,
                    file_path,
                    dpi=140,
                    output_folder=temp_dir
                )

                # Создаём задачи
                tasks = [
                    process_page(i, image, temp_dir)
                    for i, image in enumerate(images)
                ]

                # Параллельный запуск
                ocr_texts = await asyncio.gather(*tasks, return_exceptions=True)

                # Обработка ошибок
                results = []
                for i, res in enumerate(ocr_texts):
                    if isinstance(res, Exception):
                        results.append(f"Страница {i+1} (OCR): [Ошибка: {res}]")
                    else:
                        results.append(res)

            result = "\n".join(results)

        # ocr_texts = []

        # try:
        #     # Конвертируем PDF в список изображений
        #     with tempfile.TemporaryDirectory() as temp_dir:
        #         images = await asyncio.to_thread(
        #             convert_from_path,
        #             file_path,
        #             dpi=140,
        #             output_folder=temp_dir
        #         )
        #         for i, image in enumerate(images):
        #             img_path = os.path.join(temp_dir, f"page_{i}.jpg")
        #             image.save(img_path, "JPEG")

        #             # OCR через Qwen-VL
        #             ocr_text = await ocr_image_with_qwen_vl(img_path, original_filename)

        #             # Добавляем с пометкой страницы
        #             ocr_texts.append(f"Страница {i+1} (OCR): {ocr_text}")

        #     result = "\n".join(ocr_texts)
            return result.strip() if result.strip() else "[Нет читаемого текста]"

        except Exception as e:
            raise ValueError(f"Ошибка при обработке {original_filename} через OCR: {str(e)}")


    async def read_excel_file(self, file_path: str, ext: str) -> str:
        """
        Читает содержимое Excel-файла (.xls или .xlsx) и преобразует его в текстовый формат.

        Функция поддерживает оба формата Excel, используя `openpyxl` для .xlsx и попытки чтения .xls
        сначала через `openpyxl`, затем — через `xlrd`. При неудаче запускается резервный метод
        с использованием LibreOffice. Каждый лист представляется как таблица в текстовом виде
        с заголовками столбцов и данными. Результат объединяется в единую строку.

        Args:
            file_path (str): Путь к Excel-файлу на диске.

        Raises:
            ValueError: Если файл не существует или имеет неподдерживаемое расширение.
            RuntimeError: Если все попытки чтения файла завершились ошибкой.
            ValueError: Если указан неверный путь к файлу.
            RuntimeError: Если возникла ошибка при обработке данных Excel.

        Returns:
            str: Текстовое представление всех листов Excel-файла, включая:
                - название каждого листа,
                - табличные данные в читаемом формате (ограничено 20 строками на лист),
                - очищенные от NaN значения.
                Отсутствующие ячейки заменяются пустыми строками, технические названия колонок (например, 'Unnamed') переименовываются.
                В случае ошибки чтения — вызывается исключение.
        """
        if not os.path.exists(file_path):
            raise ValueError(f"Файл не найден: {file_path}")
            
        try:
            # Для .xls файлов используем openpyxl вместо xlrd
            if ext == '.xls':
                try:
                    # Пробуем прочитать с openpyxl
                    excel_data = await asyncio.to_thread(
                        pd.read_excel,
                        file_path,
                        sheet_name=None,
                        engine="openpyxl"
                    )
                except Exception as openpyxl_error:
                    logger.warning(f"openpyxl не смог прочитать .xls файл: {openpyxl_error}. Пробуем xlrd...")
                    try:
                        # Пробуем старую версию xlrd
                        excel_data = await asyncio.to_thread(pd.read_excel,file_path, sheet_name=None, engine='xlrd')
                    except Exception as xlrd_error:
                        logger.warning(f"xlrd также не сработал: {xlrd_error}. Пробуем LibreOffice...")
                        return await self.read_excel_with_libreoffice(file_path)
            elif ext == '.xlsx':
                excel_data = await asyncio.to_thread(pd.read_excel,file_path, sheet_name=None, engine='openpyxl')
            else:
                raise ValueError("Поддерживаются только файлы .xls и .xlsx")

            result = []
            
            for sheet_name, df in excel_data.items():
                result.append(f"Лист: {sheet_name}")
                # Заменяем NaN на пустые строки и преобразуем все в строки
                df = df.fillna('').astype(str)
                
                # Форматируем таблицу для лучшей читаемости
                for col in df.columns:
                    # Очищаем названия колонок от технической информации
                    if 'unnamed' in str(col).lower():
                        df = df.rename(columns={col: f'Колонка_{df.columns.get_loc(col)}'})
                
                # Используем компактное представление таблицы
                table_text = df.to_string(index=False, max_rows=20)  # Ограничиваем количество строк
                result.append(table_text)
                result.append("")  # Одна пустая строка между листами
            
            # Убираем лишние пустые строки в конце и множественные пробелы
            full_text = "\n".join(result).strip()
            # Заменяем множественные пробелы на один пробел
            full_text = re.sub(r'\s+', ' ', full_text)
            logger.info(f"Успешно извлечен текст из Excel: {len(full_text)} символов")
            return full_text

        except Exception as e:
            logger.error(f"Критическая ошибка при обработке Excel файла: {str(e)}")
            raise RuntimeError(f"Не удалось прочитать Excel файл: {str(e)}")


    async def read_excel_with_libreoffice(self, file_path: str) -> str:
        """
        Читает Excel-файл (.xls и др.) с помощью LibreOffice, конвертируя его в .xlsx и извлекая текстовое содержимое.

        Является резервным методом для обработки старых или повреждённых Excel-файлов, которые не удаётся прочитать
        стандартными библиотеками (например, `xlrd` или `openpyxl`). Запускает LibreOffice в headless-режиме,
        конвертирует файл во временный формат .xlsx, затем читает его с помощью pandas и преобразует в строку.

        Args:
            file_path (str): Путь к исходному Excel-файлу (обычно .xls), который необходимо обработать.

        Raises:
            RuntimeError: Если LibreOffice не установлен или недоступен в системе.
            RuntimeError: Если команда конвертации завершилась с ошибкой.
            RuntimeError: Если после конвертации не найден выходной .xlsx-файл.
            RuntimeError: Если произошла ошибка при чтении сконвертированного файла.
            RuntimeError: Если возникла любая другая ошибка на этапе обработки (общее исключение).

        Returns:
            str: Текстовое представление всех листов файла, включая:
                - название каждого листа,
                - табличные данные в виде строки (ограничено 20 строками на лист),
                - очищенные от NaN значений ячейки.
                Все множественные пробелы заменяются на одиночные. В случае успеха — возвращает полный текст;
                при ошибке — выбрасывает исключение с детализацией проблемы.
        """
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp()
            temp_xlsx_path = os.path.join(temp_dir, "converted.xlsx")
            
            # Проверяем доступность LibreOffice
            try:
                result = await asyncio.to_thread(subprocess.run, ['libreoffice', '--version'], capture_output=True, text=True, timeout=10)
                if result.returncode != 0:
                    raise RuntimeError("LibreOffice не установлен или недоступен")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                raise RuntimeError("LibreOffice не доступен")
            
            # Конвертируем в xlsx
            cmd = [
                'libreoffice',
                '--headless',
                '--convert-to',
                'xlsx:Calc MS Excel 2007 XML',
                '--outdir',
                temp_dir,
                file_path
            ]
            
            result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(f"Ошибка конвертации: {result.stderr}")
            
            # Ищем сконвертированный файл
            converted_files = [f for f in os.listdir(temp_dir) if f.endswith('.xlsx')]
            if not converted_files:
                raise RuntimeError("Не удалось найти сконвертированный файл")
            
            temp_xlsx_path = os.path.join(temp_dir, converted_files[0])
            excel_data = await asyncio.to_thread(pd.read_excel,temp_xlsx_path, sheet_name=None, engine='openpyxl')
            
            result = []
            for sheet_name, df in excel_data.items():
                result.append(f"Лист: {sheet_name}")
                df = df.fillna('').astype(str)
                
                # Компактное представление
                table_text = df.to_string(index=False, max_rows=20)
                result.append(table_text)
                result.append("")  # Одна пустая строка между листами
            
            full_text = "\n".join(result).strip()
            # Заменяем множественные пробелы на один пробел
            full_text = re.sub(r'\s+', ' ', full_text)
            return full_text
            
        except Exception as e:
            logger.error(f"Ошибка конвертации через LibreOffice: {str(e)}")
            raise RuntimeError(f"Не удалось обработать Excel файл даже через LibreOffice: {str(e)}")
        finally:
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception as cleanup_error:
                    logger.warning(f"Ошибка при очистке временных файлов: {cleanup_error}")


    async def extract_text_and_images_from_docx(self, file_path: str, original_filename:str, ocr_func=None) -> str:
        """
        Извлекает текст и распознаёт текст с изображений из файла DOCX.

        Функция извлекает текст из абзацев и таблиц документа, а также обрабатывает встроенные изображения,
        извлекая их из ZIP-структуры DOCX. Для каждого изображения можно выполнить OCR с помощью переданной
        функции `ocr_func`. Результаты объединяются в единый текстовый вывод.

        Args:
            file_path (str): Путь к файлу .docx.
            ocr_func (callable, optional): Функция для распознавания текста на изображении. 
                                        Должна принимать путь к изображению и возвращать строку с текстом.
                                        Если не указана, изображения помечаются без распознавания.
                                        Defaults to None.

        Raises:
            ValueError: Если произошла ошибка при обработке файла DOCX.

        Returns:
            str: Объединённый текст, содержащий:
                - обычный текст из документа;
                - результаты OCR с изображений (если `ocr_func` указана) или метки изображений.
                Все элементы разделены переносами строк. В случае ошибки — исключение.
        """
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                doc = await asyncio.to_thread(Document, file_path)
                result = []
                for para in doc.paragraphs:
                    if para.text.strip():
                        result.append(para.text)
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            if cell.text.strip():
                                result.append(cell.text)

                image_texts = []
                doc_zip = await asyncio.to_thread(zipfile.ZipFile, file_path)
                image_files = [name for name in doc_zip.namelist() if name.startswith('word/media/')]
                for img_file in image_files:
                    try:
                        if img_file.lower().endswith(('.wmf', '.emf')):
                            logger.info(f"Пропускаем неподдерживаемый формат изображения: {img_file}")
                            continue
                        img_data = doc_zip.read(img_file)
                        img_path = os.path.join(temp_dir, os.path.basename(img_file))
                        async with aiofiles.open(img_path, 'wb') as f:
                            await f.write(img_data)
                        if ocr_func:
                            ocr_text = await ocr_func(img_path, original_filename)
                            image_texts.append(f"[OCR из изображения {os.path.basename(img_file)}]: {ocr_text}")
                        else:
                            image_texts.append(f"[Изображение: {os.path.basename(img_file)}]")
                    except Exception as e:
                        logger.error(f"Ошибка обработки изображения {img_file}: {str(e)}")
                        continue

                if image_texts:
                    result.append("\n".join(image_texts))
                return "\n".join(result)

        except Exception as e:
            logger.error(f"Ошибка обработки DOCX: {str(e)}")
            raise ValueError(f"Ошибка обработки DOCX: {str(e)}")


    async def process_archive(self, file_path: str, ext: str) -> str:
        """
        Извлекает и обрабатывает файлы из архива (.zip или .rar), читая их содержимое.

        Функция последовательно:
        - распаковывает каждый файл из архива во временную директорию;
        - определяет его тип и извлекает текст с помощью `read_file`;
        - добавляет содержимое с заголовком имени файла;
        - удаляет временный файл после обработки.

        Поддерживает вложенные папки в архиве. Каталоги пропускаются.

        Args:
            file_path (str): Путь к архивному файлу (.zip или .rar).

        Returns:
            str: Объединённый текст всех обработанных файлов в формате:
                === имя_файла ===
                [содержимое]
                В случае ошибки возвращает сообщение об ошибке с именем архива.
        """
        async def process_single_file(filename, data):
            extracted_path = os.path.join(tempfile.gettempdir(), filename)
            os.makedirs(os.path.dirname(extracted_path), exist_ok=True)

            try:
                await asyncio.to_thread(write_file, extracted_path, data)
                text = await self.read_file(extracted_path, original_filename=filename)
                return f"=== {filename} ===\n{text}"
            except Exception as e:
                return f"=== {filename} ===\n[Ошибка чтения: {e}]"
            finally:
                if os.path.exists(extracted_path):
                    os.unlink(extracted_path)

        def write_file(path, data):
            with open(path, 'wb') as f:
                f.write(data)

        tasks = []

        if ext == '.zip':
            with zipfile.ZipFile(file_path, 'r') as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    data = archive.read(info)
                    tasks.append(process_single_file(info.filename, data))

        elif ext == '.rar':
            with rarfile.RarFile(file_path) as archive:
                for info in archive.infolist():
                    if info.isdir():
                        continue
                    data = archive.read(info)
                    tasks.append(process_single_file(info.filename, data))

        results = await asyncio.gather(*tasks)

        return "\n\n".join(results)
        # return results


    async def read_html(self, html_path: str) -> str:

        async with aiofiles.open(html_path, 'r', encoding='utf-8') as f:
            html_content = await f.read()

        soup = BeautifulSoup(html_content, 'html.parser')

        # Удаляем служебные блоки, если они есть
        for tag in soup(['script', 'style', 'meta', 'link']):
            tag.decompose()

        # Заменяем <br> и <p> на переносы строк
        for br in soup.find_all('br'):
            br.replace_with('\n')

        # Извлекаем текст, убираем лишние пробелы и пустые строки
        text = soup.get_text(separator='\n', strip=True)
        clean_lines = [line.strip() for line in text.splitlines() if line.strip()]
        
        return '\n'.join(clean_lines)


    async def html_to_pdf(self, html_path: str, original_filename:str) -> str:
        """Асинхронно конвертирует HTML-файл в PDF с помощью wkhtmltopdf"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            pdf_path = tmp.name

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "wkhtmltopdf",

                    # 🔥 ключевые настройки
                    "--print-media-type",          # использовать @media print
                    "--enable-local-file-access",  # доступ к локальным ресурсам
                    "--encoding", "utf-8",

                    # 📄 формат страницы
                    "--page-size", "A4",
                    "--margin-top", "10mm",
                    "--margin-bottom", "10mm",
                    "--margin-left", "10mm",
                    "--margin-right", "10mm",

                    # ⚙️ стабильность
                    "--load-error-handling", "ignore",
                    "--load-media-error-handling", "ignore",
                    # "--no-images",
                    # "--disable-javascript",

                    html_path,
                    pdf_path
                ],
                capture_output=True,
                text=True,
                timeout=30)
            
            if result.returncode != 0:
                raise RuntimeError(f"wkhtmltopdf failed: {result.stderr}")
            
            return await self.read_pdf_file(pdf_path, original_filename)
        
        except Exception as e:
            logger.error(f"Ошибка конвертации HTML → PDF для {original_filename}: {e}")
            raise


    async def read_xml_file(self, file_path: str) -> str:
        """
        Асинхронно читает XML-файл и преобразует в плоский текст.
        Безопасно для внешних XML.
        """

        def _parse_xml():
            try:
                lines = []

                for event, elem in ET.iterparse(file_path, events=("end",)):
                    tag = elem.tag.split('}')[-1]
                    text = (elem.text or "").strip()

                    if text:
                        lines.append(f"{tag}: {text}")

                    elem.clear()

                return "\n".join(lines)

            except Exception as e:
                logger.warning(f"Не удалось распарсить XML: {e}")

                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        return f.read()
                except Exception:
                    return ""

        return await asyncio.to_thread(_parse_xml)


    async def read_file(self, file_path: str, original_filename: str = None) -> str:
        """
        Читает файл и извлекает текстовое содержимое с улучшенной обработкой бинарных файлов

        Args:
            file_path (str): Путь к файлу на диске.
            original_filename (str, optional): Оригинальное имя файла (может отличаться от имени во временном хранилище).
                                            Используется для определения расширения. Defaults to None.

        Raises:
            ValueError: Если произошла ошибка при чтении файла (например, повреждён, недоступен).

        Returns:
            str: Извлечённый текст или сообщение о типе файла, если формат не поддерживается.
                Для неподдерживаемых бинарных форматов возвращается строка вида "Бинарный файл ...".
        """
        # filename_to_check = original_filename or os.path.basename(file_path)
        # _, ext = os.path.splitext(filename_to_check)
        # ext = ext.lower()
        ext = os.path.splitext(original_filename)[1].lower()
        logger.info(f"Чтение файла: {original_filename} (расширение: {ext})")

        try:
            # Если расширение .bin, пробуем определить реальный тип
            if ext == ".bin":
                real_extension = await self.detect_binary_file_type(file_path)
                logger.info(f"Определен реальный тип бинарного файла: {real_extension}")
                
                # Создаем копию файла с правильным расширением для обработки
                if real_extension != ".bin":
                    new_path = file_path + real_extension
                    shutil.copy2(file_path, new_path)
                    try:
                        result = await self.read_file(new_path, original_filename + real_extension)
                        os.unlink(new_path)
                        return result
                    except Exception as e:
                        logger.warning(f"Не удалось обработать файл с определенным расширением {real_extension}: {str(e)}")
                        os.unlink(new_path)
                        # Продолжаем с оригинальным .bin файлом

            # Основная логика обработки по расширениям
            if ext in [".txt", ".csv", ".json"]:
                text = await self.read_txt_file(file_path)
                logger.info(f"Прочитан {ext.upper()}-файл: {original_filename}, длина: {len(text)}")
                return text 
            elif ext == ".pdf":
                text = await self.read_pdf_file(file_path, original_filename)
                logger.info(f"Извлечён текст из PDF: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".xlsx", ".xls"]:
                text = await self.read_excel_file(file_path, ext)
                logger.info(f"Извлечён текст из Excel: {original_filename}, длина: {len(text)}")
                return text
            elif ext == ".pptx":
                text = await self.read_pptx_file(file_path)
                logger.info(f"Извлечён текст из PPTX: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".doc", ".docx"]:
                text = await self.read_doc_file(file_path, ext, original_filename)
                logger.info(f"Извлечён текст из DOC(X): {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".zip", ".rar"]:
                text = await self.process_archive(file_path, ext)
                logger.info(f"Извлечён текст из архива: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".html"]:
                # text = await html_to_pdf(file_path, original_filename)
                text = await self.read_html(file_path)
                logger.info(f"Извлечён текст из HTML: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".xml"]:
                text = await self.read_xml_file(file_path)
                logger.info(f"Извлечён текст из XML: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".sig"]:
                return "Файл электронной подписи. Содержит крипто данные."
            # elif ext in [".sig", ".xml"]:
            #     return []
            elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"]:
                # Обработка изображений через OCR
                try:
                    text = await self.ocr_processor.ocr_image_with_qwen_vl(file_path, original_filename)
                    logger.info(f"Извлечён текст из изображения через OCR: {original_filename}, длина: {len(text)}")
                    return text
                except Exception as e:
                    logger.warning(f"Не удалось извлечь текст из изображения {original_filename}: {str(e)}")
                    return f"Изображение {os.path.basename(file_path)} (текст не распознан)"
            else:
                # Для неизвестных форматов пробуем определить тип и обработать
                if ext == ".bin":
                    # Уже пробовали определить тип выше, если дошли сюда - не удалось
                    msg = f"Бинарный файл {os.path.basename(file_path)} (не удалось определить формат)"
                else:
                    msg = f"Бинарный файл {os.path.basename(file_path)} (формат {ext})"
                
                logger.warning(f"Неподдерживаемый формат файла: {original_filename}")
                return msg
                
        except Exception as e:
            logger.error(f"Ошибка чтения файла {original_filename}: {str(e)}", exc_info=True)
            raise ValueError(f"Ошибка чтения файла: {str(e)}")


    async def detect_binary_file_type(self, file_path: str) -> str:
        """
        Определяет тип бинарного файла по его содержимому (сигнатурам)
        
        Args:
            file_path (str): Путь к файлу
            
        Returns:
            str: Расширение файла (.pdf, .docx, .jpg и т.д.) или .bin если не удалось определить
        """
        try:
            async with aiofiles.open(file_path, 'rb') as f:
                header = await f.read(12)  # Читаем первые 12 байт для лучшего определения
            # PDF - %PDF
            if header.startswith(b'%PDF'):
                return '.pdf'
            # ZIP-based formats (DOCX, XLSX, PPTX, ODT и т.д.)
            if header.startswith(b'PK\x03\x04'):
                # Можно попробовать определить точный тип по структуре ZIP
                try:
                    with await asyncio.to_thread(zipfile.ZipFile, file_path, 'r') as zip_file:
                        namelist = zip_file.namelist()
                        # Проверяем структуру для разных форматов
                        if any(name.startswith('word/') for name in namelist):
                            return '.docx'
                        elif any(name.startswith('xl/') for name in namelist):
                            return '.xlsx'
                        elif any(name.startswith('ppt/') for name in namelist):
                            return '.pptx'
                        else:
                            return '.zip'  # обычный ZIP архив
                except:
                    return '.docx'  # по умолчанию считаем DOCX
            # Microsoft Office old formats (DOC, XLS, PPT)
            if header.startswith(b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'):
                return '.doc'  # по умолчанию DOC
            # JPEG
            if header.startswith(b'\xFF\xD8\xFF'):
                return '.jpg'
            # PNG
            if header.startswith(b'\x89PNG\r\n\x1a\n'):
                return '.png'
            # GIF
            if header.startswith(b'GIF8'):
                return '.gif'
            # BMP
            if header.startswith(b'BM'):
                return '.bmp'
            # TIFF
            if header.startswith(b'II\x2A\x00') or header.startswith(b'MM\x00\x2A'):
                return '.tiff'
            # RAR
            if header.startswith(b'Rar!\x1A\x07\x00') or header.startswith(b'Rar!\x1A\x07\x01'):
                return '.rar'
            # 7Z
            if header.startswith(b'7z\xBC\xAF\x27\x1C'):
                return '.7z'
            # Microsoft Cabinet (CAB)
            if header.startswith(b'MSCF'):
                return '.cab'
            # Windows Executable
            if header.startswith(b'MZ'):
                return '.exe'
            # UTF-8/16 text files with BOM
            if header.startswith(b'\xEF\xBB\xBF'):  # UTF-8 BOM
                return '.txt'
            if header.startswith(b'\xFF\xFE') or header.startswith(b'\xFE\xFF'):  # UTF-16 BOM
                return '.txt'

            # Пробуем определить как текстовый файл
            try:
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    await f.read(1024)  # Пробуем прочитать как текст
                return '.txt'
            except:
                pass
                
            return '.bin'
            
        except Exception as e:
            logger.error(f"Ошибка определения типа бинарного файла {file_path}: {str(e)}")
            return '.bin'