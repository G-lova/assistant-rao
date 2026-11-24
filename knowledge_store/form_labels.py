"""Оверлей названий полей форм: файл ``form_labels.json`` едет вместе с кодом.

Нужен, чтобы поправленные названия из справочника полей (``fields_spravochnik``) попадали в сервис обычным деплоем,
без запуска скриптов в контейнере прода. Названия из БД (``pe_form_fields``) оверлей дополняет и перекрывает.
Формат файла: ``{"<код формы сервиса>": {"<ключ поля>": "<название>"}}``, например ``44fz_goods_obj7``.
Файл формируется ``python -m scripts.export_form_labels <fields_spravochnik.xlsx>``.
"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Dict

LABELS_PATH = Path(__file__).with_name("form_labels.json")

# Тип формы из справочника → вид/способ в коде формы сервиса (см. knowledge_store.forms.build_form_code).
_METHOD = {"konkurs": "competition", "aukcion": "auction", "kotirovki": "quotation", "ed_postavshchik": "single_supplier",
           "tovary": "goods", "raboty": "works", "nir": "nir"}
_REPORT_METHODS = {"goods", "works", "nir"}


def service_form_codes(form_type: str, object_code: int) -> list:
    """Коды форм сервиса для типа формы справочника (``fz44_tovary`` → ``["44fz_goods_obj7"]``).

    Формы 223-ФЗ «конкурс/аукцион/котировки» в сервисе одна — ``223fz_competitive_obj6``; формы объекта 7 заведены
    отдельно для каждого закона, названия для них одинаковы.

    Args:
        form_type: Значение колонки «Тип формы» справочника (``fz44_konkurs``, ``fz223_nir`` …).
        object_code: Код объекта (6 или 7).

    Returns:
        Список кодов форм; пустой, если тип неизвестен.
    """
    law, _, kind = form_type.partition("_")
    prefix = {"fz44": "44fz", "fz223": "223fz"}.get(law)
    method = _METHOD.get(kind)
    if not prefix or not method:
        return []
    if prefix == "223fz" and method in ("competition", "auction", "quotation"):
        method = "competitive"
    return [f"{prefix}_{method}_obj{int(object_code)}"]


@lru_cache(maxsize=1)
def load_overlay() -> Dict[str, Dict[str, str]]:
    """Читает ``form_labels.json``; нет файла или он повреждён — пустой оверлей (сервис работает по названиям из БД)."""
    try:
        return json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def labels_for_form(code: str) -> Dict[str, str]:
    """Названия полей формы ``code`` из оверлея (пустой словарь, если формы в оверлее нет)."""
    return dict(load_overlay().get(code, {}))
