# Criar o quadro automaticamente

O script cria um quadro privado, seis listas e 16 cartões com descrições e critérios de conclusão. Todos os cartões começam em **A fazer**. A lista **Registro retrospectivo** fica vazia para a equipe registrar apenas atividades anteriores que confirmar. Não cria histórico antigo, reuniões, membros ou prazos individuais.

## 1. Conferir a prévia

No terminal, dentro da pasta do projeto:

```powershell
python scripts/criar_trello.py
```

Esse comando não usa a rede nem solicita credenciais. Edite `trello/plano.json` para ajustar nomes, descrições e distribuição dos cartões antes de importar. Os IDs internos dos cartões devem permanecer estáveis.

## 2. Obter acesso ao Trello

Siga a [documentação oficial de autorização](https://developer.atlassian.com/cloud/trello/guides/rest-api/authorization/). Ela orienta a obter uma API key em [Trello Apps Admin](https://trello.com/apps/admin), na aba **Trello Auth** do seu Power-Up, e autorizar um token com leitura e escrita. Essa autorização é feita por você, na sua conta. A API key do Trello e o token de autorização não são o mesmo que o token genérico de conta Atlassian.

Não envie credenciais pelo chat, não coloque em cartões nem no código. O script pode solicitá-las com entrada oculta no terminal. Se preferir, configure localmente estas variáveis de ambiente ou acrescente-as ao `.env` existente, sem substituir as configurações do modelo:

```dotenv
TRELLO_API_KEY=sua-api-key
TRELLO_TOKEN=seu-token-autorizado
# Opcional: ID do espaço de trabalho desejado.
TRELLO_WORKSPACE_ID=
```

Sem ID de workspace, a escolha segue o comportamento padrão da API e as permissões da conta. Se o Trello exigir um workspace explícito, preencha o ID e tente novamente.

## 3. Criar o quadro

```powershell
python scripts/criar_trello.py --criar
```

Só esse comando acessa o Trello e cria os objetos na conta autorizada. O programa mostra o link ao terminar. Se o comando `python` não existir, use `.\.venv\Scripts\python.exe` no lugar.

A chave e o token são enviados no cabeçalho de autorização para `https://api.trello.com`, sem aparecer no endereço ou no estado local. Ao usar o `.env`, é necessário ter `python-dotenv` instalado, como nas dependências do projeto. A entrada oculta funciona sem pacote extra.

## Continuação após uma falha

O arquivo `runtime/trello-import.json` guarda o identificador da importação, da conta, do quadro e dos cartões, sem credenciais. Se a rede cair, execute novamente com esse mesmo arquivo. O script procura o quadro e os cartões pelos marcadores nas descrições antes de criar os que faltam. Ele não move cartões já existentes, não sobrescreve textos da equipe e não apaga objetos.

Preserve o arquivo de estado e os marcadores enquanto a importação não estiver completa. Não execute duas importações simultâneas com o mesmo estado. Se um cartão for excluído depois de importado, seu ID salvo impede uma recriação automática; a equipe deve decidir manualmente como proceder.

Foram feitos testes locais com uma API controlada para verificar retomada, ausência de duplicação e preservação dos cartões movidos. A importação na sua conta precisa das suas credenciais e não foi executada nesta entrega.

## Referências

- [Autorização da API](https://developer.atlassian.com/cloud/trello/guides/rest-api/authorization/)
- [Quadros e listas](https://developer.atlassian.com/cloud/trello/rest/api-group-boards/)
- [Cartões](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)
