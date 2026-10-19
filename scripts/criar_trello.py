"""importação no trello; prévia sem rede."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = ROOT / 'trello' / 'plano.json'
DEFAULT_STATE = ROOT / 'runtime' / 'trello-import.json'


class TrelloError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise TrelloError('Redirecionamento inesperado da API. Importação interrompida.')


class Trello:
    def __init__(self, key: str, token: str):
        if not all(re.fullmatch(r'[A-Za-z0-9_-]+', value or '') for value in (key, token)):
            raise TrelloError('Chave ou token ausente ou com formato inválido.')
        self.authorization = f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"'
        self.opener = build_opener(NoRedirect())

    def call(self, method: str, path: str, **params):
        url = 'https://api.trello.com/1' + path
        headers = {'Authorization': self.authorization, 'Accept': 'application/json'}
        data = None
        if method == 'GET' and params:
            url += '?' + urlencode(params)
        elif method == 'POST':
            headers['Content-Type'] = 'application/json'
            data = json.dumps(params, ensure_ascii=False).encode('utf-8')
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            reasons = {400: 'Confira os dados e o espaço de trabalho.',
                       401: 'Confira a chave e o token de autorização.',
                       403: 'Confira as permissões do token e do espaço de trabalho.',
                       429: 'Limite temporário de requisições; aguarde antes de continuar.'}
            raise TrelloError(f'Trello respondeu HTTP {error.code}. '
                              + reasons.get(error.code, 'Tente continuar mais tarde.')) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise TrelloError('A conexão não foi concluída. Uma criação pode ter ocorrido. '
                              'Execute novamente com o mesmo arquivo de estado para reconciliar o quadro.') from None


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(plan, dict) or not isinstance(plan.get('name'), str) or not plan['name'].strip():
        raise ValueError('Informe o nome do quadro no plano.')
    lists = plan.get('lists')
    if not isinstance(lists, list) or not lists or any(not isinstance(n, str) or not n.strip() for n in lists) or len(set(lists)) != len(lists):
        raise ValueError('As listas precisam ter nomes únicos e não vazios.')
    cards = plan.get('cards')
    if not isinstance(cards, list):
        raise ValueError('O plano precisa conter uma lista de cartões.')
    seen = set()
    for card in cards:
        if not isinstance(card, dict) or not re.fullmatch(r'[a-z0-9-]+', card.get('id', '')):
            raise ValueError('Cada cartão precisa de um ID estável com letras, números ou hífens.')
        if card['id'] in seen or card.get('list') not in lists:
            raise ValueError('Cartão com ID repetido ou lista desconhecida.')
        if any(not isinstance(card.get(k), str) or not card[k].strip() for k in ('name', 'description')):
            raise ValueError('Cada cartão precisa de nome e descrição.')
        seen.add(card['id'])
    return plan


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def import_board(api: Trello, plan: dict, state_path: Path, workspace: str = '') -> dict:
    """retoma a importação e preserva cartões editados."""
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'import_id': uuid.uuid4().hex}
    owner = api.call('GET', '/members/me', fields='id')['id']
    if state.get('owner_id') not in (None, owner):
        raise TrelloError('Este estado pertence a outra conta Trello. Use a conta original.')
    if state.get('workspace', workspace) != workspace:
        raise TrelloError('O espaço de trabalho mudou. Mantenha a configuração original para continuar.')
    state.update(owner_id=owner, workspace=workspace)
    save_state(state_path, state)
    marker = f'[municipal-import:{state["import_id"]}]'
    boards = api.call('GET', '/members/me/boards', filter='all', fields='id,name,desc,closed,shortUrl,prefs')
    matches = [board for board in boards if marker in board.get('desc', '')]
    if len(matches) > 1:
        raise TrelloError('Há mais de um quadro com o marcador da importação. Confira manualmente.')
    if state.get('board_id') and (not matches or matches[0]['id'] != state['board_id']):
        raise TrelloError('Quadro anterior não localizado com seu marcador. Confira a conta e o quadro antes de continuar.')
    if matches:
        board = matches[0]
        if board.get('closed'):
            raise TrelloError('O quadro está arquivado. Reabra-o no Trello antes de continuar.')
    else:
        params = dict(name=plan['name'], desc=plan.get('description', '')+'\n\n'+marker,
                      defaultLists=False, defaultLabels=False, prefs_permissionLevel='private')
        if workspace:
            params['idOrganization'] = workspace
        board = api.call('POST', '/boards/', **params)
    state.update(board_id=board['id'], url=board.get('shortUrl') or board.get('url', ''))
    save_state(state_path, state)
    lists = api.call('GET', f'/boards/{board["id"]}/lists', filter='all')
    list_ids = {}
    for name in plan['lists']:
        existing = [item for item in lists if item['name'] == name]
        if len(existing) > 1 or any(item.get('closed') for item in existing):
            raise TrelloError(f'A lista "{name}" está duplicada ou arquivada. Confira antes de continuar.')
        item = existing[0] if existing else api.call('POST', f'/boards/{board["id"]}/lists', name=name, pos='bottom')
        list_ids[name] = item['id']
    existing_cards = api.call('GET', f'/boards/{board["id"]}/cards', filter='all', fields='id,desc')
    state.setdefault('cards', {})
    created = 0
    for card in plan['cards']:
        card_marker = f'[municipal-task:{card["id"]}]'
        matches = [item for item in existing_cards if card_marker in item.get('desc', '')]
        if len(matches) > 1:
            raise TrelloError(f'Cartão duplicado: {card["name"]}. Confira o quadro.')
        # preserva cartões editados
        if card['id'] in state['cards']:
            continue
        if matches:
            item = matches[0]
        else:
            item = api.call('POST', '/cards', idList=list_ids[card['list']], name=card['name'],
                            desc=card['description']+'\n\n'+card_marker, pos='bottom')
            created += 1
        state['cards'][card['id']] = item['id']
        save_state(state_path, state)
    return dict(url=state['url'], created_cards=created, total_cards=len(plan['cards']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--criar', action='store_true', help='Cria o quadro na conta Trello autorizada.')
    parser.add_argument('--plano', type=Path, default=DEFAULT_PLAN, help='Plano JSON editável.')
    parser.add_argument('--estado', type=Path, default=DEFAULT_STATE, help='Estado local para retomar sem duplicar.')
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plano)
        print(f'Quadro: {plan["name"]}\nVisibilidade na criação: privado\n')
        for name in plan['lists']:
            cards = [card for card in plan['cards'] if card['list'] == name]
            print(f'{name} ({len(cards)} cartões)')
            for card in cards:
                print(f'  - {card["name"]}')
        if not args.criar:
            print('\nPRÉVIA LOCAL: nenhuma requisição foi enviada ao Trello.')
            print('Para criar de verdade: python scripts/criar_trello.py --criar')
            return 0
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / '.env')
        except ImportError:
            pass
        key = os.getenv('TRELLO_API_KEY') or getpass.getpass('API key do Trello (entrada oculta): ')
        token = os.getenv('TRELLO_TOKEN') or getpass.getpass('Token do Trello (entrada oculta): ')
        result = import_board(Trello(key, token), plan, args.estado, os.getenv('TRELLO_WORKSPACE_ID', '').strip())
        print(f'\nQuadro pronto: {result["url"]}')
        print(f'Cartões novos: {result["created_cards"]}; tarefas do plano: {result["total_cards"]}.')
        return 0
    except (TrelloError, ValueError, OSError, KeyError) as error:
        print(f'Não foi possível concluir: {error}', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nOperação interrompida. Para continuar, mantenha o mesmo arquivo de estado.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
