# Prompts de Plan Mode — Boletim Jurídico NexLicit

Um prompt por sessão. Cole em plan mode, leia o plano, aprove ou corrija, execute.
Só passe para a etapa seguinte depois que a anterior estiver rodando e testada.

## Como entrar em plan mode

Três formas, todas equivalentes:

- `Shift+Tab` duas vezes durante a sessão. A primeira liga auto-accept, a segunda
  entra em plan mode. O rodapé confirma.
- Digitar `/plan` no prompt. Vale só para a próxima mensagem.
- Iniciar a sessão já em plan mode: `claude --permission-mode plan`

No Windows, algumas configurações de terminal pulam o plan mode no `Shift+Tab`.
Se isso acontecer, use `/plan`.

O plan mode é somente leitura. O Claude Code analisa, busca, lê arquivo e monta o
plano, mas não escreve nada até você aprovar.

---

## Sessão 0 — Setup do repositório

> Vou construir o Boletim Jurídico NexLicit. Leia `docs/ARQUITETURA.md` inteiro
> antes de planejar.
>
> Nesta sessão quero apenas o esqueleto do projeto, sem nenhuma lógica de
> negócio ainda:
>
> - Estrutura de pastas conforme a arquitetura
> - `pyproject.toml` ou `requirements.txt` com as dependências mínimas
> - `.gitignore` cobrindo `.env`, `*.db`, `__pycache__`, artefatos de teste
> - `.env.example` com os nomes das variáveis, sem valores
> - `README.md` curto explicando o que o projeto faz e como rodar local
> - Configuração de logging estruturado em `nucleo/log.py`
>
> Não crie coletor, não crie schema, não crie chamada de LLM. Só o esqueleto.
>
> Me mostre o plano com a lista exata de arquivos que você vai criar e o
> conteúdo resumido de cada um.

---

## Etapa 1 — Banco de dados

> Leia `docs/ARQUITETURA.md`, seção 4.
>
> Implemente a camada de persistência:
>
> - `nucleo/banco.py` com criação do schema, conexão e context manager de
>   transação usando `sqlite3` puro
> - Migração idempotente: rodar duas vezes não pode quebrar nem duplicar
> - Funções de acesso: inserir item bruto, inserir decisão com
>   `ON CONFLICT DO NOTHING` na `chave_dedup`, marcar decisão como enviada,
>   abrir e fechar registro de execução
> - Seed da tabela `fontes` com as sete fontes da seção 2, com a ordem de
>   prioridade do boletim
> - Testes cobrindo: criação do schema, idempotência da migração, rollback em
>   transação com erro, e o comportamento do índice único de dedup
>
> Antes de escrever, me mostre no plano o DDL final que você vai usar e onde ele
> diverge do documento, se divergir.

---

## Etapa 2 — Coletor da Zênite

> Leia `docs/ARQUITETURA.md`, seções 2 e 3.
>
> Primeiro descubra, não presuma. Antes de escrever qualquer parser, busque de
> verdade e me diga o que encontrou:
>
> 1. `https://zenite.com.br/wp-json/wp/v2/posts?per_page=5` responde? Que campos
>    vêm? Dá para filtrar por data com `after`?
> 2. Se não, `https://zenite.com.br/feed/` responde RSS válido?
> 3. Se nenhum dos dois, qual a estrutura HTML real da listagem em
>    `https://zenite.com.br/noticias/`?
>
> Depois implemente `coletores/zenite.py` usando a melhor opção disponível, com
> fallback para as outras. Requisitos:
>
> - Coleta incremental por data, nunca varredura completa
> - `User-Agent` identificado e intervalo entre requisições
> - Retry com backoff exponencial
> - Falha isolada: exceção não pode escapar e derrubar o job
> - Grava em `itens_brutos` com hash de conteúdo
> - Teste com fixture de resposta real salva, não com mock inventado
>
> Me mostre o resultado da investigação antes do plano de implementação.

---

## Etapa 3 — Coletores dos tribunais

Rodar em quatro sessões separadas, nesta ordem de dificuldade crescente.

### 3a — TCE-PR

> Leia `docs/ARQUITETURA.md`, seção 2.
>
> Implemente `coletores/tce_pr.py`.
>
> O Boletim Informativo de Jurisprudência do TCE-PR vem em HTML renderizado no
> servidor. Exemplo real para você inspecionar:
> `https://www.tce.pr.gov.br/conteudo/boletim-de-jurisprudencia-tce-pr-n-179-2025.htm`
>
> A página índice fica em
> `https://www.tce.pr.gov.br/fiscalizado/informativos-do-tcepr/boletim-informativo-de-jurisprudencia/`
>
> Busque as duas páginas antes de planejar. Confirme como a listagem expõe os
> boletins e qual o padrão da linha de citação no fim de cada decisão, que traz
> número do processo, número do acórdão, órgão, relator e datas.
>
> Nunca gere URL de boletim por adivinhação de número sequencial. Sempre
> descubra pela página índice.

### 3b — TCE-MG

> Implemente `coletores/tce_mg.py`.
>
> Pendência que você precisa resolver primeiro: localizar a página índice que
> lista os Informativos de Jurisprudência do TCE-MG. Eu só tenho exemplos de
> páginas individuais:
> `https://www.tce.mg.gov.br/Informativo-de-Jurisprudencia-n-333.html/Noticia/1111628977`
> e `https://www.tce.mg.gov.br/noticia/Detalhe/1111627034`
>
> Busque no site e me diga onde fica o índice antes de planejar o coletor.
> Se não existir índice navegável, me avise e discutimos alternativa. Não
> implemente varredura por incremento de ID.

### 3c — TCE-SP

> Implemente `coletores/tce_sp.py`.
>
> Duas coisas a coletar: o Boletim de Jurisprudência mensal em
> `https://www.tce.sp.gov.br/boletim-de-jurisprudencia` e as súmulas em
> `https://www.tce.sp.gov.br/boletim-de-jurisprudencia/sumulas`.
>
> Súmula não é decisão de sessão. Quando entrar uma súmula nova ou alterada,
> ela vale como item de impacto alto por padrão. Trate esse caso no coletor.

### 3d — STJ

> Implemente `coletores/stj.py`.
>
> A página de Últimas Notícias do STJ não serve, é SharePoint com renderização
> por JavaScript e volta vazia. Use o Informativo de Jurisprudência do STJ.
>
> Descubra onde ele é publicado, em que formato e com que periodicidade. Só
> depois planeje. Filtre por Direito Administrativo e, dentro dele, licitação e
> contratos administrativos.

### 3e — TCU

> Implemente `coletores/tcu.py`. É o único com PDF, deixei por último.
>
> Listagem em `https://portal.tcu.gov.br/jurisprudencia`. Ela renderiza no
> servidor, mas atenção: o filtro `?tipos=` na URL não funciona, o portal é SPA
> em Next.js e o parâmetro se perde. Raspe a listagem completa e filtre pelo
> texto do título no código.
>
> Colete duas publicações:
> - Informativo de Licitações e Contratos (quinzenal, terças)
> - Boletim de Jurisprudência (semanal)
>
> Cada linha tem link para PDF e para Word. Teste os dois e me diga qual dá
> extração mais limpa. Use PyMuPDF para o PDF.
>
> Investigue também se a base de dados abertos em
> `https://sites.tcu.gov.br/dados-abertos/jurisprudencia/` traz o mesmo conteúdo
> em CSV, o que dispensaria a raspagem. Existe referência a um arquivo
> `boletim-informativo-lc.csv` nesse diretório. Verifique antes de escrever
> qualquer parser de PDF.

---

## Etapa 4 — Fatiador

> Leia `docs/ARQUITETURA.md`, camada 2.
>
> Implemente `nucleo/fatiador.py`, que quebra uma publicação com várias decisões
> em itens individuais, mais os extratores de metadados por fonte.
>
> Estratégia por fonte, conforme a arquitetura. A Zênite não precisa fatiar,
> cada notícia já é um item.
>
> Requisito de teste: salve um documento real de cada fonte em `tests/fixtures/`
> e escreva um teste que confirme a quantidade correta de itens extraídos e a
> extração correta de número de acórdão, relator e data. Eu vou conferir os
> números na mão.

---

## Etapa 5 — Cliente LLM e triagem

> Leia `docs/ARQUITETURA.md`, camadas 3 e 4.
>
> Implemente:
> - `nucleo/llm.py`, camada trocável de cliente LLM, com Gemini Flash como
>   implementação padrão, JSON schema nativo, `temperature=0.1`,
>   `thinking_level="low"`, backoff exponencial e processamento sequencial
> - `nucleo/dedup.py` com a chave `tribunal + numero_acordao + ano` e o fallback
>   por hash quando não houver número
> - `nucleo/triagem.py` com o prompt e o schema exatos da arquitetura
>
> Depois, um comando de linha que roda a triagem sobre tudo que já está em
> `itens_brutos` e imprime uma tabela com título, decisão da triagem e motivo.
>
> Não passe para a etapa 6 antes de eu ler os descartes um por um.

---

## Etapa 6 — Análise

> Leia `docs/ARQUITETURA.md`, camada 5.
>
> Implemente `nucleo/analise.py` com os seis campos e as regras
> anti-alucinação do documento.
>
> As três proibições são obrigatórias no prompt: não afirmar que entendimento é
> novo ou que mudou jurisprudência, não citar artigo que não aparece no texto, e
> retornar `null` em vez de palpite.
>
> Comando de linha que roda a análise sobre as decisões já triadas como
> relevantes e imprime o resultado formatado.

---

## Etapa 7 — Boletim e envio

> Leia `docs/ARQUITETURA.md`, camadas 6 e 7.
>
> Implemente:
> - `nucleo/boletim.py`, montador do HTML do e-mail, agrupado por tribunal na
>   ordem de prioridade e ordenado por impacto dentro de cada tribunal
> - `nucleo/envio.py`, Gmail SMTP, credenciais por variável de ambiente
>
> Requisitos:
> - Se não houver decisão relevante, encerra com log e não envia nada
> - Rodapé listando as fontes que falharam na coleta do dia
> - Nenhum item sem número de acórdão e link entra no e-mail
> - Modo `--dry-run` que salva o HTML em arquivo em vez de enviar
>
> Visual seguindo a identidade NexLicit: navy `#12233D`, brass `#A2782A`,
> paper `#F1ECE0`, card `#FBF9F3`. E-mail tem que funcionar sem CSS externo,
> então use estilo inline.

---

## Etapa 8 — GitHub Actions

> Última etapa. Só faça depois que o pipeline inteiro rodar local de ponta a
> ponta com sucesso.
>
> Crie o workflow:
> - `cron: '37 9 * * *'` (06:37 BRT, UTC-3 fixo, o Brasil não tem mais horário
>   de verão). O minuto `:37` é proposital, o `:00` é o pior horário por
>   congestionamento no GitHub.
> - `workflow_dispatch` para eu rodar manualmente
> - Secrets: chave do Gemini, usuário e senha de app do Gmail, e-mail de destino
> - Persistência do SQLite na branch `data`: baixar antes, commitar depois
> - Se o job falhar, quero saber. Configure notificação de falha.
>
> Me mostre o YAML completo no plano antes de criar o arquivo.
