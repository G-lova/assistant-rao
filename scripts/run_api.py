"""Запуск API (uvicorn) с параметрами из ``configs/settings.py`` — команда сервиса ``rao_api`` в docker-compose.yml.

Раньше команда была ``uvicorn main:app ... --reload`` для всех окружений. Перезапуск по изменению файлов нужен на
dev / stage (правка кода видна сразу), а на проде мешает: код приходит деплоем (``.gitlab-ci.yml``: ``git pull`` →
сборка → тесты → ``docker compose up -d``), и с ``--reload`` API переходил на новый код сразу после ``git pull`` —
до тестов. Теперь ``reload`` — ``settings.API_RELOAD`` (выключен при ``ENVIRONMENT == "prod"``), одна команда
docker-compose.yml для всех окружений, без новых переменных ``.env``.

Запуск::

    python -m scripts.run_api            # параметры из settings
    python -m scripts.run_api --print    # только показать параметры (проверка без запуска)
"""
import argparse
import inspect
import json
from typing import Any, Dict

HOST = "0.0.0.0"
PORT = 20142
TIMEOUT_KEEP_ALIVE = 1200          # как прежде в docker-compose.yml (--timeout-keep-alive 1200)
TIMEOUT_GRACEFUL_SHUTDOWN = 600    # --timeout-graceful-shutdown 600


def uvicorn_kwargs(cfg: Any = None, run_fn: Any = None) -> Dict[str, Any]:
    """Аргументы ``uvicorn.run``: прежние параметры команды и ``reload`` из настроек.

    Args:
        cfg: Настройки (по умолчанию ``configs.settings``).
        run_fn: Функция запуска (по умолчанию ``uvicorn.run``) — по её сигнатуре отбрасываются параметры, которых
            нет в установленной версии uvicorn (чтобы запуск не упал из-за версии).

    Returns:
        Dict[str, Any]: Именованные аргументы для ``uvicorn.run("main:app", **kwargs)``.
    """
    if cfg is None:
        from configs import settings as cfg
    kwargs = {"host": HOST, "port": PORT, "timeout_keep_alive": TIMEOUT_KEEP_ALIVE,
              "timeout_graceful_shutdown": TIMEOUT_GRACEFUL_SHUTDOWN,
              "reload": bool(getattr(cfg, "API_RELOAD", True))}
    if run_fn is not None:
        try:
            params = inspect.signature(run_fn).parameters
            if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):  # сигнатура недоступна — передаём как есть
            pass
    return kwargs


def main() -> None:
    """Разбирает аргументы и запускает uvicorn (или печатает параметры при ``--print``)."""
    parser = argparse.ArgumentParser(description="Запуск API с параметрами из configs/settings.py")
    parser.add_argument("--print", action="store_true", help="показать параметры запуска и выйти")
    args = parser.parse_args()

    import uvicorn

    from configs import settings

    kwargs = uvicorn_kwargs(settings, uvicorn.run)
    print(f"API: ENVIRONMENT={settings.ENVIRONMENT}, параметры uvicorn {json.dumps(kwargs, ensure_ascii=False)}",
          flush=True)
    if args.print:
        return
    # reload требует приложение строкой импорта, а не объектом
    uvicorn.run("main:app", **kwargs)


if __name__ == "__main__":
    main()
