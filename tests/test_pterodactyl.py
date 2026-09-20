import runpy

import pytest

import main


@pytest.mark.parametrize('server_port,legacy_port,expected', [
    ('4176', '5000', 4176),
    (None, '5000', 5000),
    (None, None, 80),
])
def test_pterodactyl_port_precedence(monkeypatch, server_port, legacy_port, expected):
    monkeypatch.setattr('dotenv.load_dotenv', lambda **kwargs: None)
    monkeypatch.setenv('INTERNAL_IP', '0.0.0.0')
    monkeypatch.delenv('SERVER_PORT', raising=False)
    monkeypatch.delenv('PORT', raising=False)
    if server_port:
        monkeypatch.setenv('SERVER_PORT', server_port)
    if legacy_port:
        monkeypatch.setenv('PORT', legacy_port)
    monkeypatch.setenv('FLASK_DEBUG', '1')
    calls = []
    monkeypatch.setattr(main.app, 'run', lambda **kwargs: calls.append(kwargs))

    runpy.run_module('pterodactyl', run_name='__main__')

    assert calls == [{'host': '0.0.0.0', 'port': expected, 'debug': True, 'use_reloader': False}]


def test_pterodactyl_host_defaults_to_all_interfaces(monkeypatch):
    monkeypatch.setattr('dotenv.load_dotenv', lambda **kwargs: None)
    monkeypatch.delenv('INTERNAL_IP', raising=False)
    monkeypatch.setenv('SERVER_PORT', '8080')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    calls = []
    monkeypatch.setattr(main.app, 'run', lambda **kwargs: calls.append(kwargs))

    runpy.run_module('pterodactyl', run_name='__main__')

    assert calls == [{'host': '0.0.0.0', 'port': 8080, 'debug': False, 'use_reloader': False}]


@pytest.mark.parametrize('port', ['0', '65536', 'not-a-port'])
def test_pterodactyl_rejects_invalid_ports(monkeypatch, port):
    monkeypatch.setattr('dotenv.load_dotenv', lambda **kwargs: None)
    monkeypatch.setenv('SERVER_PORT', port)
    monkeypatch.setattr(main.app, 'run', lambda **kwargs: pytest.fail('Should not start server'))

    with pytest.raises(ValueError):
        runpy.run_module('pterodactyl', run_name='__main__')
