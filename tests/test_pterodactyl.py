import runpy

import pytest

import main


@pytest.mark.parametrize('port,expected', [('4176', 4176), ('5000', 5000), (None, 80)])
def test_pterodactyl_port(monkeypatch, port, expected):
    monkeypatch.setattr('dotenv.load_dotenv', lambda **kwargs: None)
    monkeypatch.setenv('HOST', '0.0.0.0')
    monkeypatch.delenv('PORT', raising=False)
    monkeypatch.setenv('SERVER_PORT', '9999')  # Legacy setting must be ignored.
    if port is not None:
        monkeypatch.setenv('PORT', port)
    monkeypatch.setenv('FLASK_DEBUG', '1')
    calls = []
    monkeypatch.setattr(main.app, 'run', lambda **kwargs: calls.append(kwargs))

    runpy.run_module('pterodactyl', run_name='__main__')

    assert calls == [{'host': '0.0.0.0', 'port': expected, 'debug': True, 'use_reloader': False}]


def test_pterodactyl_host_defaults_to_localhost(monkeypatch):
    monkeypatch.setattr('dotenv.load_dotenv', lambda **kwargs: None)
    monkeypatch.delenv('HOST', raising=False)
    monkeypatch.setenv('INTERNAL_IP', '192.168.1.237')  # Legacy setting must be ignored.
    monkeypatch.setenv('PORT', '8080')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    calls = []
    monkeypatch.setattr(main.app, 'run', lambda **kwargs: calls.append(kwargs))

    runpy.run_module('pterodactyl', run_name='__main__')

    assert calls == [{'host': '127.0.0.1', 'port': 8080, 'debug': False, 'use_reloader': False}]


@pytest.mark.parametrize('port', ['0', '65536', 'not-a-port'])
def test_pterodactyl_rejects_invalid_ports(monkeypatch, port):
    monkeypatch.setattr('dotenv.load_dotenv', lambda **kwargs: None)
    monkeypatch.setenv('PORT', port)
    monkeypatch.setattr(main.app, 'run', lambda **kwargs: pytest.fail('Should not start server'))

    with pytest.raises(ValueError):
        runpy.run_module('pterodactyl', run_name='__main__')
