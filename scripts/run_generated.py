"""Прогон ``POST /generate-summary-opinion`` по списку экспертиз и сохранение ответов для ``scripts.eval_generated``.

Для каждого ``expertise_id`` запускает задачу, ждёт результат через ``GET /task/{id}`` и пишет полный ответ в
``<папка>/<expertise_id>.json``. Уже сохранённые файлы пропускаются (``--force`` — перезаписать), поэтому
прерванный прогон можно продолжить. Только стандартная библиотека.

Запуск::

    # внутри контейнера rao_api (порт 20142, ключ берётся из окружения API_KEY):
    python -m scripts.run_generated --base-url http://localhost:20142 --ids 4669,4684,5110 --out gen_new/
    python -m scripts.run_generated --base-url http://dev:8000 --csv expertise_expert_opinion7s_rao.csv --out gen_new/ --rebuild-facts

Затем::

    python -m scripts.eval_generated "Отчеты и закупки поля.xlsx" expertise_expert_opinion7s_rao.csv gen_new/
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from scripts.eval_eis_rules import latest_opinions
from scripts.eval_generated import OUTLIERS


def call(method: str, url: str, database: str, body: Optional[dict] = None, timeout: int = 60,
         api_key: Optional[str] = None) -> tuple:
    """HTTP-запрос с JSON; возвращает ``(код, объект ответа)``. Ошибки HTTP не бросают исключение.

    Заголовок ``X-API-Key`` обязателен для сервиса (``APIKeyMiddleware``); ``X-API-Database`` выбирает базу.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "X-API-Database": database}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except OSError:
            raw = ""
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"detail": raw[:300]}


def run_one(expertise_id: str, args: argparse.Namespace) -> Dict[str, str]:
    """Обёртка над :func:`_run_one`: сетевая ошибка по одной экспертизе не прерывает весь прогон."""
    try:
        return _run_one(expertise_id, args)
    except (OSError, ValueError) as e:       # URLError, обрыв соединения, неразобранный ответ
        return {"id": expertise_id, "outcome": "failed", "detail": f"{type(e).__name__}: {e}"[:300]}


def _run_one(expertise_id: str, args: argparse.Namespace) -> Dict[str, str]:
    """Запускает задачу по одной экспертизе и ждёт результат.

    Returns:
        dict: ``{"id", "outcome", "detail"}``; ``outcome`` — ``ok`` / ``skipped`` / ``rejected`` / ``failed`` / ``timeout``.
    """
    path = os.path.join(args.out, f"{expertise_id}.json")
    if os.path.exists(path) and not args.force:
        return {"id": expertise_id, "outcome": "skipped", "detail": "файл уже есть"}
    code, started = call("POST", f"{args.base_url}/generate-summary-opinion", args.database,
                         {"expertise_id": int(expertise_id), "send_draft": False, "rebuild_facts": args.rebuild_facts},
                         api_key=args.api_key)
    if code != 200:
        return {"id": expertise_id, "outcome": "rejected", "detail": f"HTTP {code}: {(started or {}).get('detail')}"}
    task_id, deadline = started["task_id"], time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(args.poll)
        code, state = call("GET", f"{args.base_url}/task/{task_id}", args.database, api_key=args.api_key)
        status = (state or {}).get("status")
        if status == "completed":
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            return {"id": expertise_id, "outcome": "ok", "detail": (state.get("result") or {}).get("status", "")}
        if status == "failed":
            return {"id": expertise_id, "outcome": "failed", "detail": str(state.get("error"))[:300]}
    return {"id": expertise_id, "outcome": "timeout", "detail": f"нет результата за {args.timeout} с"}


def ids_from_csv(path: str) -> List[str]:
    """Идентификаторы экспертиз из выгрузки заключений экспертов (без выбросов эталона)."""
    return sorted(e for e in latest_opinions(path) if e not in OUTLIERS)


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: разбирает аргументы, прогоняет экспертизы, печатает сводку."""
    parser = argparse.ArgumentParser(description="Прогон /generate-summary-opinion по списку экспертиз")
    parser.add_argument("--base-url", required=True, help="адрес сервиса, например http://localhost:8000")
    parser.add_argument("--out", required=True, help="папка для <expertise_id>.json")
    parser.add_argument("--ids", help="список expertise_id через запятую")
    parser.add_argument("--csv", help="выгрузка заключений экспертов: взять все expertise_id из неё")
    parser.add_argument("--database", default="dev", help="значение заголовка X-API-Database (по умолчанию dev)")
    parser.add_argument("--api-key", default=os.environ.get("API_KEY"),
                        help="значение заголовка X-API-Key (по умолчанию переменная окружения API_KEY, в контейнере она есть)")
    parser.add_argument("--rebuild-facts", action="store_true", help="пересчитать факты модели перед сборкой")
    parser.add_argument("--workers", type=int, default=2, help="сколько экспертиз вести параллельно")
    parser.add_argument("--timeout", type=int, default=1800, help="сколько секунд ждать одну экспертизу")
    parser.add_argument("--poll", type=float, default=10.0, help="период опроса /task/{id}, секунд")
    parser.add_argument("--force", action="store_true", help="перезаписать уже сохранённые ответы")
    args = parser.parse_args(argv)
    args.base_url = args.base_url.rstrip("/")
    ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else (ids_from_csv(args.csv) if args.csv else [])
    if not ids:
        parser.error("нужен --ids или --csv")
    if not args.api_key:
        parser.error("нет ключа: задайте --api-key или переменную окружения API_KEY")
    os.makedirs(args.out, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = []
        for res in pool.map(lambda i: run_one(i, args), ids):
            print(f"{res['id']}: {res['outcome']} {res['detail']}", flush=True)
            results.append(res)
    bad = [r for r in results if r["outcome"] not in ("ok", "skipped")]
    print(f"\nГотово: {len(results) - len(bad)} из {len(results)}; проблемных: {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
