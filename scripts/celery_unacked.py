"""Задачи Celery, «зависшие» после пересборки или падения воркера: просмотр и возврат в очередь.

Почему зависают. В ``celery_app.py`` включено ``task_acks_late=True``: брокер (Redis) получает подтверждение
только после завершения задачи. Пока задача выполняется, её сообщение лежит в служебных ключах Redis
``unacked`` / ``unacked_index``. Если воркер остановлен жёстко (``docker compose up --build`` / ``down`` убивает
контейнер через 10 с, если воркер не успел завершиться), подтверждения нет, и Redis вернёт сообщение в
очередь только через ``VISIBILITY_TIMEOUT`` (4 ч). Всё это время ``GET /task/{id}`` отвечает ``pending``.

Порядок после пересборки:

1. Остановить воркеры: ``docker compose stop celery_worker celery_worker_rm``.
2. Посмотреть: ``docker compose run --rm celery_worker python -m scripts.celery_unacked``.
3. Вернуть в очереди: ``docker compose run --rm celery_worker python -m scripts.celery_unacked --restore``.
4. Запустить воркеры: ``docker compose start celery_worker celery_worker_rm``.

Если воркеры при этом работают, возвращайте только старые сообщения (``--min-age 600``): сообщение задачи,
которая выполняется сейчас, вернётся в очередь и выполнится второй раз.

Повторный запуск уже выполненной задачи защищён (``celery_app.already_succeeded``), а задачи, которые шли в
момент остановки, выполнятся заново с начала.
"""
import argparse
import json
import time
from typing import Any, Dict, List


def unacked(min_age: float = 0.0) -> List[Dict[str, Any]]:
    """Неподтверждённые сообщения брокера.

    Args:
        min_age: Только сообщения старше стольких секунд (с момента выдачи воркеру).

    Returns:
        List[Dict[str, Any]]: ``tag``, ``age_sec``, ``queue``, ``task``, ``task_id``.
    """
    from celery_app import celery_app

    out = []
    with celery_app.connection_for_write() as conn:
        ch = conn.default_channel
        client = ch.client
        now = time.time()
        for tag, ts in client.zrange(ch.unacked_index_key, 0, -1, withscores=True):
            tag = tag.decode() if isinstance(tag, bytes) else tag
            if now - ts < min_age:
                continue
            raw = client.hget(ch.unacked_key, tag)
            if not raw:
                continue
            message, _exchange, routing_key = json.loads(raw)
            headers = message.get("headers") or {}
            out.append({"tag": tag, "age_sec": int(now - ts), "queue": routing_key,
                        "task": headers.get("task"), "task_id": headers.get("id")})
    return out


def restore(tags: List[str]) -> int:
    """Возвращает сообщения в их очереди (в начало очереди).

    Args:
        tags: Теги доставки (``delivery_tag``).

    Returns:
        int: Сколько возвращено.
    """
    from celery_app import celery_app

    with celery_app.connection_for_write() as conn:
        ch = conn.default_channel
        for tag in tags:
            ch.qos.restore_by_tag(tag, leftmost=True)
    return len(tags)


def main() -> None:
    """Командная строка: просмотр (по умолчанию) или возврат (``--restore``)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restore", action="store_true", help="вернуть сообщения в очереди")
    ap.add_argument("--min-age", type=float, default=0.0, help="только старше N секунд")
    ap.add_argument("--queue", action="append", help="только эти очереди (evaluation, risk_monitoring …)")
    args = ap.parse_args()
    items = [m for m in unacked(args.min_age) if not args.queue or m["queue"] in args.queue]
    for m in items:
        print(f"{m['queue']:<16} {m['task'] or '?':<28} {m['task_id']}  {m['age_sec'] // 60} мин")
    print(f"Всего неподтверждённых: {len(items)}")
    if args.restore and items:
        print(f"Возвращено в очереди: {restore([m['tag'] for m in items])}")


if __name__ == "__main__":
    main()
