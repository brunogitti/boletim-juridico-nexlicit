"""Testes de nucleo/dedup.py — os 3 níveis de fallback, com foco no caso
de regressão que motivou o nível intermediário (numero_processo): dois
itens sem número de acórdão, da mesma publicação (mesmo título+data), não
podem colidir só porque caíram no hash de título+data."""

import hashlib

from nucleo.dedup import calcular_chave_dedup


def _chave(**kwargs):
    padrao = dict(
        tribunal="TCE-PR", numero_acordao=None, numero_processo=None,
        data_julgamento=None, titulo_item_bruto="Boletim 179",
        data_publicacao_item_bruto=None,
    )
    padrao.update(kwargs)
    return calcular_chave_dedup(**padrao)


def test_numero_acordao_com_barra_extrai_ano_do_proprio_numero():
    chave = _chave(numero_acordao="3190/2025", data_julgamento="2025-11-10")
    assert chave == "tce-pr|3190/2025|2025"


def test_normalizacao_ignora_caixa_e_espacos_extras():
    chave1 = _chave(tribunal="TCE-PR", numero_acordao="3190/2025")
    chave2 = _chave(tribunal="  tce-pr  ", numero_acordao="3190/2025")
    assert chave1 == chave2


def test_numero_acordao_sem_barra_usa_ano_da_data_julgamento():
    # súmula do TCE-SP: numero_acordao reaproveitado pro número da súmula,
    # sem "/ano" — o ano vem de data_julgamento (preenchido com a data de
    # publicação nesse caso, ver nucleo/fatiador._fatiar_tce_sp_sumula)
    chave = _chave(
        tribunal="TCE-SP", numero_acordao="1",
        data_julgamento="2026-03-11T00:00:00+00:00",
    )
    assert chave == "tce-sp|1|2026"


def test_numero_acordao_sem_barra_e_sem_data_usa_sentinela_de_ano():
    chave = _chave(tribunal="TCE-SP", numero_acordao="1", data_julgamento=None)
    assert chave == "tce-sp|1|0000"


def test_sem_acordao_com_processo_usa_numero_processo():
    chave = _chave(
        tribunal="STJ", numero_acordao=None,
        numero_processo="AgInt no REsp 2.162.500-RJ",
    )
    assert chave == "stj|processo|agint no resp 2.162.500-rj"


def test_regressao_dois_itens_sem_acordao_mesma_publicacao_nao_colidem():
    # achado real do fatiador: TCE-MG (decisão própria) não cita acórdão,
    # só processo — duas decisões da MESMA edição (mesmo título+data) não
    # podem virar a mesma chave só porque nenhuma tem número de acórdão
    chave1 = _chave(
        tribunal="TCE-MG", numero_acordao=None, numero_processo="1152957",
        titulo_item_bruto="Informativo de Jurisprudência n. 333",
        data_publicacao_item_bruto="2026-06-20T00:00:00+00:00",
    )
    chave2 = _chave(
        tribunal="TCE-MG", numero_acordao=None, numero_processo="9988776",
        titulo_item_bruto="Informativo de Jurisprudência n. 333",
        data_publicacao_item_bruto="2026-06-20T00:00:00+00:00",
    )
    assert chave1 != chave2


def test_sem_acordao_nem_processo_cai_no_hash_de_titulo_e_data():
    # só a Zênite, hoje: notícia em prosa, sem citação formal nenhuma
    chave = _chave(
        tribunal="Não identificado", numero_acordao=None, numero_processo=None,
        titulo_item_bruto="TCU endurece exigência de atestado técnico",
        data_publicacao_item_bruto="2026-08-01T00:00:00+00:00",
    )
    esperado = hashlib.sha256(
        "tcu endurece exigência de atestado técnico2026-08-01T00:00:00+00:00".encode("utf-8")
    ).hexdigest()
    assert chave == esperado


def test_hash_fallback_e_deterministico_e_sensivel_ao_titulo():
    chave_a1 = _chave(titulo_item_bruto="Notícia A")
    chave_a2 = _chave(titulo_item_bruto="Notícia A")
    chave_b = _chave(titulo_item_bruto="Notícia B")
    assert chave_a1 == chave_a2
    assert chave_a1 != chave_b
