"""Профили извлечения по типу документа: какие поля ``raw_data`` модель заполняет для данного типа.

Раньше у каждого документа модель заполняла одну и ту же схему из 14 полей. У акта, декларации или платёжного
поручения она «находила» метод обоснования НМЦК, сроки отчётных документов и т. п. — неуместные поля были главным
местом, где модель выдумывала (экспертиза 7185: НМЦК 1 500 000 в декларации, срок отчёта 2024-10-15 в договоре).

Теперь тип определяется заранее (:class:`evaluate_documents.type_detector.TypeDetector`), и модели отдаётся схема только
с полями профиля этого типа. Остальные поля в итоговом ``raw_data`` остаются пустыми по шаблону объединения — формат
ответа не меняется.

Здесь же — разметка страниц в блоках текста (:func:`annotate_pages`): после нарезки блок, начавшийся с середины
страницы, получает метку «[Продолжение страницы N]», чтобы модель указывала в ``evidence.page`` настоящую страницу.
"""
import copy
import json
import re
from typing import Any, Dict, List, Optional

# Все поля raw_data в порядке схемы
ALL_FIELDS = (
    "document_name", "summary", "contract_number", "dates", "finances", "nmck_method", "organizations",
    "law_references", "procurement_subject", "items", "planned_timeline", "reporting_documentation",
    "execution_location", "other_data",
)
# Поля, которые есть в любом профиле
BASE_FIELDS = ("document_name", "summary")

PROFILES: Dict[str, tuple] = {
    # договор / контракт / проект: всё, кроме метода НМЦК (в договоре его почти никогда нет — модель его придумывала)
    "contract": tuple(f for f in ALL_FIELDS if f != "nmck_method"),
    # документы закупки: извещение, описание объекта, требования к заявке, порядок оценки — все поля
    "procurement": ALL_FIELDS,
    "nmck": ("document_name", "summary", "dates", "finances", "nmck_method", "organizations", "law_references",
             "items", "other_data"),
    # акты, накладные, УПД
    "act": ("document_name", "summary", "contract_number", "dates", "finances", "organizations", "items",
            "execution_location", "other_data"),
    "supplement": ("document_name", "summary", "contract_number", "dates", "finances", "organizations", "items",
                   "planned_timeline", "other_data"),
    "payment": ("document_name", "summary", "contract_number", "dates", "finances", "organizations", "other_data"),
    # сертификаты, декларации, паспорта, гарантийные письма, фото
    "certificate": ("document_name", "summary", "contract_number", "dates", "organizations", "items",
                    "law_references", "other_data"),
    # отчёты, экспертиза результатов, документы об исполнении, взыскание пеней
    "report": ("document_name", "summary", "contract_number", "dates", "finances", "organizations", "items",
               "reporting_documentation", "other_data"),
    "regulation": ("document_name", "summary", "dates", "organizations", "law_references", "other_data"),
    # прочее и «дополнительные материалы»
    "general": ("document_name", "summary", "contract_number", "dates", "finances", "organizations",
                "law_references", "other_data"),
    # полная схема (риск-мониторинг и вызовы без типа)
    "full": ALL_FIELDS,
}

PROFILE_TITLES = {
    "contract": "договор (контракт)", "procurement": "документ закупки", "nmck": "обоснование НМЦК",
    "act": "акт / документ о приёмке", "supplement": "дополнительное соглашение", "payment": "платёжное поручение",
    "certificate": "сертификат / декларация / паспорт", "report": "отчёт / документ об исполнении",
    "regulation": "положение о закупке", "general": "прочий документ", "full": "документ",
}

TYPE_TO_PROFILE = {
    **dict.fromkeys(("docContractDoWorkFiles", "docContractNIRFiles", "docContractPostTovarFiles",
                     "docProjContractFiles"), "contract"),
    **dict.fromkeys(("docIzvejenieFiles", "docOpusObjectZacupFiles", "docTrebContentRequestFiles",
                     "docPorViewOcenkFiles", "docAcceptInafPostavFiles"), "procurement"),
    **dict.fromkeys(("docObosnNMCKFiles", "docMaterialValidNMCKFiles"), "nmck"),
    **dict.fromkeys(("docDocPriemActSdachFiles", "docActPriemTovFiles", "docPriemTovSchetFiles",
                     "docCargoTaxFiles"), "act"),
    "docDopConsentContractFiles": "supplement",
    **dict.fromkeys(("docCertValidFiles", "docTechDocFiles", "docValidGarantFiles", "docValidCountyFiles",
                     "docValidCopyriteFiles", "docPhotoCargoFiles", "docPhotoFinishWorkFiles"), "certificate"),
    **dict.fromkeys(("docExpertReportFiles", "docReportDoNIRFiles", "docValidAllIfFiles", "docVziskPenyFiles"),
                    "report"),
    "docAssetSelOrgFiles": "regulation",
}

PAYMENT_RE = re.compile(r"пл[аa]т[её]жн\w*\s+поручени|пл[аa]т[её]жк", re.I)
# Названия документов, по которым профиль понятен лучше, чем по коду типа
NAME_PROFILE_RE = (
    (PAYMENT_RE, "payment"),
    (re.compile(r"дополнительн\w*\s+соглашени|(^|[^а-я])дс[-_ №]", re.I), "supplement"),
    (re.compile(r"(^|[^а-я])кс[-_ ]?[23]|акт\w*\s+(о\s+)?при[её]мк|акт\w*\s+сдачи|упд|товарн\w*\s+накладн", re.I), "act"),
)


def profile_for(detected_type: Optional[str], filename: str = "", head_text: str = "") -> str:
    """Профиль извлечения для документа.

    Платёжное поручение определяется по имени файла или началу текста (своего кода типа у него нет). Акты и
    допсоглашения, загруженные как «дополнительные материалы», узнаются по имени файла.

    Args:
        detected_type: Тип документа от :class:`TypeDetector` (код из ``DOCUMENT_TYPE_MAPPING``).
        filename: Имя файла.
        head_text: Начало текста документа (первые 2–3 тыс. символов достаточно).

    Returns:
        str: Ключ :data:`PROFILES`.
    """
    if PAYMENT_RE.search(filename or "") or PAYMENT_RE.search((head_text or "")[:1500]):
        return "payment"
    profile = TYPE_TO_PROFILE.get(detected_type or "")
    if profile:
        return profile
    for pattern, name in NAME_PROFILE_RE:
        if pattern.search(filename or ""):
            return name
    return "general"


def profile_fields(profile: Optional[str]) -> tuple:
    """Поля ``raw_data`` профиля (неизвестный профиль — полная схема)."""
    return PROFILES.get(profile or "full", ALL_FIELDS)


def schema_for_profile(schema: Dict[str, Any], profile: Optional[str]) -> Dict[str, Any]:
    """Схема извлечения только с полями профиля.

    Args:
        schema: Полная схема (``readability`` + ``raw_data``; у старой схемы — и ``type_compliance``).
        profile: Ключ :data:`PROFILES`; ``None``/``"full"`` — схема без изменений.

    Returns:
        dict: Копия схемы; в ``raw_data.properties`` — только поля профиля, ``required`` — их пересечение.
    """
    if not profile or profile == "full":
        return schema
    out = copy.deepcopy(schema)
    raw = out["properties"]["raw_data"]
    keep = set(profile_fields(profile))
    raw["properties"] = {k: v for k, v in raw["properties"].items() if k in keep}
    if isinstance(raw.get("required"), list):
        raw["required"] = [k for k in raw["required"] if k in keep]
    return out


def profile_hint(profile: Optional[str], detected_type: Optional[str] = None, type_title: str = "") -> str:
    """Подсказка модели: что за документ и какие поля заполнять.

    Args:
        profile: Ключ :data:`PROFILES`.
        detected_type: Код типа (для текста подсказки).
        type_title: Название типа по-русски.

    Returns:
        str: Пара строк для сообщения пользователя (пустая строка для полной схемы).
    """
    if not profile or profile == "full":
        return ""
    title = type_title or PROFILE_TITLES.get(profile, "")
    fields = ", ".join(f for f in profile_fields(profile) if f not in BASE_FIELDS)
    return (f"Тип документа уже определён: {title}" + (f" ({detected_type})" if detected_type else "") + ".\n"
            f"Заполняй только поля схемы: {fields}. Не переноси в них сведения о других документах.\n")


PAGE_RE = re.compile(r"(?:^|\n)\s*(?:Страница|Page)\s+(\d{1,4})\b", re.I)


def annotate_pages(chunks: List[str]) -> List[str]:
    """Добавляет к блокам, начинающимся с середины страницы, метку «[Продолжение страницы N]».

    Текст PDF после OCR размечен строками «Страница N (OCR): …»; после нарезки на блоки модель не видела, к
    какой странице относится начало блока, и писала ``page`` наугад.

    Args:
        chunks: Блоки текста по порядку.

    Returns:
        list[str]: Те же блоки; без меток страниц в тексте — без изменений.
    """
    out, last_page = [], None
    for chunk in chunks:
        starts_with_page = bool(re.match(r"\s*(?:Страница|Page)\s+\d", chunk or "", re.I))
        if last_page is not None and not starts_with_page:
            chunk = f"[Продолжение страницы {last_page}]\n{chunk}"
        pages = PAGE_RE.findall(chunk or "")
        if pages:
            last_page = pages[-1]
        out.append(chunk)
    return out


def describe_raw_data(raw: Dict[str, Any]) -> str:
    """Краткая сводка извлечённого для журнала: «номер=…, сумм 3, дат 5, сторон 2».

    Args:
        raw: ``raw_data``.

    Returns:
        str: Сводка (без значений, кроме номера договора).
    """
    if not isinstance(raw, dict):
        return "нет данных"
    parts = []
    cn = raw.get("contract_number")
    if isinstance(cn, dict) and cn.get("value"):
        parts.append(f"номер={cn['value']}")
    for key, label in (("finances", "сумм"), ("dates", "дат"), ("organizations", "сторон"),
                       ("items", "позиций"), ("law_references", "норм")):
        value = raw.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label} {len(value)}")
    nm = raw.get("nmck_method")
    if isinstance(nm, dict) and nm.get("method"):
        parts.append(f"метод НМЦК={nm['method']}")
    return ", ".join(parts) or "ничего не извлечено"


def json_size(obj: Any) -> int:
    """Размер JSON в символах (для журнала)."""
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return -1
