"""Testes de scripts/rodar_analise.py — a recuperação do texto completo
por refatiamento + matching, a única parte testável sem chamar o Gemini
de verdade. Usa o mesmo fixture real do TCE-PR já usado na Etapa 4/5."""

from pathlib import Path

import coletores.tce_pr as tce_pr
from scripts.rodar_analise import _casar_decisao, _recuperar_texto_completo

FIXTURES = Path(__file__).parent / "fixtures"


def _item_tce_pr(**overrides) -> dict:
    html_texto = (FIXTURES / "tce_pr" / "boletim_179.html").read_text(encoding="utf-8")
    texto_bruto = tce_pr._extrair_conteudo_principal(html_texto)
    padrao = dict(
        id=1, item_bruto_id=1, fonte_nome="TCE-PR — Boletim Informativo de Jurisprudência",
        titulo_publicacao="Boletim 179", texto_bruto=texto_bruto,
        url_origem="https://x/boletim-179", data_publicacao=None,
        tribunal="TCE-PR", numero_acordao="3190/2025", numero_processo="330990/2024",
    )
    padrao.update(overrides)
    return padrao


def test_recuperar_texto_completo_acha_a_decisao_certa_entre_varias():
    # o fixture real produz 2 decisões (3190/2025 e 3220/2025) — confirma
    # que o matching pega a certa, não a primeira que aparecer
    item = _item_tce_pr(numero_acordao="3220/2025", numero_processo="582863/2012")

    texto = _recuperar_texto_completo(item)

    assert "3220/2025" in texto
    assert "3190/2025" not in texto  # não pode vazar a outra decisão


def test_recuperar_texto_completo_primeira_decisao():
    item = _item_tce_pr(numero_acordao="3190/2025", numero_processo="330990/2024")

    texto = _recuperar_texto_completo(item)

    assert "3190/2025" in texto
    assert "3220/2025" not in texto


def test_casar_decisao_sem_correspondencia_devolve_none():
    html_texto = (FIXTURES / "tce_pr" / "boletim_179.html").read_text(encoding="utf-8")
    texto_bruto = tce_pr._extrair_conteudo_principal(html_texto)
    from nucleo.fatiador import fatiar_item

    decisoes_fatiadas = fatiar_item(
        "TCE-PR — Boletim Informativo de Jurisprudência", 1, "Boletim 179",
        texto_bruto, "https://x/boletim-179", None,
    )
    item = _item_tce_pr(numero_acordao="9999/9999", numero_processo="inexistente")

    assert _casar_decisao(decisoes_fatiadas, item) is None
