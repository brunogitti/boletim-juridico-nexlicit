import csv
import io
from pathlib import Path

import pytest

import coletores.tcu as tcu
from nucleo.banco import conectar, criar_schema, inserir_item_bruto, seed_fontes

FIXTURES = Path(__file__).parent / "fixtures" / "tcu"


class _RespostaFalsa:
    def __init__(self, conteudo: bytes):
        self.content = conteudo


@pytest.fixture
def conexao():
    conexao = conectar(":memory:")
    criar_schema(conexao)
    seed_fontes(conexao)
    conexao.commit()
    yield conexao
    conexao.close()


def _fonte_id_tcu(conexao) -> int:
    linha = conexao.execute("SELECT id FROM fontes WHERE nome LIKE 'TCU%'").fetchone()
    assert linha is not None
    return linha["id"]


def _ler_csv_fixture(nome: str) -> list[dict]:
    texto = (FIXTURES / nome).read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(texto), delimiter="|"))


# --- Montagem de item a partir de uma linha ------------------------------

def test_montar_item_usa_fixture_real_da_lc():
    linhas = _ler_csv_fixture("boletim-informativo-lc.csv")
    item = tcu._montar_item(linhas[0], "TCU Informativo LC")

    assert item is not None
    assert item.titulo == "TCU Informativo LC 528/2026 — Acórdão 2357/2026 Primeira Câmara"
    assert item.data_publicacao is None
    assert "Relator Ministro Bruno Dantas" in item.texto_bruto
    assert "Nas licitações de obras" in item.texto_bruto
    assert item.url_origem == (
        "https://pesquisa.apps.tcu.gov.br/documento/acordao-completo/*/"
        "COLEGIADO%3A%22Primeira%20C%C3%A2mara%22%20NUMACORDAO%3A2357%20"
        "ANOACORDAO%3A2026/DTRELEVANCIA%20desc,%20NUMACORDAOINT%20desc/0"
    )


def test_montar_item_pula_linha_sem_textoacordao():
    linhas = _ler_csv_fixture("boletim-informativo-lc.csv")
    linha_vazia = next(l for l in linhas if not l["TEXTOACORDAO"].strip())

    assert tcu._montar_item(linha_vazia, "TCU Informativo LC") is None


def test_montar_item_usa_fixture_real_do_boletim():
    linhas = _ler_csv_fixture("boletim-jurisprudencia.csv")
    item = tcu._montar_item(linhas[0], "TCU Boletim Jurisprudência")

    assert item is not None
    assert item.titulo == "TCU Boletim Jurisprudência 593/2026 — Acórdão 1774/2026 Plenário"
    assert "Relator Ministro Walton Alencar Rodrigues" in item.texto_bruto


# --- Corte incremental / bootstrap ----------------------------------------

def test_linhas_pendentes_bootstrap_pega_so_as_edicoes_mais_recentes():
    texto = (FIXTURES / "boletim-informativo-lc.csv").read_text(encoding="utf-8-sig")
    leitor = csv.DictReader(io.StringIO(texto), delimiter="|")

    pendentes = tcu._linhas_pendentes(leitor, ultima_edicao=None, limite=100)

    titulos = {l["TITULO"] for l in pendentes}
    assert titulos == {
        "Informativo de Licitações e Contratos 528/2026",
        "Informativo de Licitações e Contratos 527/2026",
    }
    assert "Informativo de Licitações e Contratos 474/2024" not in titulos


def test_linhas_pendentes_incremental_para_na_edicao_conhecida():
    texto = (FIXTURES / "boletim-informativo-lc.csv").read_text(encoding="utf-8-sig")
    leitor = csv.DictReader(io.StringIO(texto), delimiter="|")

    pendentes = tcu._linhas_pendentes(leitor, ultima_edicao=527, limite=100)

    assert len(pendentes) == 1
    assert pendentes[0]["TITULO"] == "Informativo de Licitações e Contratos 528/2026"


def test_linhas_pendentes_respeita_limite():
    texto = (FIXTURES / "boletim-informativo-lc.csv").read_text(encoding="utf-8-sig")
    leitor = csv.DictReader(io.StringIO(texto), delimiter="|")

    pendentes = tcu._linhas_pendentes(leitor, ultima_edicao=None, limite=1)

    assert len(pendentes) == 1


# --- coletar(): orquestração e falha isolada -------------------------------

def test_coletar_grava_das_duas_publicacoes(conexao, monkeypatch):
    fonte_id = _fonte_id_tcu(conexao)
    lc_bytes = (FIXTURES / "boletim-informativo-lc.csv").read_bytes()
    boletim_bytes = (FIXTURES / "boletim-jurisprudencia.csv").read_bytes()

    def _requisitar_falso(sessao, url):
        if "boletim-informativo-lc" in url:
            return _RespostaFalsa(lc_bytes)
        return _RespostaFalsa(boletim_bytes)

    monkeypatch.setattr(tcu, "_requisitar", _requisitar_falso)

    resultado = tcu.coletar(conexao, fonte_id)

    # bootstrap: 2 edições da LC (528 com 1 item válido + 527 com 2) = 3,
    # mais 2 edições do Boletim (só existe 593 no fixture, 4 itens) = 4
    assert resultado.itens_novos == 7
    assert resultado.erro is None


def test_coletar_falha_isolada_por_publicacao(conexao, monkeypatch):
    fonte_id = _fonte_id_tcu(conexao)
    boletim_bytes = (FIXTURES / "boletim-jurisprudencia.csv").read_bytes()

    def _requisitar_falso(sessao, url):
        if "boletim-informativo-lc" in url:
            raise RuntimeError("CSV da LC fora do ar")
        return _RespostaFalsa(boletim_bytes)

    monkeypatch.setattr(tcu, "_requisitar", _requisitar_falso)

    resultado = tcu.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 4  # só o Boletim
    assert resultado.erro is None


def test_coletar_falha_isolada_quando_tudo_falha(conexao, monkeypatch):
    fonte_id = _fonte_id_tcu(conexao)

    def _requisitar_falso(sessao, url):
        raise RuntimeError("fora do ar")

    monkeypatch.setattr(tcu, "_requisitar", _requisitar_falso)

    resultado = tcu.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 0
    assert resultado.erro is not None


# --- _maior_edicao_coletada -------------------------------------------------

def test_maior_edicao_coletada_sem_historico_devolve_none(conexao):
    fonte_id = _fonte_id_tcu(conexao)
    assert tcu._maior_edicao_coletada(conexao, fonte_id, tcu._PADRAO_TITULO_LC) is None


def test_maior_edicao_coletada_le_do_titulo_e_nao_mistura_publicacoes(conexao):
    fonte_id = _fonte_id_tcu(conexao)
    for titulo, hash_ in [
        ("TCU Informativo LC 527/2026 — Acórdão 1128/2026 Plenário", "hash-lc-527"),
        ("TCU Informativo LC 528/2026 — Acórdão 2357/2026 Primeira Câmara", "hash-lc-528"),
        ("TCU Boletim Jurisprudência 593/2026 — Acórdão 1774/2026 Plenário", "hash-bol-593"),
    ]:
        inserir_item_bruto(
            conexao, fonte_id=fonte_id, url_origem=f"https://x/{hash_}",
            titulo=titulo, data_publicacao=None, texto_bruto="texto",
            hash_conteudo=hash_,
        )
    conexao.commit()

    assert tcu._maior_edicao_coletada(conexao, fonte_id, tcu._PADRAO_TITULO_LC) == 528
    assert tcu._maior_edicao_coletada(conexao, fonte_id, tcu._PADRAO_TITULO_BOLETIM) == 593
