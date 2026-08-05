from pathlib import Path

import pytest

import coletores.stj as stj
from nucleo.banco import conectar, criar_schema, inserir_item_bruto, seed_fontes

FIXTURES = Path(__file__).parent / "fixtures" / "stj"


class _RespostaFalsa:
    def __init__(self, texto: str):
        self.text = texto


@pytest.fixture
def conexao():
    conexao = conectar(":memory:")
    criar_schema(conexao)
    seed_fontes(conexao)
    conexao.commit()
    yield conexao
    conexao.close()


def _fonte_id_stj(conexao) -> int:
    linha = conexao.execute("SELECT id FROM fontes WHERE nome LIKE 'STJ%'").fetchone()
    assert linha is not None
    return linha["id"]


# --- Feed Atom -----------------------------------------------------------

def test_listar_edicoes_feed_ignora_extraordinarias(monkeypatch):
    feed = (FIXTURES / "feed.xml").read_text(encoding="utf-8")
    monkeypatch.setattr(stj, "_requisitar", lambda sessao, url: _RespostaFalsa(feed))

    edicoes = stj._listar_edicoes_feed(sessao=None)

    # o fixture tem 895, 33E, 32E, 31E, 894, 893 intercalados -- só as
    # 3 regulares (sem sufixo E) devem sobrar
    assert [e.numero for e in edicoes] == ["0895", "0894", "0893"]
    assert edicoes[0].data_publicacao == "2026-08-04T03:00:00+00:00"
    assert edicoes[0].url == (
        "https://ww2.stj.jus.br/jurisprudencia/externo/informativo/"
        "?acao=pesquisarumaedicao&livre=0895.cod.&from=feed"
    )


def test_normalizar_data_iso_converte_horario_de_brasilia_pra_utc():
    assert stj._normalizar_data_iso("2026-08-04T00:00:00-03:00") == (
        "2026-08-04T03:00:00+00:00"
    )


# --- Extração de notas -----------------------------------------------------

def test_extrair_notas_administrativas_usa_fixture_real():
    html_texto = (FIXTURES / "edicao_887.html").read_text(encoding="utf-8")
    edicao = stj.EdicaoFeed(
        numero="0887", data_publicacao="2026-05-05T03:00:00+00:00",
        url="https://x/edicao-887",
    )

    itens = stj._extrair_notas_administrativas(html_texto, edicao)

    # 3 notas no fixture, 1 é Direito Tributário -- só 2 devem sobrar
    assert len(itens) == 2

    com_link, sem_link = itens

    assert com_link.titulo == "STJ Informativo n. 887 — AgInt no REsp 2.162.500-RJ"
    assert com_link.url_origem == (
        "https://processo.stj.jus.br/processo/pesquisa/?num_processo=REsp 2162500"
    )
    assert com_link.data_publicacao == "2026-05-05T03:00:00+00:00"
    assert "Rel. Ministro Benedito Gonçalves" in com_link.texto_bruto
    assert "DIREITO ADMINISTRATIVO" in com_link.texto_bruto

    # processo em segredo de justiça: sem link -> cai pra URL da edição
    assert sem_link.titulo == "STJ Informativo n. 887 — Processo em segredo de justiça"
    assert sem_link.url_origem == "https://x/edicao-887"


def test_extrair_notas_administrativas_sem_fixture_devolve_vazio():
    assert stj._extrair_notas_administrativas("<html></html>", stj.EdicaoFeed(
        numero="0001", data_publicacao="2026-01-01T00:00:00+00:00", url="https://x",
    )) == []


# --- coletar(): orquestração, bootstrap e falha isolada -------------------

def test_coletar_faz_bootstrap_quando_nada_coletado(conexao, monkeypatch):
    fonte_id = _fonte_id_stj(conexao)

    edicoes = [
        stj.EdicaoFeed(numero=f"{900 - n:04d}", data_publicacao="2026-01-01T00:00:00+00:00",
                        url=f"https://x/{900 - n}")
        for n in range(5)
    ]  # 900, 899, 898, 897, 896 -- mais recente primeiro, como o feed real
    monkeypatch.setattr(stj, "_listar_edicoes_feed", lambda sessao: edicoes)

    chamadas = []

    def _coletar_edicao_falso(sessao, edicao):
        chamadas.append(edicao.numero)
        return [stj.ItemColetado(
            url_origem=edicao.url, titulo=f"STJ Informativo n. {int(edicao.numero)} — X",
            data_publicacao=edicao.data_publicacao, texto_bruto=f"texto {edicao.numero}",
        )]

    monkeypatch.setattr(stj, "_coletar_edicao", _coletar_edicao_falso)

    resultado = stj.coletar(conexao, fonte_id)

    # bootstrap: só as 3 mais recentes (900, 899, 898), processadas da
    # mais antiga pra mais nova
    assert chamadas == ["0898", "0899", "0900"]
    assert resultado.itens_novos == 3
    assert resultado.erro is None


def test_coletar_so_pega_edicoes_novas_quando_ja_tem_historico(conexao, monkeypatch):
    fonte_id = _fonte_id_stj(conexao)
    inserir_item_bruto(
        conexao, fonte_id=fonte_id, url_origem="https://x/velha",
        titulo="STJ Informativo n. 897 — algo antigo", data_publicacao=None,
        texto_bruto="texto", hash_conteudo="hash-de-teste-897",
    )
    conexao.commit()

    edicoes = [
        stj.EdicaoFeed(numero="0899", data_publicacao="2026-01-01T00:00:00+00:00", url="https://x/899"),
        stj.EdicaoFeed(numero="0898", data_publicacao="2026-01-01T00:00:00+00:00", url="https://x/898"),
        stj.EdicaoFeed(numero="0897", data_publicacao="2026-01-01T00:00:00+00:00", url="https://x/897"),
    ]
    monkeypatch.setattr(stj, "_listar_edicoes_feed", lambda sessao: edicoes)

    chamadas = []

    def _coletar_edicao_falso(sessao, edicao):
        chamadas.append(edicao.numero)
        return []

    monkeypatch.setattr(stj, "_coletar_edicao", _coletar_edicao_falso)

    stj.coletar(conexao, fonte_id)

    # só as edições > 897 (a maior já coletada)
    assert chamadas == ["0898", "0899"]


def test_coletar_falha_isolada_por_edicao(conexao, monkeypatch):
    fonte_id = _fonte_id_stj(conexao)

    edicoes = [
        stj.EdicaoFeed(numero="0002", data_publicacao="2026-01-01T00:00:00+00:00", url="https://x/2"),
        stj.EdicaoFeed(numero="0001", data_publicacao="2026-01-01T00:00:00+00:00", url="https://x/1"),
    ]
    monkeypatch.setattr(stj, "_listar_edicoes_feed", lambda sessao: edicoes)

    def _coletar_edicao_falso(sessao, edicao):
        if edicao.numero == "0002":
            raise RuntimeError("página quebrada")
        return [stj.ItemColetado(
            url_origem=edicao.url, titulo="STJ Informativo n. 1 — X",
            data_publicacao=edicao.data_publicacao, texto_bruto="texto ok",
        )]

    monkeypatch.setattr(stj, "_coletar_edicao", _coletar_edicao_falso)

    resultado = stj.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 1
    assert resultado.erro is None


def test_coletar_falha_isolada_quando_tudo_falha(conexao, monkeypatch):
    fonte_id = _fonte_id_stj(conexao)
    monkeypatch.setattr(
        stj, "_listar_edicoes_feed",
        lambda sessao: (_ for _ in ()).throw(RuntimeError("feed fora do ar")),
    )

    resultado = stj.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 0
    assert resultado.erro is not None


def test_maior_edicao_coletada_sem_historico_devolve_none(conexao):
    fonte_id = _fonte_id_stj(conexao)
    assert stj._maior_edicao_coletada(conexao, fonte_id) is None


def test_maior_edicao_coletada_le_do_titulo(conexao):
    fonte_id = _fonte_id_stj(conexao)
    for numero in (887, 895, 890):
        inserir_item_bruto(
            conexao, fonte_id=fonte_id, url_origem=f"https://x/{numero}",
            titulo=f"STJ Informativo n. {numero} — X", data_publicacao=None,
            texto_bruto="texto", hash_conteudo=f"hash-{numero}",
        )
    conexao.commit()

    assert stj._maior_edicao_coletada(conexao, fonte_id) == 895
