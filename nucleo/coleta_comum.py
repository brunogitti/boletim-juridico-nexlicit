"""nucleo/coleta_comum.py — constantes compartilhadas entre os coletores.

Hoje só o User-Agent. Ficou centralizado depois do segundo caso real do
mesmo bug: STJ (Etapa 3d) e depois Zênite (2026-08-05) voltaram 403 do
Cloudflare especificamente por causa de caractere acentuado no header
`User-Agent` — confirmado por teste isolado nos dois casos, não é suposição.
Sem acento aqui não é estilo, é o que passa no WAF. Cada coletor nasceu com
a própria cópia da string (achado real de cada investigação, não um valor
inventado), mas duas recorrências do mesmo bug em fontes diferentes é sinal
de que a constante devia ser uma só.
"""

USER_AGENT = (
    "BoletimJuridicoNexLicit/0.1 (uso pessoal e nao comercial; "
    "ver docs/ARQUITETURA.md)"
)
