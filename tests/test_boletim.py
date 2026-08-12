"""Testes de nucleo/boletim.py — monta HTML a partir de dicts simples,
sem tocar banco nem LLM."""

import logging
from datetime import date

from nucleo.boletim import (
    _agrupar_por_tribunal,
    _ordenar_por_impacto,
    _prioridade_tribunal,
    _titulo_decisao,
    montar_boletim,
)


def _decisao(**overrides) -> dict:
    padrao = dict(
        id=1, tribunal="TCE-PR", numero_acordao="3190/2025", numero_processo=None,
        orgao_julgador="Primeira Câmara", relator="Fulano de Tal",
        data_julgamento="2025-11-10", url_inteiro_teor="https://x/acordao",
        tema="qualificação técnica", artigos_lei=["art. 67"], impacto="alto",
        resumo="Resumo da decisão.",
    )
    padrao.update(overrides)
    return padrao


# --- montar_boletim() -------------------------------------------------------

def test_lista_vazia_devolve_none():
    assert montar_boletim([], data_referencia=date(2026, 8, 12)) is None


def test_html_contem_cabecalho_e_panorama():
    html = montar_boletim([_decisao()], data_referencia=date(2026, 8, 12))

    assert "Boletim Jurídico NexLicit" in html
    assert "12/08/2026" in html
    assert "Panorama: 1 decisão relevante" in html


def test_html_escapa_caracteres_especiais():
    html = montar_boletim(
        [_decisao(resumo="Trata de <requisito> A & B.", tema="A & B")],
        data_referencia=date(2026, 8, 12),
    )

    assert "<requisito>" not in html
    assert "&lt;requisito&gt;" in html


def test_html_contem_rodape_com_fontes_com_falha():
    html = montar_boletim(
        [_decisao()], data_referencia=date(2026, 8, 12),
        fontes_com_falha=["TCU", "STJ"],
    )

    assert "TCU" in html
    assert "STJ" in html
    assert "falharam" in html


def test_html_sem_fontes_com_falha_nao_menciona_rodape_de_falha():
    html = montar_boletim([_decisao()], data_referencia=date(2026, 8, 12))

    assert "falharam" not in html


def test_item_sem_ancora_e_rejeitado_e_logado(caplog):
    # achado real: mesmo decisões pré-filtradas por triagem_status podem,
    # em tese, chegar aqui sem link — revalidação defensiva do
    # não-negociável (CLAUDE.md), nunca deve confiar cegamente no upstream
    valida = _decisao(id=1, numero_acordao="1/2026")
    invalida = _decisao(id=2, numero_acordao="2/2026", url_inteiro_teor=None)

    with caplog.at_level(logging.ERROR):
        html = montar_boletim([valida, invalida], data_referencia=date(2026, 8, 12))

    assert "Acórdão 1/2026" in html
    assert "Acórdão 2/2026" not in html
    assert any("sem âncora" in registro.message for registro in caplog.records)


def test_todas_sem_ancora_devolve_none(caplog):
    invalida = _decisao(url_inteiro_teor=None)

    with caplog.at_level(logging.ERROR):
        resultado = montar_boletim([invalida], data_referencia=date(2026, 8, 12))

    assert resultado is None


# --- _prioridade_tribunal() / _agrupar_por_tribunal() -----------------------

def test_prioridade_tribunal_segue_ordem_da_camada_6():
    assert _prioridade_tribunal("TCU") < _prioridade_tribunal("TCE-SP")
    assert _prioridade_tribunal("TCE-SP") < _prioridade_tribunal("STJ")
    assert _prioridade_tribunal("STJ") < _prioridade_tribunal("TCE-MG")
    assert _prioridade_tribunal("TCE-MG") < _prioridade_tribunal("TCE-PR")


def test_prioridade_tribunal_e_case_insensitive():
    assert _prioridade_tribunal("tcu") == _prioridade_tribunal("TCU")


def test_prioridade_tribunal_fora_dos_5_cai_no_bucket_demais():
    assert _prioridade_tribunal("TCE/SC") > _prioridade_tribunal("TCE-PR")
    assert _prioridade_tribunal("Órgão governamental") > _prioridade_tribunal("TCE-PR")


def test_agrupar_mescla_tribunal_via_zenite_com_tribunal_dedicado():
    # achado real (banco de verdade): decisoes.tribunal é texto livre do
    # LLM pras notícias da Zênite — uma notícia da Zênite citando
    # "TCE-PR" pelo nome tem que cair na MESMA seção do coletor dedicado
    # do TCE-PR, não numa seção separada
    do_coletor = _decisao(id=1, tribunal="TCE-PR")
    da_zenite = _decisao(id=2, tribunal="TCE-PR", numero_acordao=None,
                          numero_processo="processo-via-zenite")

    grupos = _agrupar_por_tribunal([do_coletor, da_zenite])

    assert len(grupos) == 1
    tribunal, decisoes_do_grupo = grupos[0]
    assert tribunal == "TCE-PR"
    assert len(decisoes_do_grupo) == 2


def test_agrupar_ordena_grupos_por_prioridade():
    grupos = _agrupar_por_tribunal([
        _decisao(id=1, tribunal="TCE-PR"),
        _decisao(id=2, tribunal="TCU"),
        _decisao(id=3, tribunal="TCE-MG"),
    ])

    assert [tribunal for tribunal, _ in grupos] == ["TCU", "TCE-MG", "TCE-PR"]


def test_agrupar_bucket_demais_fica_por_ultimo_e_alfabetico():
    grupos = _agrupar_por_tribunal([
        _decisao(id=1, tribunal="Órgão governamental"),
        _decisao(id=2, tribunal="TCU"),
        _decisao(id=3, tribunal="TCE/SC"),
    ])

    assert [tribunal for tribunal, _ in grupos] == [
        "TCU", "TCE/SC", "Órgão governamental",
    ]


# --- _ordenar_por_impacto() --------------------------------------------------

def test_ordenar_por_impacto_alto_medio_baixo():
    decisoes = [
        _decisao(id=1, impacto="baixo"),
        _decisao(id=2, impacto="alto"),
        _decisao(id=3, impacto="medio"),
    ]

    ordenadas = _ordenar_por_impacto(decisoes)

    assert [d["id"] for d in ordenadas] == [2, 3, 1]


# --- _titulo_decisao() -------------------------------------------------------

def test_titulo_usa_acordao_quando_presente():
    titulo = _titulo_decisao(_decisao(tribunal="TCE-PR", numero_acordao="3190/2025"))
    assert titulo == "TCE-PR — Acórdão 3190/2025"


def test_titulo_cai_pra_processo_de_verdade_quando_sem_acordao():
    titulo = _titulo_decisao(_decisao(
        tribunal="TCE-SP", numero_acordao=None, numero_processo="012834.989.25-3",
    ))
    assert titulo == "TCE-SP — Processo 012834.989.25-3"


def test_titulo_nao_prefixa_processo_quando_e_numero_de_edital():
    # achado real (2026-08-11): numero_processo reaproveita o slot pra
    # número de edital/Concorrência/Resolução — o valor já carrega o
    # próprio rótulo, prefixar "Processo" ficaria redundante/errado
    titulo = _titulo_decisao(_decisao(
        tribunal="TCE/SC", numero_acordao=None,
        numero_processo="Concorrência Presencial n. 05/2026",
    ))
    assert titulo == "TCE/SC — Concorrência Presencial n. 05/2026"
    assert "Processo Concorrência" not in titulo
