# Handoff do LeopoldinAI

Estado em **19/10/2026**. Nossa entrega final é **04/11/2026, às 23h55**: depois disso, relatório e apresentação ficam bloqueados.

Escrevi este documento para passar o projeto para vocês: o que deixei pronto, o que ainda falta, por que fiz algumas escolhas e onde costuma dar problema. Para instalar e usar o app, vejam o [README.md](README.md).

## Resumo

Deixei a parte de software pronta e testada. **O trabalho ainda não está pronto para entregar**, porque falta o que depende de todos nós: identificar os integrantes, decidir as propostas, escrever o relatório e montar a apresentação.

| Exigência do enunciado | Situação |
|---|---|
| Arquitetura de agentes (agente único com validação humana) | ✅ Implementada |
| Análise da planilha e pontos críticos | ✅ Implementada |
| Informações externas | ✅ Artigos da LRF e da Lei 4.320 recuperados nos sites oficiais |
| Entrevista com o cliente | ✅ Feita em 15/10/2026; cadastrei as respostas no app |
| Resultados quantitativos | ✅ Calculados; o plano-base não fecha o déficit recorrente (ver abaixo) |
| Fluxo de revisão humana | ✅ Funciona; ❌ falta registrarmos decisões reais |
| Papéis do grupo (Consultor, Scrum Master, equipe) | ❌ Vazios no app |
| Relatório MDT UFSM | ❌ Não iniciado |
| Apresentação final | ❌ Não iniciada |

## O que falta, em ordem

1. **Preencher os papéis** na tela *Equipe e entrevista*: quem é o Consultor, quem é o Scrum Master e quem faz parte da equipe, com o que cada um fez. O enunciado pede isso antes de tudo, e a apresentação começa por aí.
2. **Gerar a análise da alternativa e decidir.** Na tela *Simulação*, cliquem em "Carregar alternativa para estudar" (corte de 32% nos gastos com cargos de confiança) e gerem uma análise com o agente de IA. Depois, em *Revisão e decisão*, decidam as cinco propostas com justificativa. O Refis pede o nome de um aprovador em nível superior.
   - As decisões precisam ser nossas. Os testes mostram que o fluxo funciona, mas não substituem a nossa avaliação.
   - Se quiserem comparar, gerem também uma análise do plano-base.
3. **Decidir o que o relatório diz sobre o déficit.** O plano-base não resolve. A alternativa de 32% resolve nas contas, mas só se as hipóteses se confirmarem. Precisamos escolher se recomendamos a alternativa, outra combinação ou se só registramos o limite.
4. **Escrever o relatório** conforme o MDT UFSM. O enunciado **proíbe usar IA para escrevê-lo**. Este handoff e o README servem de consulta, não de texto para copiar. As seções obrigatórias estão na página 2 do enunciado.
5. **Montar a apresentação**: equipe e papéis, diagnóstico (entrevista e planilha) e o plano com os resultados.
6. **Trocar as chaves.** As chaves do Gemini e do Groq que estão no `.env` acabaram expostas, então vou gerar outras (Google AI Studio e console.groq.com), apagar as antigas e atualizar o `.env`.

## Análises salvas no app

Ficam em `runtime/municipal.sqlite3` e aparecem no seletor da tela de Revisão.

| Versão | Data | Tipo | Observação |
|---|---|---|---|
| `529004d0`, `754be8bb`, `dd63d7a2` | 16 e 17/10 | Demonstração | Versões antigas, de quando o app tinha 2 propostas. Uma tem uma decisão de teste. Podem ignorar. |
| `c8ea0a14` | 18/10 | Agente de IA (Gemini) | Plano-base com as 5 propostas, sem decisões. É anterior à meta de equilíbrio. |
| `9f1f014f` | 19/10 | Agente de IA (Gemini) | Gerada com as três hipóteses principais em 100% (cortar todos os cargos de confiança, todos pagarem). Os valores são irreais. **Não usem.** |

Em 19/10 tentei duas vezes gerar a análise da alternativa de 32%, e nas duas o Google respondeu com erro 503 (alta demanda). Depois descobri outro limite: o plano gratuito do Gemini Flash aceita só cerca de **20 requisições por dia**, umas 3 a 5 análises, e a cota acabou. Por isso coloquei **modelos reserva**: se o principal falhar, o app tenta o Gemini Flash-Lite e depois o Groq (ver a tabela de decisões). Ainda falta gerar a análise da alternativa pela tela.

## Números para o relatório e a apresentação

Saem da planilha e das hipóteses padrão, e dá para conferir todos na tela.

- **Situação:**
  - resultado de −R$ 7,2 mi (receita arrecadada de R$ 577,3 mi contra despesa liquidada de R$ 584,5 mi);
  - arrecadação R$ 65,2 mi abaixo do previsto (convênios com 38% do previsto, IPTU com 70%);
  - Saúde R$ 3,5 mi acima da dotação e Infraestrutura R$ 17 mi abaixo;
  - folha equivalente a 66,6% da receita;
  - dívida ativa de R$ 100,1 mi, com só 36% de recuperação estimada;
  - restos a pagar sem cobertura de R$ 18,2 mi.
- **Plano-base:** +R$ 5,25 mi no ano 1 e −R$ 3,25 mi por ano depois; cobre 55% do déficit recorrente; acumulado em 10 anos de −R$ 23,97 mi.
- **Alternativa de 32%:** +R$ 8,69 mi no ano 1 e +R$ 0,19 mi por ano depois; acumulado de +R$ 10,42 mi. **Falha no cenário conservador** (pior ano −R$ 3,50 mi) e não foi validada.
- **Pontos de equilíbrio de referência:**
  - mantendo as outras medidas, o déficit fecha com cerca de 31% de corte nos cargos de confiança ou 16% no custeio;
  - se cada medida tivesse de cobrir o déficit sozinha, seriam 35,6% ou 27,5%;
  - os 56 cargos da tela são equivalência pelo custo médio.

## Decisões técnicas que tomei e por quê

| Decisão | Motivo |
|---|---|
| Agente único com validação humana, não multiagente | O enunciado aceita os dois. Como os números já saem do Python, vários agentes só reescreveriam o mesmo texto, com mais falhas e mais tempo. As três ferramentas cobrem os papéis do roteiro (financeiro, operacional e coleta externa), e o revisor é a pessoa. |
| O Python calcula; a IA só ordena e explica | Os números ficam reproduzíveis e testáveis. A IA não inventa valores nem propostas, e o código rejeita respostas fora das regras. |
| Gemini `gemini-3.5-flash` como principal | É o modelo que melhor seguiu as regras com os dados completos. A família 2.5 recusa contas novas. Tirei a DeepSeek do projeto. |
| Reservas: Gemini `3.5-flash-lite` e Groq `gpt-oss-120b` | A cota gratuita do Flash acaba rápido e o Google às vezes fica sobrecarregado. O Flash-Lite passou em 4 de 4 testes, usa a mesma chave e tem cota própria. O Groq também passou em 4 de 4, mas no plano gratuito só aceita 8.000 tokens por minuto, então recebe dados resumidos e leva uns 2 minutos. O `3.1-flash-lite` foi descartado: inventou propostas e fontes sem consultar as ferramentas. |
| Temporários desligados por padrão | O cliente citou isso como possibilidade ("talvez"), e o STF (Tema 612) restringe contratação temporária. Deixei a opção só para teste. |
| Desconto do Refis sobre o valor total devido | A planilha não separa principal, juros e multa. O teto de 50% foi escolha minha, não do cliente. |
| Desconto do Refis fixo entre cenários | Se aumentasse a renúncia no cenário otimista, a receita cairia e ele ficaria pior que o base. |
| Três cenários em vez de um número só | Os percentuais não foram confirmados pelo cliente, e não vamos ter outra entrevista. |
| "Despesa liquidada", não "dinheiro que saiu" | Liquidação e pagamento são etapas diferentes (Lei 4.320). |
| Recuperação fixa de artigos, sem banco vetorial | O enunciado pede RAG na fundamentação teórica, não na implementação. O nome certo para o que o app faz é "recuperação fixa de artigos oficiais como contexto". |
| SQLite local | Guarda versões e decisões só com acréscimo, e já vem com o Python, sem instalar nada. |

## Armadilhas que encontrei

- **Servidor antigo preso na porta.** Se a tela mostrar dados velhos ou erro ao carregar, pode haver um servidor de outra sessão ainda ligado. Fechem os terminais do app (ou encerrem os processos `python.exe` que rodam `main.py`) e abram o `iniciar.bat` de novo.
- **Reiniciar depois de mudar o `.env` ou o código.** O servidor só lê os dois ao iniciar.
- **Erros do Gemini (503 ou cota esgotada).** O app tenta de novo e, se não der, passa para as reservas. Quando isso acontece, a tela de Revisão avisa qual modelo respondeu. Se todos falharem, esperem alguns minutos (ou o dia seguinte, se for cota).
- **A ordem das propostas muda entre execuções.** É normal para um modelo de linguagem, e a decisão é sempre da pessoa.
- **O repositório não leva o banco.** O GitHub (https://github.com/LeonardoBalk/agentes) tem o código e a planilha, mas não a pasta `runtime/`, com as respostas da entrevista, os papéis e as decisões. Quem clonar precisa copiar o meu `runtime/municipal.sqlite3` ou cadastrar a entrevista de novo a partir do PDF.
- **O `.env` não vai junto.** Cada máquina precisa do seu `.env` com a chave. Peçam a nova quando eu trocar.
- **Planilha.** O `data/estudo_municipal.xlsx` é idêntico ao `Estudo_de_Caso_Gestao_Municipal_Receitas_Despesas.xlsx` que recebemos. O app lê só as células de entrada e falha de propósito se o layout mudar.

## Como conferir se está tudo funcionando

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
node --check web/app.js
```

O resultado esperado é **41 testes, OK**. Depois abram o app e confiram se o selo do canto mostra "Agente de IA ligado" e se a tela de Simulação mostra a alternativa de 32%.

## Onde está cada coisa

| Para… | Arquivo |
|---|---|
| Regras de cálculo das propostas, cenários e equilíbrio | `municipal/simulacao.py` |
| Instruções e validação do agente | `municipal/agente.py` |
| Leitura da planilha e diagnóstico | `municipal/dados.py` |
| Artigos oficiais consultados | `municipal/fontes.py` e o cache em `runtime/sources.json` |
| Perguntas da entrevista | `municipal/storage.py` (`QUESTIONS`) |
| Telas | `web/index.html`, `web/app.js`, `web/style.css` |
| Plano de tarefas do grupo (16 cartões) | `trello/plano.json` e `trello/README.md` |
| Enunciado e entrevista originais | Tenho os dois PDFs: `Trabalho final - enunciado.pdf` e `Entrevista_Prefeitura_Santa_Leopoldina.pdf` |
