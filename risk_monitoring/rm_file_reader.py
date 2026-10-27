"""``FileReader`` риск-мониторинга: PDF — по текстовому слою, OCR только страниц-сканов.

Общий ``configs.file_reader.FileReader`` распознаёт моделью **каждую** страницу PDF, а документы Word
(``.doc``, ``.docx``, ``.odt``, ``.rtf``) сначала конвертирует в PDF — то есть и у них каждая страница идёт в OCR,
хотя текст в документе уже есть. Прогон 06.07 (26.10.2026): «Информационная карта.docx» читалась 1062 с, файлы
первой пачки — 8555 с, прогон не уложился в лимит задачи. Этот класс подменяет только чтение PDF — им пользуются
и Word (через конвертацию), и документы внутри архивов: текстовый слой берётся как есть, OCR — для страниц без
пригодного слоя (``pdf_text.read_pdf``). Модуль экспертизы (``evaluate_documents``) не затрагивается.
"""
from configs.file_reader import FileReader
from configs.logger import get_logger
from risk_monitoring import pdf_text

logger = get_logger(__name__)


class RMFileReader(FileReader):
    """``FileReader`` с чтением PDF по текстовому слою (см. модуль)."""

    async def read_pdf_file(self, file_path: str, original_filename: str) -> str:
        """Текст PDF: слой там, где он пригоден, OCR — для остальных страниц; при сбое — общий способ (OCR всех).

        Args:
            file_path: Путь к PDF.
            original_filename: Имя исходного файла (для журнала и OCR).

        Returns:
            str: Текст по страницам.
        """
        try:
            res = await pdf_text.read_pdf(file_path, original_filename, getattr(self, "ocr_processor", None))
            if res and res["text"].strip():
                return res["text"]
        except Exception as e:  # noqa: BLE001 — общий способ надёжнее, чем ничего
            logger.warning(f"pdf_text: {original_filename}: {type(e).__name__}: {e}; OCR всех страниц")
        return await super().read_pdf_file(file_path, original_filename)
