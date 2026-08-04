from pathlib import Path

import pytest
import requests

import coletores.tce_pr as tce_pr
from nucleo.banco import conectar, criar_schema, inserir_item_bruto, seed_fontes

FIXTURES = Path(__file__).parent / "fixtures" / "tce_pr"


@pytest.fixture
def conexao():
    conexao = conectar(":memory:")
    criar_schema(conexao)
    seed_fontes(conexao)
    conexao.commit()
    yield conexao
    conexao.close()


def _fonte_id_tce_pr(conexao) -> int:
    linha = conexao.execute(
        "SELECT id FROM fontes WHERE nome LIKE 'TCE-PR%'"
    ).fetchone()
    assert linha is not None
    return linha["id"]


def test_listar_boletins_indice_usa_fixture_real(monkeypatch):
    conteudo = (FIXTURES / "indice.html").read_text(encoding="utf-8")
    monkeypatch.setattr(
        tce_pr, "_requisitar",
        lambda sessao, url: type("R", (), {"text": conteudo})(),
    )

    boletins = tce_pr._listar_boletins_indice(requests.Session())

    # 9 links no fixture, mas o "Último Boletim" repete o 184 do dropdown —
    # tem que deduplicar por URL
    assert len(boletins) == 9
    assert boletins[0].numero == "184"
    assert boletins[0].ano == "2026"
    assert boletins[0].url == (
        "https://www.tce.pr.gov.br/conteudo/"
        "boletim-informativo-de-jurisprudencia-do-tce-pr-n-184-2026.htm"
    )

    # padrão de URL antigo (.../{id}/area/242/) também resolve certo
    boletim_168 = next(b for b in boletins if b.numero == "168")
    assert boletim_168.url == (
        "https://www.tce.pr.gov.br/conteudo/"
        "boletim-de-jurisprudencia-tce-pr-n-168-2025/365744/area/242/"
    )

    # mesma URL do exemplo real dado na etapa
    boletim_179 = next(b for b in boletins if b.numero == "179")
    assert boletim_179.url == (
        "https://www.tce.pr.gov.br/conteudo/"
        "boletim-de-jurisprudencia-tce-pr-n-179-2025.htm"
    )


def test_extrair_conteudo_principal_boletim_179_word_export():
    html_texto = (FIXTURES / "boletim_179.html").read_text(encoding="utf-8")

    conteudo = tce_pr._extrair_conteudo_principal(html_texto)

    assert "<span" not in conteudo  # bagunça de estilo do Word, fora
    assert "<style" not in conteudo
    assert "Primeira Câmara" not in conteudo or "PRIMEIRA CÂMARA" in conteudo
    assert (
        '<a href="https://viajuris.tce.pr.gov.br/Pesquisa/Visualizar/'
        '3190-2025-primeira-camara-thiago-barbosa-cordeiro-tomada-de-'
        'contas-extraordinaria-prejulgados-5/200060">Acórdão n.º 3190/2025</a>'
        in conteudo
    )
    assert "julgado em 10/11/2025, veiculado em 24/11/2025 no DETC" in conteudo


def test_extrair_conteudo_principal_boletim_184_nao_vaza_css():
    html_texto = (FIXTURES / "boletim_184.html").read_text(encoding="utf-8")

    conteudo = tce_pr._extrair_conteudo_principal(html_texto)

    # a 184 embute um <style> de ~50KB dentro do <main> — não pode aparecer
    assert "box-sizing" not in conteudo
    assert "<style" not in conteudo
    assert "--blue" not in conteudo
    # mas o link do ViaJuris (padrão de URL novo, diferente do 179) sobrevive
    assert (
        '<a href="https://viajuris.tce.pr.gov.br/Pesquisa/Visualizar/'
        '459/2026/ACO/S1C">Acórdão n.º 459/2026</a>' in conteudo
    )


def test_estimar_data_publicacao_usa_a_mais_recente():
    html_texto = (FIXTURES / "boletim_179.html").read_text(encoding="utf-8")
    conteudo = tce_pr._extrair_conteudo_principal(html_texto)

    data = tce_pr._estimar_data_publicacao(conteudo)

    # item 1 veiculado 24/11/2025, item 2 veiculado 25/11/2025 -> pega o maior
    assert data == "2025-11-25T03:00:00+00:00"


def test_estimar_data_publicacao_sem_citacao_devolve_none():
    assert tce_pr._estimar_data_publicacao("texto sem nenhuma citação") is None


def test_coletar_grava_apenas_boletins_novos(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_pr(conexao)

    indice = [
        tce_pr.BoletimIndice(numero="180", ano="2025",
                              url="https://www.tce.pr.gov.br/x180.htm", titulo="Boletim 180"),
        tce_pr.BoletimIndice(numero="179", ano="2025",
                              url="https://www.tce.pr.gov.br/x179.htm", titulo="Boletim 179"),
    ]
    monkeypatch.setattr(tce_pr, "_listar_boletins_indice", lambda sessao: indice)

    # 179 já foi coletado numa execução anterior
    inserir_item_bruto(
        conexao, fonte_id=fonte_id, url_origem="https://www.tce.pr.gov.br/x179.htm",
        titulo="Boletim 179", data_publicacao=None, texto_bruto="texto",
        hash_conteudo="hash-179-existente",
    )
    conexao.commit()

    chamadas = []

    def _coletar_boletim_falso(sessao, boletim):
        chamadas.append(boletim.url)
        return tce_pr.ItemColetado(
            url_origem=boletim.url, titulo=boletim.titulo,
            data_publicacao=None, texto_bruto=f"texto de {boletim.numero}",
        )

    monkeypatch.setattr(tce_pr, "_coletar_boletim", _coletar_boletim_falso)

    resultado = tce_pr.coletar(conexao, fonte_id)

    assert chamadas == ["https://www.tce.pr.gov.br/x180.htm"]
    assert resultado.itens_novos == 1
    assert resultado.itens_repetidos == 0
    assert resultado.erro is None


def test_coletar_falha_isolada_por_boletim(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_pr(conexao)

    indice = [
        tce_pr.BoletimIndice(numero="2", ano="2025", url="https://x/2.htm", titulo="B2"),
        tce_pr.BoletimIndice(numero="1", ano="2025", url="https://x/1.htm", titulo="B1"),
    ]
    monkeypatch.setattr(tce_pr, "_listar_boletins_indice", lambda sessao: indice)

    def _coletar_boletim_falso(sessao, boletim):
        if boletim.numero == "2":
            raise RuntimeError("página quebrada")
        return tce_pr.ItemColetado(
            url_origem=boletim.url, titulo=boletim.titulo,
            data_publicacao=None, texto_bruto="texto ok",
        )

    monkeypatch.setattr(tce_pr, "_coletar_boletim", _coletar_boletim_falso)

    resultado = tce_pr.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 1  # o "1" entrou mesmo com o "2" falhando
    assert resultado.erro is None  # sucesso parcial não é falha total


def test_coletar_falha_isolada_quando_tudo_falha(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_pr(conexao)
    monkeypatch.setattr(
        tce_pr, "_listar_boletins_indice",
        lambda sessao: (_ for _ in ()).throw(RuntimeError("índice fora do ar")),
    )

    resultado = tce_pr.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 0
    assert resultado.erro is not None


def test_coletar_respeita_limite_por_execucao(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_pr(conexao)
    indice = [
        tce_pr.BoletimIndice(numero=str(n), ano="2025", url=f"https://x/{n}.htm", titulo=f"B{n}")
        for n in range(5, 0, -1)
    ]
    monkeypatch.setattr(tce_pr, "_listar_boletins_indice", lambda sessao: indice)

    chamadas = []

    def _coletar_boletim_falso(sessao, boletim):
        chamadas.append(boletim.numero)
        return tce_pr.ItemColetado(
            url_origem=boletim.url, titulo=boletim.titulo,
            data_publicacao=None, texto_bruto=f"texto {boletim.numero}",
        )

    monkeypatch.setattr(tce_pr, "_coletar_boletim", _coletar_boletim_falso)

    resultado = tce_pr.coletar(conexao, fonte_id, limite_por_execucao=2)

    assert chamadas == ["5", "4"]
    assert resultado.itens_novos == 2


def test_boletins_ja_coletados(conexao):
    fonte_id = _fonte_id_tce_pr(conexao)
    inserir_item_bruto(
        conexao, fonte_id=fonte_id, url_origem="https://www.tce.pr.gov.br/x.htm",
        titulo="X", data_publicacao=None, texto_bruto="texto",
        hash_conteudo="hash-de-teste-x",
    )
    conexao.commit()

    assert tce_pr._boletins_ja_coletados(conexao, fonte_id) == {
        "https://www.tce.pr.gov.br/x.htm"
    }
