"""importação sem rede."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.criar_trello import DEFAULT_PLAN, Trello, TrelloError, import_board, load_plan, main


class FakeTrello:
    def __init__(self):
        self.boards = []
        self.lists = []
        self.cards = []
        self.posts = []
        self.fail_after_card = False
        self.owner = 'owner-test'

    def call(self, method, path, **params):
        if method == 'GET':
            if path == '/members/me':
                return {'id': self.owner}
            if path == '/members/me/boards':
                return copy.deepcopy(self.boards)
            if path.endswith('/lists'):
                return copy.deepcopy(self.lists)
            if path.endswith('/cards'):
                return copy.deepcopy(self.cards)
        self.posts.append((path, params))
        if path == '/boards/':
            item = dict(params, id='board-test', shortUrl='https://trello.com/b/test', closed=False)
            self.boards.append(item)
        elif path.endswith('/lists'):
            item = dict(params, id=f'list-{len(self.lists)}', closed=False)
            self.lists.append(item)
        elif path == '/cards':
            item = dict(params, id=f'card-{len(self.cards)}', closed=False)
            self.cards.append(item)
            if self.fail_after_card:
                self.fail_after_card = False
                raise TrelloError('Conexão perdida depois de criar o cartão.')
        else:
            raise AssertionError((method, path))
        return copy.deepcopy(item)


class TrelloTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_plan(DEFAULT_PLAN)

    def test_dry_run_has_no_network_and_needs_no_credentials(self):
        with patch.object(Trello, 'call', side_effect=AssertionError('Não deve acessar rede')):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main([]), 0)
            self.assertIn('PRÉVIA LOCAL', output.getvalue())

    def test_private_board_and_rerun_preserves_moved_cards(self):
        api = FakeTrello()
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state.json'
            result = import_board(api, self.plan, state)
            self.assertEqual(result['created_cards'], 16)
            self.assertEqual(len(api.lists), 6)
            self.assertEqual(api.boards[0]['prefs_permissionLevel'], 'private')
            self.assertFalse(api.boards[0]['defaultLists'])
            self.assertTrue(all(c['idList'] == 'list-0' for c in api.cards))
            count = len(api.posts)
            api.cards[0]['idList'] = 'list-4'
            api.cards[0]['desc'] = 'Descrição editada pela equipe.'
            result = import_board(api, self.plan, state)
            self.assertEqual(result['created_cards'], 0)
            self.assertEqual(len(api.posts), count)
            self.assertEqual(api.cards[0]['idList'], 'list-4')
            self.assertEqual(api.cards[0]['desc'], 'Descrição editada pela equipe.')

    def test_resume_after_ambiguous_card_response_does_not_duplicate(self):
        api = FakeTrello()
        api.fail_after_card = True
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state.json'
            with self.assertRaises(TrelloError):
                import_board(api, self.plan, state)
            self.assertEqual(len(api.cards), 1)
            result = import_board(api, self.plan, state)
            self.assertEqual(result['created_cards'], 15)
            self.assertEqual(len(api.boards), 1)
            self.assertEqual(len(api.cards), 16)

    def test_different_account_cannot_reuse_state(self):
        api = FakeTrello()
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state.json'
            import_board(api, self.plan, state)
            previous = len(api.posts)
            api.owner = 'other-owner'
            with self.assertRaises(TrelloError):
                import_board(api, self.plan, state)
            self.assertEqual(len(api.posts), previous)

    def test_credentials_stay_in_header(self):
        api = Trello('test-key', 'test-token')
        with patch.object(api.opener, 'open', return_value=io.BytesIO(b'{"id":"test"}')) as request:
            api.call('POST', '/cards', name='Uma tarefa', idList='list-test')
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, 'https://api.trello.com/1/cards')
        self.assertEqual(sent.get_header('Authorization'), 'OAuth oauth_consumer_key="test-key", oauth_token="test-token"')
        self.assertNotIn('test-token', sent.data.decode())


if __name__ == '__main__':
    unittest.main()
