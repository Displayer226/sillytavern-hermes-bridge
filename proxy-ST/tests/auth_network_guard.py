"""Prevent network connections during the isolated authentication tests."""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_network_connections(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network connections are disabled in authentication tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
