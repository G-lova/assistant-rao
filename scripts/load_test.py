import asyncio
import os
import time
from typing import List

import aiohttp
from dotenv import load_dotenv


# ==============================
# НАСТРОЙКИ ТЕСТИРОВАНИЯ
# ==============================

load_dotenv()

# URL вашего сервиса
BASE_URL = "http://95.64.227.126:2300"

# API-ключ (если используется middleware)
API_KEY = os.getenv("API_KEY")

# Путь к папке с тестовыми файлами (должны существовать на машине, где запускается тест)
TEST_FILES_DIR = "./example"

# Примеры ссылок (облака или ЕИС)
TEST_LINKS = [
    "https://docs.google.com/spreadsheets/d/18sTvcCT15hCsWZpjlgFKlIBfHmxhpaMF/edit?usp=drive_link&ouid=117039718756827184704&rtpof=true&sd=true",
    "https://docs.google.com/document/d/1QClSSqpEV2QNbL3oJMPjedL0n28PCsig/edit?usp=drive_link&ouid=117039718756827184704&rtpof=true&sd=true",
    "https://disk.yandex.ru/i/ODl0pw1hX41wpQ",
    "https://disk.yandex.ru/i/O9E-d0Qxj8zOrw"
]

# Сколько файлов/ссылок отправлять в ОДНОМ запросе
DOCS_PER_REQUEST = 4  # например: 1 файл + 1 ссылка, или 2 файла и т.д.

# Сколько ЗАПРОСОВ отправить ПАРАЛЛЕЛЬНО (нагрузка)
TOTAL_REQUESTS = 10

# Законодательство и способ закупки (можно оставить по умолчанию)
LEGISLATION = "44-ФЗ"
PROCUREMENT_METHOD = "Конкурс"

# ==============================
# Подготовка списка файлов
# ==============================

def get_test_files() -> List[str]:
    if not os.path.exists(TEST_FILES_DIR):
        print(f"Папка {TEST_FILES_DIR} не найдена. Будут использоваться только ссылки.")
        return []
    files = [os.path.join(TEST_FILES_DIR, f) for f in os.listdir(TEST_FILES_DIR) if os.path.isfile(os.path.join(TEST_FILES_DIR, f))]
    if not files:
        print("В папке test_files нет файлов.")
    return files

# ==============================
# Функция отправки одного запроса с замером времени
# ==============================

async def send_evaluate_request(session: aiohttp.ClientSession, request_id: int, files: List[str], links: List[str]):
    procurement_id = f"load_test_{request_id}_{os.getpid()}"
    
    data = aiohttp.FormData()
    data.add_field("procurement_id", procurement_id)
    data.add_field("legislation", LEGISLATION)
    data.add_field("procurement_method", PROCUREMENT_METHOD)
    data.add_field("expertise_details", "Полный комплект документов о закупке")
    
    opened_files = []
    try:
        for file_path in files:
            f = open(file_path, 'rb')
            opened_files.append(f)
            data.add_field(
                name="files",
                value=f,
                filename=os.path.basename(file_path),
                content_type="application/octet-stream"
            )
        for link in links:
            data.add_field("links", link)

        headers = {"X-API-Key": API_KEY}
        start_time = time.time()
        timeout = aiohttp.ClientTimeout(total=600)
        async with session.post(f"{BASE_URL}/evaluate-documents", data=data, headers=headers, timeout=timeout) as resp:
            elapsed = time.time() - start_time
            status = resp.status
            try:
                response_json = await resp.json()
                docs_processed = response_json.get("documents_processed", 0)
                overall = response_json.get("overall_conclusion", "unknown")
            except:
                docs_processed = 0
                overall = "parse_error"

            print(f"[{request_id:2d}] Статус: {status} | Обработано: {docs_processed} | Итог: {overall} | Время: {elapsed:.2f} сек")
            return {
                "success": status == 200,
                "elapsed": elapsed,
                "status": status,
                "docs_processed": docs_processed
            }
    finally:
        for f in opened_files:
            f.close()

# ==============================
# Основная функция нагрузки
# ==============================

async def main():
    test_files = get_test_files()
    all_links = TEST_LINKS
    
    if not test_files and not all_links:
        print("Нет ни файлов, ни ссылок для тестирования!")
        return

    print(f"Найдено файлов: {len(test_files)}")
    print(f"Найдено ссылок: {len(all_links)}")
    print(f"Отправка {TOTAL_REQUESTS} запросов по {DOCS_PER_REQUEST} документов каждый...\n")

    start_total = time.time()
    async with aiohttp.ClientSession() as session:
        tasks = []
        for i in range(TOTAL_REQUESTS):
            files_to_send = []
            links_to_send = []

            if test_files:
                for j in range(DOCS_PER_REQUEST // 2 + DOCS_PER_REQUEST % 2):
                    files_to_send.append(test_files[(i + j) % len(test_files)])
            if all_links:
                for j in range(DOCS_PER_REQUEST // 2):
                    links_to_send.append(all_links[(i + j) % len(all_links)])
            
            if not files_to_send and links_to_send:
                links_to_send = all_links[:min(DOCS_PER_REQUEST, len(all_links))]
            if not links_to_send and files_to_send:
                files_to_send = test_files[:min(DOCS_PER_REQUEST, len(test_files))]

            task = send_evaluate_request(session, i + 1, files_to_send, links_to_send)
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)

    total_time = time.time() - start_total

    # Фильтруем только успешные результаты (не исключения)
    valid_results = [r for r in results if isinstance(r, dict)]
    success_count = sum(1 for r in valid_results if r["success"])
    error_count = len(valid_results) - success_count

    # Собираем времена
    times = [r["elapsed"] for r in valid_results if r.get("elapsed") is not None]
    min_time = min(times) if times else 0
    max_time = max(times) if times else 0
    avg_time = sum(times) / len(times) if times else 0

    # ==============================
    # Вывод статистики
    # ==============================
    print("\n" + "="*60)
    print(f"Успешно: {success_count}")
    print(f"Ошибок:  {error_count}")
    print(f" Время на запросы:")
    print(f" Мин: {min_time:.2f} сек")
    print(f" Макс: {max_time:.2f} сек")
    print(f" Среднее: {avg_time:.2f} сек")
    print(f"Общее время выполнения: {total_time:.2f} сек")
    print("="*60)

# ==============================
# Запуск
# ==============================

if __name__ == "__main__":
    asyncio.run(main())