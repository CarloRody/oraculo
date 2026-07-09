"""Seed initial monitored URLs (the 4 known external links)."""

from monitor.url_registry import add_url


SEED_URLS = [
    {
        "name": "Portal NFe - Lista de Conteúdo",
        "url": "https://www.nfe.fazenda.gov.br/portal/listaConteudo.aspx?tipoConteudo=ndIjl+iEFdE=&AspxAutoDetectCookieSupport=1",
        "area_id": 3,  # NFe nota fiscal eletrônica
        "fetch_mode": "js_browser",
    },
    {
        "name": "Reforma Tributária - Orientações 2026",
        "url": "https://www.gov.br/receitafederal/pt-br/acesso-a-informacao/acoes-e-programas/programas-e-atividades/reforma-tributaria-do-consumo/orientacoes-2026",
        "area_id": 1,  # Reforma Tributária
        "fetch_mode": "http",
    },
    {
        "name": "Reforma Tributária - Entenda",
        "url": "https://www.gov.br/receitafederal/pt-br/acesso-a-informacao/acoes-e-programas/programas-e-atividades/reforma-tributaria-do-consumo/entenda",
        "area_id": 1,
        "fetch_mode": "http",
    },
    {
        "name": "Reforma Tributária - Marcos",
        "url": "https://www.gov.br/receitafederal/pt-br/acesso-a-informacao/acoes-e-programas/programas-e-atividades/reforma-tributaria-do-consumo/marcos",
        "area_id": 1,
        "fetch_mode": "http",
    },
]


def seed():
    for url_data in SEED_URLS:
        try:
            result = add_url(**url_data)
            print(f"  ✅ {result['name']} (id={result['id']})")
        except ValueError as e:
            print(f"  ⚠️  Skip ({e})")


if __name__ == "__main__":
    seed()
