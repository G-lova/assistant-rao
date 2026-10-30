"""Разбор ответов модели и запасная модель перепроверки правил (сбой 30.10.2026: «В ответе модели нет JSON»)."""
import asyncio
import unittest
from unittest import mock

from configs import settings
from risk_monitoring import llm_json
from risk_monitoring import rule_exclusions as rx


class ParseTest(unittest.TestCase):
    """``parse_json`` с размышлениями модели."""

    def test_think_stripped(self):
        """Закрытый блок размышлений убирается, JSON разбирается."""
        self.assertEqual(llm_json.parse_json('<think>сначала подумаю {a}</think>\n{"checks": []}'), {"checks": []})

    def test_think_unclosed(self):
        """Ответ оборван на размышлениях — понятная ошибка, а не «нет JSON»."""
        with self.assertRaisesRegex(ValueError, "размышлениях"):
            llm_json.parse_json("<think>долго думаю и не успеваю")


class FakeLLM:
    """Модель с заготовленными ответами."""

    def __init__(self, answers, model="m"):
        """Ответы по очереди."""
        self.answers, self.model = list(answers), model

    async def ask(self, system, user, schema=None, max_tokens=0, list_key=None):
        """Следующий ответ или исключение."""
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


class AskListTest(unittest.TestCase):
    """``LLMJson.ask`` принимает массив как объект с ``list_key``."""

    def test_list_key(self):
        """Массив вместо объекта → {checks: […]}; без list_key — ошибка с журналом ответа."""
        llm = llm_json.LLMJson(object(), "m", retries=1)

        async def fake(*a, **k):
            """Ответ модели — массив."""
            return '[{"indicator": "pz_p2", "status": "unclear", "reason": "нет"}]'

        with mock.patch.object(llm_json, "call_llm", fake):
            data = asyncio.run(llm.ask("s", "u", {"type": "object"}, list_key="checks"))
            self.assertEqual(data["checks"][0]["indicator"], "pz_p2")
            with self.assertLogs(llm_json.logger.name, "WARNING") as logs, self.assertRaises(ValueError):
                asyncio.run(llm.ask("s", "u", {"type": "object"}))
            self.assertIn("ответ не разобран", " ".join(logs.output))


class RetryStrictTest(unittest.TestCase):
    """Повтор после markdown-ответа: схема и «только JSON» в системном промпте даже при guided_json."""

    def test_retry_adds_schema(self):
        """Первый ответ — markdown (как qwen3-vl 30.10.2026), второй — JSON; во втором запросе есть схема."""
        llm = llm_json.LLMJson(object(), "m", retries=2)
        systems, answers = [], ["**checks:** - **pz_p2**: unclear", '{"checks": []}']

        async def fake(client, **k):
            """Ответы по очереди, системный промпт — в список."""
            systems.append(k["messages"][0]["content"])
            return answers.pop(0)

        async def no_sleep(_):
            """Без ожидания между попытками."""

        with mock.patch.object(llm_json, "call_llm", fake), mock.patch.object(llm_json.asyncio, "sleep", no_sleep):
            self.assertEqual(asyncio.run(llm.ask("sys", "u", {"type": "object"})), {"checks": []})
        self.assertNotIn("строго один JSON", systems[0])
        self.assertIn("строго один JSON", systems[1])

    def test_prompt_asks_json(self):
        """Запрос перепроверки прямо требует JSON и перечисляет показатели."""
        cfg = rx.load_config()
        p = rx.build_prompt(["pz_p2"], [{"file_id": 1, "file_name": "a", "text": "t"}], cfg, {})
        self.assertIn('{"checks"', p)
        self.assertIn("Только JSON", p)


class FallbackTest(unittest.TestCase):
    """Перепроверка правил переходит на запасную модель, если основная не дала JSON."""

    def test_fallback(self):
        """Основная — ValueError, запасная — ответ; результат по запасной."""
        main = FakeLLM([ValueError("В ответе модели нет JSON")], "reasoning")
        main.fallback = FakeLLM([{"checks": [{"indicator": "pz_p2", "status": "confirmed", "reason": "есть"}]}], "base")
        texts = {1: "Обеспечение исполнения контракта установлено в размере 5 %."}
        out = asyncio.run(rx.check_rules(main, {"pz_p2": 1}, texts, {1: "проект.docx"}, settings))
        self.assertEqual(out["pz_p2"]["status"], "confirmed")

    def test_no_fallback_raises(self):
        """Запасной нет — ошибка уходит наверх (анализ закупки продолжится без перепроверки)."""
        main = FakeLLM([ValueError("В ответе модели нет JSON")])
        with self.assertRaises(ValueError):
            asyncio.run(rx.check_rules(main, {"pz_p2": 1}, {1: "обеспечение исполнения 5 %"}, {1: "a"}, settings))


if __name__ == "__main__":
    unittest.main()
