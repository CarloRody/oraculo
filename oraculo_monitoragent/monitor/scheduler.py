"""Agendador de scans periódicos — implementação mínima, sem dependência
nova (croniter/APScheduler). Mesma filosofia de manter este serviço leve
num SBC de 3.8GB (ver docstring de rag_wrapper.py). Entende só o subconjunto
de sintaxe cron já usado em config.yaml (5 campos: minuto hora dia-do-mês
mês dia-da-semana, com '*' ou lista separada por vírgula) — suficiente pra
scans periódicos, não é um cron completo (sem ranges "1-5" nem steps "*/15").
"""

import asyncio
from datetime import datetime


def _field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    return value in {int(p) for p in field.split(",")}


def cron_matches(cron_expr: str, when: datetime) -> bool:
    """True se `when` satisfaz os 5 campos de `cron_expr`."""
    parts = cron_expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        _field_matches(minute, when.minute)
        and _field_matches(hour, when.hour)
        and _field_matches(dom, when.day)
        and _field_matches(month, when.month)
        and _field_matches(dow, when.isoweekday() % 7)  # cron: 0/7=domingo; isoweekday domingo=7
    )


async def run_scheduler(cron_expr: str, scan_fn, poll_seconds: int = 30):
    """Loop de fundo: acorda a cada `poll_seconds`, roda `scan_fn()` (função
    síncrona, potencialmente demorada) numa thread separada — sem isso,
    travaria o event loop e derrubaria o resto da API enquanto escaneia —
    no máximo uma vez por minuto que casar com o cron. Nunca levanta:
    um scan que falha só é logado, o loop continua rodando."""
    last_run_minute = None
    loop = asyncio.get_running_loop()
    while True:
        now = datetime.now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if minute_key != last_run_minute and cron_matches(cron_expr, now):
            last_run_minute = minute_key
            print(f"[scheduler] Cron '{cron_expr}' casou às {minute_key} — iniciando scan_all()")
            try:
                result = await loop.run_in_executor(None, scan_fn)
                print(f"[scheduler] Scan concluído: {result.get('total_scanned', '?')} URL(s), "
                      f"{result.get('changed', '?')} mudança(s), {result.get('errors', '?')} erro(s)")
            except Exception as e:
                print(f"[scheduler] Scan periódico falhou: {e}")
        await asyncio.sleep(poll_seconds)
