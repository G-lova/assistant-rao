"""Проверка модели извлечения теми же запросами, что шлют анализаторы (без повторов клиента).

Запуск в контейнере воркера (тот же .env, сеть и модель)::

    docker compose exec rao_celery_worker_rm python -m scripts.llm_probe
    docker compose exec rao_celery_worker_rm python -m scripts.llm_probe --parallel 5 --chars 8000

Проверки:

1. короткий запрос (5 токенов) — жив ли экземпляр модели;
2. запрос извлечения данных: промпт ``data_extractor_prompt.txt``, схема ``guided_json``, ``max_tokens`` как у
   ``DataExtractor`` — так выглядит реальная нагрузка (504 шлюза бывает и у живой модели, если ответ генерируется
   дольше таймаута шлюза);
3. ``--parallel N`` таких запросов одновременно — как при прогоне (2 процесса × ``RM_FILE_CONCURRENCY``).

Для каждого запроса — код/ошибка, время, токены запроса и ответа, ``finish_reason`` и скорость генерации.
"""
import argparse
import asyncio
import json
import time

SAMPLE = (
    "ГОСУДАРСТВЕННЫЙ КОНТРАКТ № 0123100000126000001 г. Москва «09» июля 2026 г. ГБУ «Заказчик», ИНН 7701234567, "
    "КПП 770101001, именуемое в дальнейшем «Заказчик», и ООО «Поставщик», ИНН 7702345678, заключили настоящий "
    "контракт о нижеследующем. 1. Предмет контракта: поставка медицинских изделий согласно спецификации. "
    "2. Цена контракта составляет 1 900 000 (один миллион девятьсот тысяч) рублей 00 копеек, НДС не облагается. "
    "3. Срок поставки: до 30 сентября 2026 года. Аванс 30 % цены контракта. Обеспечение исполнения — 5 %. "
)


async def one(client, model, messages, extra, max_tokens, label):
    """Один запрос без повторов: итог и замеры."""
    t = time.monotonic()
    try:
        r = await client.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens,
                                                 temperature=0, extra_body=extra or None)
        sec = time.monotonic() - t
        u = getattr(r, "usage", None)
        out_tok = getattr(u, "completion_tokens", None)
        speed = f", {out_tok / sec:.0f} ток/с" if out_tok and sec > 0 else ""
        print(f"{label}: 200 за {sec:.1f} с; токены запроса {getattr(u, 'prompt_tokens', '?')}, ответа {out_tok}"
              f"{speed}; finish_reason={r.choices[0].finish_reason}; "
              f"ответ {len(r.choices[0].message.content or '')} симв.")
    except Exception as e:  # noqa: BLE001 — нужен итог, а не трассировка
        status = getattr(e, "status_code", None)
        print(f"{label}: ОШИБКА за {time.monotonic() - t:.1f} с: {type(e).__name__} {status or ''} {str(e)[:200]}")


async def main(parallel: int, chars: int, timeout: float) -> None:
    """Три проверки по очереди."""
    from openai import AsyncOpenAI

    from configs import settings
    from configs.config import Config
    from configs.resources import read_text
    from evaluate_documents.doc_profiles import schema_for_profile

    cfg = Config.get_m_model_config()
    # без resilient(): повторы и паузы клиента скрыли бы, сколько длится один запрос
    client = AsyncOpenAI(base_url=cfg.api_url.rstrip("/"), api_key=cfg.api_key, timeout=timeout, max_retries=0)
    print(f"Модель {cfg.model}, {cfg.api_url}; таймаут клиента {timeout:.0f} с, "
          f"EXTRACTION_MAX_TOKENS={settings.EXTRACTION_MAX_TOKENS}")

    await one(client, cfg.model, [{"role": "user", "content": "Ответь одним словом: да"}], None, 5, "1. короткий")

    text = (SAMPLE * (chars // len(SAMPLE) + 1))[:chars]
    schema = schema_for_profile(json.loads(read_text("schemas/data_extractor_schema.json")), None)
    messages = [{"role": "system", "content": read_text("prompts/data_extractor_prompt.txt")},
                {"role": "user", "content": f"Проанализируй текст, оцени его читаемость, извлеки данные:\n\n{text}"}]
    extra = {"guided_json": schema}
    await one(client, cfg.model, messages, extra, settings.EXTRACTION_MAX_TOKENS, f"2. извлечение ({chars} симв.)")
    if parallel > 1:
        print(f"3. {parallel} запросов извлечения одновременно:")
        await asyncio.gather(*(one(client, cfg.model, messages, extra, settings.EXTRACTION_MAX_TOKENS, f"   №{i + 1}")
                               for i in range(parallel)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--parallel", type=int, default=3, help="одновременных запросов извлечения (по умолчанию 3)")
    ap.add_argument("--chars", type=int, default=3000, help="символов текста в запросе извлечения (по умолчанию 3000)")
    ap.add_argument("--timeout", type=float, default=600, help="таймаут клиента, с (по умолчанию 600)")
    a = ap.parse_args()
    asyncio.run(main(a.parallel, a.chars, a.timeout))
