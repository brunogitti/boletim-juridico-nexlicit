"""nucleo/boletim.py — Camada 6: monta o HTML do e-mail a partir das
decisões já triadas como relevantes e analisadas (Camada 5).

Não fala com o banco nem com a rede — recebe as decisões prontas (linhas
de `decisoes`, ou qualquer objeto com acesso tipo dicionário aos mesmos
campos) e devolve uma string HTML, ou `None` quando não há nada a
enviar. Quem orquestra (scripts/rodar_boletim.py) decide o que fazer com
o resultado.
"""

import html
import logging
from collections.abc import Mapping, Sequence
from datetime import date

from nucleo.analise import tem_ancora

logger = logging.getLogger(__name__)

# Ordem de prioridade da Camada 6 (docs/ARQUITETURA.md): "TCU, TCE-SP,
# STJ, TCE-MG, TCE-PR, demais". Chave já normalizada (ver _normalizar_tribunal).
_PRIORIDADE_TRIBUNAL = {"TCU": 1, "TCE-SP": 3, "STJ": 4, "TCE-MG": 5, "TCE-PR": 6}
_PRIORIDADE_DEMAIS = 999  # bucket "demais" — qualquer tribunal fora dos 5 nomeados

_IMPACTO_ORDEM = {"alto": 0, "medio": 1, "baixo": 2}
_IMPACTO_ORDEM_DESCONHECIDO = 3

# Palavras que indicam que numero_processo guarda, na verdade, um número de
# edital/instrumento (achado real 2026-08-11: numero_processo reaproveita
# esse slot pra Concorrência/Pregão/Edital/Resolução — ver
# nucleo/triagem.mesclar_metadados). Nesses casos o valor já carrega o
# próprio rótulo ("Concorrência Presencial n. 05/2026") — prefixar
# "Processo" na frente ficaria redundante/errado.
_PADRAO_INSTRUMENTO_PROPRIO = (
    "concorrência", "concorrencia", "pregão", "pregao", "edital",
    "resolução", "resolucao", "portaria", "instrução normativa", "instrucao normativa",
)

# Paleta NexLicit (docs/PROMPTS-PLAN-MODE.md, Etapa 7). _INK não veio
# especificado — cinza-azulado escuro derivado do navy, só pra texto de
# corpo ter contraste sobre o paper/card claros.
_NAVY = "#12233D"
_BRASS = "#A2782A"
_PAPER = "#F1ECE0"
_CARD = "#FBF9F3"
_INK = "#2B2F38"


def _normalizar_tribunal(tribunal: str) -> str:
    """Só pra consulta em _PRIORIDADE_TRIBUNAL — o rótulo exibido no HTML
    continua o texto original (não força "TCE/SC" a virar "TCE-SC")."""
    return tribunal.strip().upper().replace("/", "-")


def _prioridade_tribunal(tribunal: str) -> int:
    return _PRIORIDADE_TRIBUNAL.get(_normalizar_tribunal(tribunal), _PRIORIDADE_DEMAIS)


def _agrupar_por_tribunal(
    decisoes: Sequence[Mapping],
) -> list[tuple[str, list[Mapping]]]:
    """Agrupa pelo tribunal real citado na decisão (não pela fonte que
    coletou — achado real: notícia da Zênite pode citar "TCE-PR" pelo
    nome, e nesse caso entra na mesma seção do coletor dedicado do
    TCE-PR, porque o leitor quer tudo sobre aquele tribunal junto).

    Ordena os grupos por prioridade (Camada 6); dentro do bucket "demais"
    (tribunal fora dos 5 nomeados), ordena por ordem alfabética do rótulo,
    pra ter uma ordem determinística sem inventar prioridade que a doc não
    definiu."""
    grupos: dict[str, list[Mapping]] = {}
    for decisao in decisoes:
        grupos.setdefault(decisao["tribunal"], []).append(decisao)

    tribunais_ordenados = sorted(
        grupos, key=lambda t: (_prioridade_tribunal(t), t),
    )
    return [(tribunal, grupos[tribunal]) for tribunal in tribunais_ordenados]


def _ordenar_por_impacto(decisoes: list[Mapping]) -> list[Mapping]:
    return sorted(
        decisoes,
        key=lambda d: (
            _IMPACTO_ORDEM.get(d["impacto"], _IMPACTO_ORDEM_DESCONHECIDO),
            d["id"],
        ),
    )


def _titulo_decisao(decisao: Mapping) -> str:
    """Camada 6: nunca o título do documento-fonte (repete pra todas as
    decisões da mesma edição, não identifica uma em particular). Monta um
    título específico: "{tribunal} — Acórdão {numero_acordao}", com
    fallback pra numero_processo quando não houver número de acórdão."""
    tribunal = decisao["tribunal"]
    numero_acordao = decisao["numero_acordao"]
    numero_processo = decisao["numero_processo"]

    if numero_acordao:
        identificador = f"Acórdão {numero_acordao}"
    elif numero_processo:
        numero_lower = numero_processo.strip().lower()
        if numero_lower.startswith(_PADRAO_INSTRUMENTO_PROPRIO):
            # já carrega o próprio rótulo (ex. "Concorrência Presencial
            # n. 05/2026") — prefixar "Processo" seria redundante
            identificador = numero_processo
        else:
            identificador = f"Processo {numero_processo}"
    else:
        # não deveria acontecer — tem_ancora() já barrou isso antes de
        # chegar aqui — mas sem identificador nenhum não inventa um
        identificador = "(sem identificador)"

    return f"{tribunal} — {identificador}"


def montar_boletim(
    decisoes: Sequence[Mapping], *, data_referencia: date,
    fontes_com_falha: Sequence[str] = (),
) -> str | None:
    """Monta o HTML completo do e-mail. Devolve None se `decisoes` vier
    vazia — Camada 7: "se não houver decisão relevante, o job encerra sem
    enviar" (decisão de encerrar é de quem chama; aqui só devolve None).

    Revalida a âncora (tem_ancora) item a item antes de incluir no HTML,
    mesmo as decisões chegando pré-filtradas por triagem_status —
    redundância barata pra uma regra não-negociável que já vazou nesta
    sessão por mais de um motivo diferente. Item que falhar essa
    revalidação é descartado com log de erro, nunca incluído."""
    validas = []
    for decisao in decisoes:
        if tem_ancora(
            numero_acordao=decisao["numero_acordao"],
            numero_processo=decisao["numero_processo"],
            url_inteiro_teor=decisao["url_inteiro_teor"],
        ):
            validas.append(decisao)
        else:
            logger.error(
                "decisão sem âncora chegou no boletim — não deveria "
                "acontecer, triagem_status já deveria ter barrado antes",
                extra={"decisao_id": decisao["id"], "tribunal": decisao["tribunal"]},
            )

    if not validas:
        return None

    grupos = _agrupar_por_tribunal(validas)

    partes = [_html_cabecalho(data_referencia, total=len(validas))]
    for tribunal, decisoes_do_grupo in grupos:
        partes.append(_html_secao_tribunal(tribunal, _ordenar_por_impacto(decisoes_do_grupo)))
    partes.append(_html_rodape(fontes_com_falha))

    return _html_documento("".join(partes))


def _html_documento(corpo: str) -> str:
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
border="0" style="background-color:{_PAPER};">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0" \
border="0" style="max-width:640px;width:100%;">
        {corpo}
      </table>
    </td>
  </tr>
</table>"""


def _html_cabecalho(data_referencia: date, *, total: int) -> str:
    data_formatada = data_referencia.strftime("%d/%m/%Y")
    plural = "decisões relevantes" if total != 1 else "decisão relevante"
    return f"""
        <tr>
          <td style="background-color:{_NAVY};padding:28px 24px;\
border-radius:8px 8px 0 0;">
            <p style="margin:0;font-family:Georgia,'Times New Roman',serif;\
font-size:22px;font-weight:bold;color:{_PAPER};">
              Boletim Jurídico NexLicit
            </p>
            <p style="margin:6px 0 0;font-family:Arial,sans-serif;\
font-size:14px;color:{_PAPER};">
              {html.escape(data_formatada)}
            </p>
            <p style="margin:16px 0 0;font-family:Arial,sans-serif;\
font-size:13px;color:{_BRASS};">
              Panorama: {total} {plural} nesta edição.
            </p>
          </td>
        </tr>"""


def _html_secao_tribunal(tribunal: str, decisoes: list[Mapping]) -> str:
    linhas = "".join(_html_card_decisao(d) for d in decisoes)
    return f"""
        <tr>
          <td style="background-color:{_PAPER};padding:20px 24px 4px;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:15px;\
font-weight:bold;letter-spacing:0.06em;text-transform:uppercase;\
color:{_BRASS};border-bottom:2px solid {_BRASS};padding-bottom:6px;">
              {html.escape(tribunal)}
            </p>
          </td>
        </tr>
        {linhas}"""


def _html_card_decisao(decisao: Mapping) -> str:
    titulo = html.escape(_titulo_decisao(decisao))
    impacto = (decisao["impacto"] or "?").upper()

    subtitulo_partes = [
        p for p in (decisao["orgao_julgador"], _formatar_relator(decisao["relator"]),
                    decisao["data_julgamento"]) if p
    ]
    subtitulo = " — ".join(html.escape(p) for p in subtitulo_partes)

    tema_html = ""
    if decisao["tema"]:
        tema_html = (
            f'<p style="margin:6px 0 0;font-family:Arial,sans-serif;'
            f'font-size:12px;color:{_BRASS};">Tema: {html.escape(decisao["tema"])}</p>'
        )

    resumo = html.escape(decisao["resumo"] or "(sem resumo)")

    artigos_html = ""
    artigos = decisao["artigos_lei"]
    if artigos:
        artigos_texto = ", ".join(html.escape(a) for a in artigos)
        artigos_html = (
            f'<p style="margin:12px 0 0;font-family:Arial,sans-serif;'
            f'font-size:12px;color:{_INK};"><strong>Artigos:</strong> {artigos_texto}</p>'
        )

    link = html.escape(decisao["url_inteiro_teor"])

    return f"""
        <tr>
          <td style="background-color:{_PAPER};padding:8px 24px 16px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
border="0" style="background-color:{_CARD};border-radius:6px;">
              <tr>
                <td style="padding:16px 20px;">
                  <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;\
font-weight:bold;letter-spacing:0.04em;color:{_BRASS};">[{impacto}]</p>
                  <p style="margin:2px 0 0;font-family:Georgia,'Times New Roman',serif;\
font-size:16px;font-weight:bold;color:{_NAVY};">{titulo}</p>
                  {f'<p style="margin:4px 0 0;font-family:Arial,sans-serif;font-size:12px;color:{_INK};">{subtitulo}</p>' if subtitulo else ""}
                  {tema_html}
                  <p style="margin:12px 0 0;font-family:Arial,sans-serif;\
font-size:13px;line-height:1.5;color:{_INK};">{resumo}</p>
                  {artigos_html}
                  <p style="margin:14px 0 0;">
                    <a href="{link}" style="font-family:Arial,sans-serif;\
font-size:12px;font-weight:bold;color:{_BRASS};text-decoration:none;">
                      &rarr; Inteiro teor
                    </a>
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""


def _formatar_relator(relator: str | None) -> str | None:
    return f"Rel. {relator}" if relator else None


def _html_rodape(fontes_com_falha: Sequence[str]) -> str:
    falhas_html = ""
    if fontes_com_falha:
        lista = ", ".join(html.escape(f) for f in fontes_com_falha)
        falhas_html = (
            f'<p style="margin:8px 0 0;font-family:Arial,sans-serif;'
            f'font-size:11px;color:{_BRASS};">Fontes que falharam na coleta '
            f'de hoje: {lista}</p>'
        )
    return f"""
        <tr>
          <td style="background-color:{_NAVY};padding:20px 24px;\
border-radius:0 0 8px 8px;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;\
color:{_PAPER};">Boletim Jurídico NexLicit — gerado automaticamente.</p>
            {falhas_html}
          </td>
        </tr>"""
