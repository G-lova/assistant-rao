"""Хранилище знаний «РАО Эксперт»: запись результатов /evaluate-documents в таблицы ``pe_*``.

Модуль работает по принципу write-through: основной пайплайн не зависит от результата записи,
все вызовы идут через :func:`knowledge_store.safe.safe_call_async` и управляются флагом
``KNOWLEDGE_STORE_ENABLED``.
"""
