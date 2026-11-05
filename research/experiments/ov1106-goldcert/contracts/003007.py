"""Ground: VacBot booleans, importable vocabulary, and module-scoped logging."""
import logging
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, '/testbed')
import sucks


class PublicVacuum(unittest.TestCase):
    def test_status_booleans(self):
        # The instruction exposes these raw status fields to downstream consumers.
        bot = object.__new__(sucks.VacBot)
        for status in ('auto', 'edge', 'spot', 'single_room', 'stop', 'return', 'returning', 'charging', 'idle', 'offline', None):
            bot.vacuum_status = status
            bot.clean_status = status
            bot.charge_status = status
            self.assertIs(bot.is_cleaning, status in {'auto', 'edge', 'spot', 'single_room'})
            self.assertIs(bot.is_charging, status == 'charging')

    def test_importable_vocabulary_without_constant_name_pins(self):
        # Names are deliberately unconstrained; values are the public baseline vocabulary.
        exported = {value for name, value in vars(sucks).items()
                    if not name.startswith('_') and name.isupper() and isinstance(value, str)}
        for value in ('auto', 'edge', 'spot', 'single_room', 'stop', 'normal', 'high',
                      'return', 'returning', 'charging', 'idle', 'main_brush', 'side_brush', 'filter'):
            self.assertIn(value, exported)

    def test_login_diagnostics_use_library_logger(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        library = logging.getLogger('sucks')
        old_root, old_library = root.level, library.level
        root.setLevel(logging.DEBUG)
        library.setLevel(logging.DEBUG)
        root.addHandler(handler)
        response = Mock()
        response.json.return_value = {'code': '1005'}
        try:
            with patch('requests.get', return_value=response):
                with self.assertRaises(ValueError):
                    sucks.EcoVacsAPI('device', 'account', 'hash', 'us', 'na')
        finally:
            root.removeHandler(handler)
            root.setLevel(old_root)
            library.setLevel(old_library)
        # The original library's login warning is an observable diagnostic. Its wording is not pinned.
        diagnostics = [r for r in records if r.levelno >= logging.WARNING]
        self.assertTrue(diagnostics, 'login failure must retain its warning diagnostic')
        self.assertTrue(all(r.name == 'sucks' or r.name.startswith('sucks.') for r in diagnostics))
        self.assertFalse(any(r.name == 'root' for r in records))


if __name__ == '__main__':
    unittest.main()
