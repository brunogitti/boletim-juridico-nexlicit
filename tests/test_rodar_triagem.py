"""Testes de scripts/rodar_triagem.py — _itens_pendentes() e _formatar_linha(),
as únicas partes com lógica testável sem chamar o Gemini de verdade."""

import pytest

import scripts.rodar_triagem as rodar_triagem
from nucleo.banco import conectar, criar_schema, inserir_item_bruto, seed_fontes
from nucleo.fatiador import DecisaoFatiada
from nucleo.triagem import ResultadoTriagem
from scripts.rodar_triagem import _formatar_linha, _itens_pendentes, _processar_item


@pytest.fixture
def conexao():
    conexao = conectar(":memory:")
    criar_schema(conexao)
    seed_fontes(conexao)
    conexao.commit()
    yield conexao
    conexao.close()


def _fonte_id(conexao) -> int:
    linha = conexao.execute("SELECT id FROM fontes WHERE nome = 'Zênite'").fetchone()
    assert linha is not None
    return linha["id"]


def _inserir(conexao, fonte_id: int, status: str, hash_conteudo: str) -> int:
    item_id = inserir_item_bruto(
        conexao, fonte_id=fonte_id, url_origem=f"https://x/{hash_conteudo}",
        titulo="t", data_publicacao=None, texto_bruto="texto",
        hash_conteudo=hash_conteudo, status=status,
    )
    conexao.commit()
    assert item_id is not None
    return item_id


def test_sem_flag_pega_so_coletado(conexao):
    fonte_id = _fonte_id(conexao)
    _inserir(conexao, fonte_id, "coletado", "a")
    _inserir(conexao, fonte_id, "erro", "b")
    _inserir(conexao, fonte_id, "fatiado", "c")

    itens = _itens_pendentes(conexao, limite=None, reprocessar_erros=False)

    assert [item["status"] for item in itens] == ["coletado"]


def test_com_flag_soma_coletado_e_erro_sem_excluir_nenhum(conexao):
    fonte_id = _fonte_id(conexao)
    _inserir(conexao, fonte_id, "coletado", "a")
    _inserir(conexao, fonte_id, "erro", "b")
    _inserir(conexao, fonte_id, "fatiado", "c")

    itens = _itens_pendentes(conexao, limite=None, reprocessar_erros=True)

    assert sorted(item["status"] for item in itens) == ["coletado", "erro"]


def test_limite_e_respeitado_com_a_flag_ligada(conexao):
    fonte_id = _fonte_id(conexao)
    _inserir(conexao, fonte_id, "coletado", "a")
    _inserir(conexao, fonte_id, "erro", "b")

    itens = _itens_pendentes(conexao, limite=1, reprocessar_erros=True)

    assert len(itens) == 1


def test_ordena_por_id_independente_do_status(conexao):
    fonte_id = _fonte_id(conexao)
    id_primeiro = _inserir(conexao, fonte_id, "erro", "b")
    id_segundo = _inserir(conexao, fonte_id, "coletado", "a")

    itens = _itens_pendentes(conexao, limite=None, reprocessar_erros=True)

    assert [item["id"] for item in itens] == [id_primeiro, id_segundo]


# --- _formatar_linha ---------------------------------------------------

def _decisao_fatiada(**overrides) -> DecisaoFatiada:
    padrao = dict(
        item_bruto_id=1, tribunal="X", numero_acordao=None, numero_processo=None,
        orgao_julgador=None, relator=None, data_julgamento=None,
        url_inteiro_teor=None, texto_decisao="texto",
    )
    padrao.update(overrides)
    return DecisaoFatiada(**padrao)


def _resultado_triagem(**overrides) -> ResultadoTriagem:
    padrao = dict(
        relevante=True, motivo="motivo qualquer", tema=None, tribunal=None,
        numero_acordao=None, numero_identificador=None, relator=None,
        data_julgamento=None, impacto_estimado=None,
    )
    padrao.update(overrides)
    return ResultadoTriagem(**padrao)


def test_formatar_linha_prefere_identificador_exibicao_quando_presente():
    item = {"titulo": "Edição X"}
    decisao = _decisao_fatiada(identificador_exibicao="Acórdão 1/2026 — Rel. Fulano")
    metadados = {"numero_acordao": "1/2026"}

    linha = _formatar_linha(item, decisao, metadados, _resultado_triagem())

    assert "Edição X — Acórdão 1/2026 — Rel. Fulano" in linha


def test_formatar_linha_cai_pro_padrao_antigo_sem_identificador_exibicao():
    # TCE-SP e Zênite ainda não populam o campo — não pedido nesta etapa
    item = {"titulo": "Súmula TCE-SP n.º 1"}
    decisao = _decisao_fatiada(identificador_exibicao=None)
    metadados = {"numero_acordao": "1"}

    linha = _formatar_linha(item, decisao, metadados, _resultado_triagem())

    assert "Súmula TCE-SP n.º 1 1" in linha


# --- _processar_item: lista vazia não pode ser sucesso silencioso ----------

def test_processar_item_levanta_erro_quando_fatiar_item_devolve_lista_vazia(
    conexao, monkeypatch,
):
    # achado real: TCE-MG com formato não reconhecido devolvia 0 decisões
    # e o item era marcado 'fatiado' sem nada dentro, sem aviso nenhum
    fonte_id = _fonte_id(conexao)
    item_id = _inserir(conexao, fonte_id, "coletado", "a")
    item = conexao.execute(
        "SELECT itens_brutos.*, fontes.nome AS fonte_nome "
        "FROM itens_brutos JOIN fontes ON fontes.id = itens_brutos.fonte_id "
        "WHERE itens_brutos.id = ?",
        (item_id,),
    ).fetchone()

    monkeypatch.setattr(rodar_triagem, "fatiar_item", lambda *a, **kw: [])

    with pytest.raises(ValueError):
        _processar_item(conexao, cliente=None, item=item)
