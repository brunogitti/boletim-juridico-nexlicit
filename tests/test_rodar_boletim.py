"""Testes de scripts/rodar_boletim.py — banco real em arquivo temporário
(main() abre a própria conexão via DATABASE_PATH), envio sempre mockado."""

import json
import sys

import pytest

import scripts.rodar_boletim as rodar_boletim
from nucleo.banco import (
    atualizar_analise,
    conectar,
    criar_schema,
    inserir_decisao,
    inserir_item_bruto,
    seed_fontes,
    transacao,
)
from scripts.rodar_boletim import (
    _credenciais_email,
    _decisoes_pendentes,
    _linha_para_decisao,
    _lista_fontes_com_falha,
)


@pytest.fixture
def caminho_banco(tmp_path):
    caminho = str(tmp_path / "boletim_teste.db")
    conexao = conectar(caminho)
    criar_schema(conexao)
    with transacao(conexao):
        seed_fontes(conexao)
    conexao.close()
    return caminho


def _popular_decisao_pronta(caminho_banco, **overrides) -> int:
    """Cria fonte (já semeada) + item_bruto + decisao relevante e
    analisada — exatamente o estado que rodar_boletim.py espera encontrar
    pendente de envio."""
    conexao = conectar(caminho_banco)
    fonte_id = conexao.execute(
        "SELECT id FROM fontes WHERE nome = 'Zênite'"
    ).fetchone()["id"]

    with transacao(conexao):
        item_id = inserir_item_bruto(
            conexao, fonte_id=fonte_id, url_origem="https://x/item",
            titulo="Notícia de teste", data_publicacao="2026-08-10",
            texto_bruto="texto", hash_conteudo=f"hash-{overrides.get('id', 1)}",
        )
        padrao = dict(
            item_bruto_id=item_id, chave_dedup=f"chave-{overrides.get('id', 1)}",
            tribunal="TCE-PR", numero_acordao="1/2026",
            url_inteiro_teor="https://x/acordao", triagem_status="relevante",
        )
        padrao.update({k: v for k, v in overrides.items() if k != "id"})
        decisao_id = inserir_decisao(conexao, **padrao)
        atualizar_analise(
            conexao, decisao_id, resumo="Resumo de teste.", impacto="alto",
        )
    conexao.close()
    assert decisao_id is not None
    return decisao_id


def _enviado_em(caminho_banco, decisao_id):
    conexao = conectar(caminho_banco)
    linha = conexao.execute(
        "SELECT enviado_em FROM decisoes WHERE id = ?", (decisao_id,)
    ).fetchone()
    conexao.close()
    return linha["enviado_em"]


# --- funções auxiliares puras -----------------------------------------------

def test_linha_para_decisao_converte_artigos_lei_de_json():
    item = {"id": 1, "artigos_lei": json.dumps(["art. 67", "art. 92"])}
    dados = _linha_para_decisao(item)
    assert dados["artigos_lei"] == ["art. 67", "art. 92"]


def test_linha_para_decisao_artigos_lei_none_permanece_none():
    item = {"id": 1, "artigos_lei": None}
    assert _linha_para_decisao(item)["artigos_lei"] is None


def test_lista_fontes_com_falha_none_vira_lista_vazia():
    assert _lista_fontes_com_falha(None) == []


def test_lista_fontes_com_falha_separa_por_virgula_e_tira_espaco():
    assert _lista_fontes_com_falha("TCU, STJ ,TCE-PR") == ["TCU", "STJ", "TCE-PR"]


def test_credenciais_email_falta_uma_levanta_system_exit(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "user@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "senha")
    monkeypatch.delenv("EMAIL_DESTINATARIO", raising=False)

    with pytest.raises(SystemExit):
        _credenciais_email()


def test_credenciais_email_completas_devolve_tupla(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "user@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "senha")
    monkeypatch.setenv("EMAIL_DESTINATARIO", "dest@exemplo.com")

    destinatario, usuario, senha = _credenciais_email()
    assert (destinatario, usuario, senha) == ("dest@exemplo.com", "user@gmail.com", "senha")


def test_decisoes_pendentes_ignora_ja_enviadas_e_descartadas(caminho_banco):
    id_pendente = _popular_decisao_pronta(caminho_banco, id=1, chave_dedup="c1")
    id_descartada = _popular_decisao_pronta(
        caminho_banco, id=2, chave_dedup="c2", triagem_status="descartado",
    )

    conexao = conectar(caminho_banco)
    with transacao(conexao):
        conexao.execute(
            "UPDATE decisoes SET enviado_em = '2026-08-11T00:00:00' WHERE id = ?",
            (id_descartada,),
        )
    pendentes = _decisoes_pendentes(conexao)
    conexao.close()

    ids = [linha["id"] for linha in pendentes]
    assert id_pendente in ids
    assert id_descartada not in ids


# --- main() end-to-end (envio sempre mockado) -------------------------------

def _preparar_ambiente(monkeypatch, caminho_banco, *, argv=("rodar_boletim",)):
    monkeypatch.setenv("DATABASE_PATH", caminho_banco)
    monkeypatch.setenv("GMAIL_USER", "user@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "senha-app")
    monkeypatch.setenv("EMAIL_DESTINATARIO", "dest@exemplo.com")
    monkeypatch.setattr(sys, "argv", list(argv))


def test_main_sem_pendentes_nao_chama_enviar_email(monkeypatch, caminho_banco):
    _preparar_ambiente(monkeypatch, caminho_banco)
    chamadas = []
    monkeypatch.setattr(rodar_boletim, "enviar_email", lambda *a, **kw: chamadas.append(1))

    rodar_boletim.main()

    assert chamadas == []


def test_main_dry_run_nao_marca_enviado_nem_chama_enviar_email(monkeypatch, caminho_banco, tmp_path):
    decisao_id = _popular_decisao_pronta(caminho_banco)
    saida = str(tmp_path / "saida.html")
    _preparar_ambiente(
        monkeypatch, caminho_banco,
        argv=("rodar_boletim", "--dry-run", "--saida", saida),
    )
    chamadas = []
    monkeypatch.setattr(rodar_boletim, "enviar_email", lambda *a, **kw: chamadas.append(1))

    rodar_boletim.main()

    assert chamadas == []
    assert _enviado_em(caminho_banco, decisao_id) is None
    with open(saida, encoding="utf-8") as arquivo:
        conteudo = arquivo.read()
    assert "Boletim Jurídico NexLicit" in conteudo


def test_main_envio_bem_sucedido_marca_enviado_em(monkeypatch, caminho_banco):
    decisao_id = _popular_decisao_pronta(caminho_banco)
    _preparar_ambiente(monkeypatch, caminho_banco)
    chamadas = []
    monkeypatch.setattr(
        rodar_boletim, "enviar_email",
        lambda destinatario, assunto, html, **kw: chamadas.append((destinatario, assunto)),
    )

    rodar_boletim.main()

    assert len(chamadas) == 1
    assert chamadas[0][0] == "dest@exemplo.com"
    assert _enviado_em(caminho_banco, decisao_id) is not None


def test_main_envio_que_falha_nao_marca_enviado_em(monkeypatch, caminho_banco):
    decisao_id = _popular_decisao_pronta(caminho_banco)
    _preparar_ambiente(monkeypatch, caminho_banco)

    def _envio_que_falha(*a, **kw):
        raise RuntimeError("SMTP indisponível")

    monkeypatch.setattr(rodar_boletim, "enviar_email", _envio_que_falha)

    with pytest.raises(RuntimeError):
        rodar_boletim.main()

    assert _enviado_em(caminho_banco, decisao_id) is None


def test_main_sem_ancora_nao_envia_e_nao_marca(monkeypatch, caminho_banco):
    # decisão relevante+analisada mas sem link — nunca deveria existir
    # (a Camada 5 já barra isso), mas se existisse, montar_boletim
    # devolve None e main() não pode chamar enviar_email
    decisao_id = _popular_decisao_pronta(caminho_banco, url_inteiro_teor=None)
    _preparar_ambiente(monkeypatch, caminho_banco)
    chamadas = []
    monkeypatch.setattr(rodar_boletim, "enviar_email", lambda *a, **kw: chamadas.append(1))

    rodar_boletim.main()

    assert chamadas == []
    assert _enviado_em(caminho_banco, decisao_id) is None
