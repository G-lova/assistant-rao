"""Дозагрузка положений о закупке (223-ФЗ) из ЕИС за период — для отчёта о положениях организаций (11.11.2026).

ЕИС отдаёт положения только запросом «регион × дата» (``getDocsByOrgRegionRequest``), поэтому год — это
регионы × дни запросов. Порядок выбран так, чтобы полезное приходило раньше:

1. **Сначала типовые положения** (``typalOrderClause``): их мало, и они покрывают большинство организаций — все
   заказчики, присоединённые к типовому положению учредителя, перечислены в самом положении. Положение размещает
   учредитель (федеральные — в Москве), поэтому регион 77 идёт первым, затем регионы по числу наших организаций.
2. **Затем положения заказчиков** (``orderClause``) — только по регионам организаций, которых нет ни в одном
   загруженном типовом положении (:meth:`ClauseBackfill.scope`): остальным регионам запросы не нужны.
3. **Дни — от сегодняшнего назад:** последняя версия типового положения содержит полный текущий список заказчиков,
   поэтому покрытие появляется уже в первые часы.

**Лимит ЕИС** — 90 запросов в минуту на всех, кто ходит с адреса (в т. ч. ночная загрузка внешней системы).
Между запросами — ``TYPAL_EIS_MIN_INTERVAL_SEC`` (1,5 с ≈ 40 в минуту, половина лимита); при ответе «превышено
количество запросов» — пауза ``TYPAL_EIS_LIMIT_WAIT_SEC`` и повтор (``TypalLoader.load_region``).

**Журнал** ``rm_eis_backfill_log`` (миграция 016): выполненные пары «вид × регион × день» при повторном запуске
пропускаются — дозагрузку можно прервать и продолжить; ошибки повторяются.

**Положения заказчиков:** сохраняются все найденные в архиве (реквизиты, заказчик, способы — дёшево, пригодится для
новых организаций), но файлы скачиваются и читаются только у организаций справочника (``inns``). Файлы типовых
положений при дозагрузке не читаются: после неё :meth:`TypalLoader.read_latest_files` читает только последние
версии — иначе скачивали бы одни и те же положения на каждую версию года.
"""
import time
from collections import Counter
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from configs.logger import get_logger

logger = get_logger(__name__)

FIRST_REGIONS = ("77",)   # учредители федеральных организаций (Минобрнауки и др.) — Москва


def region_order(inns: Iterable[str], first: Sequence[str] = FIRST_REGIONS) -> List[str]:
    """Регионы ФНС по первым двум цифрам ИНН: сначала ``first`` (всегда — там размещают типовые положения
    федеральные учредители), затем по числу организаций. Для положений заказчиков ``first = ()``.

    Код региона в ИНН — регион налоговой инспекции, где организация поставлена на учёт; почти всегда совпадает с
    регионом размещения в ЕИС.
    """
    cnt = Counter(str(i).strip()[:2] for i in inns if str(i or "").strip()[:2].isdigit())
    rest = [r for r, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])) if r not in first]
    return list(first) + rest


def days_back(date_from: date, date_to: date) -> List[str]:
    """Дни периода от последнего к первому, ``ГГГГ-ММ-ДД``."""
    n = (date_to - date_from).days
    return [(date_to - timedelta(days=i)).isoformat() for i in range(n + 1)]


class ClauseBackfill:
    """Дозагрузка за период с журналом.

    Args:
        loader: ``TypalLoader`` (``read_files`` выключается: файлы — отдельным шагом).
        inns: ИНН организаций справочника (файлы положений заказчиков — только их).
    """

    def __init__(self, loader: Any, inns: Iterable[str]):
        """Настраивает загрузчик: без файлов, положения заказчиков других организаций — только реквизиты."""
        self.loader = loader
        self.inns = {str(i).strip() for i in inns if str(i or "").strip()}
        loader.read_files = False
        loader.only_inns = self.inns
        loader.keep_others_metadata = True
        self.pool = loader.repo.pool

    async def scope(self, kind: str) -> set:
        """ИНН, по регионам которых идёт загрузка вида.

        Типовые — все организации. Положения заказчиков — только организации, которых нет ни в одном загруженном
        типовом положении: остальные уже покрыты типовым, а запросов «регион × день» по их регионам не нужно
        (собственное положение при типовом учредителя у них не найдётся — это редкий случай, его ловит отчёт по
        уже загруженным данным).
        """
        if kind != "customer":
            return self.inns
        rows = await self.pool.fetch(
            "SELECT DISTINCT cu.inn FROM rm_typal_clause_customers cu JOIN rm_typal_clauses c ON c.id = cu.clause_id "
            "WHERE c.clause_type = 'typal' AND cu.inn = ANY($1::text[])", sorted(self.inns))
        covered = {r["inn"] for r in rows}
        rest = self.inns - covered
        logger.info(f"Положения заказчиков: организаций без типового положения {len(rest)} из {len(self.inns)}")
        return rest

    async def done_pairs(self, kind: str) -> set:
        """Выполненные пары «регион, день» вида (``done`` / ``no_data``)."""
        rows = await self.pool.fetch(
            "SELECT region, day FROM rm_eis_backfill_log WHERE kind = $1 AND status IN ('done', 'no_data')", kind)
        return {(r["region"], r["day"].isoformat()) for r in rows}

    async def _log(self, kind: str, region: str, day: str, status: str, versions: int = 0,
                   error: Optional[str] = None) -> None:
        """Запись журнала (перезапись при повторе)."""
        await self.pool.execute(
            "INSERT INTO rm_eis_backfill_log (kind, region, day, status, versions, error) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (kind, region, day) DO UPDATE SET status = EXCLUDED.status, versions = EXCLUDED.versions, "
            "error = EXCLUDED.error, done_at = NOW()",
            kind, region, date.fromisoformat(day), status, versions, (error or "")[:500] or None)

    async def run(self, date_from: date, date_to: date, kinds: Sequence[str] = ("typal", "customer"),
                  regions: Optional[Sequence[str]] = None, max_requests: Optional[int] = None) -> Dict[str, Any]:
        """Дозагрузка: вид → регион → дни назад; выполненные пары пропускаются.

        Args:
            date_from: Первый день периода.
            date_to: Последний день периода.
            kinds: ``typal`` и / или ``customer``.
            regions: Коды регионов (по умолчанию — по ИНН организаций, :func:`region_order`).
            max_requests: Остановиться после стольких запросов (порциями, например на ночь).

        Returns:
            Dict[str, Any]: ``requests``, ``versions``, ``errors``, ``skipped``, ``left`` (сколько пар осталось),
            ``seconds``, ``stats`` загрузчика.
        """
        days = days_back(date_from, date_to)
        t0 = time.monotonic()
        out: Dict[str, Any] = {"requests": 0, "versions": 0, "errors": 0, "skipped": 0, "left": 0,
                               "regions": {}, "kinds": list(kinds)}
        stop = False
        for kind in kinds:
            regs = list(regions) if regions else region_order(await self.scope(kind),
                                                              FIRST_REGIONS if kind == "typal" else ())
            out["regions"][kind] = regs
            done = await self.done_pairs(kind)
            for reg in regs:
                for day in days:
                    if (reg, day) in done:
                        out["skipped"] += 1
                        continue
                    if stop or (max_requests is not None and out["requests"] >= max_requests):
                        stop = True
                        out["left"] += 1
                        continue
                    out["requests"] += 1
                    try:
                        self.loader.last_eis = {"status": "success", "error": None}
                        loaded = await self.loader.load_region(reg, day, kind=kind)
                        eis = getattr(self.loader, "last_eis", None) or {"status": "success"}
                        if eis["status"] == "error":
                            out["errors"] += 1
                            await self._log(kind, reg, day, "error", error=eis.get("error"))
                            continue
                        status = "done" if eis["status"] == "success" else "no_data"
                        await self._log(kind, reg, day, status, len(loaded))
                        out["versions"] += len(loaded)
                    except Exception as e:  # noqa: BLE001 — день не останавливает остальные, повторится при запуске
                        out["errors"] += 1
                        logger.warning(f"Дозагрузка положений ({kind}) {reg} за {day}: {type(e).__name__}: {e}")
                        await self._log(kind, reg, day, "error", error=f"{type(e).__name__}: {e}")
                    if out["requests"] % 100 == 0:
                        logger.info(f"Дозагрузка положений: запросов {out['requests']}, версий {out['versions']}, "
                                    f"ошибок {out['errors']}; сейчас {kind} {reg} {day}")
        out["seconds"] = round(time.monotonic() - t0)
        out["stats"] = dict(self.loader.stats)
        return out

    async def progress(self) -> List[Dict[str, Any]]:
        """Сводка журнала по видам и статусам."""
        rows = await self.pool.fetch(
            "SELECT kind, status, COUNT(*) AS n, SUM(versions) AS versions, MAX(done_at) AS last "
            "FROM rm_eis_backfill_log GROUP BY kind, status ORDER BY kind, status")
        return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()} for r in rows]
